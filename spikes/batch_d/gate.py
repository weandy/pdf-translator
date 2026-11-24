"""批次 D 的配对统计和确定性准入裁决。"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import fmean
from typing import Any

from pydantic import BaseModel

from .contract import (
    ALL_CATEGORIES,
    ALL_DISCIPLINES,
    ALL_GROUPS,
    ArbitrationRecord,
    CandidateReview,
    CaseDoc,
    DecisionRecord,
    DecisionState,
    ExtractionSnapshot,
    FactAnchor,
    GateCheck,
    GenerationRecord,
    GroupId,
    PolicyConfig,
    PrivateMap,
    ReviewerBallot,
    ReviewerId,
    RunMode,
    SegmentCategory,
    canonical_json,
    compute_mqm_score,
    load_policy,
    sha256_canonical,
)

type VoteKey = tuple[ReviewerId, int, str, str, GroupId]
_REVIEWERS: tuple[ReviewerId, ...] = ("codex", "neon")
_PAIRS: tuple[tuple[GroupId, GroupId], ...] = (("B", "A"), ("C", "B"), ("C", "A"))


def _unique[T, K](rows: Iterable[T], key: Callable[[T], K], name: str) -> dict[K, T]:
    indexed = {}
    for row in rows:
        k = key(row)
        if k in indexed:
            raise ValueError(f"{name}重复：{k}")
        indexed[k] = row
    return indexed


def _coverage[K](actual: Iterable[K], expected: Iterable[K], name: str) -> None:
    observed, required = set(actual), set(expected)
    if observed != required:
        missing, extra = required - observed, observed - required
        raise ValueError(
            f"{name}覆盖不符：缺 {len(missing)} 个，多 {len(extra)} 个，首个差异 {min(map(str, missing | extra))}"
        )


def unblind_ballots(
    private_maps: Sequence[PrivateMap], ballots: Sequence[ReviewerBallot]
) -> dict[VoteKey, CandidateReview]:
    """按评审、轮次和题号还原折叠候选。证据缺失或映射含糊时抛出 ValueError。"""
    if len({m.run_id for m in private_maps} | {b.run_id for b in ballots}) != 1:
        raise ValueError("私有映射与评票须属于同一个 run_id")
    if len({b.mode for b in ballots}) != 1:
        raise ValueError("双评缺失或混合了运行模式")
    maps = _unique(private_maps, lambda m: (m.reviewer, m.round_idx), "私有映射")
    votes = _unique(ballots, lambda b: (b.reviewer, b.round_idx), "评票")
    _coverage(maps, votes, "映射与评票")
    families = {r: {b.model_family.strip().casefold() for b in ballots if b.reviewer == r} for r in _REVIEWERS}
    if any(len(names) != 1 or "" in names for names in families.values()) or families["codex"] == families["neon"]:
        raise ValueError("两位评审须各有独立且一致的模型家族")

    expanded: dict[VoteKey, CandidateReview] = {}
    for (reviewer, rnd), private in sorted(maps.items()):
        items = _unique(private.items, lambda i: i.item_id, "私有题号")
        reviews = _unique(votes[reviewer, rnd].reviews, lambda i: i.item_id, "评票题号")
        _coverage(reviews, items, "题号")
        for item_id, item in items.items():
            if rnd != item.round_idx:
                raise ValueError(f"题 {item_id} 跨轮拼接")
            candidates = _unique(reviews[item_id].candidates, lambda c: c.label, "候选标签")
            _coverage(candidates, item.label_to_groups, f"题 {item_id} 候选")
            _coverage(item.group_to_label, ALL_GROUPS, f"题 {item_id} 组别")
            for label, groups in item.label_to_groups.items():
                if not groups or any(item.group_to_label.get(g) != label for g in groups):
                    raise ValueError(f"题 {item_id} 标签正反映射不符")
                for group in groups:
                    key = (reviewer, rnd, item.doc_id, item.segment_id, group)
                    if key in expanded:
                        raise ValueError(f"展开后重复票：{key}")
                    expanded[key] = candidates[label]
            _coverage((g for groups in item.label_to_groups.values() for g in groups), ALL_GROUPS, f"题 {item_id} 展开")
    _coverage(
        (k[1:] for k in expanded if k[0] == "codex"),
        (k[1:] for k in expanded if k[0] == "neon"),
        "两位评审配对",
    )
    return expanded


def _anchors(docs: Sequence[CaseDoc]) -> dict[tuple[str, str], dict[str, FactAnchor]]:
    indexed: dict[tuple[str, str], dict[str, FactAnchor]] = defaultdict(dict)
    for doc in docs:
        for anchor in doc.anchors:
            indexed[doc.doc_id, anchor.segment_id][anchor.anchor_id] = anchor
    return indexed


def _protected(review: CandidateReview, anchors: Mapping[str, FactAnchor]) -> Counter[str]:
    errors: Counter[str] = Counter()
    for err in review.errors:
        anchor = anchors.get(err.anchor_id)
        if err.protected and anchor is None:
            raise ValueError(f"保护性错误未定位到本段锚点：{err.anchor_id!r}")
        if err.protected or (anchor and anchor.protected):
            errors[err.anchor_id] += 1
    for check in review.checks:
        anchor = anchors.get(check.check_id)
        if check.status == "fail" and anchor and anchor.protected:
            errors[check.check_id] = max(1, errors[check.check_id])
    return errors


def detect_disagreements(
    unblinded: Mapping[VoteKey, CandidateReview],
    docs: Sequence[CaseDoc],
    policy: PolicyConfig,
    arbitrations: Sequence[ArbitrationRecord] = (),
) -> list[dict[str, Any]]:
    """整段仲裁覆盖该段，指定锚点或组的仲裁只覆盖对应争议。"""
    anchors = _anchors(docs)
    resolved = {
        (a.round_idx, a.doc_id, a.segment_id, group, a.anchor_id)
        for a in arbitrations
        if a.resolved
        for group in (a.resolved_errors.keys() or ALL_GROUPS)
    }
    pending = []
    for reviewer, rnd, doc, seg, group in sorted(unblinded):
        if reviewer != "codex":
            continue
        coord = (rnd, doc, seg, group)
        left, right = (unblinded[(r, *coord)] for r in _REVIEWERS)
        facts = anchors.get((doc, seg), {})
        conflict = set(_protected(left, facts)) ^ set(_protected(right, facts))
        gap = abs(compute_mqm_score(left.errors) - compute_mqm_score(right.errors))
        if (*coord, "") in resolved:
            continue
        base = {"round_idx": rnd, "doc_id": doc, "segment_id": seg, "group": group}
        if gap >= policy.max_disagreement_score_gap:
            pending.append({**base, "kind": "score_gap", "score_gap": gap})
        for anchor in sorted(conflict):
            if (*coord, anchor) not in resolved:
                pending.append({**base, "kind": "protected", "anchor_id": anchor})
    return pending


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def paired_bootstrap_ci(
    doc_deltas_by_discipline: Mapping[str, Sequence[float]], *, resamples: int = 2000, seed: int = 20261125
) -> tuple[float, float]:
    """保留每学科原文档数，以文档配对差为抽样单位，返回线性插值的百分位区间。"""
    strata = [sorted(doc_deltas_by_discipline[k]) for k in sorted(doc_deltas_by_discipline)]
    if not strata or not all(strata) or resamples < 1:
        raise ValueError("自助采样需要非空学科、文档和正的重复次数")
    rng = random.Random(seed)
    estimates = sorted(fmean(x for s in strata for x in rng.choices(s, k=len(s))) for _ in range(resamples))
    return _percentile(estimates, 0.025), _percentile(estimates, 0.975)


def exact_one_sided_sign_test_p(wins: int, losses: int) -> float:
    """忽略平局，计算公平二项分布的单侧上尾概率。"""
    if min(wins, losses) < 0:
        raise ValueError("文档胜负数须非负")
    n = wins + losses
    return sum(math.comb(n, i) for i in range(wins, n + 1)) / (2**n)


@dataclass
class _Usage:
    cost: Decimal | None = Decimal(0)
    requests: int = 0
    seconds: Decimal | None = Decimal(0)

    def add(self, record: GenerationRecord | ExtractionSnapshot) -> None:
        self.cost = _add_measurement(self.cost, record.cost_cny)
        self.requests += record.attempts if isinstance(record, GenerationRecord) else 1
        self.seconds = _add_measurement(self.seconds, record.elapsed_s)


def _add_measurement(total: Decimal | None, value: float | None) -> Decimal | None:
    if total is None or value is None or not math.isfinite(value):
        return None
    return total + Decimal(str(value))


def _usage_summary(samples: Sequence[_Usage]) -> dict[str, Any]:
    costs = [s.cost for s in samples if s.cost is not None]
    times = sorted(float(s.seconds) for s in samples if s.seconds is not None)
    return {
        "document_runs": len(samples),
        "cost_cny": float(sum(costs)) if samples and len(costs) == len(samples) else None,
        "requests": sum(s.requests for s in samples),
        "p95_latency_s": _percentile(times, 0.95) if samples and len(times) == len(samples) else None,
    }


def evaluate_cost_and_latency(
    docs: Sequence[CaseDoc],
    generations: Sequence[GenerationRecord],
    snapshots: Sequence[ExtractionSnapshot],
    policy: PolicyConfig,
) -> GateCheck:
    """每篇每轮的串行抽取、翻译合计为一次耗时样本，B 和 C 各负担完整的 B 抽取。"""
    holdout = sorted(d.doc_id for d in docs if d.split == "holdout")
    usage = {(g, d, r): _Usage() for g in ALL_GROUPS for d in holdout for r in range(policy.rounds)}
    for row in generations:
        if row.doc_id in holdout:
            usage[row.group, row.doc_id, row.round_idx].add(row)
    for snap in snapshots:
        if snap.doc_id in holdout:
            groups: tuple[GroupId, ...] = ("B", "C") if snap.source_group == "B" else ("A",)
            for group in groups:
                usage[group, snap.doc_id, snap.round_idx].add(snap)
    totals = {g: _usage_summary([v for k, v in usage.items() if k[0] == g]) for g in ALL_GROUPS}
    per_doc = {
        g: {d: _usage_summary([usage[g, d, r] for r in range(policy.rounds)]) for d in holdout} for g in ALL_GROUPS
    }
    limits = {
        "cost_cny": policy.max_cost_ratio,
        "requests": policy.max_request_ratio,
        "p95_latency_s": policy.max_p95_latency_ratio,
    }
    comparisons = {}
    for group, baseline in _PAIRS:
        ratios = {}
        for metric in limits:
            num, den = totals[group][metric], totals[baseline][metric]
            ratios[metric] = None if num is None or not den else float(Decimal(str(num)) / Decimal(str(den)))
        review = [k for k, v in ratios.items() if v is None]
        over = [k for k, v in ratios.items() if v is not None and v > limits[k]]
        comparisons[f"{group}-{baseline}"] = {
            "passed": not (review or over),
            "ratios": ratios,
            "needs_review": review,
            "over_limit": over,
        }
    failed = [
        f"{k} {', '.join(v['needs_review'] + v['over_limit'])}" for k, v in comparisons.items() if not v["passed"]
    ]
    return GateCheck(
        name="cost_and_latency",
        passed=not failed,
        detail="成本、请求与 p95 耗时达标" if not failed else "成本门槛超限或需复核：" + "；".join(failed),
        metrics={"groups": totals, "per_doc": per_doc, "limits": limits, "comparisons": comparisons},
    )


def _evidence_rows(records: Sequence[BaseModel], run_id: str) -> list[str]:
    """保留有序术语和盲排；票、题和输入文件的读取次序均不参与摘要。"""
    digests = []
    for record in records:
        data = record.model_dump(mode="json", exclude={"elapsed_s", "request_id"})
        if "run_id" in data:
            data["belongs_to_run"] = data.pop("run_id") == run_id
        if isinstance(record, PrivateMap):
            for item in data["items"]:
                for groups in item["label_to_groups"].values():
                    groups.sort()
            data["items"].sort(key=canonical_json)
        elif isinstance(record, ReviewerBallot):
            for item in data["reviews"]:
                item.pop("request_id")
                for candidate in item["candidates"]:
                    candidate["checks"].sort(key=canonical_json)
                    candidate["errors"].sort(key=canonical_json)
                item["candidates"].sort(key=canonical_json)
            data["reviews"].sort(key=canonical_json)
        elif isinstance(record, ArbitrationRecord):
            for errors in data["resolved_errors"].values():
                errors.sort(key=canonical_json)
        elif isinstance(record, CaseDoc):
            data["anchors"].sort(key=canonical_json)
        digests.append(sha256_canonical(data))
    return sorted(digests)


def _validate(
    run_id: str,
    mode: RunMode,
    docs: Sequence[CaseDoc],
    generations: Sequence[GenerationRecord],
    snapshots: Sequence[ExtractionSnapshot],
    private_maps: Sequence[PrivateMap],
    ballots: Sequence[ReviewerBallot],
    policy: PolicyConfig,
) -> dict[VoteKey, CandidateReview]:
    corpus = _unique(docs, lambda d: d.doc_id, "文档")
    holdout = {k: d for k, d in corpus.items() if d.split == "holdout"}
    if len(holdout) != policy.holdout_docs:
        raise ValueError(f"留出文档数 {len(holdout)}，策略要求 {policy.holdout_docs}")
    segments = {(d.doc_id, s.segment_id): s for d in holdout.values() for s in d.segments if s.scored}
    for doc in holdout.values():
        scored = [s for s in doc.segments if s.scored]
        if len(scored) != policy.scored_segments_per_doc or {s.category for s in scored} != set(ALL_CATEGORIES):
            raise ValueError(f"留出文档 {doc.doc_id} 的计分段数或类别不全")
    if any(r.run_id != run_id or r.mode != mode for r in (*generations, *ballots)):
        raise ValueError("生成记录或评票的 run_id / mode 与本次裁决不符")
    if any(s.doc_id not in corpus for s in (*generations, *snapshots)):
        raise ValueError("生成记录或快照引用了未知文档")
    snaps = _unique(
        (s for s in snapshots if s.doc_id in holdout), lambda s: (s.round_idx, s.doc_id, s.source_group), "抽取"
    )
    rounds = range(policy.rounds)
    _coverage(snaps, ((r, d, g) for r in rounds for d in holdout for g in ("A", "B")), "抽取快照")
    records = _unique(
        (g for g in generations if g.doc_id in holdout),
        lambda g: (g.round_idx, g.doc_id, g.segment_id, g.group),
        "生成段次",
    )
    _coverage(records, ((r, d, s, g) for r in rounds for d, s in segments for g in ALL_GROUPS), "生成段次")
    for (rnd, doc_id, seg_id, group), rec in records.items():
        seg = segments[doc_id, seg_id]
        if rec.status != "done" or rec.compatibility_fallback or not rec.target.strip():
            raise ValueError(f"生成未完成或发生兼容回退：{rnd}/{doc_id}/{seg_id}/{group}")
        if (rec.source, rec.formulas, rec.category) != (seg.source, seg.formulas, seg.category):
            raise ValueError(f"生成与语料的源段或类别不符：{doc_id}/{seg_id}/{group}")
        if rec.snapshot_hash != snaps[rnd, doc_id, "A" if group == "A" else "B"].ordered_hash:
            raise ValueError(f"生成使用了错误的术语快照：{rnd}/{doc_id}/{seg_id}/{group}")
    expanded = unblind_ballots(private_maps, ballots)
    _coverage(expanded, ((r, *k) for r in _REVIEWERS for k in records), "还原评票")
    for private in private_maps:
        for item in private.items:
            rnd, doc_id, seg_id = item.round_idx, item.doc_id, item.segment_id
            if segments[doc_id, seg_id].category != item.category:
                raise ValueError(f"盲评映射的段类别不符：{item.item_id}")
            by_group: dict[GroupId, GenerationRecord] = {g: records[rnd, doc_id, seg_id, g] for g in ALL_GROUPS}
            if len({v.expanded_source for v in by_group.values()}) != 1:
                raise ValueError(f"三组评题源文不一致：{item.item_id}")
            pairs = {(item.group_to_label[g], v.target) for g, v in by_group.items()}
            if len(pairs) != len(item.label_to_groups) or len(pairs) != len({v.target for v in by_group.values()}):
                raise ValueError(f"标签折叠与生成译文不一致：{item.item_id}")
    return expanded


@dataclass(frozen=True)
class _Rating:
    score: int
    major: bool
    correct: bool
    protected: Counter[str]


def _rating(review: CandidateReview, category: SegmentCategory, anchors: Mapping[str, FactAnchor]) -> _Rating:
    target_ids = {a.anchor_id for a in anchors.values() if a.kind == category} | {category}
    if category == "concept":
        target_ids.add("terminology")
    incorrect = any(c.status == "fail" and c.check_id in target_ids for c in review.checks) or any(
        e.category in target_ids or e.sub_category in target_ids or e.anchor_id in target_ids for e in review.errors
    )
    return _Rating(
        compute_mqm_score(review.errors),
        any(e.severity == "major" for e in review.errors),
        not incorrect,
        _protected(review, anchors),
    )


@dataclass(frozen=True)
class _Pair:
    doc: CaseDoc
    seg_id: str
    category: SegmentCategory
    rnd: int
    candidate: _Rating
    baseline: _Rating


def _target_check(name: str, rows: Sequence[_Pair], min_gain: int | None, policy: PolicyConfig) -> GateCheck:
    by_doc: Counter[str] = Counter()
    by_round: Counter[int] = Counter()
    by_discipline: Counter[str] = Counter()
    for row in rows:
        delta = int(row.candidate.correct) - int(row.baseline.correct)
        by_doc[row.doc.doc_id] += delta
        by_round[row.rnd] += delta
        by_discipline[row.doc.discipline] += delta
    gain = sum(by_doc.values())
    wins = sum(v > 0 for v in by_doc.values())
    losses = sum(v < 0 for v in by_doc.values())
    p = exact_one_sided_sign_test_p(wins, losses)
    gaining = sum(v > 0 for v in by_discipline.values())
    metrics = {
        "segment_rounds": len(rows),
        "documents": len(by_doc),
        "net_gain": gain,
        "min_gain": min_gain or 0,
        "candidate_correct": sum(r.candidate.correct for r in rows),
        "baseline_correct": sum(r.baseline.correct for r in rows),
        "by_round": dict(sorted(by_round.items())),
        "by_discipline": dict(by_discipline),
        "wins": wins,
        "ties": len(by_doc) - wins - losses,
        "losses": losses,
        "p_value": p,
    }
    conditions = {"净增": gain >= (min_gain or 0)}
    if min_gain is not None:
        conditions.update(
            {
                "改善学科数": gaining >= policy.min_gain_disciplines,
                "各轮不退": all(v >= 0 for v in by_round.values()),
                "单侧符号检验": p <= policy.sign_test_alpha,
            }
        )
        metrics.update({"min_gain_disciplines": policy.min_gain_disciplines, "sign_test_alpha": policy.sign_test_alpha})
    failed = [k for k, ok in conditions.items() if not ok]
    detail = f"净增 {gain}/{len(rows)}，改善学科 {gaining} 个，分轮净增 {dict(by_round)}，文档单侧 p={p:.6g}"
    return GateCheck(
        name=name,
        passed=not failed,
        metrics=metrics,
        detail=detail + ("，未达标：" + "、".join(failed) if failed else ""),
    )


def _document_scores(rows: Sequence[_Pair]) -> dict[str, tuple[float, float]]:
    by_doc: dict[str, list[_Pair]] = defaultdict(list)
    for row in rows:
        by_doc[row.doc.doc_id].append(row)
    return {
        doc: (fmean(r.candidate.score for r in part), fmean(r.baseline.score for r in part))
        for doc, part in by_doc.items()
    }


def _score_summary(scores: Sequence[tuple[float, float]]) -> dict[str, Any]:
    return {
        "documents": len(scores),
        "candidate_mean": fmean(s[0] for s in scores),
        "baseline_mean": fmean(s[1] for s in scores),
        "mean_delta": fmean(a - b for a, b in scores),
    }


def _quality_checks(prefix: str, rows: list[_Pair], docs: Sequence[CaseDoc], policy: PolicyConfig) -> list[GateCheck]:
    overall = _document_scores(rows)
    controls = [r for r in rows if r.category == "control"]
    checks = []
    for key, scores, count, limit in (
        ("overall", overall, len(rows), policy.min_overall_mean_delta),
        ("control", _document_scores(controls), len(controls), policy.min_control_mean_delta),
    ):
        metrics = _score_summary(list(scores.values()))
        checks.append(
            GateCheck(
                name=f"{prefix}.{key}",
                passed=metrics["mean_delta"] >= limit,
                detail=f"文档等权配对均差 {metrics['mean_delta']:.4g}，下限 {limit:g}",
                metrics={**metrics, "segment_rounds": count, "limit": limit},
            )
        )
    strata: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for doc in docs:
        strata[doc.discipline].append(overall[doc.doc_id])
        strata["with_reference" if doc.has_reference else "without_reference"].append(overall[doc.doc_id])
    bootstrap = {k: [a - b for a, b in strata[k]] for k in ALL_DISCIPLINES if k in strata}
    low, high = paired_bootstrap_ci(bootstrap, resamples=policy.bootstrap_resamples, seed=policy.bootstrap_seed)
    checks.append(
        GateCheck(
            name=f"{prefix}.ci",
            passed=low >= policy.min_bootstrap_ci_low,
            detail=f"文档配对差 95% CI [{low:.4g}, {high:.4g}]，下限门槛 {policy.min_bootstrap_ci_low:g}",
            metrics={
                "low": low,
                "high": high,
                "documents": len(overall),
                "resamples": policy.bootstrap_resamples,
                "seed": policy.bootstrap_seed,
            },
        )
    )
    for key in (*ALL_DISCIPLINES, "with_reference", "without_reference"):
        members = strata[key]
        metrics = _score_summary(members) if members else {"documents": 0, "mean_delta": None}
        checks.append(
            GateCheck(
                name=f"{prefix}.stratum.{key}",
                passed=bool(members) and metrics["mean_delta"] >= policy.min_stratum_mean_delta,
                detail=f"{key} 文档均差 {metrics['mean_delta']}，下限 {policy.min_stratum_mean_delta:g}",
                metrics=metrics,
            )
        )
    added = [
        {"doc_id": r.doc.doc_id, "segment_id": r.seg_id, "round_idx": r.rnd, "anchor_id": anchor, "count": n}
        for r in rows
        for anchor, n in sorted((r.candidate.protected - r.baseline.protected).items())
    ]
    checks.append(
        GateCheck(
            name=f"{prefix}.protected",
            passed=not added,
            detail=f"{len(added)} 个锚点段次新增保护错",
            metrics={"added": added, "segment_rounds": len(rows), "new_errors": sum(a["count"] for a in added)},
        )
    )
    for key, predicate in (("major", lambda s: s.major), ("at_most_85", lambda s: s.score <= 85)):
        baseline = sum(predicate(r.baseline) for r in rows)
        candidate = sum(predicate(r.candidate) for r in rows)
        checks.append(
            GateCheck(
                name=f"{prefix}.{key}",
                passed=candidate <= baseline,
                detail=f"{key} 段次从 {baseline} 变为 {candidate}",
                metrics={"candidate": candidate, "baseline": baseline, "segment_rounds": len(rows)},
            )
        )
    return checks


def _compare(
    candidate: GroupId,
    baseline: GroupId,
    ratings: Mapping[VoteKey, _Rating],
    docs: Sequence[CaseDoc],
    policy: PolicyConfig,
    cost: GateCheck,
) -> list[GateCheck]:
    pair = f"{candidate}-{baseline}"
    checks = []
    thresholds: dict[SegmentCategory, int | None]
    if candidate == "B":
        thresholds = {"concept": policy.min_net_gain_b_concept}
    elif baseline == "B":
        thresholds = {
            "concept": None,
            "formula_relation": policy.min_net_gain_c_formula_relation,
            "clause_boundary": policy.min_net_gain_c_clause_boundary,
        }
    else:
        thresholds = {"concept": None, "formula_relation": None, "clause_boundary": None}
    for reviewer in _REVIEWERS:
        prefix = f"{pair}.{reviewer}"
        rows = []
        for doc in docs:
            for seg in doc.segments:
                if not seg.scored:
                    continue
                for rnd in range(policy.rounds):
                    key = (reviewer, rnd, doc.doc_id, seg.segment_id)
                    rows.append(
                        _Pair(
                            doc,
                            seg.segment_id,
                            seg.category,
                            rnd,
                            ratings[(*key, candidate)],
                            ratings[(*key, baseline)],
                        )
                    )
        for cat, limit in thresholds.items():
            checks.append(_target_check(f"{prefix}.{cat}", [r for r in rows if r.category == cat], limit, policy))
        checks.extend(_quality_checks(prefix, rows, docs, policy))
    cm = cost.metrics["comparisons"][pair]
    checks.append(
        GateCheck(
            name=f"{pair}.cost",
            passed=cm["passed"],
            detail=f"成本、请求、耗时超限 {cm['over_limit']}，需复核 {cm['needs_review']}",
            metrics=cm,
        )
    )
    return checks


def decide(
    *,
    run_id: str,
    mode: RunMode,
    docs: Sequence[CaseDoc],
    generations: Sequence[GenerationRecord],
    snapshots: Sequence[ExtractionSnapshot],
    private_maps: Sequence[PrivateMap],
    ballots: Sequence[ReviewerBallot],
    arbitrations: Sequence[ArbitrationRecord] = (),
    policy: PolicyConfig | None = None,
    provenance: Mapping[str, str] | None = None,
) -> DecisionRecord:
    """先校验证据与分歧，再逐评审执行 B-A、C-B 与 C-A 的全部门槛。"""
    policy = policy or load_policy()
    inputs: dict[str, Sequence[BaseModel]] = {
        "docs": docs,
        "generations": generations,
        "snapshots": snapshots,
        "private_maps": private_maps,
        "ballots": ballots,
        "arbitrations": arbitrations,
    }
    evidence = sha256_canonical(
        {
            "mode": mode,
            "policy": policy.model_dump(mode="json"),
            **{k: _evidence_rows(v, run_id) for k, v in inputs.items()},
        }
    )
    holdout = sorted((d for d in docs if d.split == "holdout"), key=lambda d: d.doc_id)
    summary: dict[str, Any] = {
        "holdout_docs": len(holdout),
        "rounds": policy.rounds,
        "input_counts": {k: len(v) for k, v in inputs.items()},
    }
    checks: list[GateCheck] = []

    def decision(state: DecisionState, reasons: list[str]) -> DecisionRecord:
        return DecisionRecord(
            run_id=run_id,
            mode=mode,
            state=state,
            eligible_b=state in ("ELIGIBLE_B_ONLY", "ELIGIBLE_B_AND_C"),
            eligible_c=state == "ELIGIBLE_B_AND_C",
            reasons=reasons,
            checks=checks,
            summary=summary,
            evidence_hash=evidence,
            provenance=dict(provenance or {}),
        )

    try:
        votes = _validate(run_id, mode, docs, generations, snapshots, private_maps, ballots, policy)
        anchors = _anchors(holdout)
        categories: dict[tuple[str, str], SegmentCategory] = {
            (d.doc_id, s.segment_id): s.category for d in holdout for s in d.segments if s.scored
        }
        ratings = {k: _rating(v, categories[k[2], k[3]], anchors.get((k[2], k[3]), {})) for k, v in votes.items()}
    except ValueError as exc:
        checks.append(GateCheck(name="integrity", passed=False, detail=str(exc)))
        return decision("INCOMPLETE", [str(exc)])
    checks.append(
        GateCheck(
            name="integrity",
            passed=True,
            detail="三组生成、术语快照与双评还原完整",
            metrics={"expanded_votes": len(votes), "scored_segments": len(categories)},
        )
    )
    pending = detect_disagreements(votes, holdout, policy, arbitrations)
    checks.append(
        GateCheck(
            name="disagreements",
            passed=not pending,
            detail=f"{len(pending)} 项未决分歧",
            metrics={"unresolved": pending},
        )
    )
    if mode == "live" and pending:
        return decision("HOLD", [checks[-1].detail])

    cost = evaluate_cost_and_latency(holdout, generations, snapshots, policy)
    checks.append(cost)
    b_checks = _compare("B", "A", ratings, holdout, policy, cost)
    c_checks = _compare("C", "B", ratings, holdout, policy, cost) + _compare("C", "A", ratings, holdout, policy, cost)
    b_ok = all(check.passed for check in b_checks)
    c_checks.append(GateCheck(name="C.requires_B", passed=b_ok, detail="C 准入须以 B 全部门槛达标为前提"))
    checks.extend(b_checks + c_checks)
    outcome: DecisionState = "RETAIN_A"
    if b_ok:
        outcome = "ELIGIBLE_B_AND_C" if all(check.passed for check in c_checks) else "ELIGIBLE_B_ONLY"
    reasons = [f"{check.name}：{check.detail}" for check in checks if not check.passed]
    if mode == "selftest":
        summary["simulated_gate_preview"] = {
            "state": "HOLD" if pending else outcome,
            "checks": [c.model_dump(mode="json") for c in checks],
            "reasons": reasons,
        }
        return decision("OFFLINE_VERIFIED", ["离线证据完整，模拟裁决仅供核验"])
    return decision(outcome, reasons or ["两位评审的 B、C 质量门槛及三组成本比较全部达标"])

"""批次 D（TR-011 / TR-012 / A-B-C 消融）统一运行器：selftest / live / report。"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .contract import (
    ALL_GROUPS,
    PROTOCOL_VERSION,
    ArbitrationRecord,
    CaseDoc,
    DecisionRecord,
    DecisionState,
    EndpointConfig,
    ExtractionSnapshot,
    GenerationRecord,
    JudgeItem,
    JudgeItemReview,
    LiveConfig,
    PolicyConfig,
    PrivateMap,
    ReviewerBallot,
    ReviewerId,
    RunMode,
    canonical_json,
    load_policy,
    sha256_canonical,
    sha256_text,
)
from .gate import decide
from .generate import PreparedTrees, run_generation
from .judge import TransportFn, build_blind_packets, review_packet, write_blind_artifacts
from .term_diagnostics import TermDiagnosticReport, diagnose_terms

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_REPO_ROOT = _PACKAGE_DIR.parents[1]
_REVIEWERS: tuple[ReviewerId, ...] = ("codex", "neon")

EXIT_CODE_BY_STATE: dict[DecisionState, int] = {
    "OFFLINE_VERIFIED": 0,
    "ELIGIBLE_B_AND_C": 0,
    "ELIGIBLE_B_ONLY": 3,
    "RETAIN_A": 4,
    "INCOMPLETE": 2,
    "HOLD": 2,
}


def exit_code_for_state(state: DecisionState) -> int:
    return EXIT_CODE_BY_STATE.get(state, 1)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as scratch:
        staged = Path(scratch) / path.name
        with staged.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)


@contextmanager
def _block_network() -> Iterator[list[str]]:
    """在离线 selftest 期间阻断所有 socket 外连并记录尝试次数。"""
    attempts: list[str] = []
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create = socket.create_connection

    def _blocked_connect(self: socket.socket, address: Any) -> Any:
        attempts.append(f"connect:{address!r}")
        raise RuntimeError(f"离线模式禁止网络连接：{address!r}")

    def _blocked_connect_ex(self: socket.socket, address: Any) -> Any:
        attempts.append(f"connect_ex:{address!r}")
        raise RuntimeError(f"离线模式禁止网络连接：{address!r}")

    def _blocked_create(address: Any, *args: Any, **kwargs: Any) -> Any:
        attempts.append(f"create_connection:{address!r}")
        raise RuntimeError(f"离线模式禁止网络连接：{address!r}")

    socket.socket.connect = _blocked_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _blocked_connect_ex  # type: ignore[method-assign]
    socket.create_connection = _blocked_create  # type: ignore[assignment]
    try:
        yield attempts
    finally:
        socket.socket.connect = orig_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = orig_connect_ex  # type: ignore[method-assign]
        socket.create_connection = orig_create  # type: ignore[assignment]


def load_fixtures(
    repo_root: Path,
    *,
    split: str = "holdout",
    limit_docs: int | None = None,
) -> list[CaseDoc]:
    path = repo_root / "spikes" / "batch_d" / "fixtures" / f"{split}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"找不到语料文件：{path}")
    docs = [CaseDoc.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit_docs is not None:
        if limit_docs <= 0 or limit_docs > len(docs):
            raise ValueError(f"limit_docs 必须在 1..{len(docs)} 之间，实际为 {limit_docs}")
        by_disc: dict[str, list[CaseDoc]] = {}
        for doc in docs:
            by_disc.setdefault(doc.discipline, []).append(doc)
        picked: list[CaseDoc] = []
        idx = 0
        disc_order = sorted(by_disc)
        while len(picked) < limit_docs:
            progressed = False
            for disc in disc_order:
                bucket = by_disc[disc]
                if idx < len(bucket) and len(picked) < limit_docs:
                    picked.append(bucket[idx])
                    progressed = True
            if not progressed:
                break
            idx += 1
        docs = picked
    return docs


def _load_arbitrations(path: Path | None) -> list[ArbitrationRecord]:
    if path is None or not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        return [ArbitrationRecord.model_validate(item) for item in json.loads(text)]
    return [ArbitrationRecord.model_validate_json(line) for line in text.splitlines() if line.strip()]


def _offline_judge_transport(reviewer: ReviewerId) -> TransportFn:
    """严格盲态离线评审替身：仅依据匿名题面确定性输出合法 JSON，不接触组名或映射。"""

    def _transport(endpoint: EndpointConfig, _api_key: str, _system: str, user: str) -> tuple[str, str, str]:
        item = JudgeItem.model_validate_json(user)
        candidates_out: list[dict[str, Any]] = []
        for cand in item.candidates:
            errors: list[dict[str, Any]] = []
            text = cand.text
            if "。" in text and ("尽管" in text[:8] or "虽然" in text[:8]):
                head = text.split("。", 1)[0] + "。"
                if len(head) < len(text) and len(head) <= 28:
                    errors.append(
                        {
                            "category": "fluency",
                            "sub_category": "clause_boundary",
                            "severity": "minor",
                            "protected": False,
                            "source_span": "",
                            "target_span": head,
                            "reason": "段内从句保留句号断句",
                        }
                    )
            if "按分布" in text and "<m1/>" in text and "泊松" not in text and "服从" not in text:
                errors.append(
                    {
                        "category": "accuracy",
                        "sub_category": "formula_relation",
                        "severity": "minor",
                        "protected": False,
                        "source_span": "",
                        "target_span": "按分布",
                        "reason": "公式外关系表述偏简略",
                    }
                )
            score = max(0, 100 - sum(15 if e["severity"] == "major" else 4 for e in errors))
            candidates_out.append({"label": cand.label, "errors": errors, "score": score})
        payload = {"item_id": item.item_id, "candidates": candidates_out}
        req_id = f"offline-{reviewer}-{item.item_id}"
        return canonical_json(payload), endpoint.model, req_id

    return _transport


def attach_private_anchors(
    ballots: Sequence[ReviewerBallot],
    private_maps: Sequence[PrivateMap],
    docs: Sequence[CaseDoc],
) -> list[ReviewerBallot]:
    """在私有侧把缺失 anchor_id 的保护性错误归属到本段已冻结的保护锚点。"""
    anchors_by_seg: dict[tuple[str, str], list[str]] = {}
    for doc in docs:
        for anchor in doc.anchors:
            if anchor.protected:
                anchors_by_seg.setdefault((doc.doc_id, anchor.segment_id), []).append(anchor.anchor_id)
    map_lookup: dict[tuple[ReviewerId, int, str], tuple[str, str]] = {}
    for pmap in private_maps:
        for item in pmap.items:
            map_lookup[(pmap.reviewer, pmap.round_idx, item.item_id)] = (item.doc_id, item.segment_id)

    patched_ballots: list[ReviewerBallot] = []
    for ballot in ballots:
        new_reviews: list[JudgeItemReview] = []
        for rev in ballot.reviews:
            coord = map_lookup.get((ballot.reviewer, ballot.round_idx, rev.item_id))
            seg_anchors = anchors_by_seg.get(coord, []) if coord is not None else []
            new_cands = []
            for cand in rev.candidates:
                new_errs = []
                for err in cand.errors:
                    if err.protected and not err.anchor_id and seg_anchors:
                        new_errs.append(err.model_copy(update={"anchor_id": seg_anchors[0]}))
                    else:
                        new_errs.append(err)
                new_cands.append(cand.model_copy(update={"errors": new_errs}))
            new_reviews.append(rev.model_copy(update={"candidates": new_cands}))
        patched_ballots.append(ballot.model_copy(update={"reviews": new_reviews}))
    return patched_ballots


def _collect_term_diagnostics(
    docs: Sequence[CaseDoc],
    snapshots: Sequence[ExtractionSnapshot],
    generations: Sequence[GenerationRecord],
) -> dict[str, Any]:
    docs_by_id = {d.doc_id: d for d in docs}
    gen_texts: dict[tuple[int, str, str], list[str]] = {}
    for rec in generations:
        gen_texts.setdefault((rec.round_idx, rec.doc_id, rec.group), []).append(rec.target)

    reports: list[dict[str, Any]] = []
    totals = {
        "A": {"suspect": 0, "mixed": 0, "expected": 0, "covered": 0, "distinguished": 0},
        "B": {"suspect": 0, "mixed": 0, "expected": 0, "covered": 0, "distinguished": 0},
    }
    for snap in snapshots:
        doc = docs_by_id.get(snap.doc_id)
        if doc is None:
            continue
        diag: TermDiagnosticReport = diagnose_terms(
            snap.raw_terms,
            snap.normalized_terms,
            user_terms=doc.user_terms,
            expected_pairs=doc.term_pairs,
            translated_texts=gen_texts.get((snap.round_idx, snap.doc_id, snap.source_group), ()),
        )
        bucket = totals[snap.source_group]
        bucket["suspect"] += diag.suspect_collision_count
        bucket["mixed"] += diag.mixed_group_count
        bucket["expected"] += diag.expected_pair_total
        bucket["covered"] += diag.expected_pair_covered
        bucket["distinguished"] += diag.expected_pair_distinguished
        reports.append(
            {
                "round_idx": snap.round_idx,
                "doc_id": snap.doc_id,
                "group": snap.source_group,
                "report": diag.model_dump(mode="json"),
            }
        )
    return {"totals": totals, "per_snapshot": reports}


def _preflight_live(
    cfg: LiveConfig,
    docs: Sequence[CaseDoc],
    policy: PolicyConfig,
    *,
    generate_only: bool,
) -> list[str]:
    errors: list[str] = []
    required_eps: list[tuple[str, EndpointConfig]] = [("generator", cfg.generator)]
    if not generate_only:
        required_eps.extend([("codex", cfg.codex), ("neon", cfg.neon)])
    for role, ep in required_eps:
        if not os.environ.get(ep.key_env, "").strip():
            errors.append(f"缺少环境变量凭据 {ep.key_env}（角色 {role}）")

    scored_segs = sum(1 for d in docs for s in d.segments if s.scored)
    est_extract_reqs = len(docs) * policy.rounds * 2
    est_translate_reqs = len(docs) * policy.rounds * len(ALL_GROUPS)
    est_judge_reqs = 0 if generate_only else scored_segs * policy.rounds * len(_REVIEWERS)
    est_total_reqs = est_extract_reqs + est_translate_reqs + est_judge_reqs
    if est_total_reqs > cfg.budget.max_requests:
        errors.append(f"预计最低请求数 {est_total_reqs} 超出预算上限 {cfg.budget.max_requests}")

    pricing = cfg.pricing or policy.default_pricing
    gen_price = pricing.get(cfg.generator.model)
    if gen_price is not None:
        total_words = sum(len(s.source.split()) for d in docs for s in d.segments if s.scored)
        est_in_tokens = (total_words * 3 + len(docs) * 1200) * policy.rounds * len(ALL_GROUPS)
        est_out_tokens = (total_words * 2 + len(docs) * 400) * policy.rounds * len(ALL_GROUPS)
        est_cost = (
            est_in_tokens * gen_price.cache_miss_cny_per_m + est_out_tokens * gen_price.output_cny_per_m
        ) / 1_000_000.0
        if est_cost > cfg.budget.max_cost_cny:
            errors.append(f"预计生成费用 ¥{est_cost:.4f} 超出预算上限 ¥{cfg.budget.max_cost_cny:.4f}")
    return errors


def render_markdown_report(
    decision: DecisionRecord,
    diagnostics: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> str:
    lines = [
        "# 批次 D（TR-011 / TR-012 / A-B-C 消融）评测报告",
        "",
        f"- **协议版本**：`{decision.protocol_version}`",
        f"- **运行标识**：`{decision.run_id}`",
        f"- **运行模式**：`{decision.mode}`",
        f"- **最终状态**：**`{decision.state}`**",
        f"- **生产准入资格**：`eligible_b={decision.eligible_b}`，`eligible_c={decision.eligible_c}`",
        f"- **证据摘要 (`evidence_hash`)**：`{decision.evidence_hash}`",
        f"- **基线提交**：`{manifest.get('base_sha', '')}`",
        "",
        "## 1. 术语碰撞与覆盖诊断（A vs B）",
        "",
        "| 组别 | 可疑撞名组 | 混合组 | 预期词对总数 | 覆盖数 | 成功区分数 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    totals = diagnostics.get("totals", {})
    for group in ("A", "B"):
        row = totals.get(group, {})
        lines.append(
            f"| {group} | {row.get('suspect', 0)} | {row.get('mixed', 0)} "
            f"| {row.get('expected', 0)} | {row.get('covered', 0)} | {row.get('distinguished', 0)} |"
        )
    lines.extend(["", "## 2. 门禁逐项结果", "", "| 门禁项 | 结果 | 说明 |", "|---|---|---|"])
    for chk in decision.checks:
        status = "PASS" if chk.passed else "FAIL"
        lines.append(f"| `{chk.name}` | {status} | {chk.detail} |")
    if decision.reasons:
        lines.extend(["", "## 3. 结论说明", ""])
        for reason in decision.reasons:
            lines.append(f"- {reason}")
    lines.append("")
    return "\n".join(lines)


def _write_run_outputs(
    out_dir: Path,
    *,
    manifest: dict[str, Any],
    diagnostics: dict[str, Any],
    decision: DecisionRecord,
) -> None:
    _atomic_write_text(out_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _atomic_write_text(out_dir / "diagnostics.json", json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n")
    _atomic_write_text(out_dir / "decision.json", decision.model_dump_json(indent=2) + "\n")
    _atomic_write_text(out_dir / "report.md", render_markdown_report(decision, diagnostics, manifest))


def _offline_endpoints() -> dict[ReviewerId, EndpointConfig]:
    return {
        "codex": EndpointConfig(
            protocol="openai_compat",
            base_url="https://offline.invalid/v1",
            model="codex-offline-stub",
            model_family="offline-openai-family",
            key_env="OFFLINE_CODEX_KEY",
            allowed_model_ids=["codex-offline-stub"],
        ),
        "neon": EndpointConfig(
            protocol="anthropic",
            base_url="https://offline.invalid",
            model="neon-offline-stub",
            model_family="offline-gemini-family",
            key_env="OFFLINE_NEON_KEY",
            allowed_model_ids=["neon-offline-stub"],
        ),
    }


def _build_manifest(
    *,
    run_id: str,
    mode: RunMode,
    seed: int,
    policy: PolicyConfig,
    trees: PreparedTrees,
    docs: Sequence[CaseDoc],
    network_calls: int,
    generate_only: bool,
    reviewer_meta: Mapping[ReviewerId, Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run_id,
        "mode": mode,
        "base_sha": trees.base_sha,
        "patch_hashes": dict(trees.patch_hashes),
        "prompt_hashes": {g: dict(v) for g, v in trees.prompt_hashes.items()},
        "policy": policy.model_dump(mode="json"),
        "doc_ids": [d.doc_id for d in docs],
        "seed_fingerprint": sha256_text(f"{run_id}:{mode}:{seed}"),
        "network_calls": network_calls,
        "generate_only": generate_only,
        "reviewers": {r: dict(meta) for r, meta in reviewer_meta.items()},
    }


def execute_selftest(
    *,
    repo_root: Path,
    out_dir: Path,
    seed: int = 20261125,
    run_id: str = "selftest-v1",
    limit_docs: int | None = None,
    rounds: int | None = None,
) -> tuple[DecisionRecord, int]:
    resolved_out = out_dir.resolve()
    if resolved_out.is_relative_to(repo_root.resolve()):
        raise ValueError("实验输出目录必须位于仓库外")
    policy = load_policy()
    docs = load_fixtures(repo_root, split="holdout", limit_docs=limit_docs)
    effective_rounds = policy.rounds if rounds is None else rounds
    effective_policy = policy.model_copy(update={"holdout_docs": len(docs), "rounds": effective_rounds})

    with _block_network() as net_attempts:
        trees, snapshots, generations = run_generation(
            run_id=run_id,
            mode="selftest",
            repo_root=repo_root,
            out_dir=resolved_out,
            docs=docs,
            rounds=effective_rounds,
            policy=effective_policy,
        )
        endpoints = _offline_endpoints()
        private_maps: list[PrivateMap] = []
        ballots: list[ReviewerBallot] = []
        for reviewer in _REVIEWERS:
            for rnd in range(effective_rounds):
                item_seed = seed + (101 if reviewer == "codex" else 202) * (rnd + 1)
                packet, pmap = build_blind_packets(
                    run_id=run_id,
                    docs=docs,
                    generations=generations,
                    reviewer=reviewer,
                    round_idx=rnd,
                    private_seed=item_seed,
                )
                write_blind_artifacts(resolved_out, packet, pmap)
                private_maps.append(pmap)
                ballot = review_packet(
                    packet,
                    endpoints[reviewer],
                    run_id=run_id,
                    mode="selftest",
                    out_path=resolved_out / "reviews" / f"{reviewer}_r{rnd}.jsonl",
                    transport=_offline_judge_transport(reviewer),
                    dry_key="",
                )
                ballots.append(ballot)

        anchored_ballots = attach_private_anchors(ballots, private_maps, docs)
        diagnostics = _collect_term_diagnostics(docs, snapshots, generations)
        provenance = {
            "base_sha": trees.base_sha,
            "tr011_patch_sha256": trees.patch_hashes["tr011.patch"],
            "tr012_patch_sha256": trees.patch_hashes["tr012.patch"],
            "network_calls": str(len(net_attempts)),
        }
        decision = decide(
            run_id=run_id,
            mode="selftest",
            docs=docs,
            generations=generations,
            snapshots=snapshots,
            private_maps=private_maps,
            ballots=anchored_ballots,
            policy=effective_policy,
            provenance=provenance,
        )
        manifest = _build_manifest(
            run_id=run_id,
            mode="selftest",
            seed=seed,
            policy=effective_policy,
            trees=trees,
            docs=docs,
            network_calls=len(net_attempts),
            generate_only=False,
            reviewer_meta={
                r: {"model_family": endpoints[r].model_family, "model": endpoints[r].model} for r in _REVIEWERS
            },
        )
        _write_run_outputs(resolved_out, manifest=manifest, diagnostics=diagnostics, decision=decision)
    return decision, exit_code_for_state(decision.state)


def execute_live(
    *,
    repo_root: Path,
    config_path: Path,
    out_dir: Path,
    seed: int = 20261125,
    run_id: str = "live-v1",
    generate_only: bool = False,
    arbitrations_path: Path | None = None,
    transport_map: Mapping[ReviewerId, TransportFn] | None = None,
) -> tuple[DecisionRecord, int]:
    resolved_out = out_dir.resolve()
    if resolved_out.is_relative_to(repo_root.resolve()):
        raise ValueError("实验输出目录必须位于仓库外")
    cfg = LiveConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    policy = load_policy()
    docs = load_fixtures(repo_root, split="holdout")
    preflight_errors = _preflight_live(cfg, docs, policy, generate_only=generate_only)
    if preflight_errors:
        decision = DecisionRecord(
            run_id=run_id,
            mode="live",
            state="INCOMPLETE",
            eligible_b=False,
            eligible_c=False,
            reasons=preflight_errors,
            checks=[],
            summary={"preflight_failed": True},
            evidence_hash=sha256_canonical({"run_id": run_id, "preflight_errors": preflight_errors}),
            provenance={"base_sha": policy.baseline_sha},
        )
        _atomic_write_text(resolved_out / "decision.json", decision.model_dump_json(indent=2) + "\n")
        return decision, exit_code_for_state("INCOMPLETE")

    trees, snapshots, generations = run_generation(
        run_id=run_id,
        mode="live",
        repo_root=repo_root,
        out_dir=resolved_out,
        docs=docs,
        rounds=policy.rounds,
        policy=policy,
        endpoint=cfg.generator,
        pricing=cfg.pricing or policy.default_pricing,
    )
    diagnostics = _collect_term_diagnostics(docs, snapshots, generations)
    manifest = _build_manifest(
        run_id=run_id,
        mode="live",
        seed=seed,
        policy=policy,
        trees=trees,
        docs=docs,
        network_calls=-1,
        generate_only=generate_only,
        reviewer_meta={
            "codex": {"model_family": cfg.codex.model_family, "model": cfg.codex.model},
            "neon": {"model_family": cfg.neon.model_family, "model": cfg.neon.model},
        },
    )
    if generate_only:
        decision = DecisionRecord(
            run_id=run_id,
            mode="live",
            state="INCOMPLETE",
            eligible_b=False,
            eligible_c=False,
            reasons=["已按 --generate-only 完成三组生成，尚缺双盲评审票"],
            checks=[],
            summary={"generated_records": len(generations), "snapshots": len(snapshots)},
            evidence_hash=sha256_canonical(
                {
                    "run_id": run_id,
                    "generate_only": True,
                    "snapshots": [s.ordered_hash for s in snapshots],
                }
            ),
            provenance={
                "base_sha": trees.base_sha,
                "tr011_patch_sha256": trees.patch_hashes["tr011.patch"],
                "tr012_patch_sha256": trees.patch_hashes["tr012.patch"],
            },
        )
        _write_run_outputs(resolved_out, manifest=manifest, diagnostics=diagnostics, decision=decision)
        return decision, exit_code_for_state("INCOMPLETE")

    endpoints: dict[ReviewerId, EndpointConfig] = {"codex": cfg.codex, "neon": cfg.neon}
    private_maps: list[PrivateMap] = []
    ballots: list[ReviewerBallot] = []
    for reviewer in _REVIEWERS:
        for rnd in range(policy.rounds):
            item_seed = seed + (101 if reviewer == "codex" else 202) * (rnd + 1)
            packet, pmap = build_blind_packets(
                run_id=run_id,
                docs=docs,
                generations=generations,
                reviewer=reviewer,
                round_idx=rnd,
                private_seed=item_seed,
            )
            write_blind_artifacts(resolved_out, packet, pmap)
            private_maps.append(pmap)
            ballot = review_packet(
                packet,
                endpoints[reviewer],
                run_id=run_id,
                mode="live",
                out_path=resolved_out / "reviews" / f"{reviewer}_r{rnd}.jsonl",
                transport=transport_map.get(reviewer) if transport_map else None,
            )
            ballots.append(ballot)

    anchored_ballots = attach_private_anchors(ballots, private_maps, docs)
    arbitrations = _load_arbitrations(arbitrations_path)
    provenance = {
        "base_sha": trees.base_sha,
        "tr011_patch_sha256": trees.patch_hashes["tr011.patch"],
        "tr012_patch_sha256": trees.patch_hashes["tr012.patch"],
        "codex_model": cfg.codex.model,
        "neon_model": cfg.neon.model,
    }
    decision = decide(
        run_id=run_id,
        mode="live",
        docs=docs,
        generations=generations,
        snapshots=snapshots,
        private_maps=private_maps,
        ballots=anchored_ballots,
        arbitrations=arbitrations,
        policy=policy,
        provenance=provenance,
    )
    _write_run_outputs(resolved_out, manifest=manifest, diagnostics=diagnostics, decision=decision)
    return decision, exit_code_for_state(decision.state)


def execute_report(
    *,
    repo_root: Path,
    run_dir: Path,
    arbitrations_path: Path | None = None,
) -> tuple[DecisionRecord, int]:
    resolved = run_dir.resolve()
    manifest = json.loads((resolved / "manifest.json").read_text(encoding="utf-8"))
    policy = PolicyConfig.model_validate(manifest["policy"])
    mode: RunMode = manifest["mode"]
    run_id: str = manifest["run_id"]
    all_docs = load_fixtures(repo_root, split="holdout")
    doc_ids = set(manifest["doc_ids"])
    docs = [d for d in all_docs if d.doc_id in doc_ids]

    snaps_path = resolved / "private" / "snapshots.jsonl"
    gens_path = resolved / "private" / "generations.jsonl"
    snapshots = [
        ExtractionSnapshot.model_validate_json(line)
        for line in snaps_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    generations = [
        GenerationRecord.model_validate_json(line)
        for line in gens_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    private_maps: list[PrivateMap] = []
    ballots: list[ReviewerBallot] = []
    offline_eps = _offline_endpoints()
    for reviewer in _REVIEWERS:
        for rnd in range(policy.rounds):
            pmap_path = resolved / "private" / f"map_{reviewer}_r{rnd}.json"
            rev_path = resolved / "reviews" / f"{reviewer}_r{rnd}.jsonl"
            if pmap_path.exists():
                private_maps.append(PrivateMap.model_validate_json(pmap_path.read_text(encoding="utf-8")))
            if rev_path.exists():
                reviews: list[JudgeItemReview] = []
                for line in rev_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    row.pop("binding_sha256", None)
                    reviews.append(JudgeItemReview.model_validate(row))
                ep = offline_eps[reviewer]
                recorded = manifest.get("reviewers", {}).get(reviewer, {})
                model_id = str(
                    recorded.get("model") or (reviews[0].model_echo if reviews and reviews[0].model_echo else ep.model)
                )
                model_family = str(recorded.get("model_family") or ep.model_family)
                ballots.append(
                    ReviewerBallot(
                        run_id=run_id,
                        mode=mode,
                        reviewer=reviewer,
                        round_idx=rnd,
                        model_family=model_family,
                        model_id=model_id,
                        reviews=reviews,
                    )
                )

    anchored = attach_private_anchors(ballots, private_maps, docs)
    diagnostics = _collect_term_diagnostics(docs, snapshots, generations)
    decision = decide(
        run_id=run_id,
        mode=mode,
        docs=docs,
        generations=generations,
        snapshots=snapshots,
        private_maps=private_maps,
        ballots=anchored,
        arbitrations=_load_arbitrations(arbitrations_path),
        policy=policy,
        provenance={"base_sha": manifest["base_sha"], "recomputed_from_disk": "true"},
    )
    _write_run_outputs(resolved, manifest=manifest, diagnostics=diagnostics, decision=decision)
    return decision, exit_code_for_state(decision.state)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批次 D（TR-011 / TR-012 / A-B-C 消融）离线自检与在线评测入口")
    sub = parser.add_subparsers(dest="command", required=True)

    p_self = sub.add_parser("selftest", help="无外网离线确定性端到端自检")
    p_self.add_argument("--out", type=Path, required=True, help="仓库外输出目录")
    p_self.add_argument("--seed", type=int, default=20261125, help="固定盲化随机种子")
    p_self.add_argument("--run-id", default="selftest-v1", help="固定运行标识")
    p_self.add_argument("--limit-docs", type=int, default=None, help="可选限制留出篇数（默认全量 24 篇）")
    p_self.add_argument("--rounds", type=int, default=None, help="可选覆盖轮数（默认使用 policy.json）")

    p_live = sub.add_parser("live", help="接入仓库外私有凭据配置运行真实 A/B/C 与异构双盲评审")
    p_live.add_argument("--config", type=Path, required=True, help="仓库外 live-config.json 路径")
    p_live.add_argument("--out", type=Path, required=True, help="仓库外输出目录")
    p_live.add_argument("--seed", type=int, default=20261125, help="私有随机种子")
    p_live.add_argument("--run-id", default="live-v1", help="本次运行标识")
    p_live.add_argument("--generate-only", action="store_true", help="仅运行 A/B/C 生成，不执行双盲评审")
    p_live.add_argument("--arbitrations", type=Path, default=None, help="可选盲态仲裁记录 JSON/JSONL")

    p_rep = sub.add_parser("report", help="从已落盘证据无网络重算门禁与报告")
    p_rep.add_argument("--run", type=Path, required=True, help="已有运行目录")
    p_rep.add_argument("--arbitrations", type=Path, default=None, help="可选盲态仲裁记录 JSON/JSONL")
    return parser


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = (repo_root or _DEFAULT_REPO_ROOT).resolve()
    try:
        if args.command == "selftest":
            decision, code = execute_selftest(
                repo_root=root,
                out_dir=args.out,
                seed=args.seed,
                run_id=args.run_id,
                limit_docs=args.limit_docs,
                rounds=args.rounds,
            )
        elif args.command == "live":
            decision, code = execute_live(
                repo_root=root,
                config_path=args.config,
                out_dir=args.out,
                seed=args.seed,
                run_id=args.run_id,
                generate_only=args.generate_only,
                arbitrations_path=args.arbitrations,
            )
        else:
            decision, code = execute_report(
                repo_root=root,
                run_dir=args.run,
                arbitrations_path=args.arbitrations,
            )
    except Exception as exc:
        print(f"[batch_d] 执行失败：{exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "run_id": decision.run_id,
                "mode": decision.mode,
                "state": decision.state,
                "eligible_b": decision.eligible_b,
                "eligible_c": decision.eligible_c,
                "evidence_hash": decision.evidence_hash,
            },
            ensure_ascii=False,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())

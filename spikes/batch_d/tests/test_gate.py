"""在合成留出集上验证双评、统计单位、硬阈值与终态优先级。"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from typing import Any, Literal

from ..contract import (
    ALL_CATEGORIES,
    ALL_DISCIPLINES,
    ALL_GROUPS,
    ArbitrationRecord,
    CandidateReview,
    CaseDoc,
    CaseSegment,
    CheckJudgment,
    DecisionRecord,
    ExtractionSnapshot,
    FactAnchor,
    GenerationRecord,
    GroupId,
    JudgeItemReview,
    MqmError,
    PrivateItemMapping,
    PrivateMap,
    ReviewerBallot,
    ReviewerId,
    RunMode,
    SegmentCategory,
    load_policy,
)
from ..gate import decide, evaluate_cost_and_latency, exact_one_sided_sign_test_p, paired_bootstrap_ci, unblind_ballots

type Rubric = Callable[[ReviewerId, GroupId, SegmentCategory, int, int], list[MqmError]]


def _normal(reviewer: ReviewerId, group: GroupId, cat: SegmentCategory, doc: int, rnd: int) -> list[MqmError]:
    if cat == "concept" and group == "A":
        return [MqmError(category="terminology", severity="minor")]
    if cat in ("formula_relation", "clause_boundary") and group != "C":
        return [MqmError(category="accuracy", sub_category=cat, severity="minor")]
    return []


class Evidence:
    def __init__(self, mode: RunMode = "live", rule: Rubric = _normal, fold_control: bool = False):
        self.mode = mode
        self.docs: list[CaseDoc] = []
        self.generations: list[GenerationRecord] = []
        self.snapshots: list[ExtractionSnapshot] = []
        self.maps: list[PrivateMap] = []
        self.ballots: list[ReviewerBallot] = []
        self.policy = load_policy()
        self.policy.bootstrap_resamples = 100
        for i in range(24):
            doc_id = f"synthetic-{i:02}"
            has_reference = bool((i // len(ALL_DISCIPLINES)) % 2)
            segments = [
                CaseSegment(
                    segment_id=cat,
                    order=j,
                    category=cat,
                    source=f"Synthetic example {j}.",
                    reference_zh="合成参照。" if has_reference else None,
                )
                for j, cat in enumerate(ALL_CATEGORIES)
            ]
            anchors = [
                FactAnchor(anchor_id=f"fact-{j}", segment_id=s.segment_id, kind=s.category, description="合成事实")
                for j, s in enumerate(segments)
            ]
            self.docs.append(
                CaseDoc(
                    doc_id=doc_id,
                    split="holdout",
                    discipline=ALL_DISCIPLINES[i % 4],
                    template_family=f"synthetic-{i}",
                    title="Synthetic case",
                    abstract="Synthetic text.",
                    segments=segments,
                    anchors=anchors,
                    has_reference=has_reference,
                )
            )
        for rnd in range(2):
            items: list[PrivateItemMapping] = []
            reviewers: tuple[ReviewerId, ...] = ("codex", "neon")
            reviews: dict[ReviewerId, list[JudgeItemReview]] = {r: [] for r in reviewers}
            for i, doc in enumerate(self.docs):
                snapshot_hashes = {}
                source_groups: tuple[Literal["A", "B"], ...] = ("A", "B")
                for g in source_groups:
                    snap = ExtractionSnapshot(
                        doc_id=doc.doc_id,
                        round_idx=rnd,
                        source_group=g,
                        domain=f"domain-{g}",
                        raw_response_sha256="raw",
                        prompt_sha256="extract",
                        cost_cny=1,
                        elapsed_s=1,
                    )
                    self.snapshots.append(snap)
                    snapshot_hashes[g] = snap.ordered_hash
                for seg in doc.segments:
                    item_id = f"item-{i}-{seg.order}"
                    labels: dict[GroupId, str] = {}
                    grouped: dict[str, list[GroupId]] = {}
                    for j, group in enumerate(ALL_GROUPS):
                        label = "x" if fold_control and seg.category == "control" else f"k{j}"
                        labels[group] = label
                        grouped.setdefault(label, []).append(group)
                        self.generations.append(
                            GenerationRecord(
                                run_id="r",
                                mode=mode,
                                round_idx=rnd,
                                group=group,
                                doc_id=doc.doc_id,
                                segment_id=seg.segment_id,
                                category=seg.category,
                                source=seg.source,
                                expanded_source=seg.source,
                                target=f"合成译文 {label}",
                                status="done",
                                attempts=1,
                                snapshot_hash=snapshot_hashes["A" if group == "A" else "B"],
                                system_prompt_sha256="system",
                                user_prompt_sha256="user",
                                cost_cny=1,
                                elapsed_s=1,
                            )
                        )
                    items.append(
                        PrivateItemMapping(
                            item_id=item_id,
                            doc_id=doc.doc_id,
                            segment_id=seg.segment_id,
                            category=seg.category,
                            round_idx=rnd,
                            permutation=("A", "B", "C"),
                            group_to_label=labels,
                            label_to_groups=grouped,
                        )
                    )
                    for r in reviewers:
                        candidates = [
                            CandidateReview(label=label, errors=rule(r, groups[0], seg.category, i, rnd))
                            for label, groups in grouped.items()
                        ]
                        reviews[r].append(JudgeItemReview(item_id=item_id, candidates=candidates))
            for r in reviewers:
                self.maps.append(PrivateMap(run_id="r", reviewer=r, round_idx=rnd, seed_fingerprint=r, items=items))
                self.ballots.append(
                    ReviewerBallot(
                        run_id="r", mode=mode, reviewer=r, round_idx=rnd, model_family=r, model_id=r, reviews=reviews[r]
                    )
                )

    def decide(self, **overrides: Any) -> DecisionRecord:
        args: dict[str, Any] = dict(
            run_id="r",
            mode=self.mode,
            docs=self.docs,
            generations=self.generations,
            snapshots=self.snapshots,
            private_maps=self.maps,
            ballots=self.ballots,
            policy=self.policy,
        )
        return decide(**(args | overrides))


def _metrics(record: DecisionRecord, name: str) -> dict[str, Any]:
    return next(c.metrics for c in record.checks if c.name == name)


class GateTest(unittest.TestCase):
    def test_live_both_eligible_and_folded_denominators(self):
        data = Evidence(fold_control=True)
        data.policy = load_policy()
        votes = unblind_ballots(data.maps, data.ballots)
        self.assertEqual(len(votes), 2 * 2 * 24 * 4 * 3)
        key = ("codex", 0, data.docs[0].doc_id, "control")
        self.assertIs(votes[(*key, "A")], votes[(*key, "C")])
        record = data.decide()
        self.assertEqual(record.state, "ELIGIBLE_B_AND_C", record.reasons)
        self.assertTrue(record.eligible_b and record.eligible_c)
        self.assertTrue(all(c.passed for c in record.checks))
        for name in ("B-A.codex.concept", "C-B.neon.formula_relation", "C-B.neon.clause_boundary"):
            m = _metrics(record, name)
            self.assertEqual(
                (m["net_gain"], m["segment_rounds"], m["wins"], m["ties"], m["losses"]), (48, 48, 24, 0, 0)
            )
            self.assertEqual(m["p_value"], 2**-24)
        m = _metrics(record, "B-A.codex.overall")
        self.assertEqual((m["mean_delta"], m["documents"], m["segment_rounds"]), (1, 24, 192))
        self.assertEqual(_metrics(record, "B-A.codex.ci")["low"], 1)

    def test_offline_preview_never_grants_eligibility(self):
        data = Evidence("selftest")
        record = data.decide()
        self.assertEqual(record.state, "OFFLINE_VERIFIED")
        self.assertFalse(record.eligible_b or record.eligible_c)
        preview = record.summary["simulated_gate_preview"]
        self.assertEqual(preview["state"], "ELIGIBLE_B_AND_C")
        self.assertTrue(all(c["passed"] for c in preview["checks"]))
        self.assertEqual(data.decide(ballots=[]).state, "INCOMPLETE")

    def test_integrity_precedes_live_decision(self):
        d = Evidence()
        first, *rest = d.generations
        ballot, *others = d.ballots
        changes = {
            "missing_ballot": dict(ballots=others),
            "foreign_map": dict(private_maps=[d.maps[0].model_copy(update={"run_id": "other"}), *d.maps[1:]]),
            "foreign_ballot": dict(ballots=[ballot.model_copy(update={"run_id": "other"}), *others]),
            "foreign_generation": dict(generations=[first.model_copy(update={"run_id": "other"}), *rest]),
            "mixed_mode": dict(ballots=[ballot.model_copy(update={"mode": "selftest"}), *others]),
            "duplicate_ballot": dict(ballots=[*d.ballots, ballot]),
            "same_family": dict(
                ballots=[
                    b.model_copy(update={"model_family": " SAME " if b.reviewer == "codex" else "same"})
                    for b in d.ballots
                ]
            ),
            "failed": dict(generations=[first.model_copy(update={"status": "failed"}), *rest]),
            "fallback": dict(generations=[first.model_copy(update={"compatibility_fallback": True}), *rest]),
            "wrong_snapshot": dict(generations=[first.model_copy(update={"snapshot_hash": "foreign"}), *rest]),
            "missing_generation": dict(generations=rest),
            "duplicate_generation": dict(generations=[first, *d.generations]),
            "missing_snapshot": dict(snapshots=d.snapshots[1:]),
            "missing_doc": dict(docs=d.docs[1:]),
        }
        for cause, kwargs in changes.items():
            with self.subTest(cause=cause):
                record = d.decide(**kwargs)
                self.assertEqual(record.state, "INCOMPLETE")
                self.assertTrue(record.reasons)
                self.assertFalse(record.eligible_b or record.eligible_c)

    def test_fold_requires_identical_text_and_unique_expanded_votes(self):
        d = Evidence(fold_control=True)
        control = next(g for g in d.generations if g.category == "control")
        control.target += "变体"
        self.assertEqual(d.decide().state, "INCOMPLETE")
        item = d.maps[0].items[0]
        duplicate = item.model_copy(update={"item_id": "extra"})
        d.maps[0] = d.maps[0].model_copy(update={"items": [*d.maps[0].items, duplicate]})
        extra = d.ballots[0].reviews[0].model_copy(update={"item_id": "extra"})
        d.ballots[0].reviews.append(extra)
        with self.assertRaisesRegex(ValueError, "展开后重复票"):
            unblind_ballots(d.maps, d.ballots)

    def test_missing_and_duplicate_candidate_votes(self):
        d = Evidence()
        item = d.ballots[0].reviews[0]
        last = item.candidates.pop()
        with self.assertRaisesRegex(ValueError, "候选"):
            unblind_ballots(d.maps, d.ballots)
        item.candidates.extend([last, last])
        with self.assertRaisesRegex(ValueError, "重复"):
            unblind_ballots(d.maps, d.ballots)

    def test_score_gap_of_fifteen_requires_its_own_resolution(self):
        def rule(r, g, cat, i, rnd):
            if r == "neon" and g == "A" and cat == "control" and i == rnd == 0:
                return [MqmError(category="fluency", severity="major")]
            return _normal(r, g, cat, i, rnd)

        d = Evidence(rule=rule)
        arb = ArbitrationRecord(doc_id=d.docs[0].doc_id, segment_id="control", round_idx=0, reason="复核")
        record = d.decide(arbitrations=[arb])
        self.assertEqual(record.state, "HOLD")
        pending = _metrics(record, "disagreements")["unresolved"]
        self.assertEqual([(p["kind"], p["score_gap"], p["group"]) for p in pending], [("score_gap", 15, "A")])
        arb.resolved = True
        arb.resolved_errors = {"C": []}
        self.assertEqual(d.decide(arbitrations=[arb]).state, "HOLD")
        arb.resolved_errors = {"A": []}
        self.assertEqual(d.decide(arbitrations=[arb]).state, "ELIGIBLE_B_AND_C")

    def test_equal_scores_can_disagree_on_protected_fact(self):
        def rule(r, g, cat, i, rnd):
            if g == "A" and cat == "control" and i == rnd == 0:
                if r == "neon":
                    return [MqmError(category="accuracy", severity="minor", anchor_id="fact-3", protected=True)]
                return [MqmError(category="fluency", severity="minor")]
            return _normal(r, g, cat, i, rnd)

        d = Evidence(rule=rule)
        arb = ArbitrationRecord(
            doc_id=d.docs[0].doc_id, segment_id="control", round_idx=0, anchor_id="other", reason="复核", resolved=True
        )
        record = d.decide(arbitrations=[arb])
        self.assertEqual(record.state, "HOLD")
        self.assertEqual(_metrics(record, "disagreements")["unresolved"][0]["anchor_id"], "fact-3")
        arb.anchor_id = "fact-3"
        self.assertEqual(d.decide(arbitrations=[arb]).state, "ELIGIBLE_B_AND_C")
        self.assertEqual(Evidence("selftest", rule=rule).decide().state, "OFFLINE_VERIFIED")

    def test_c_must_improve_both_categories_and_keep_overall_quality(self):
        def one_category(r, g, cat, i, rnd):
            if g == "C" and cat == "clause_boundary":
                return _normal(r, "B", cat, i, rnd)
            return _normal(r, g, cat, i, rnd)

        record = Evidence(rule=one_category).decide()
        self.assertEqual(record.state, "ELIGIBLE_B_ONLY")
        self.assertTrue(record.eligible_b)
        self.assertFalse(record.eligible_c)
        self.assertEqual(_metrics(record, "C-B.codex.clause_boundary")["net_gain"], 0)

        def loses_quality(r, g, cat, i, rnd):
            if g == "C" and cat == "control":
                return [MqmError(category="fluency", severity="major")]
            return _normal(r, g, cat, i, rnd)

        record = Evidence(rule=loses_quality).decide()
        self.assertEqual(record.state, "ELIGIBLE_B_ONLY")
        failed = {c.name for c in record.checks if not c.passed}
        self.assertTrue({f"C-B.neon.{k}" for k in ("control", "overall", "ci", "major", "at_most_85")} <= failed)
        self.assertIn("C-A.neon.control", failed)

    def test_c_cannot_skip_b_and_reviewers_cannot_offset_one_another(self):
        def opposite(r, g, cat, i, rnd):
            if r == "neon" and cat == "concept":
                return [MqmError(category="terminology", severity="minor")] if g == "B" else []
            return _normal(r, g, cat, i, rnd)

        record = Evidence(rule=opposite).decide()
        self.assertEqual(record.state, "RETAIN_A")
        self.assertFalse(record.eligible_b or record.eligible_c)
        self.assertEqual(_metrics(record, "B-A.neon.concept")["net_gain"], -48)
        self.assertEqual(_metrics(record, "B-A.codex.concept")["net_gain"], 48)
        self.assertTrue(all(c.passed for c in record.checks if c.name.startswith("C-A.")))

    def test_five_fixed_anchors_cannot_offset_one_new_error(self):
        def rule(r, g, cat, i, rnd):
            if cat == "control" and rnd == 0 and ((i < 5 and g == "A") or (i == 5 and g == "B")):
                return [MqmError(category="accuracy", severity="minor", anchor_id="fact-3", protected=True)]
            return _normal(r, g, cat, i, rnd)

        d = Evidence(rule=rule)
        record = d.decide()
        self.assertEqual(record.state, "RETAIN_A")
        self.assertEqual(
            _metrics(record, "B-A.codex.protected")["added"],
            [dict(doc_id=d.docs[5].doc_id, segment_id="control", round_idx=0, anchor_id="fact-3", count=1)],
        )

    def test_cost_allocates_extraction_and_enforces_inclusive_ratios(self):
        d = Evidence()
        for row in d.generations:
            if row.group == "C":
                row.cost_cny = 1.125
                row.elapsed_s = 1.3125
        for row in d.generations:
            if row.group == "C" and row.round_idx == 0 and row.category == "control":
                row.attempts = 2
        cost = evaluate_cost_and_latency(d.docs, d.generations, d.snapshots, d.policy)
        self.assertTrue(cost.passed)
        self.assertEqual(
            cost.metrics["comparisons"]["C-B"]["ratios"], {"cost_cny": 1.1, "requests": 1.1, "p95_latency_s": 1.25}
        )
        self.assertEqual(cost.metrics["groups"]["B"]["cost_cny"], 240)
        self.assertEqual(cost.metrics["per_doc"]["C"][d.docs[0].doc_id]["cost_cny"], 11)
        next(s for s in d.snapshots if s.source_group == "B").cost_cny = 2
        larger = evaluate_cost_and_latency(d.docs, d.generations, d.snapshots, d.policy)
        self.assertEqual(larger.metrics["groups"]["B"]["cost_cny"], 241)
        self.assertEqual(larger.metrics["groups"]["C"]["cost_cny"], 265)
        self.assertEqual(d.decide().state, "ELIGIBLE_B_ONLY")

    def test_cost_missing_or_zero_denominator_requires_review(self):
        d = Evidence()
        next(g for g in d.generations if g.group == "C").cost_cny = None
        record = d.decide()
        self.assertEqual(record.state, "ELIGIBLE_B_ONLY")
        self.assertEqual(_metrics(record, "C-B.cost")["needs_review"], ["cost_cny"])
        for s in d.snapshots:
            s.elapsed_s = 0
        for g in d.generations:
            g.elapsed_s = 0
        cost = evaluate_cost_and_latency(d.docs, d.generations, d.snapshots, d.policy)
        self.assertIsNone(cost.metrics["comparisons"]["B-A"]["ratios"]["p95_latency_s"])

    def test_hash_ignores_order_paths_and_telemetry_and_binds_evidence(self):
        d = Evidence("selftest")
        first = d.decide(provenance={"out": "first", "clock": "1"})
        d.generations.reverse()
        d.maps.reverse()
        d.ballots.reverse()
        d.ballots[0].reviews.reverse()
        d.docs.reverse()
        d.generations[0].elapsed_s = 100
        d.generations[0].request_id = "new-call"
        second = d.decide(provenance={"out": "second", "clock": "2"})
        self.assertEqual(first.evidence_hash, second.evidence_hash)
        d.generations[0].target += "变更"
        self.assertNotEqual(first.evidence_hash, d.decide().evidence_hash)

    def test_target_uses_checks_and_document_level_signs(self):
        d = Evidence()
        for b in d.ballots:
            for i, item in enumerate(b.reviews[::4]):
                # 四篇文档各改善两轮。把八段次当作八篇会误判为显著。
                if i >= 4:
                    item.candidates[1].checks = [CheckJudgment(check_id="concept", status="fail")]
        record = d.decide()
        self.assertEqual(record.state, "RETAIN_A")
        m = _metrics(record, "B-A.codex.concept")
        self.assertEqual((m["wins"], m["ties"], m["losses"], m["net_gain"], m["p_value"]), (4, 20, 0, 8, 0.0625))

    def test_exact_sign_test(self):
        for wins, losses, expected in [(0, 0, 1), (0, 3, 1), (4, 0, 1 / 16), (5, 0, 1 / 32), (3, 1, 5 / 16)]:
            self.assertEqual(exact_one_sided_sign_test_p(wins, losses), expected)
        with self.assertRaises(ValueError):
            exact_one_sided_sign_test_p(-1, 1)

    def test_exact_gain_and_discipline_boundaries(self):
        def rule(r, g, cat, i, rnd):
            if cat != "concept":
                return _normal(r, g, cat, i, rnd)
            if (g == "A" and i < 10 and rnd == i % 2) or (g == "B" and 10 <= i < 13):
                return [MqmError(category="terminology", severity="minor")]
            return []

        d = Evidence(rule=rule)
        record = d.decide()
        self.assertEqual(record.state, "ELIGIBLE_B_AND_C", record.reasons)
        m = _metrics(record, "B-A.codex.concept")
        self.assertEqual((m["net_gain"], m["wins"], m["ties"], m["losses"]), (4, 10, 11, 3))
        self.assertEqual(m["p_value"], 378 / 8192)
        self.assertEqual(sum(v > 0 for v in m["by_discipline"].values()), 2)
        for field, limit in (("min_net_gain_b_concept", 5), ("min_gain_disciplines", 3)):
            with self.subTest(field=field):
                self.assertEqual(d.decide(policy=d.policy.model_copy(update={field: limit})).state, "RETAIN_A")

    def test_pooled_gain_cannot_hide_one_discipline_or_a_losing_round(self):
        def one_discipline(r, g, cat, i, rnd):
            if cat == "concept" and i % 4:
                return []
            return _normal(r, g, cat, i, rnd)

        record = Evidence(rule=one_discipline).decide()
        self.assertEqual(record.state, "RETAIN_A")
        m = _metrics(record, "B-A.codex.concept")
        self.assertEqual((m["net_gain"], m["p_value"]), (12, 1 / 64))

        def one_round(r, g, cat, i, rnd):
            if cat == "concept" and rnd == 1:
                return [MqmError(category="terminology", severity="minor")] if i == 0 and g == "B" else []
            return _normal(r, g, cat, i, rnd)

        record = Evidence(rule=one_round).decide()
        self.assertEqual(record.state, "RETAIN_A")
        self.assertEqual(_metrics(record, "B-A.codex.concept")["by_round"], {0: 24, 1: -1})

    def test_c_must_preserve_b_concepts_and_each_a_target(self):
        def formula(r, g, cat, i, rnd):
            if cat == "formula_relation":
                if g == "A":
                    return []
                if i == rnd == 0 and g == "C":
                    return _normal(r, "B", cat, i, rnd)
            return _normal(r, g, cat, i, rnd)

        record = Evidence(rule=formula).decide()
        self.assertEqual(record.state, "ELIGIBLE_B_ONLY")
        self.assertEqual(_metrics(record, "C-A.neon.formula_relation")["net_gain"], -1)
        self.assertEqual(_metrics(record, "C-B.neon.formula_relation")["net_gain"], 47)

        def concept(r, g, cat, i, rnd):
            if cat == "concept" and i == rnd == 0 and g == "C":
                return _normal(r, "A", cat, i, rnd)
            return _normal(r, g, cat, i, rnd)

        record = Evidence(rule=concept).decide()
        self.assertEqual(record.state, "ELIGIBLE_B_ONLY")
        self.assertEqual(_metrics(record, "C-B.neon.concept")["net_gain"], -1)

    def test_p95_aggregates_document_latency_before_taking_percentile(self):
        d = Evidence()
        for g in d.generations:
            if g.group == "C" and g.doc_id in {doc.doc_id for doc in d.docs[:3]} and g.category == "control":
                g.elapsed_s = 6
                g.attempts = 6
        record = d.decide()
        self.assertEqual(record.state, "ELIGIBLE_B_ONLY")
        m = _metrics(record, "C-B.cost")
        self.assertEqual(m["ratios"]["p95_latency_s"], 2)
        self.assertEqual(m["over_limit"], ["requests", "p95_latency_s"])

    def test_bootstrap_resamples_whole_docs_with_fixed_stratum_sizes(self):
        self.assertEqual(paired_bootstrap_ci({"large": [0, 0, 0], "small": [12]}), (3, 3))
        sample = {"first": [-2, 0, 3], "second": [4]}
        interval = paired_bootstrap_ci(sample)
        self.assertEqual(interval, paired_bootstrap_ci({"second": [4], "first": [3, -2, 0]}))
        self.assertEqual(interval, (-0.5, 3.25))
        with self.assertRaises(ValueError):
            paired_bootstrap_ci({})


if __name__ == "__main__":
    unittest.main()

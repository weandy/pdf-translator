"""Wave 0 契约、策略与安全红线守卫单元测试。"""

from __future__ import annotations

import unittest

from pydantic import ValidationError
from spikes.batch_d.contract import (
    BASELINE_CACHE_GOLD_SHA256,
    BASELINE_EXTRACT_PROMPT_SHA256,
    BASELINE_RULES_SHA256,
    BASELINE_SHA,
    BudgetConfig,
    CaseDoc,
    CaseSegment,
    DecisionRecord,
    EndpointConfig,
    ExtractionSnapshot,
    FactAnchor,
    JudgeItem,
    LiveConfig,
    MqmError,
    PrivateItemMapping,
    compute_mqm_score,
    load_policy,
    sha256_text,
)


class ContractTest(unittest.TestCase):
    def test_policy_loads_and_matches_frozen_baseline(self) -> None:
        policy = load_policy()
        self.assertEqual(policy.baseline_sha, BASELINE_SHA)
        self.assertEqual(policy.baseline_prompt_version, "v7")
        self.assertEqual(policy.baseline_validation_version, 7)
        self.assertEqual(policy.dev_docs, 8)
        self.assertEqual(policy.holdout_docs, 24)
        self.assertEqual(policy.rounds, 2)
        self.assertEqual(policy.tr012_max_char_delta, 120)

    def test_case_doc_validates_placeholders_orders_and_anchors(self) -> None:
        doc = CaseDoc(
            doc_id="synth-01",
            split="dev",
            discipline="operations_scheduling",
            template_family="fam-01",
            title="Synthetic Scheduling Study",
            abstract="This study distinguishes cycle span and completion duration.",
            headings=["1 Introduction"],
            segments=[
                CaseSegment(
                    segment_id="s0",
                    order=0,
                    section="1 Introduction",
                    source="Value <m1/> follows the rule.",
                    formulas={"m1": "x > 0"},
                    category="formula_relation",
                )
            ],
            anchors=[
                FactAnchor(
                    anchor_id="a1",
                    segment_id="s0",
                    kind="formula_relation",
                    description="Preserve inequality relation",
                )
            ],
        )
        self.assertEqual(doc.doc_id, "synth-01")

        with self.assertRaises(ValidationError):
            CaseSegment(
                segment_id="s-bad",
                order=0,
                source="Value <m1/> and <m2/>.",
                formulas={"m1": "x"},
            )

        with self.assertRaises(ValidationError):
            CaseDoc(
                doc_id="synth-bad-anchor",
                split="dev",
                discipline="operations_scheduling",
                template_family="fam-02",
                title="Bad Anchor",
                abstract="Abstract.",
                segments=[CaseSegment(segment_id="s0", order=0, source="Plain text.")],
                anchors=[
                    FactAnchor(
                        anchor_id="a1",
                        segment_id="missing",
                        kind="control",
                        description="Broken ref",
                    )
                ],
            )

    def test_selftest_decision_cannot_grant_promotion_eligibility(self) -> None:
        with self.assertRaises(ValidationError):
            DecisionRecord(
                run_id="r1",
                mode="selftest",
                state="ELIGIBLE_B_AND_C",
                eligible_b=True,
                eligible_c=True,
                evidence_hash=sha256_text("x"),
            )

        ok = DecisionRecord(
            run_id="r1",
            mode="selftest",
            state="OFFLINE_VERIFIED",
            eligible_b=False,
            eligible_c=False,
            evidence_hash=sha256_text("x"),
        )
        self.assertEqual(ok.state, "OFFLINE_VERIFIED")

    def test_c_cannot_be_eligible_without_b(self) -> None:
        with self.assertRaises(ValidationError):
            DecisionRecord(
                run_id="r2",
                mode="live",
                state="RETAIN_A",
                eligible_b=False,
                eligible_c=True,
                evidence_hash=sha256_text("y"),
            )

    def test_live_config_enforces_independent_families_and_positive_budget(self) -> None:
        gen = EndpointConfig(
            protocol="openai_compat",
            base_url="https://api.example.com/v1",
            model="deepseek-chat",
            model_family="deepseek",
            key_env="GEN_KEY",
        )
        codex = EndpointConfig(
            protocol="openai_compat",
            base_url="https://api.example.com/v1",
            model="gpt-judge",
            model_family="openai",
            key_env="CODEX_KEY",
        )
        neon = EndpointConfig(
            protocol="anthropic",
            base_url="https://api.example.com",
            model="gemini-judge",
            model_family="gemini",
            key_env="NEON_KEY",
        )
        budget = BudgetConfig(max_requests=500, max_cost_cny=20.0, max_seconds_per_stage=600.0)
        cfg = LiveConfig(generator=gen, codex=codex, neon=neon, budget=budget)
        self.assertEqual(cfg.codex.model_family, "openai")
        self.assertIs(cfg.generator.allow_private, False)

        with self.assertRaises(ValidationError):
            LiveConfig(
                generator=gen,
                codex=codex,
                neon=codex.model_copy(update={"model": "other-openai"}),
                budget=budget,
            )

        with self.assertRaises(ValidationError):
            BudgetConfig(max_requests=0, max_cost_cny=10.0, max_seconds_per_stage=60.0)

    def test_blind_item_forbids_leaking_fields(self) -> None:
        with self.assertRaises(ValidationError):
            JudgeItem.model_validate(
                {
                    "item_id": "t01",
                    "source_expanded": "Hello world.",
                    "candidates": [{"label": "k1", "text": "你好世界。"}],
                    "group": "B",
                }
            )

    def test_private_mapping_and_snapshot_hash_and_mqm(self) -> None:
        mapping = PrivateItemMapping(
            item_id="t01",
            doc_id="d1",
            segment_id="s0",
            round_idx=0,
            category="concept",
            label_to_groups={"k1": ["A", "B"], "k2": ["C"]},
            group_to_label={"A": "k1", "B": "k1", "C": "k2"},
            permutation=("B", "C", "A"),
        )
        self.assertEqual(mapping.group_to_label["C"], "k2")

        snap = ExtractionSnapshot(
            doc_id="d1",
            round_idx=0,
            source_group="B",
            domain="测试领域",
            use_library=False,
            normalized_terms=[("makespan", "最大完工时间")],
            raw_response_sha256=sha256_text("raw"),
            prompt_sha256=BASELINE_EXTRACT_PROMPT_SHA256,
        )
        self.assertEqual(len(snap.ordered_hash), 64)
        self.assertEqual(len(BASELINE_RULES_SHA256), 64)
        self.assertEqual(len(BASELINE_CACHE_GOLD_SHA256), 64)

        score = compute_mqm_score(
            [
                MqmError(category="accuracy", severity="major", protected=True),
                MqmError(category="fluency", severity="minor"),
            ]
        )
        self.assertEqual(score, 81)


if __name__ == "__main__":
    unittest.main()

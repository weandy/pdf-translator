"""TR-011 固定基线补丁与术语诊断回归测试。"""

from __future__ import annotations

import unittest
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory

from pdftr.glossary import extract
from pdftr.glossary.normalize import normalize_terms

from ..contract import BASELINE_EXTRACT_PROMPT_SHA256, TermPairExpectation
from ..term_diagnostics import diagnose_terms, verify_tr011_patch

_ROOT = Path(__file__).resolve().parents[3]
_PATCH = _ROOT / "spikes/batch_d/tr011.patch"


class PatchTest(unittest.TestCase):
    def test_patch_applies_to_frozen_source_and_matches_both_prompt_hashes(self) -> None:
        self.assertNotIn(b"\r", _PATCH.read_bytes())
        self.assertEqual(
            verify_tr011_patch(_ROOT),
            {
                "baseline": BASELINE_EXTRACT_PROMPT_SHA256,
                "candidate": "cd47c3aaad53f1db917fb9554a0738b3399f86a4644931350e203e02e739ead3",
            },
        )

    def test_rejects_patch_with_an_additional_file(self) -> None:
        extra = (
            b"diff --git a/unexpected.txt b/unexpected.txt\n"
            b"new file mode 100644\n--- /dev/null\n+++ b/unexpected.txt\n@@ -0,0 +1 @@\n+unexpected\n"
        )
        with TemporaryDirectory() as tmp:
            patch = Path(tmp) / "two-files.patch"
            patch.write_bytes(_PATCH.read_bytes() + extra)
            with self.assertRaisesRegex(ValueError, "只允许"):
                verify_tr011_patch(_ROOT, patch)

    def test_rejects_crlf(self) -> None:
        with TemporaryDirectory() as tmp:
            patch = Path(tmp) / "crlf.patch"
            patch.write_bytes(_PATCH.read_bytes().replace(b"\n", b"\r\n"))
            with self.assertRaisesRegex(ValueError, "LF"):
                verify_tr011_patch(_ROOT, patch)


class TermDiagnosticsTest(unittest.TestCase):
    def test_distinct_pair_collides_in_raw_and_is_distinguished_after_normalization(self) -> None:
        raw = {"cycle span": " 周期\t时长 ", "stage span": "周期  时长"}
        norm = [("cycle spans", "周期时长"), ("stage spans", "阶段时长")]
        pair = TermPairExpectation(source_a="stage span", source_b="cycle span", relation="distinct")
        report = diagnose_terms(raw, norm, expected_pairs=[pair])
        (group,) = report.raw_collisions
        self.assertEqual((group.kind, group.target, group.pair_kinds), ("suspect", "周期 时长", ["suspect"]))
        self.assertEqual(report.normalized_collisions, [])
        self.assertEqual((report.raw_term_count, report.normalized_term_count), (2, 2))
        self.assertEqual(
            (report.expected_pair_total, report.expected_pair_covered, report.expected_pair_distinguished), (1, 1, 1)
        )
        (item,) = report.pair_items
        self.assertEqual((item.target_a, item.target_b, item.distinguished), ("阶段时长", "周期时长", True))
        self.assertTrue(item.in_raw_a and item.in_raw_b and item.in_normalized_a and item.in_normalized_b)

    def test_ordinary_priority_and_its_plural_are_one_source_key(self) -> None:
        raw = extract.parse("priority=优先级\npriorities=优先级").terms
        normalized = normalize_terms(raw, ["One priority precedes two priorities."])
        report = diagnose_terms(raw, normalized)
        self.assertEqual((len(raw), len(normalized)), (2, 1))
        self.assertEqual((report.raw_collisions, report.normalized_collisions), ([], []))
        self.assertEqual(report.suspect_collision_count, 0)

    def test_target_case_punctuation_and_internal_space_remain_significant(self) -> None:
        terms = {"first": "Q 值", "second": "q 值", "third": "Q值", "fourth": "Q-值"}
        self.assertEqual(diagnose_terms(terms, terms).normalized_collisions, [])

    def test_abbreviation_definition_and_explicit_alias_are_transitive(self) -> None:
        terms = {"TGs": "阈值门", "threshold gates": "阈值门", "trigger gate": "阈值门"}
        pair = TermPairExpectation(source_a="trigger gate", source_b="threshold gate", relation="synonym")
        report = diagnose_terms(terms, terms, definitions={"TG": "threshold gate"}, expected_pairs=[pair])
        (group,) = report.normalized_collisions
        self.assertEqual(group.kind, "benign_synonym_or_acronym")
        self.assertEqual(group.pair_kinds, ["benign_synonym_or_acronym"] * 3)
        self.assertFalse(group.involves_user_term)
        self.assertEqual((report.suspect_collision_count, report.expected_pair_covered), (0, 1))

    def test_explicit_acronym_does_not_require_a_definition_and_is_case_sensitive(self) -> None:
        pair = TermPairExpectation(source_a="Phase Cell", source_b="PC", relation="acronym")
        for short, expected_kind in (("PCs", "benign_synonym_or_acronym"), ("pc", "suspect")):
            with self.subTest(short=short):
                terms = {"phase cells": "相位单元", short: "相位单元"}
                report = diagnose_terms(terms, terms, expected_pairs=[pair])
                self.assertEqual(report.normalized_collisions[0].kind, expected_kind)

    def test_user_mapping_requires_both_sources_and_the_actual_target(self) -> None:
        terms = {"step size": "步幅", "grid step": "步幅"}
        for user_terms, expected_kind in (
            ({"step-size": "步幅 ", "grid steps": " 步幅"}, "user_intentional_same"),
            ({"step size": "步幅"}, "suspect"),
            ({"step size": "步长", "grid step": "步长"}, "suspect"),
        ):
            with self.subTest(user_terms=user_terms):
                (group,) = diagnose_terms(terms, terms, user_terms=user_terms).normalized_collisions
                self.assertEqual(group.kind, expected_kind)
                self.assertTrue(group.involves_user_term)

    def test_user_same_expectation_alone_exempts_the_pair(self) -> None:
        terms = {"phase span": "时长", "step span": "时长"}
        pair = TermPairExpectation(source_a="step span", source_b="phase span", relation="user_same")
        report = diagnose_terms(terms, terms, expected_pairs=[pair], translated_texts=["“时长”与“时长”"])
        self.assertEqual(report.normalized_collisions[0].kind, "user_intentional_same")
        self.assertEqual((report.suspect_collision_count, report.expected_pair_covered), (0, 1))
        self.assertFalse(report.pair_items[0].in_body_conflict)

    def test_mixed_group_retains_each_pair_classification(self) -> None:
        terms = {"anchor gate": "门", "AG": "门", "filter gate": "门"}
        report = diagnose_terms(terms, terms, definitions={"AG": "anchor gate"})
        (group,) = report.normalized_collisions
        self.assertEqual(group.kind, "mixed")
        self.assertEqual(group.canonical_keys, ["acr:AG", "word:anchor gate", "word:filter gate"])
        pairs = dict(zip(combinations(group.sources, 2), group.pair_kinds, strict=True))
        self.assertEqual(
            pairs,
            {
                ("AG", "anchor gate"): "benign_synonym_or_acronym",
                ("AG", "filter gate"): "suspect",
                ("anchor gate", "filter gate"): "suspect",
            },
        )
        self.assertEqual((report.suspect_collision_count, report.mixed_group_count), (1, 1))

    def test_coverage_requires_both_normalized_terms_and_excludes_optional_pairs(self) -> None:
        raw = {"level shift": "偏移", "pulse shift": "移动", "unit size": "单元大小"}
        normalized = normalize_terms(raw, ["A level shift changes the unit size."])
        pairs = [
            TermPairExpectation(source_a="level shift", source_b="unit size", relation="distinct"),
            TermPairExpectation(source_a="pulse shift", source_b="level shift", relation="distinct"),
            TermPairExpectation(source_a="gap size", source_b="gap width", relation="synonym"),
            TermPairExpectation(
                source_a="accuracy", source_b="priority", relation="distinct", expect_in_glossary=False
            ),
        ]
        report = diagnose_terms(raw, normalized, expected_pairs=pairs)
        self.assertEqual(
            (report.expected_pair_total, report.expected_pair_covered, report.expected_pair_distinguished), (3, 1, 1)
        )
        self.assertEqual(report.missed_expected_pairs, [("pulse shift", "level shift"), ("gap size", "gap width")])
        _, partial, absent, optional = report.pair_items
        self.assertTrue(partial.in_raw_a and partial.in_raw_b and partial.in_normalized_b)
        self.assertFalse(partial.in_normalized_a)
        self.assertEqual((partial.target_a, partial.target_b, partial.distinguished), (None, "偏移", False))
        self.assertFalse(absent.in_raw_a or absent.in_raw_b or absent.in_normalized_a or absent.in_normalized_b)
        self.assertFalse(optional.expect_in_glossary)

    def test_body_evidence_requires_explicit_pair_context(self) -> None:
        terms = {"stage span": "时长", "cycle span": "时长"}
        pair = TermPairExpectation(source_a="cycle span", source_b="stage span", relation="distinct")
        for texts, flags in (
            (["“时长”与“时长”的差为零。"], (True, False)),
            (["时长（stage spans）与时长（cycle span）分别计量。"], (False, True)),
            (["时长已知。", "索引（stage span）和单元（cycle span）均有效。"], (False, False)),
            (["时长（cycle span）用于此步。", "时长（stage span）用于下一步。"], (False, False)),
            ([], (False, False)),
        ):
            with self.subTest(texts=texts):
                (item,) = diagnose_terms(terms, terms, expected_pairs=[pair], translated_texts=texts).pair_items
                self.assertEqual((item.in_body_conflict, item.body_clear_despite_same_target), flags)
                self.assertFalse(item.distinguished)

    def test_production_parse_filters_standalone_accuracy_and_keeps_qualified_term(self) -> None:
        found = extract.parse("领域：合成测量\naccuracy=准确度\nphase accuracy=相位准确度")
        self.assertEqual(found.terms, {"phase accuracy": "相位准确度"})


if __name__ == "__main__":
    unittest.main()

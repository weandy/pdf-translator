"""检查批次 D 合成语料的分层、评测边界和生产词表保留率。"""

from __future__ import annotations

import re
import subprocess
import unicodedata
import unittest
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from spikes.batch_d.contract import ALL_CATEGORIES, ALL_DISCIPLINES, CaseDoc, contains_emoji, sha256_text

from pdftr.domain.text import RICH_TAG, normalize_term_key
from pdftr.glossary.extract import extract_acronym_definitions, parse
from pdftr.glossary.normalize import normalize_terms

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROOT = FIXTURES.parents[2]
FORMULA = re.compile(r"<(m\d+)/>")
CITATION = re.compile(r"\[\d+(?:,\s*\d+)*\]")
NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
HAN = re.compile(r"[\u3400-\u9fff]")
# 只用公开标记拦截出处、文件和模型痕迹，避免在测试自身收录私有标识。
FORBIDDEN = re.compile(
    r"\b(?:arxiv|orcid|doi|isbn|issn|github|gitlab|openreview|supplementary material)\b"
    r"|10\.\d{4,9}/|(?:https?|file)://|www\.|[\w.+-]+@[\w.-]+\.\w+"
    r"|\.(?:pdf|docx?|tex|bib|pptx?|xlsx?|csv|jsonl?|zip)\b",
    re.IGNORECASE,
)
LOCAL_PATH = re.compile(
    r"[a-zA-Z]:[\\/]|\\\\[^\\/ ]+[\\/]|//[\w.-]+/|(?:~|\.\.?)[\\/]"
    r"|(?<![\w<])/(?:[\w.~ -]+/)*[\w.~ -]+"
)
# 契约的常用 emoji 范围之外，还有旗帜、键帽、变体选择符和少数独立符号。
EXTRA_EMOJI = re.compile(
    r"[\U0001f000-\U0001f2ff\u00a9\u00ae\u20e3\ufe0f\u231a-\u231b\u23e9-\u23f3\u23f8-\u23fa"
    r"\u25aa-\u25ab\u25b6\u25c0\u25fb-\u25fe\u2b05-\u2b07\u2b1b\u2b1c\u2b50\u2b55"
    r"\u203c\u2049\u2122\u2139\u2194-\u2199\u21a9\u21aa\u2328\u23cf\u24c2\u2934\u2935"
    r"\u3030\u303d\u3297\u3299]"
)


def _strings(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _digest(source: str) -> str:
    folded = unicodedata.normalize("NFKC", source).casefold()
    folded = FORMULA.sub(" formula ", RICH_TAG.sub("", folded))
    return sha256_text(" ".join(re.findall(r"[a-z]+", folded)))


def _load(split: str) -> list[CaseDoc]:
    path = FIXTURES / f"{split}.jsonl"
    docs: list[CaseDoc] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            docs.append(CaseDoc.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path.name} 第 {number} 行不符合 CaseDoc 契约") from exc
    return docs


class CorpusTest(unittest.TestCase):
    splits: dict[str, list[CaseDoc]]
    docs: list[CaseDoc]

    @classmethod
    def setUpClass(cls) -> None:
        cls.splits = {split: _load(split) for split in ("dev", "holdout")}
        cls.docs = [doc for docs in cls.splits.values() for doc in docs]

    def test_strata_and_scored_segments(self) -> None:
        for split, per_cell in (("dev", 1), ("holdout", 3)):
            docs = self.splits[split]
            self.assertEqual(len(docs), 8 * per_cell)
            self.assertEqual(
                Counter((d.discipline, d.has_reference) for d in docs),
                {(field, ref): per_cell for field in ALL_DISCIPLINES for ref in (True, False)},
            )
            for doc in docs:
                with self.subTest(split=split, doc=doc.doc_id):
                    self.assertEqual(doc.split, split)
                    scored = [s for s in doc.segments if s.scored]
                    self.assertEqual(Counter(s.category for s in scored), Counter(ALL_CATEGORIES))
                    clause = next(s for s in scored if s.category == "clause_boundary")
                    self.assertIn(". ", clause.source)
                    self.assertLessEqual(len(doc.segments) - len(scored), 1)
                    self.assertTrue(doc.abstract.strip() and doc.headings and all(h.strip() for h in doc.headings))
                    self.assertTrue(all(s.section in doc.headings for s in doc.segments))
                    self.assertTrue(
                        any(
                            s.role == "text" and s.source != doc.abstract and len(s.source.split()) >= 30
                            for s in doc.segments
                        )
                    )
                    protected = {(a.segment_id, a.kind) for a in doc.anchors if a.protected}
                    self.assertTrue({(s.segment_id, s.category) for s in scored} <= protected)

    def test_disjoint_documents_templates_pairs_and_normalized_segments(self) -> None:
        self.assertEqual(len({d.doc_id for d in self.docs}), len(self.docs))
        dev, holdout = self.splits["dev"], self.splits["holdout"]
        self.assertFalse({d.template_family for d in dev} & {d.template_family for d in holdout})
        self.assertFalse(
            {_digest(s.source) for d in dev for s in d.segments}
            & {_digest(s.source) for d in holdout for s in d.segments}
        )
        # 无序对还可识别把同一题的两术语对调、改大小写或单复数的情形。
        pairs = [
            {frozenset(map(normalize_term_key, (p.source_a, p.source_b))) for d in docs for p in d.term_pairs}
            for docs in (dev, holdout)
        ]
        self.assertFalse(pairs[0] & pairs[1])
        # 直接从源文取误断两侧的句首词，防止只换案例或模板名便复用开发集句架。
        openings = [
            {
                tuple(part.split()[0].casefold() for part in s.source.rstrip(".").split(". "))
                for d in docs
                for s in d.segments
                if s.category == "clause_boundary"
            }
            for docs in (dev, holdout)
        ]
        self.assertFalse(openings[0] & openings[1])

    def test_expected_terms_survive_production_parse_and_normalization(self) -> None:
        for doc in self.docs:
            with self.subTest(doc=doc.doc_id):
                body = [s.source for s in doc.segments if s.role not in ("title", "reference")]
                concept = next(s.source for s in doc.segments if s.scored and s.category == "concept")
                distinct = [p for p in doc.term_pairs if p.expect_in_glossary and p.relation == "distinct"]
                self.assertTrue(distinct, "每篇至少有一对须入表的相近概念")
                expected = {t for p in doc.term_pairs if p.expect_in_glossary for t in (p.source_a, p.source_b)}
                # parse 接收模型答复而非文章；用固定中文目标隔离抽取的随机性，正文只来自本篇。
                reply = "\n".join(f"{t}={doc.user_terms.get(t, '测试译名')}" for t in sorted(expected))
                parsed = parse(reply)
                retained = normalize_terms(parsed.terms, body, doc.headings)
                self.assertCountEqual(map(normalize_term_key, retained), map(normalize_term_key, expected))
                in_concept = {normalize_term_key(t) for t in normalize_terms(parsed.terms, [concept])}
                for pair in distinct:
                    keys = {normalize_term_key(pair.source_a), normalize_term_key(pair.source_b)}
                    self.assertEqual(len(keys), 2)
                    self.assertTrue(keys <= in_concept, pair)
                for pair in doc.term_pairs:
                    if pair.relation == "user_same":
                        self.assertEqual(doc.user_terms[pair.source_a], doc.user_terms[pair.source_b])
                        for seg in doc.segments:
                            if seg.reference_zh and pair.source_a.casefold() in seg.source.casefold():
                                self.assertIn(doc.user_terms[pair.source_a], seg.reference_zh)
                    elif pair.relation == "acronym":
                        definitions = extract_acronym_definitions(body)
                        self.assertIn(
                            frozenset((pair.source_a.lower(), pair.source_b.lower())),
                            {frozenset((abbr.lower(), name.lower())) for abbr, name in definitions.items()},
                        )

    def test_formula_bindings_and_reference_literals(self) -> None:
        for doc in self.docs:
            for seg in doc.segments:
                with self.subTest(doc=doc.doc_id, segment=seg.segment_id):
                    keys = FORMULA.findall(seg.source)
                    self.assertEqual(set(keys), set(seg.formulas))
                    self.assertTrue(all(tex.strip() for tex in seg.formulas.values()))
                    if seg.category == "formula_relation":
                        self.assertTrue(keys)
                    for tag in re.findall(r"<[^<>]*>", seg.source):
                        self.assertTrue(RICH_TAG.fullmatch(tag) or FORMULA.fullmatch(tag), tag)
                    if not doc.has_reference:
                        self.assertIsNone(seg.reference_zh)
                    elif seg.scored:
                        ref = seg.reference_zh or ""
                        self.assertRegex(ref, HAN)
                        for tag in re.findall(r"<[^<>]*>", ref):
                            self.assertTrue(RICH_TAG.fullmatch(tag) or FORMULA.fullmatch(tag), tag)
                        self.assertCountEqual(keys, FORMULA.findall(ref))
                        self.assertEqual(CITATION.findall(seg.source), CITATION.findall(ref))
                        self.assertEqual(RICH_TAG.findall(seg.source), RICH_TAG.findall(ref))
                        self.assertCountEqual(
                            NUMBER.findall(FORMULA.sub("", seg.source)), NUMBER.findall(FORMULA.sub("", ref))
                        )

    def test_holdout_controls_and_relation_coverage(self) -> None:
        docs = self.splits["holdout"]
        controls = [s for d in docs for s in d.segments if s.scored and s.category == "control"]
        sources = "\n".join(s.source for s in controls)
        for literal in ("priority", "Transformer (big)", "[12, 15]", "%", "<i>", "<b>"):
            self.assertIn(literal, sources)
        self.assertEqual(
            {p.relation for d in docs for p in d.term_pairs}, {"distinct", "synonym", "acronym", "user_same"}
        )
        self.assertTrue(
            any(v.rstrip().endswith((r"\leq", r"\geq", "=")) for s in controls for v in s.formulas.values())
        )
        anchors = {a.anchor_id for d in docs for a in d.anchors if a.protected}
        for kind in ("bound", "negation", "assumption", "proportion", "variables", "equality", "distribution"):
            self.assertIn(f"formula_relation-{kind}", anchors)
        for kind in ("conditional", "cross", "ambiguous"):
            self.assertIn(f"control-{kind}", anchors)
        for doc in docs:
            for a in doc.anchors:
                if a.anchor_id == "control-cross":
                    current = next(s for s in doc.segments if s.segment_id == a.segment_id)
                    self.assertGreater(current.order, 0)
                    self.assertFalse(doc.segments[current.order - 1].scored)
                    self.assertTrue(current.scored)

    def test_git_includes_fixture_paths(self) -> None:
        paths = [str((FIXTURES / f"{split}.jsonl").relative_to(ROOT)) for split in self.splits]
        # --no-index 也能发现文件提交后新增的误忽略规则。
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--", *paths],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_privacy_and_hygiene_in_all_fields(self) -> None:
        # 三个经过审阅的通用缩写均须在本篇正文给出全称（见入表测试）。
        allowed = {"PTE", "RTT", "LCM"}
        for doc in self.docs:
            with self.subTest(doc=doc.doc_id):
                for value in _strings(doc.model_dump()):
                    self.assertFalse(contains_emoji(value) or EXTRA_EMOJI.search(value))
                    value = unicodedata.normalize("NFKC", value)
                    self.assertNotRegex(value, FORBIDDEN)
                    self.assertNotRegex(value, LOCAL_PATH)
                    self.assertNotRegex(value, r"\b[A-Za-z]*[a-z][A-Z][A-Za-z0-9]*\b")
                    self.assertFalse(set(re.findall(r"(?<![A-Za-z0-9])[A-Z]{2,}(?![a-z])", value)) - allowed)


if __name__ == "__main__":
    unittest.main()

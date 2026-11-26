"""TR-012 的生产金样、候选补丁及隔离运行时守卫。"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from spikes.batch_d.contract import (
    BASELINE_CACHE_GOLD_SHA256,
    BASELINE_EXTRACT_PROMPT_SHA256,
    BASELINE_RULES_SHA256,
    BASELINE_SHA,
    BASELINE_VALIDATION_VERSION,
    TR012_EXACT_CHAR_DELTA,
    TR012_MAX_CHAR_DELTA,
    sha256_text,
)

import pdftr
from pdftr.domain.ir import Segment
from pdftr.glossary import extract
from pdftr.translation import prompt, service
from pdftr.translation.batching import VALIDATION_VERSION

_REPO = Path(__file__).resolve().parents[3]
_TARGET = "backend/src/pdftr/translation/prompt.py"
_PATCH = Path(__file__).resolve().parents[1] / "tr012.patch"
_BINDING = "即使式内已有相应记号，也须译出式外明示的分布或关系及其谓语，保留其对象和原文的确定程度。"
_CLAUSE = "同一编号内，让步或条件从句被误断且主句归属明确时，可调整标点接回原主句。归属不明时保留原句界。"


def _assert_imports_from(src: Path) -> None:
    for module in (pdftr, extract, prompt, service):
        assert module.__file__ and Path(module.__file__).resolve().is_relative_to(src), module.__file__


def _gold_cache_key(*, validation_version: int = VALIDATION_VERSION) -> str:
    # 金样输入含 order，当前生产 Segment 会在验证时忽略这个额外字段。
    seg = Segment.model_validate(
        {"id": "s0", "job_id": "j0", "order": 0, "source": "Inspect the sample.", "block_ids": ["p0-b0"]}
    )
    return service.cache_key(seg, "synthetic-model", {}, "default", validation_version=validation_version)


def _check_candidate_rules(baseline: str) -> None:
    assert sha256_text(baseline) == BASELINE_RULES_SHA256
    lines = baseline.split("\n")
    for prefix, addition in (("- 调整语序时，", _BINDING), ("- 意译优先：", _CLAUSE)):
        (index,) = (i for i, line in enumerate(lines) if line.startswith(prefix))
        assert lines[index].endswith("。")
        lines[index] += addition
    assert "\n".join(lines) == prompt.RULES
    delta = len(prompt.RULES) - len(baseline)
    assert delta == TR012_EXACT_CHAR_DELTA == 91
    assert delta <= TR012_MAX_CHAR_DELTA == 120


def _check_candidate_behavior() -> None:
    ctx = prompt.DocContext(domain="合成测量", terms={"marker": "标记"})
    assert prompt.system_prompt(ctx) == prompt.RULES + "\n\n领域：合成测量\n\n本文术语表（全文统一使用）：\nmarker=标记"

    source = "The reading <m1/> at the marker <m2/> may follow a uniform distribution."
    formulas = {"m1": r"u\sim U", "m2": "q"}
    expanded = prompt.expand_formulas(source, formulas)
    assert expanded == r"The reading <m1>u\sim U</m1> at the marker <m2>q</m2> may follow a uniform distribution."
    # 对照段没有声明分布；即使式内出现分布记号，其正文也逐字保留。
    control = "Record the mark <m1/>."
    control_formulas = {"m1": r"z\sim Q"}
    clause = "Although the tray tilts. The marker stays level."
    fragment = "If the lamp dims."
    next_segment = "The dial stays fixed."
    items = [
        (1, expanded),
        (2, prompt.expand_formulas(control, control_formulas)),
        (3, clause),
        (4, fragment),
        (5, next_segment),
    ]
    meta = [prompt.SegmentPromptMeta(5, prev_context=fragment)]
    assert prompt.user_prompt(items, [], {}, scoped_meta=meta).splitlines() == [
        f"第5段前文（仅供理解指代，勿翻译）：{fragment}",
        f"1|{expanded}",
        r"2|Record the mark <m1>z\sim Q</m1>.",
        f"3|{clause}",
        f"4|{fragment}",
        f"5|{next_segment}",
    ]

    problems = service._target_problems
    reordered = "标记<m2/>处的读数<m1/>可能服从均匀分布。"
    assert problems(source, reordered, formulas=formulas) == []
    missing = problems(source, reordered.replace("<m2/>", ""), formulas=formulas)
    assert any("公式占位符" in error for error in missing), missing
    assert problems(control, "记录标记<m1/>。", formulas=control_formulas) == []
    assert problems(clause, "尽管托盘倾斜，标记仍保持水平。") == []

    comparison = "Check whether <m1/> the limit at the marker."
    comparator = {"m1": r"x\leq"}
    assert problems(comparison, "检查<m1/>标记处的限值是否成立。", formulas=comparator) == []
    split = problems(comparison, "检查<m1/>在标记处的限值是否成立。", formulas=comparator)
    assert any("比较右值" in error for error in split), split

    previous = "The spare indicator on the upper shelf changed during the trial."
    assert problems(next_segment, "刻度盘保持固定。", prev_context=previous) == []
    leaked = problems(next_segment, previous + " 刻度盘保持固定。", prev_context=previous)
    assert any("参考上文" in error for error in leaked), leaked


def _check_candidate(src: Path, baseline: str) -> None:
    _assert_imports_from(src)
    assert (prompt.__doc__ or "").startswith("翻译提示词 v8")
    assert prompt.PROMPT_VERSION == service.PROMPT_VERSION == "v8"
    assert VALIDATION_VERSION == service.VALIDATION_VERSION == BASELINE_VALIDATION_VERSION
    assert _gold_cache_key() != BASELINE_CACHE_GOLD_SHA256
    _check_candidate_rules(baseline)
    _check_candidate_behavior()


class PromptTest(unittest.TestCase):
    def test_production_gold(self) -> None:
        _assert_imports_from(_REPO / "backend/src")
        self.assertEqual(prompt.PROMPT_VERSION, "v7")
        self.assertEqual(VALIDATION_VERSION, 8)
        self.assertEqual(sha256_text(extract.PROMPT), BASELINE_EXTRACT_PROMPT_SHA256)
        self.assertEqual(sha256_text(prompt.RULES), BASELINE_RULES_SHA256)
        self.assertEqual(_gold_cache_key(validation_version=BASELINE_VALIDATION_VERSION), BASELINE_CACHE_GOLD_SHA256)
        self.assertNotEqual(_gold_cache_key(), BASELINE_CACHE_GOLD_SHA256)

    def test_patch_scope_and_lf(self) -> None:
        patch = _PATCH.read_bytes()
        self.assertNotIn(b"\r", patch)
        self.assertTrue(patch.endswith(b"\n"))
        stat = subprocess.check_output(["git", "apply", "--numstat", "-z", _PATCH], cwd=_REPO, timeout=30)
        self.assertEqual(stat, f"4\t4\t{_TARGET}\0".encode())

    def test_candidate_in_archived_tree(self) -> None:
        archive = subprocess.check_output(
            ["git", "-c", "core.autocrlf=false", "archive", "--format=tar", BASELINE_SHA, "backend/src/pdftr"],
            cwd=_REPO,
            timeout=30,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp).resolve()
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(tree, filter="data")
            subprocess.run(["git", "apply", "--check", "--whitespace=error", _PATCH], cwd=tree, check=True, timeout=30)
            subprocess.run(["git", "apply", "--whitespace=error", _PATCH], cwd=tree, check=True, timeout=30)
            # 先选择临时源码，再导入本测试。独立进程也会重新绑定 service 中按名导入的版本号。
            bootstrap = (
                "import sys\n"
                "from pathlib import Path\n"
                "sys.path[:0] = sys.argv[1:3]\n"
                "from spikes.batch_d.tests.test_prompt import _check_candidate\n"
                "_check_candidate(Path(sys.argv[1]), sys.stdin.read())"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", "-c", bootstrap, tree / "backend/src", _REPO],
                cwd=tree,
                input=prompt.RULES,
                encoding="utf-8",
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

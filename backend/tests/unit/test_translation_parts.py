from datetime import datetime, timedelta, timezone

from pdftr.domain.llm import ChatUsage
from pdftr.pipeline.usecases import EXPORT_CACHE_VERSION
from pdftr.rendering.typeset import normalize_cjk_spacing as normalize_export_spacing
from pdftr.translation.batching import (
    bold_problems,
    estimate_tokens,
    italic_problems,
    make_batches,
    normalize_cjk_spacing,
    parse_numbered,
    placeholders_match,
)
from pdftr.translation.cost import cost_cny, is_deepseek_peak
from pdftr.translation.prompt import PROMPT_VERSION, DocContext, expand_formulas, system_prompt, user_prompt

BJ = timezone(timedelta(hours=8))


def test_estimate_tokens():
    assert estimate_tokens("a" * 400) == 101
    assert estimate_tokens("中文十个字测试一下哦") == 11


def test_make_batches_respects_budget():
    assert make_batches(list("abcde"), [600, 600, 600, 2000, 10], limit=1500) == [["a", "b"], ["c"], ["d"], ["e"]]


def test_parse_numbered():
    text = "```\n1|第一段\n2｜第二段\n接着第二段\n\n3 | 第三段\n```"
    assert parse_numbered("1|中文 Latin\ncontinuation 2\n3") == {1: "中文 Latin continuation 2 3"}
    assert parse_numbered(text) == {1: "第一段", 2: "第二段接着第二段", 3: "第三段"}


def test_placeholders_match():
    assert placeholders_match("a <m1/> b <m2/>", "乙<m2/>甲<m1/>")
    assert not placeholders_match("a <m1/> b <m2/>", "甲<m1/>")
    assert not placeholders_match("a <m1/>", "甲<m1/><m1/>")


def test_bold_structure_rejects_added_emphasis_and_checks_source_emphasis():
    source = "An <b>important</b> detail."
    assert bold_problems(source, "一个<b>重要</b>细节。") == []
    assert bold_problems(source, "一个<b><sup>重要</sup></b>细节。") == []
    assert "丢失" in bold_problems(source, "一个重要细节。")[0]
    assert "配对" in bold_problems(source, "一个<b>重要细节。")[0]
    assert "多出" in bold_problems(source, "一个<b>重要</b><b>细节</b>。")[0]
    assert "嵌套" in bold_problems(source, "一个</b>重要<b>细节。")[0]
    assert "嵌套" in bold_problems(source, "一个<b><sup>重要</b></sup>细节。")[0]
    assert "没有内容" in bold_problems(source, "<b> </b>一个重要细节。")[0]
    assert "新增粗体" in bold_problems("No emphasis.", "保持原有<b>自然格式和</b>行为。")[0]
    assert "未完整配对" in bold_problems("No emphasis.", "保持原有<b>自然格式和行为。")[0]
    assert bold_problems(source, "<sup>不新增上下标校验</sub>一个<b>重要</b>细节。") == []


def test_italic_structure_rejects_loss_and_added_emphasis():
    source = "An <i>important</i> detail."
    assert italic_problems(source, "一个<i>重要</i>细节。") == []
    assert "丢失" in italic_problems(source, "一个重要细节。")[0]
    assert "配对" in italic_problems(source, "一个<i>重要细节。")[0]
    assert "新增斜体" in italic_problems("No emphasis.", "保持原有<i>自然格式和</i>行为。")[0]
    assert "没有内容" in italic_problems(source, "<i> </i>一个重要细节。")[0]
    assert "嵌套" in italic_problems(source, "一个<i><b>重要</i></b>细节。")[0]


def test_prompts():
    assert expand_formulas("Let <m1/> be", {"m1": r"\alpha"}) == r"Let <m1>\alpha</m1> be"
    sys_p = system_prompt(DocContext(title="T", domain="NLP", abstract="A", terms={"encoder": "编码器"}))
    assert "论文标题：T" in sys_p and "encoder=编码器" in sys_p and "<m1>O(1)</m1>" in sys_p
    user = user_prompt([(1, "x"), (2, "y")], ["3 Model"], {"attention": "注意力"})
    assert PROMPT_VERSION == "v3" and EXPORT_CACHE_VERSION == 7
    assert "本文术语表（全文统一使用）" in sys_p
    assert "Problem statement" in sys_p and "Available online" in sys_p and "「等」" in sys_p
    assert user.splitlines() == [
        "本批段落所在章节：3 Model",
        "参考词库（仅在语境与论文领域吻合时参考，冲突时以自然学术译法为准）：attention=注意力",
        "1|x",
        "2|y",
    ]


def test_spacing_is_identical_in_translation_and_export():
    text = "用 <b>ResNet 50</b> 验证 2 层 <m1/> 、中文  多空格\n保持 English words (Lee et al., 2020)。"
    expected = "用<b>ResNet 50</b>验证2层<m1/> 、中文  多空格\n保持English words (Lee et al., 2020)。"
    assert normalize_cjk_spacing(text) == normalize_export_spacing(text) == expected
    assert normalize_cjk_spacing(expected) == expected
    assert normalize_cjk_spacing("中文\t<m1/>\u00a0段落  X") == "中文<m1/>段落  X"


def test_cost_and_peak():
    usage = ChatUsage(prompt_tokens=8000, cache_hit_tokens=1000, completion_tokens=6000)
    # 空闲：1000×0.02 + 7000×1 + 6000×4 = 31020 / 1e6
    assert cost_cny("deepseek-flash", usage, peak=False) == 0.03102
    assert cost_cny("deepseek-flash", usage, peak=True) == 0.06204
    assert cost_cny("unknown-model", usage, peak=False) is None
    assert is_deepseek_peak(datetime(2026, 10, 28, 10, 0, tzinfo=BJ))  # 周三上午
    assert not is_deepseek_peak(datetime(2026, 10, 28, 12, 30, tzinfo=BJ))  # 午休
    assert not is_deepseek_peak(datetime(2026, 10, 31, 10, 0, tzinfo=BJ))  # 周六


def test_placeholder_problems_explain_what_is_wrong():
    from pdftr.translation.batching import placeholder_problems

    src = "Let <m1/> and <m2/> be given."
    assert placeholder_problems(src, "设 <m1/> 与 <m2/> 已知。") == []
    assert placeholder_problems(src, "设 <m1/> 已知。") == ["少了公式 2（<m2/>）"]
    assert placeholder_problems(src, "设 <m1/><m1/> 与 <m2/>。") == ["公式 1（<m1/>）出现了 2 次"]
    assert placeholder_problems(src, "设 <m1/> 与 <m2/>、<m3/>。") == ["多了不存在的公式占位 <m3/>"]

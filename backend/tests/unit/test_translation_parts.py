from datetime import datetime, timedelta, timezone

from pdftr.domain.llm import ChatUsage
from pdftr.translation.batching import estimate_tokens, make_batches, parse_numbered, placeholders_match
from pdftr.translation.cost import cost_cny, is_deepseek_peak
from pdftr.translation.prompt import DocContext, expand_formulas, system_prompt, user_prompt

BJ = timezone(timedelta(hours=8))


def test_estimate_tokens():
    assert estimate_tokens("a" * 400) == 101
    assert estimate_tokens("中文十个字测试一下哦") == 11


def test_make_batches_respects_budget():
    assert make_batches(list("abcde"), [600, 600, 600, 2000, 10], limit=1500) == [["a", "b"], ["c"], ["d"], ["e"]]


def test_parse_numbered():
    text = "```\n1|第一段\n2｜第二段\n接着第二段\n\n3 | 第三段\n```"
    assert parse_numbered(text) == {1: "第一段", 2: "第二段接着第二段", 3: "第三段"}


def test_placeholders_match():
    assert placeholders_match("a <m1/> b <m2/>", "乙<m2/>甲<m1/>")
    assert not placeholders_match("a <m1/> b <m2/>", "甲<m1/>")
    assert not placeholders_match("a <m1/>", "甲<m1/><m1/>")


def test_prompts():
    assert expand_formulas("Let <m1/> be", {"m1": r"\alpha"}) == r"Let <m1>\alpha</m1> be"
    sys_p = system_prompt(DocContext(title="T", domain="NLP", abstract="A", terms={"encoder": "编码器"}))
    assert "论文标题：T" in sys_p and "encoder=编码器" in sys_p and "<m1>O(1)</m1>" in sys_p
    user = user_prompt([(1, "x"), (2, "y")], ["3 Model"], {"attention": "注意力"})
    assert user.splitlines() == ["本批段落所在章节：3 Model", "本批术语（推荐译法）：attention=注意力", "1|x", "2|y"]


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

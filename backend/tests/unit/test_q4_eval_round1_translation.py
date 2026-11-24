"""Q4 Round 1 共性缺陷修复回归测试（引文校验、协议守卫、Prompt v6、高公式升档、编号空格、术语抽取与仲裁）。"""

import asyncio
import re

import pytest

from pdftr.domain.ir import Segment, SegmentStatus
from pdftr.domain.llm import ChatOptions, ChatResult, ChatUsage
from pdftr.glossary.extract import PROMPT as EXTRACT_PROMPT
from pdftr.glossary.extract import Extraction, frequent_candidates
from pdftr.glossary.extract import parse as parse_terms
from pdftr.glossary.matcher import TermSelection
from pdftr.rendering.typeset import normalize_cjk_spacing as normalize_export_spacing
from pdftr.translation.batching import (
    VALIDATION_VERSION,
    citation_problems,
    normalize_cjk_spacing,
    parse_numbered,
    prose_problems,
)
from pdftr.translation.prompt import PROMPT_VERSION, RULES, DocContext
from pdftr.translation.service import TranslateOptions, _target_problems, translate

# ---------- P0-1: 引文校验器假阳性与补盲 ----------


def test_natural_language_parenthesized_citations_are_symmetric_while_call_indices_stay_excluded():
    assert (
        citation_problems("Linux (Seccomp Notify)[18] and (EHRs)[1,2].", "Linux（Seccomp Notify）[18]与（EHRs）[1,2]。")
        == []
    )
    assert citation_problems("Linux (Seccomp Notify)[18].", "Linux(Seccomp Notify)[18]。") == []
    assert citation_problems("Linux (Seccomp Notify)[18].", "Linux（Seccomp Notify）[19]。")
    assert citation_problems("Electronic Health Records (EHRs)[1,2].", "电子健康记录（EHRs）[1]。")

    # 真正函数调用下标（左括号紧贴 ASCII 标识符）依然按索引排除。
    assert (
        citation_problems("Compute foo(x)[0] and arr.get(i)[1] and foo(bar(x))[2]. See [18].", "计算后见[18]。") == []
    )
    assert citation_problems("Compute foo(x)[0]. See [18].", "计算 foo(x)[0]，见[18]。") == []


def test_source_excluded_numbers_do_not_cause_false_positives_and_dual_role_enforces_range():
    # 01 s48 类场景：源文 excluded 区间在译文保留区间表述时不误报多出引文。
    src = "We follow [12] and sample in [256<i>,</i> 480]."
    assert citation_problems(src, "我们遵循[12]，并在[256, 480]内随机采样。") == []
    assert citation_problems(src, "我们遵循[12]，从[256, 480]取值。") == []
    assert citation_problems(src, "我们遵循[12]，并在[256, 480]内随机采样[99]。")

    # 同号既是真引文又是数组索引：合法次数为 [want, want + excluded] = [1, 2]。
    dual = "Given [21] and arr[21]."
    assert citation_problems(dual, "根据文献与arr[21]。")
    assert citation_problems(dual, "根据[21]和arr[21]。") == []
    assert citation_problems(dual, "根据[21]和元素[21]。") == []
    assert citation_problems(dual, "根据[21]、[21]和元素[21]。")


def test_single_positive_citations_after_vector_or_matrix_nouns_are_not_misclassified():
    assert (
        citation_problems(
            "We encode residual vectors [17] and Fisher Vector [30].", "我们编码残差向量[17]和Fisher向量[30]。"
        )
        == []
    )
    assert citation_problems("Fisher Vector [30].", "Fisher向量[31]。")
    assert citation_problems("encoding residual vectors [17].", "编码残差向量。")
    # 译文前置中文“向量”且多值引文已在源文确认时，不被中文前置词误排。
    assert citation_problems("The residual representations [17, 18].", "残差向量[17, 18]。") == []
    # 多值数据字面量仍按数据上下文排除。
    assert (
        citation_problems("Use vectors [1, 2] and matrices [3, 4]. See [7].", "使用向量[1, 2]与矩阵[3, 4]。见[7]。")
        == []
    )


def test_superscript_numeric_range_citations_and_mixed_locator_citations():
    # 上标数字与范围引文：等值通过、改号或丢弃报错。
    assert citation_problems("Previous studies.<sup>2,22–25</sup>", "先前研究。<sup>2,22–25</sup>") == []
    assert citation_problems("Previous studies.<sup>2,22–25</sup>", "先前研究。<sup>2，22-25</sup>") == []
    assert citation_problems("Previous studies.<sup>2,22–25</sup>", "先前研究。<sup>2,22–26</sup>")
    assert citation_problems("Recent methods<sup>4–10</sup> improve results.", "近期方法<sup>4–9</sup>改进了结果。")
    assert citation_problems("Reported in patients<sup>14</sup>.", "在患者中已有报道。")
    assert citation_problems("Reported in patients<sup>14</sup>.", "在患者中已有报道<sup>14</sup>。") == []

    # 科学幂次、单位、电荷与序数词不误判为上标引文。
    assert (
        citation_problems(
            "10<sup>6</sup> m<sup>2</sup> n<sup>2</sup> Ca<sup>2+</sup> x<sup>-3</sup> 1<sup>st</sup>.", "结果。"
        )
        == []
    )
    assert (
        citation_problems(
            "Area 5 m<sup>2</sup> in patients<sup>14</sup>.", "面积5米<sup>2</sup>，见患者<sup>14</sup>。"
        )
        == []
    )

    # 混合定位引文：提取主引文编号保护，防整块删除或改号。
    assert (
        citation_problems(
            "By [17, Prop. 1.2.12] and [12, Theorem 3] and [5, Eq. (2)].",
            "由[17, 命题 1.2.12]、[12, 定理 3]及[5, 式(2)]。",
        )
        == []
    )
    assert citation_problems("By [17, Prop. 1.2.12].", "由文献[17]的命题1.2.12。") == []
    assert citation_problems("By [17, Prop. 1.2.12].", "由命题1.2.12。")
    assert citation_problems("By [12, Theorem 3].", "由[13, 定理 3]。")
    assert citation_problems("By [5, Eq. (2)].", "由[6, 公式(2)]。")


# ---------- P0-2: 协议闭合标签泄漏与守卫 ----------


def test_parse_numbered_strips_inline_closing_and_leading_protocol_tags():
    assert parse_numbered("1|第一段\n2|第二段</translations>\n3|多余尾注", batch_size=2) == {1: "第一段", 2: "第二段"}
    assert parse_numbered("<translations>1|单行</translations>\n2|忽略", batch_size=2) == {1: "单行"}
    assert parse_numbered("<translations>\n1|甲\n续行</translations>\n2|尾注", batch_size=2) == {1: "甲续行"}
    assert parse_numbered("1|甲\n2|乙</TRANSLATIONS> 额外说明\n1|重复", batch_size=2) == {1: "甲", 2: "乙"}


def test_target_problems_rejects_protocol_and_malformed_formula_tags():
    assert VALIDATION_VERSION == 4
    assert any("协议标签" in p for p in _target_problems("A sentence.", "一句译文。</translations>"))
    assert any("协议标签" in p for p in _target_problems("A sentence.", "<translations>一句译文。"))
    assert any("公式标签" in p for p in _target_problems("Let <m1/> be x.", "设<m1>x</m1>为输入。"))
    assert any("公式标签" in p for p in _target_problems("Let <m1/> be x.", "设<m1/>为输入</m1>。"))
    # 若源文本身字面包含该标签，则不误拦。
    assert _target_problems("Literal <translations> tag.", "字面 <translations> 标签。") == []


# ---------- P0-3 & P1-2: Prompt v6、高公式密度自动思考升档与低误报漏译词 ----------


def test_prompt_v6_rules_cover_formula_scope_and_segment_boundary():
    assert PROMPT_VERSION == "v6"
    assert "每条编号仅翻译该编号自身包含的文字" in RULES
    assert "对任意<m3/>，<m1/>当且仅当<m2/>" in RULES
    assert "<m1/>在<m3/>上与<m2/>重合" in RULES
    assert "Reject ... if" in RULES


def test_high_formula_density_segments_auto_escalate_thinking_without_dragging_plain_segments():
    class RecordingChat:
        model = "deepseek-flash"

        def __init__(self, *, fail_high_once: bool = False) -> None:
            self.calls: list[tuple[str, ChatOptions]] = []
            self.fail_high_once = fail_high_once

        async def list_models(self) -> list[str]:
            return [self.model]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            self.calls.append((user, opts))
            out = []
            for line in user.splitlines():
                m = re.match(r"^(\d+)\|(.*)$", line)
                if not m:
                    continue
                text = re.sub(r"<(m\d+)>.*?</\1>", r"<\1/>", m.group(2))
                if self.fail_high_once and "<m4/>" in text:
                    self.fail_high_once = False
                    text = text.replace("<m4/>", "")
                out.append(f"{m.group(1)}|译:{text}")
            return ChatResult(text="\n".join(out), usage=ChatUsage(prompt_tokens=10, completion_tokens=10))

    class MemCache:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        def get(self, key: str) -> str | None:
            return self.data.get(key)

        def put(self, key: str, target: str) -> None:
            self.data[key] = target

        def purge_older_than(self, hours: int) -> int:
            return 0

    plain1 = Segment(id="s1", job_id="j", block_ids=["p0-b1"], source="Plain first sentence.", section="2 Method")
    dense = Segment(
        id="s2",
        job_id="j",
        block_ids=["p0-b2"],
        source="For <m1/> and <m2/>, we have <m3/> with <m4/>.",
        formulas={"m1": "x", "m2": "y", "m3": "z", "m4": "w"},
        section="2 Method",
    )
    plain2 = Segment(id="s3", job_id="j", block_ids=["p0-b3"], source="Plain second sentence.", section="2 Method")

    chat = RecordingChat(fail_high_once=True)
    out, report = asyncio.run(
        translate(
            [plain1, dense, plain2], chat=chat, cache=MemCache(), ctx=DocContext(), opts=TranslateOptions(concurrency=1)
        )
    )
    assert all(s.status is SegmentStatus.DONE for s in out)
    assert report.requests == 3
    plain_calls = [opts for user, opts in chat.calls if "Plain first sentence." in user]
    dense_calls = [opts for user, opts in chat.calls if "<m4>w</m4>" in user]
    assert len(plain_calls) == 1 and plain_calls[0].thinking == "off"
    # 首轮及第 2 轮重试均保持 thinking="low"
    assert len(dense_calls) == 2 and all(o.thinking == "low" for o in dense_calls)


def test_leaked_common_english_word_check_has_zero_false_positives_on_valid_terms():
    assert prose_problems("Previous networks degrade with depth.", "以往的networks随深度增加而退化。")
    assert prose_problems("These layers are hard to optimize.", "这些 layers 很难优化。")
    assert prose_problems("Our design improves existing methods.", "我们的设计改进了以往的methods。")

    # 合法保留英文专名、系统标识符、代码/术语名（如 function/socket）、缩写、括号原名注释与反引号代码均 0 误报。
    allowed = [
        ("Let f(x) be a function that continues.", "设 <m1/> 为 function，跨栏续接。"),
        ("We use TCP/IP and eBPF with Seccomp Notify.", "我们使用TCP/IP和eBPF结合Seccomp Notify机制。"),
        ("Container processes avoid root privileges via getpeername.", "容器进程通过getpeername避免获取root权限。"),
        ("Terms follow SNOMED CT and the Text field.", "术语遵循SNOMED CT标准以及Text字段。"),
        ("We evaluate ResNet with BatchNorm layers.", "我们评估带BatchNorm层的ResNet模型。"),
        ("Previous networks (networks) use `layers`.", "以往的网络（networks）使用`layers`属性。"),
        ("We compare neural networks in practice.", "我们在实践中比较neural networks的表现。"),
    ]
    for src, dst in allowed:
        assert (
            prose_problems(src, dst, translated_terms={"root": "root", "function": "函数", "utility": "效用函数"}) == []
        )


# ---------- P2-1: normalize_cjk_spacing 保留行首编号空格 ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 引言", "1 引言"),
        ("2.1 无根容器与 Docker 隔离", "2.1 无根容器与Docker隔离"),
        ("3.2.1 方法", "3.2.1 方法"),
        ("A 附录", "A 附录"),
        ("A.1 证明", "A.1 证明"),
        ("图1 不同TCM 的性能", "图1 不同TCM的性能"),
        ("图 1 不同TCM 的性能", "图1 不同TCM的性能"),
        ("表2 性能对比", "表2 性能对比"),
        ("<b>表 2.1</b> 性能对比", "<b>表2.1</b> 性能对比"),
        ("图A.1 补充结果", "图A.1 补充结果"),
        ("算法1 迭代过程\n定理2 收敛界", "算法1 迭代过程\n定理2 收敛界"),
        # 非编号场景仍正常收紧空格
        ("X 光检查与 T 检验", "X光检查与T检验"),
        ("A 模型优于 B 模型", "A模型优于B模型"),
        ("2020 年的研究验证 2 层网络", "2020年的研究验证2层网络"),
        ("0.5 毫米精度", "0.5毫米精度"),
    ],
)
def test_normalize_cjk_spacing_preserves_leading_numbering_in_both_modules(raw, expected):
    assert normalize_cjk_spacing(raw) == normalize_export_spacing(raw) == expected
    assert normalize_cjk_spacing(expected) == expected


# ---------- P1-3 & P2-2: 术语抽取上限、泛词过滤与用户术语复合词冲突消解 ----------


def test_glossary_parse_hard_caps_at_45_and_prompt_contains_domain_guidance():
    lines = ["领域：计算机视觉", "AI公共词库：启用"] + [f"term_{i}=术语{i}" for i in range(60)]
    found = parse_terms("\n".join(lines))
    assert len(found.terms) == 45
    assert "term_0" in found.terms and "term_44" in found.terms and "term_45" not in found.terms
    assert "root privileges=root 权限" in EXTRACT_PROMPT
    assert "Blood Pressure" in EXTRACT_PROMPT
    assert "Text信息片段" in EXTRACT_PROMPT


def test_frequent_candidates_suppresses_generic_unigrams_but_keeps_multiword_phrases():
    texts = [
        "residual network plain network convolutional layer training error model accuracy complexity. "
        "networks layers methods models problems optimization performance datasets."
    ] * 8
    candidates = frequent_candidates(texts)
    for generic in (
        "network",
        "networks",
        "layer",
        "layers",
        "model",
        "models",
        "training",
        "error",
        "accuracy",
        "plain",
    ):
        assert generic not in candidates
    assert "residual network" in candidates
    assert "convolutional layer" in candidates
    assert "training error" in candidates


def test_term_selection_drops_conflicting_compound_paper_terms_including_plurals():
    found = Extraction(
        terms={
            "utility": "效用",
            "power utility": "幂效用",
            "concavified utility": "凹化效用",
            "log utilities": "对数效用",
            "expected utility": "期望效用函数",
            "risk aversion": "风险厌恶",
        },
        use_library=False,
    )
    selection = TermSelection(found, {"utility": "效用函数"}, lambda: None)
    assert selection.paper == {
        "utility": "效用函数",
        "expected utility": "期望效用函数",
        "risk aversion": "风险厌恶",
    }
    user_hits, paper_hits, _ = selection.match_segment(
        "The power utility, log utilities, and expected utility show risk aversion."
    )
    assert user_hits == {"utility": "效用函数"}
    assert "power utility" not in paper_hits and "log utilities" not in paper_hits
    assert paper_hits == {"risk aversion": "风险厌恶"}

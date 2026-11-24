"""Q4 Round 6 终极机械收口回归测试（R6-1 ~ R6-6）。"""

import asyncio
import re
from pathlib import Path

import pymupdf

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import (
    Block,
    BlockStyle,
    BlockType,
    Document,
    Page,
    PageKind,
    Segment,
    SegmentPart,
    SegmentStatus,
    Span,
)
from pdftr.domain.llm import ChatOptions, ChatResult
from pdftr.extraction.outline import pdf_outline
from pdftr.glossary.extract import _skip_extracted_term, extract_terms
from pdftr.glossary.extract import parse as parse_terms
from pdftr.pipeline.usecases import EXPORT_CACHE_VERSION
from pdftr.rendering.background import make_background
from pdftr.rendering.export import export_pdf
from pdftr.rendering.links import VERSION as LINKS_VERSION
from pdftr.rendering.links import extract_links
from pdftr.translation.batching import VALIDATION_VERSION, percentage_problems
from pdftr.translation.prompt import PROMPT_VERSION
from pdftr.translation.service import (
    _HIGH_FORMULA_SOLO_THRESHOLD,
    _adjacent_formula_number_problems,
    _leaked_latex_command_problems,
    _split_high_formula_batches,
    _target_problems,
    _trailing_comparator_formula_problems,
    _Unit,
)

# ---- 版本与基线不变性 ----


def test_r6_versions_bumped_and_prompt_version_unchanged():
    assert VALIDATION_VERSION == 6
    assert LINKS_VERSION == 6
    assert EXPORT_CACHE_VERSION == 15
    assert PROMPT_VERSION == "v7"


# ---- R6-1: 命名内部跳转（LINK_NAMED）与直接跳转（LINK_GOTO）纵坐标一致性（含导出与书签） ----


def _make_pdf_with_named_and_goto_links_and_outline(pdf_path: Path) -> dict[str, float]:
    """构造含真实 /Names /Dests（页底与页顶命名目标）、直接 LINK_GOTO 以及混合书签的合成 PDF。"""
    expected_top = {
        "Hfootnote.1": 710.0,  # 页高 800，PDF 原生左下原点 y=90 → 距页顶 710
        "cite.topref": 70.0,  # 页高 800，PDF 原生左下原点 y=730 → 距页顶 70
        "direct": 260.0,  # 直接 LINK_GOTO（MuPDF 左上原点 y=260 → PDF /XYZ y=540）
    }
    with pymupdf.open() as pdf:
        pdf.new_page(width=600, height=800)
        pdf.new_page(width=600, height=800)
        p0, p1 = pdf[0], pdf[1]
        p0.insert_text((50, 90), "Method", fontsize=11)
        p0.insert_text((90, 86), "1", fontsize=7)
        p0.insert_text((105, 90), "follows [2] and [3].", fontsize=11)
        p1.insert_text((72, 70), "[2] Top Reference Entry.", fontsize=11)
        p1.insert_text((120, 260), "[3] Mid Direct Target.", fontsize=11)
        p1.insert_text((64.5, 710), "1 Bottom Footnote Text.", fontsize=10)

        p1_xref = pdf.page_xref(1)
        fn_rect = p0.search_for("1")[0]
        ref2_rect = p0.search_for("[2]")[0]
        ref3_rect = p0.search_for("[3]")[0]

        # 写入真实 /Names /Dests（PDF 原生左下原点坐标系）
        dests_xref = pdf.get_new_xref()
        pdf.update_object(
            dests_xref,
            f"<< /Names [ (Hfootnote.1) [ {p1_xref} 0 R /XYZ 64.5 90.0 0 ] "
            f"(cite.topref) [ {p1_xref} 0 R /XYZ 72.0 730.0 0 ] ] >>",
        )
        names_xref = pdf.get_new_xref()
        pdf.update_object(names_xref, f"<< /Dests {dests_xref} 0 R >>")
        catalog_xref = pdf.pdf_catalog()
        pdf.xref_set_key(catalog_xref, "Names", f"{names_xref} 0 R")

        # 第 0 页添加两条真实命名链接（LINK_NAMED）
        def add_named_link(rect: pymupdf.Rect, name: str) -> int:
            pdf_rect = rect * ~pymupdf.Matrix(p0.transformation_matrix)
            xref = pdf.get_new_xref()
            pdf.update_object(
                xref,
                f"<< /Type /Annot /Subtype /Link "
                f"/Rect [ {pdf_rect.x0:.3f} {pdf_rect.y0:.3f} {pdf_rect.x1:.3f} {pdf_rect.y1:.3f} ] "
                f"/Border [ 0 0 0 ] /A << /S /GoTo /D ({name}) >> >>",
            )
            return xref

        a1 = add_named_link(fn_rect, "Hfootnote.1")
        a2 = add_named_link(ref2_rect, "cite.topref")
        pdf.xref_set_key(pdf.page_xref(0), "Annots", f"[ {a1} 0 R {a2} 0 R ]")

        # 再插入一条直接 LINK_GOTO（MuPDF 左上原点 y=260）
        p0.insert_link(
            {
                "kind": pymupdf.LINK_GOTO,
                "from": ref3_rect,
                "page": 1,
                "to": pymupdf.Point(120.0, expected_top["direct"]),
            }
        )

        # 构造混合书签：1 条命名目的地书签（cite.topref -> top=70）+ 1 条直接 GoTo 书签（/XYZ 120 540 0 -> top=260）
        outlines_xref = pdf.get_new_xref()
        ol1_xref = pdf.get_new_xref()
        ol2_xref = pdf.get_new_xref()
        pdf.update_object(
            outlines_xref,
            f"<< /Type /Outlines /First {ol1_xref} 0 R /Last {ol2_xref} 0 R /Count 2 >>",
        )
        pdf.update_object(
            ol1_xref,
            f"<< /Title (Named Top) /Parent {outlines_xref} 0 R /Next {ol2_xref} 0 R "
            f"/A << /S /GoTo /D (cite.topref) >> >>",
        )
        pdf.update_object(
            ol2_xref,
            f"<< /Title (Direct Mid) /Parent {outlines_xref} 0 R /Prev {ol1_xref} 0 R "
            f"/A << /S /GoTo /D [ {p1_xref} 0 R /XYZ 120.0 540.0 0 ] >> >>",
        )
        pdf.xref_set_key(catalog_xref, "Outlines", f"{outlines_xref} 0 R")

        pdf.save(pdf_path)
    return expected_top


def _xyz_pdf_y(pdf: pymupdf.Document, link_xref: int) -> float:
    obj = pdf.xref_object(link_xref)
    match = re.search(r"/XYZ\s+[-0-9.]+\s+([-0-9.]+)\s+", obj)
    assert match is not None, f"missing /XYZ in {obj}"
    return float(match.group(1))


def test_r6_1_named_and_goto_links_and_outline_preserve_top_down_y_in_mono_and_dual(tmp_path: Path):
    orig_pdf = tmp_path / "named-links.pdf"
    bg_pdf = tmp_path / "named-bg.pdf"
    expected_top = _make_pdf_with_named_and_goto_links_and_outline(orig_pdf)

    # 先核验原稿中确实同时存在 LINK_NAMED 与 LINK_GOTO
    with pymupdf.open(orig_pdf) as src:
        raw_links = src[0].get_links()
        assert sorted(lk["kind"] for lk in raw_links) == [pymupdf.LINK_GOTO, pymupdf.LINK_NAMED, pymupdf.LINK_NAMED]

    # 1. 核验 outline.py 对混合命名书签与直接 GoTo 书签均返回精确页顶距离
    outline = pdf_outline(orig_pdf)
    assert [item.title for item in outline] == ["Named Top", "Direct Mid"]
    assert abs(outline[0].top - expected_top["cite.topref"]) < 0.5
    assert abs(outline[1].top - expected_top["direct"]) < 0.5

    # 2. 核验 extract_links 的 dest_top 与 dest_y 均统一为 MuPDF 左上原点坐标
    blk = Block(
        id="p0-b0",
        page=0,
        type=BlockType.TEXT,
        bbox=Rect(x0=40.0, y0=70.0, x1=320.0, y1=110.0),
        order=0,
        spans=[Span(text="Method<sup>1</sup> follows [2] and [3].", bbox=Rect(x0=40, y0=70, x1=320, y1=110), size=11)],
        style=BlockStyle(font_size=11.0),
        translatable=True,
    )
    doc = Document(
        job_id="r6-links",
        pages=[
            Page(index=0, width=600, height=800, kind=PageKind.NATIVE, blocks=[blk]),
            Page(index=1, width=600, height=800, kind=PageKind.NATIVE, blocks=[]),
        ],
    )
    seg = Segment(
        id="s0",
        job_id="r6-links",
        block_ids=["p0-b0"],
        source="Method<sup>1</sup> follows [2] and [3].",
        parts=[SegmentPart(block_id="p0-b0", chars=30)],
        target="该方法<sup>1</sup>遵循文献[2]与[3]。",
        status=SegmentStatus.DONE,
    )
    extracted = extract_links(orig_pdf, doc, {"p0-b0"})
    assert len(extracted) == 3
    by_x = {round(lk.dest_x, 1): lk for lk in extracted}
    assert set(by_x) == {64.5, 72.0, 120.0}
    for x_key, name in ((64.5, "Hfootnote.1"), (72.0, "cite.topref"), (120.0, "direct")):
        lk = by_x[x_key]
        assert lk.dest_page == 1
        assert abs(lk.dest_top - expected_top[name]) < 0.5
        assert abs(lk.dest_y - expected_top[name]) < 0.5

    # 3. 核验 mono 导出写出的 GoTo /XYZ 解析后纵坐标与原命名/直接目的地误差 < 0.5pt
    make_background(orig_pdf, [blk], bg_pdf)
    mono_bytes = export_pdf(orig_pdf, bg_pdf, doc, [seg], mode="mono", links=extracted)
    with pymupdf.open(stream=mono_bytes, filetype="pdf") as mono:
        jumps = [lk for lk in mono[0].get_links() if lk["kind"] == pymupdf.LINK_GOTO]
        assert len(jumps) == 3
        got_by_x = {round(lk["to"].x, 1): lk for lk in jumps}
        assert set(got_by_x) == {64.5, 72.0, 120.0}
        for x_key, name in ((64.5, "Hfootnote.1"), (72.0, "cite.topref"), (120.0, "direct")):
            lk = got_by_x[x_key]
            assert lk["page"] == 1
            assert abs(lk["to"].y - expected_top[name]) < 0.5
            # 同步验证 PDF 底层 /XYZ 写入的原生左下原点 y = 800 - top
            assert abs(_xyz_pdf_y(mono, lk["xref"]) - (800.0 - expected_top[name])) < 0.5

    # 4. 核验 dual 导出（原侧 + 译侧）所有 6 条跳转纵坐标一致且横坐标归属各自半页
    dual_bytes = export_pdf(orig_pdf, bg_pdf, doc, [seg], mode="dual", side="right", links=extracted)
    with pymupdf.open(stream=dual_bytes, filetype="pdf") as dual:
        jumps = [lk for lk in dual[0].get_links() if lk["kind"] == pymupdf.LINK_GOTO]
        left_jumps = [lk for lk in jumps if lk["from"].x0 < 600.0]
        right_jumps = [lk for lk in jumps if lk["from"].x0 >= 600.0]
        assert len(left_jumps) == 3 and len(right_jumps) == 3
        for half_jumps, dx in ((left_jumps, 0.0), (right_jumps, 600.0)):
            got_by_x = {round(lk["to"].x - dx, 1): lk for lk in half_jumps}
            assert set(got_by_x) == {64.5, 72.0, 120.0}
            for x_key, name in ((64.5, "Hfootnote.1"), (72.0, "cite.topref"), (120.0, "direct")):
                lk = got_by_x[x_key]
                assert lk["page"] == 1
                assert abs(lk["to"].y - expected_top[name]) < 0.5
                assert abs(_xyz_pdf_y(dual, lk["xref"]) - (800.0 - expected_top[name])) < 0.5


# ---- R6-2: 百分数事实锚点机械守卫 ----


def test_r6_2_percentage_guard_catches_hallucinated_and_missing_percentages():
    # 05_med_struct s63 真实源文（含 95% 与公式内 α=0.05），模型误写 90% 必须精准拦截
    s63_src = (
        "Fig.2a shows the results of the pair-wise comparison of TCMs with the Nemenyi test for <m1/> "
        "The value <m2/> in the Studentised range statistic table with ∞ degrees of freedom for this level "
        "of confidence is 4.387, yields a critical difference <m3/> "
        "For example, at this level of confidence (95%, see Fig. 2a) it can not be concluded that A outperforms B."
    )
    s63_formulas = {
        "m1": r"\mathtt { q } = 0 . 0 5",
        "m2": r"q _ { \alpha }",
        "m3": r"\mathtt { C D } = 0 . 7 5 4",
    }
    s63_bad_tgt = (
        "图2a展示了在<m1/>时的两两比较结果。在置信水平为90%、自由度为∞的学生化极差统计表中，"
        "<m2/>的值为4.387，得到临界差异<m3/>。例如，在该置信水平下（95%，见图2a），不能得出A优于B。"
    )
    problems = percentage_problems(s63_src, s63_bad_tgt, formulas=s63_formulas)
    assert problems == ["译文新增了原文不存在的百分数 90%，请严格忠实原文数值"]
    assert _target_problems(s63_src, s63_bad_tgt, formulas=s63_formulas) == problems

    # 忠实译文（仅含 95%）通过
    s63_good_tgt = (
        "图2a展示了在<m1/>时的两两比较结果。在该置信水平下、自由度为∞的学生化极差统计表中，"
        "<m2/>的值为4.387，得到临界差异<m3/>。例如，在该置信水平下（95%，见图2a），不能得出A优于B。"
    )
    assert percentage_problems(s63_src, s63_good_tgt, formulas=s63_formulas) == []
    assert _target_problems(s63_src, s63_good_tgt, formulas=s63_formulas) == []

    # 遗漏原文 95% 被拦截
    s63_missing_tgt = (
        "图2a展示了在<m1/>时的两两比较结果。<m2/>的值为4.387，得到临界差异<m3/>。"
        "例如，在该置信水平下（见图2a），不能得出结论。"
    )
    assert percentage_problems(s63_src, s63_missing_tgt, formulas=s63_formulas) == ["译文遗漏了原文百分数 95%"]

    # α=0.05 对应 95% 置信水平或 5% 显著性水平合法放行；中文“百分之90”新增同样拦截
    assert (
        percentage_problems(
            "We test at <m1/>.", "我们在95%置信水平与5%显著性水平下检验<m1/>。", formulas={"m1": r"\alpha = 0 . 0 5"}
        )
        == []
    )
    assert percentage_problems("We test at α=0.05.", "我们在百分之90置信水平下检验α=0.05。") == [
        "译文新增了原文不存在的百分数 90%，请严格忠实原文数值"
    ]

    # 范围写法 10–20% 与 10%–20% 双向兼容不误报；等价小数 0.95 放行
    assert percentage_problems("Accuracy improves by 10–20%.", "准确率提升10%–20%。") == []
    assert percentage_problems("Accuracy improves by 10%–20%.", "准确率提升10–20%。") == []
    assert percentage_problems("Confidence is 95%.", "置信度为0.95。") == []


# ---- R6-3: 公式旁括号字面数值守卫 + 比较符结尾公式衔接机械守卫 ----


def test_r6_3_adjacent_number_and_trailing_comparator_guards():
    s44_src = (
        "Reject <m1/> if: <m2/> critical value at <m3/> in <m4/> distribution table "
        "with k-1 degrees of freedom, being (1 <m5/>) the confidence level we want to achieve."
    )
    s44_formulas = {
        "m1": r"{ \sf H } _ { 0 }",
        "m2": r"\sf F \geq",
        "m3": r"( 1 - \mathtt { a } )",
        "m4": r"\mathsf { X } ^ { 2 }",
        "m5": r"- { \mathfrak { a } }",
    }
    # R5 真实错译：既丢了 (1 <m5/>) 的 1，又把 <m2/> 后紧接“在”割裂比较式
    s44_bad_tgt = "若<m2/>在<m4/>分布表中自由度为k-1、置信水平为<m3/>（即我们希望达到的<m5/>）时的临界值，则拒绝<m1/>。"
    adj = _adjacent_formula_number_problems(s44_src, s44_bad_tgt)
    comp = _trailing_comparator_formula_problems(s44_src, s44_bad_tgt, s44_formulas)
    assert adj == ["请保留公式 <m5/> 旁的数值 1（原文写作 (1 <m5/>)）"]
    assert len(comp) == 1 and "<m2/>" in comp[0] and "在" in comp[0]
    combined = _target_problems(s44_src, s44_bad_tgt, formulas=s44_formulas)
    assert combined == adj + comp

    # 正确译文：括号定语紧接 <m2/>，且保留 (1<m5/>) 中的 1 -> 全部通过
    s44_good_tgt = (
        "若<m2/>（自由度为k-1的<m4/>分布表中<m3/>处的临界值），其中(1<m5/>)为我们希望达到的置信水平，则拒绝<m1/>。"
    )
    assert _target_problems(s44_src, s44_good_tgt, formulas=s44_formulas) == []

    # 真实 R6 正例通过，单变量仅删除 <m5/> 前的 1 时（即使前 6 字符紧邻 <m1/>）绝不把 <m1/> 编号 1 误判为字面 1
    s44_r6_tgt = "若<m2/>自由度为k-1的<m4/>分布表中<m3/>处的临界值，则拒绝<m1/>，其中1<m5/>为我们期望达到的置信水平。"
    s44_r6_drop_one = (
        "若<m2/>自由度为k-1的<m4/>分布表中<m3/>处的临界值，则拒绝<m1/>，其中<m5/>为我们期望达到的置信水平。"
    )
    assert _target_problems(s44_src, s44_r6_tgt, formulas=s44_formulas) == []
    assert _target_problems(s44_src, s44_r6_drop_one, formulas=s44_formulas) == [
        "请保留公式 <m5/> 旁的数值 1（原文写作 (1 <m5/>)）"
    ]
    assert _adjacent_formula_number_problems("Take (<m1/> + 1) and <m2/>.", "执行<m1/>以及<m2/>。") == [
        "请保留公式 <m1/> 旁的数值 1（原文写作 (<m1/> + 1)）"
    ]

    # 合法放行：<m2/>对应于……的临界值、<m2/>临界值
    assert (
        _trailing_comparator_formula_problems(
            s44_src, "若<m2/>对应于<m4/>分布表中<m3/>处的临界值，其中(1<m5/>)为置信水平，则拒绝<m1/>。", s44_formulas
        )
        == []
    )
    assert (
        _trailing_comparator_formula_problems(s44_src, "若<m2/>临界值（自由度为k-1），则拒绝<m1/>。", s44_formulas)
        == []
    )

    # 右侧字面数字模式 (<m1/> + 1) 丢失 1 同样拦截；保留时放行
    assert _adjacent_formula_number_problems("Take (<m1/> + 1) steps.", "执行(<m1/>)步。") == [
        "请保留公式 <m1/> 旁的数值 1（原文写作 (<m1/> + 1)）"
    ]
    assert _adjacent_formula_number_problems("Take (<m1/> + 1) steps.", "执行(<m1/> + 1)步。") == []

    # 非比较符结尾的公式（如 05 s61 的 \mathsf{F} \geq \mathsf{X}^{2}）后接“在”绝不误报
    assert (
        _trailing_comparator_formula_problems(
            "Reject if <m2/> in table.",
            "若<m2/>在分布表中成立，则拒绝。",
            {"m2": r"\mathsf { F } \geq \mathsf { X } ^ { 2 }"},
        )
        == []
    )


# ---- R6-4: 译文字面量 LaTeX 命令泄漏机械守卫 ----


def test_r6_4_leaked_latex_command_guard():
    src = "It is clear that function ¯v is concave in x, <m1/> for all <m2/>."
    bad_bar = "显然，函数\\bar v关于x是凹的，对所有<m2/>均有<m1/>。"
    bad_tilde = "显然，函数\\tilde U在\\mathbb{R}上是凹的，对所有<m2/>均有<m1/>。"
    assert _leaked_latex_command_problems(src, bad_bar) == [
        "译文意外泄漏了 LaTeX 命令（\\bar），公式请仅保留 <mN/> 占位符"
    ]
    assert _target_problems(src, bad_bar) == ["译文意外泄漏了 LaTeX 命令（\\bar），公式请仅保留 <mN/> 占位符"]
    assert _leaked_latex_command_problems(src, bad_tilde) == [
        "译文意外泄漏了 LaTeX 命令（\\tilde、\\mathbb），公式请仅保留 <mN/> 占位符"
    ]

    # 正常中文译文不报错
    assert _target_problems(src, "显然，函数¯v关于x是凹的，对所有<m2/>均有<m1/>。") == []
    # 原文本身含反斜杠路径/字面命令不误报
    assert _target_problems(r"Read C:\data\in\file.txt first.", r"先读取路径 C:\data\in\file.txt。") == []
    assert _target_problems(r"Use C:\Users\name\config.yaml.", r"使用配置 C:\Users\name\config.yaml。") == []


# ---- R6-5: 过滤 缩写=纯英文多词全称 误抽词条 ----


def test_r6_5_skip_acronym_to_english_full_name_and_prompt_rule():
    assert _skip_extracted_term("VMs", "virtual machines")
    assert _skip_extracted_term("TCM", "Typed Comparison Method")
    assert not _skip_extracted_term("TCM", "类型化比较方法")
    assert not _skip_extracted_term("TCM", "TCM")
    assert not _skip_extracted_term("Seccomp Notify", "Seccomp Notify")
    assert not _skip_extracted_term("root privileges", "root 权限")

    parsed = parse_terms(
        "\n".join(
            [
                "领域：系统网络",
                "AI公共词库：停用",
                "VMs=virtual machines",
                "TCM=Typed Comparison Method",
                "TCM=类型化比较方法",
                "ResNet=ResNet",
                "Seccomp Notify=Seccomp Notify",
                "root privileges=root 权限",
            ]
        )
    )
    assert parsed.terms == {
        "TCM": "类型化比较方法",
        "ResNet": "ResNet",
        "Seccomp Notify": "Seccomp Notify",
        "root privileges": "root 权限",
    }

    seen_prompts: list[str] = []

    class CaptureChat:
        model = "fake"

        async def list_models(self) -> list[str]:
            return []

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            seen_prompts.append(user)
            return ChatResult(text="领域：系统网络\nAI公共词库：停用\nVMs=virtual machines\nVM=虚拟机")

    found = asyncio.run(
        extract_terms(
            CaptureChat(),
            title="TCP/IP Acceleration",
            abstract="Containers and VMs.",
            intro="",
            headings=["Introduction"],
            definitions={"VM": "virtual machines"},
        )
    )
    assert "禁止输出「缩写=英文全称」" in seen_prompts[0]
    assert found.terms == {"VM": "虚拟机"}


# ---- R6-6: 超高密度公式段（>= 10 式）独立成批 ----


def _unit(name: str, formula_count: int) -> _Unit:
    formulas = {f"m{i}": f"x_{i}" for i in range(1, formula_count + 1)}
    source = " ".join(f"<{k}/>" for k in formulas)
    return _Unit(
        source=source,
        formulas=formulas,
        section="S",
        role=BlockType.TEXT,
        prev_context="",
        seg_indexes=[0],
        key=name,
        user_terms={},
        reference_terms={},
    )


def test_r6_6_ultra_high_formula_segment_runs_solo_while_moderate_segments_still_merge():
    assert _HIGH_FORMULA_SOLO_THRESHOLD == 10

    # 11 式段必须独立成批，不再与紧邻的 7 式段拼成 18 式批；而 6 式 + 7 式仍合并为 1 批守住降本红利
    u_plain = _unit("plain", 1)
    u6 = _unit("s6", 6)
    u7 = _unit("s7", 7)
    u11 = _unit("s11", 11)
    u7b = _unit("s7b", 7)
    u10 = _unit("s10", 10)

    batches = _split_high_formula_batches([[u_plain, u6, u7, u11, u7b, u10]])
    assert [[u.key for u in b] for b in batches] == [
        ["plain"],
        ["s6", "s7"],
        ["s11"],
        ["s7b"],
        ["s10"],
    ]

    # 11 式段夹在两个 5 式段中间时，前后分别独立，11 式单独成批
    batches_mid = _split_high_formula_batches([[_unit("a5", 5), _unit("b11", 11), _unit("c5", 5), _unit("d6", 6)]])
    assert [[u.key for u in b] for b in batches_mid] == [
        ["a5"],
        ["b11"],
        ["c5", "d6"],
    ]

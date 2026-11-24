"""Q4 Round 3 共性缺陷修复包 A 回归测试（上标、INLINE_ABSTRACT、高公式批/超时、split 半径、术语收口）。"""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import Block, BlockStyle, BlockType, DocMeta, Document, Page, Segment, SegmentStatus, Span
from pdftr.domain.job import Job, JobOptions
from pdftr.domain.llm import ChatOptions, ChatResult, ChatUsage
from pdftr.domain.text import INLINE_ABSTRACT
from pdftr.fusion.build import _meta
from pdftr.glossary.extract import PROMPT as EXTRACT_PROMPT
from pdftr.glossary.extract import extract_terms
from pdftr.glossary.extract import parse as parse_terms
from pdftr.pipeline.runner import Pipeline, PipelineDeps
from pdftr.pipeline.secrets import Secrets
from pdftr.rendering.export import split_by_parts
from pdftr.translation.batching import VALIDATION_VERSION, citation_problems, normalize_formula_placeholders
from pdftr.translation.prompt import DocContext
from pdftr.translation.segmenter import front_matter_ids, segment
from pdftr.translation.service import TranslateOptions, _request_timeout, _target_problems, translate

# ---------- R3-1: 上标引文校验顿号与公式占位符假阳性修复 ----------


def test_sup_citation_accepts_chinese_enumeration_comma_dashes_and_formula_placeholders():
    assert VALIDATION_VERSION == 6

    # 02_chem_battery s5: 中文顿号、破折号、省略号后紧跟上标引文
    src_enum = (
        "Strategies include cation doping,<sup>14</sup> surface coating,<sup>15,16</sup> and design.<sup>17</sup>"
    )
    dst_enum = "策略包括阳离子掺杂、<sup>14</sup>表面涂层、<sup>15,16</sup>以及结构设计。<sup>17</sup>"
    assert citation_problems(src_enum, dst_enum) == []
    assert (
        citation_problems(
            "Earlier work—<sup>12</sup> and more…<sup>13</sup>",
            "早期工作—<sup>12</sup>与后续…<sup>13</sup>",
        )
        == []
    )

    # 02_chem_battery s13: 上标引文在译文中紧跟 <mN/> 行内化学式/公式占位符
    src_formula_multi = "Materials such as <m4/><sup>46,47</sup> and others<sup>10–13</sup> are studied."
    dst_formula_multi = "像<m4/><sup>46,47</sup>这样的材料及其他体系<m5/><sup>10–13</sup>已获研究。"
    assert citation_problems(src_formula_multi, dst_formula_multi) == []

    # 源文已确认的单数字上标引文，在译文因语序调整紧跟 <mN/> 后必须放行（无论编号 < 10 还是 >= 10）
    assert citation_problems("Reported for layered cathodes<sup>21</sup>.", "如<m1/><sup>21</sup>所示。") == []
    assert citation_problems("Reported for layered cathodes<sup>5</sup>.", "如<m1/><sup>5</sup>所示。") == []
    assert (
        citation_problems(
            "As seen in <m1/><sup>21</sup>, stability improves.",
            "如<m1/><sup>21</sup>所示，稳定性提升。",
        )
        == []
    )


def test_sup_citation_rejects_mutations_and_keeps_chemistry_and_math_powers_safe():
    # 真实上标引文删除或改号仍被准确拒绝
    assert citation_problems("Previous studies.<sup>2,22–25</sup>", "先前研究。<sup>2,22–26</sup>")
    assert citation_problems("Cathodes <m1/><sup>40–43</sup> show high capacity.", "正极材料<m1/>表现出高容量。")
    assert citation_problems(
        "As seen in <m1/><sup>21</sup>, stability improves.",
        "如<m1/><sup>22</sup>所示，稳定性提升。",
    )
    assert citation_problems(
        "Materials <m4/><sup>46,47</sup> are stable.",
        "像<m4/><sup>46,48</sup>这样的材料稳定。",
    )

    # 化学电荷、负指数、纯数字幂与源文数学平方不误判为上标引文
    assert (
        citation_problems(
            "Mn<sup>2+</sup>, O<sup>2-</sup>, 10<sup>-3</sup>, 10<sup>4</sup>, and <m1/><sup>2</sup>.",
            "锰离子Mn<sup>2+</sup>、氧离子O<sup>2-</sup>、10<sup>-3</sup>、10<sup>4</sup>与<m1/><sup>2</sup>。",
        )
        == []
    )
    assert citation_problems("Compute <m1/><sup>2</sup> and <m2/><sup>12</sup> = 0.", "计算结果。") == []
    # 同段既有数学平方 <m1/>^2 又有真实引文 <sup>14</sup> 时，译文保留两者不报多出引文 2
    assert (
        citation_problems(
            "The term <m1/><sup>2</sup> was analyzed in studies<sup>14</sup>.",
            "该项<m1/><sup>2</sup>已在研究<sup>14</sup>中分析。",
        )
        == []
    )


# ---------- R3-3: INLINE_ABSTRACT 匹配句点与 Key words ----------


def test_inline_abstract_matches_period_and_prevents_front_matter_skipping_keywords():
    box = Rect.of([0, 0, 200, 12])
    style = BlockStyle(font_size=10)

    def make_block(bid: str, btype: BlockType, text: str) -> Block:
        order = int(bid.split("-b")[1])
        return Block(
            id=bid,
            page=0,
            type=btype,
            bbox=box,
            order=order,
            spans=[Span(text=text, line=0, bbox=box, size=10)],
            style=style,
            translatable=True,
        )

    samples = (
        "Abstract. We study...",
        "Keywords. RL, finance",
        "Key words. portfolio",
        "Key word: utility",
        "Index terms. control",
    )
    for sample in samples:
        assert INLINE_ABSTRACT.match(sample) is not None
    assert INLINE_ABSTRACT.match("Abstract algebra studies groups.") is None

    blocks = [
        make_block("p0-b0", BlockType.TITLE, "Reinforcement Learning for Utility Maximization"),
        make_block("p0-b1", BlockType.TEXT, "A. Researcher, Finance Dept, author@example.edu"),
        make_block("p0-b2", BlockType.TEXT, "Abstract. We develop a continuous-time reinforcement learning framework."),
        make_block("p0-b4", BlockType.TEXT, "Keywords. Reinforcement learning, portfolio optimization, utility"),
        make_block("p0-b5", BlockType.TITLE, "1. Introduction"),
        make_block("p0-b6", BlockType.TEXT, "We introduce the problem setup and notation."),
    ]
    doc = Document(job_id="finrl", pages=[Page(index=0, width=600, height=800, blocks=blocks)])

    assert front_matter_ids(doc) == {"p0-b1"}
    seg_block_ids = [s.block_ids for s in segment(doc)]
    assert ["p0-b2"] in seg_block_ids
    assert ["p0-b4"] in seg_block_ids
    assert ["p0-b1"] not in seg_block_ids

    # 同时核查 fusion/build._meta 能正确从 "Abstract. ..." 抽取摘要且不误抽 Keywords
    meta = _meta(doc.pages, parser="mineru", used_cloud=True)
    assert meta.abstract == "We develop a continuous-time reinforcement learning framework."


# ---------- R3-5: 高公式批限容、思考超时放宽与 <mN /> 窄规范化 ----------


class _MemCache:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def put(self, key: str, target: str) -> None:
        self.data[key] = target

    def purge_older_than(self, hours: int) -> int:
        return 0


def _formula_seg(idx: int, count: int, section: str = "3 Proofs") -> Segment:
    tags = " ".join(f"<m{i}/>" for i in range(1, count + 1))
    formulas = {f"m{i}": f"x_{i}" for i in range(1, count + 1)}
    return Segment(
        id=f"s{idx}",
        job_id="j",
        block_ids=[f"p0-b{idx}"],
        source=f"Segment {idx} with {tags}.",
        formulas=formulas,
        section=section,
    )


def test_high_formula_batches_enforce_max_four_segments_and_eighteen_formulas():
    class BatchRecorder:
        model = "deepseek-flash"

        def __init__(self) -> None:
            self.batches: list[tuple[list[str], ChatOptions]] = []

        async def list_models(self) -> list[str]:
            return [self.model]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            rows = re.findall(r"^(\d+)\|(.*)$", user, re.M)
            self.batches.append(([row[1] for row in rows], opts))
            out = []
            for num, body in rows:
                compact = re.sub(r"<(m\d+)>.*?</\1>", r"<\1/>", body)
                out.append(f"{num}|译:{compact}")
            return ChatResult(text="\n".join(out), usage=ChatUsage(prompt_tokens=10, completion_tokens=10))

    # 4 段各 5 个公式 -> [3段(15公式), 1段(5公式)]（累计公式上限 18）
    chat1 = BatchRecorder()
    segs_5 = [_formula_seg(1, 5), _formula_seg(2, 5), _formula_seg(3, 5), _formula_seg(4, 5)]
    out1, report1 = asyncio.run(
        translate(segs_5, chat=chat1, cache=_MemCache(), ctx=DocContext(), opts=TranslateOptions(concurrency=1))
    )
    assert all(s.status is SegmentStatus.DONE for s in out1) and report1.requests == 2
    assert [len(b) for b, _ in chat1.batches] == [3, 1]
    assert all(opts.thinking == "low" for _, opts in chat1.batches)

    # 3 段各 6 个公式 -> 合并为 1 批(18公式)；单段 12 (>=10) 独立成批；单段 6 独立成批；4 公式段走普通快速批
    chat2 = BatchRecorder()
    segs_mix = [
        _formula_seg(1, 6),
        _formula_seg(2, 6),
        _formula_seg(3, 6),
        _formula_seg(4, 12),
        _formula_seg(5, 6),
        _formula_seg(6, 4),
    ]
    out2, report2 = asyncio.run(
        translate(segs_mix, chat=chat2, cache=_MemCache(), ctx=DocContext(), opts=TranslateOptions(concurrency=1))
    )
    assert all(s.status is SegmentStatus.DONE for s in out2) and report2.requests == 4
    assert [(len(b), opts.thinking) for b, opts in chat2.batches] == [
        (1, "off"),
        (3, "low"),
        (1, "low"),
        (1, "low"),
    ]

    # 普通段与高公式段混排：普通段合并保持不变，高公式段独立控容
    chat3 = BatchRecorder()
    plain1 = Segment(id="p1", job_id="j", block_ids=["p0-b0"], source="Plain one.", section="3 Proofs")
    plain2 = Segment(id="p2", job_id="j", block_ids=["p0-b9"], source="Plain two.", section="3 Proofs")
    segs_mixed = [plain1, _formula_seg(1, 10), _formula_seg(2, 10), plain2]
    out3, report3 = asyncio.run(
        translate(segs_mixed, chat=chat3, cache=_MemCache(), ctx=DocContext(), opts=TranslateOptions(concurrency=1))
    )
    assert all(s.status is SegmentStatus.DONE for s in out3) and report3.requests == 3
    assert [(len(b), opts.thinking) for b, opts in chat3.batches] == [(2, "off"), (1, "low"), (1, "low")]


def test_thinking_request_timeout_relaxes_to_at_least_60s_without_breaking_small_test_timeouts():
    assert _request_timeout(30.0, "off") == 30.0
    assert _request_timeout(30.0, "low") == 60.0
    assert _request_timeout(90.0, "low") == 90.0
    assert _request_timeout(10.0, "low") == 60.0
    assert _request_timeout(0.05, "off") == 0.05
    assert _request_timeout(0.05, "low") == pytest.approx(0.10)


def test_spaced_self_closing_formula_placeholder_is_narrowly_normalized_while_malformed_tags_fail():
    assert normalize_formula_placeholders("设 <m1 /> 与 <m12   /> 已知。") == "设 <m1/> 与 <m12/> 已知。"
    assert normalize_formula_placeholders('<m1 x="1"/> <m2> </m2> <M1 />') == '<m1 x="1"/> <m2> </m2> <M1 />'

    # 非法标签仍被严格拒绝
    assert _target_problems("Let <m1/> be x.", "设<m1>为x。")
    assert _target_problems("Let <m1/> be x.", "设</m1>为x。")
    assert _target_problems("Let <m1/> be x.", '设<m1 x="1"/>为x。')
    assert _target_problems("Let <m1/> be x.", '设<m1/><m1 x="1"/>为x。')

    # translate 主流程中，模型输出 <m1 /> 规范化后一次通过且缓存写入 <m1/>
    class SpacedPlaceholderChat:
        model = "deepseek-flash"

        async def list_models(self) -> list[str]:
            return [self.model]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            return ChatResult(text="1|令 <m1 /> 与 <m2  /> 为给定变量。")

    seg = Segment(
        id="s1",
        job_id="j",
        block_ids=["p0-b1"],
        source="Let <m1/> and <m2/> be variables.",
        formulas={"m1": "x", "m2": "y"},
    )
    cache = _MemCache()
    out, report = asyncio.run(translate([seg], chat=SpacedPlaceholderChat(), cache=cache, ctx=DocContext()))
    assert report.requests == 1 and report.failed == 0
    assert out[0].target == "令<m1/>与<m2/>为给定变量。"
    assert list(cache.data.values()) == ["令<m1/>与<m2/>为给定变量。"]


# ---------- R3-7: split_by_parts 容量感知搜索半径 ----------


def test_split_by_parts_limits_search_radius_for_narrow_tail_and_head_blocks():
    # 01_ai_resnet s5 典型场景：[646, 40]，距离目标前 25 字有句号，末块不能被塞成 40+ 字蚂蚁字
    body = "深度残差网络在图像识别中表现出色，通过跳跃连接缓解退化问题，" * 7
    tail_with_period = "先前架构存在明显瓶颈。因此更深层网络在训练阶段表现出更高训练误差且并非过拟合导致最终影响收敛"
    text = body + tail_with_period
    parts = split_by_parts(text, [646, 40])
    assert "".join(parts) == text
    assert len(parts[1]) <= 22

    # 反向窄首块 [40, 646]：首块也不会向前/向后吸进过多内容
    head_text = (
        "网络退化并非由过拟合引起而是优化困难导致后续长正文开始，"
        + "实验结果表明残差学习能够稳定收敛并显著提升准确率" * 8
    )
    head_parts = split_by_parts(head_text, [40, 646])
    assert "".join(head_parts) == head_text
    assert len(head_parts[0]) <= 22

    # 3 块含极窄中间块 [300, 20, 300]
    mid_text = "甲" * 100 + "。" + "乙" * 25 + "丙" * 100
    mid_parts = split_by_parts(mid_text, [300, 20, 300])
    assert len(mid_parts) == 3 and "".join(mid_parts) == mid_text
    assert len(mid_parts[1]) <= 15


# ---------- R3-8: 术语抽取收口与透传 user_terms ----------


def test_glossary_prompt_parse_filters_and_user_terms_passthrough(tmp_path: Path):
    # Prompt 规则校验
    assert "操作系统/Linux 权限语境" in EXTRACT_PROMPT
    assert "root node" in EXTRACT_PROMPT and "根节点" in EXTRACT_PROMPT
    assert "sockets=sockets" in EXTRACT_PROMPT

    # parse 后置过滤：丢弃孤立通用泛词与普通小写英文复数自映射，保留合法多词/缩写/白名单词
    raw = "\n".join(
        [
            "领域：系统安全与机器学习",
            "AI公共词库：停用",
            "optimize=优化",
            "Accuracy=准确率",
            "complexity=复杂度",
            "model=model",
            "sockets=sockets",
            "containers=containers",
            "nodes=nodes",
            "residual network=残差网络",
            "root node=根节点",
            "root privileges=root 权限",
            "ResNet=ResNet",
            "SVMs=SVMs",
            "https=https",
            "macos=macos",
            "cgroups=cgroups",
            "getpeername=getpeername",
        ]
    )
    parsed = parse_terms(raw)
    for dropped in ("optimize", "Accuracy", "complexity", "model", "sockets", "containers", "nodes"):
        assert dropped not in parsed.terms
    assert parsed.terms == {
        "residual network": "残差网络",
        "root node": "根节点",
        "root privileges": "root 权限",
        "ResNet": "ResNet",
        "SVMs": "SVMs",
        "https": "https",
        "macos": "macos",
        "cgroups": "cgroups",
        "getpeername": "getpeername",
    }

    # extract_terms 透传 user_terms 到 prompt
    seen_prompts: list[str] = []

    class CaptureChat:
        model = "fake"

        async def list_models(self) -> list[str]:
            return [self.model]

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            seen_prompts.append(user)
            return ChatResult(text="领域：金融强化学习\nAI公共词库：停用\npower utility=幂效用函数")

    asyncio.run(
        extract_terms(
            CaptureChat(),
            title="RL in Finance",
            abstract="We optimize power utility.",
            intro="Portfolio optimization intro.",
            headings=["1 Introduction"],
            candidates=("power utility",),
            user_terms={"utility": "效用函数"},
        )
    )
    assert len(seen_prompts) == 1
    assert "用户已指定术语（若抽取包含这些词的复合术语，译法须与之保持一致）：utility=效用函数" in seen_prompts[0]

    # Pipeline._glossary 将 job.options.user_terms 透传给 extract_terms
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    box = Rect.of([0, 0, 100, 10])
    doc = Document(
        job_id="job1",
        meta=DocMeta(title="Finance RL", abstract="We study power utility maximization."),
        pages=[
            Page(
                index=0,
                width=600,
                height=800,
                blocks=[
                    Block(
                        id="p0-b0",
                        page=0,
                        type=BlockType.TEXT,
                        bbox=box,
                        order=0,
                        spans=[Span(text="power utility " * 20, line=0, bbox=box, size=10)],
                        style=BlockStyle(font_size=10),
                        translatable=True,
                    )
                ],
            )
        ],
    )
    (job_dir / "ir.json").write_text(doc.model_dump_json(), encoding="utf-8")
    seg = Segment(id="s0", job_id="job1", block_ids=["p0-b0"], source="power utility " * 20, role=BlockType.TEXT)
    job = Job(
        id="job1",
        filename="fin.pdf",
        options=JobOptions(user_terms={"utility": "效用函数", "portfolio": "投资组合"}),
    )

    pipeline_prompts: list[str] = []

    class PipelineChat(CaptureChat):
        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            pipeline_prompts.append(user)
            return ChatResult(text="领域：金融工程\nAI公共词库：停用\npower utility=幂效用函数")

    artifacts = SimpleNamespace(path=lambda jid, name: job_dir / name, job_dir=lambda jid: job_dir)
    store = SimpleNamespace(segments=lambda jid: [seg], save_job=lambda j: None)
    events = SimpleNamespace(publish=lambda j: None)
    deps = PipelineDeps(
        artifacts=cast(Any, artifacts),
        store=cast(Any, store),
        cache=_MemCache(),
        events=cast(Any, events),
        secrets=cast(Any, SimpleNamespace()),
        parser_factory=cast(Any, lambda s, p: None),
        chat_factory=lambda s: PipelineChat(),
        term_matcher=lambda: None,
    )
    pipeline = Pipeline(deps)
    secrets = Secrets(
        llm_api_key="k",
        llm_base_url="https://example.test",
        llm_model="m",
        parser_token="p",
    )
    asyncio.run(pipeline._glossary(job, secrets))
    assert len(pipeline_prompts) == 1
    assert "utility=效用函数" in pipeline_prompts[0] and "portfolio=投资组合" in pipeline_prompts[0]
    assert (job_dir / "glossary.json").exists()

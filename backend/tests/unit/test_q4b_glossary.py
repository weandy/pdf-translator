import asyncio
from itertools import pairwise

import pytest

from pdftr.domain.llm import ChatOptions, ChatResult, ChatUsage
from pdftr.glossary.extract import Extraction, extract_terms, frequent_candidates, parse
from pdftr.glossary.matcher import TermMatcher, TermSelection
from pdftr.glossary.normalize import normalize_terms
from pdftr.glossary.sources import clean_zh


def test_spans_keep_original_offsets_and_prefer_longest_then_exact_spelling():
    matcher = TermMatcher({
        "policy": "策略",
        "policy gradient": "策略梯度",
        "policy-gradients": "政策梯度",
        "TCP/IP": "协议簇",
        "TCP IP": "分词",
        "ADAM": "缩写",
        "Adam": "优化器",
        "SVM": "支持向量机",
        "AP@0.5": "平均精度",
        "H2O": "水",
    })  # fmt: skip
    text = (
        "用 <b>po</b>licy-<i>gradients</i>, policy gradient, TCP/IP, TCP IP, ADAM, Adam, SVMs, AP@0.5, H<sub>2</sub>O."
    )
    hits = matcher.match_spans(text)
    assert [(text[m.start : m.end], m.key, m.term, m.target) for m in hits[:3]] == [
        ("po</b>licy-<i>gradients", "word:policy gradient", "policy-gradients", "政策梯度"),
        ("policy gradient", "word:policy gradient", "policy gradient", "策略梯度"),
        ("TCP/IP", "lit:tcp/ip", "TCP/IP", "协议簇"),
    ]
    assert [m.term for m in hits[3:]] == ["TCP IP", "ADAM", "Adam", "SVM", "AP@0.5", "H2O"]
    assert all(a.end <= b.start for a, b in pairwise(hits))
    assert matcher.match("policy-gradients", accept=lambda _key, term, _target: term == "policy gradient") == {
        "policy gradient": "策略梯度"
    }


@pytest.mark.parametrize(
    "gap",
    [
        ". ",
        ".",
        ", ",
        "; ",
        "/",
        " / ",
        "\n",
        "\r\n",
        "\u2028",
        "<m0/>",
        " https://example.org/policy ",
        " www.example.org/gradient ",
    ],
)
def test_matcher_rejects_punctuation_formulas_urls_and_vertical_gaps(gap):
    matcher = TermMatcher({"policy gradient": "策略梯度", "example": "示例"})
    assert matcher.match(f"policy{gap}gradient") == {}
    assert matcher.match("policy\t-\u00a0gradient") == {"policy gradient": "策略梯度"}
    assert matcher.match("policy example") == {"example": "示例"}


def test_three_layers_arbitrate_by_instance_and_retry_shorter_unoccupied_span():
    found = Extraction(terms={"fair value": "公允价值", "search operator": "搜索算子"}, use_library=True)
    library = TermMatcher({
        "value": "数值", "fair value": "公正价值", "adaptive search": "自适应搜索", "adaptive": "自适应",
        "search operator": "搜索运算符", "saddle point": "鞍点",
    })  # fmt: skip
    selection = TermSelection(found, {"operator": "算子"}, lambda: library)
    text = "An adaptive search operator estimates fair value; another value defines a saddle point."
    user, paper, public = selection.match_segment(text)
    assert user == {"operator": "算子"}
    assert paper == {"fair value": "公允价值"}
    assert public == {"adaptive search": "自适应搜索", "value": "数值", "saddle point": "鞍点"}

    selection = TermSelection(found, {"search operator": "检索算子"}, lambda: library)
    assert selection.match_segment(text) == (
        {"search operator": "检索算子"},
        {"fair value": "公允价值"},
        {"adaptive": "自适应", "value": "数值", "saddle point": "鞍点"},
    )


def test_library_only_checks_same_key_conflicts_with_actual_higher_hits():
    library = TermMatcher({"ai": "公共译法", "box": "公共框"}, min_length=2)
    selection = TermSelection(Extraction(use_library=True), {"AI": "专名", "boxes": "用户框"}, lambda: library)
    assert selection.match_segment("ai AI boxes and box") == (
        {"AI": "专名", "boxes": "用户框"},
        {},
        {"ai": "公共译法"},
    )


def test_normalize_terms_counts_only_body_and_preserves_acronym_categories():
    terms = {
        "Recall": "召回率",
        "Precision": "精确率",
        "Introduction": "引言",
        "policy gradient": "策略梯度",
        "cross segment": "跨段",
        "ADAM": "缩写",
        "Adam": "Adam",
    }
    paragraphs = [
        "Recall and precision are measured.", "High recall is useful.", "introduction introduction.",
        "We use policy-<b>gradient</b> with ADAM and Adam across",
        "segment boundaries; cross",
        "segment windows.",
    ]  # fmt: skip
    result = normalize_terms(terms, paragraphs, ["II. Recall", "3 Precision", "1. Introduction"])
    assert result == {"Recall": "召回率", "policy-gradient": "策略梯度", "ADAM": "缩写", "Adam": "Adam"}
    assert normalize_terms({"ADAM": "缩写"}, ["We use Adam."]) == {}
    assert normalize_terms({"Recall": "召回率"}, "II. Recall\nRecall", ["II. Recall"]) == {}
    assert normalize_terms({"Recall": "召回率"}, "II. Recall\nRecall\nRecall", ["II. Recall"]) == {"Recall": "召回率"}


def test_frequent_candidates_keeps_technical_words_and_only_real_ngrams():
    texts = [
        "<b>off</b>-policy, k-means, TCP/IP, H2O, LoRA, GNNs, SVMs, GNN GNNs, SVM SVMs. "
        "beam search tree. beam <m1/> tree; beam https://example.org/node tree; beam www.example.org tree. "
        "beam/tree; beam\nforest. 2020 5kg 10ms 3.5. THE."
    ] * 3
    got = frequent_candidates([*texts, "ranking", "metric", "AP@0.5"])
    assert got[:2] == ("beam", "tree")
    assert got.index("GNNs") < got.index("SVMs")
    assert all(
        word in got
        for word in (
            "off-policy",
            "k-means",
            "TCP/IP",
            "H2O",
            "LoRA",
            "beam search",
            "beam search tree",
            "search tree",
            "AP@0.5",
        )
    )
    assert {
        "GNN GNNs",
        "SVM SVMs",
        "beam tree",
        "beam forest",
        "ranking metric",
        "2020",
        "5kg",
        "10ms",
        "3.5",
        "THE",
    }.isdisjoint(got)
    assert got.index("beam search") < got.index("beam search tree") < got.index("search tree")


def test_frequent_candidates_enforces_count_and_character_budgets_with_rare_reserve():
    frequent = [f"cell{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(35)]
    repeated = [f"sample {word}" for word in frequent for _ in range(6)]
    technical = [f"metric-{i}" for i in range(18)]
    rare = [f"AP@0.{i}" for i in range(7)]
    candidates = frequent_candidates([*repeated, *technical, *technical, *rare])
    assert len(candidates) == 60
    assert [word for word in candidates if "@" in word] == rare[:5]
    long_repeated = [f"{word * 10} chamber" for word in frequent for _ in range(6)]
    candidates = frequent_candidates([*long_repeated, *rare])
    assert len("、".join(candidates)) <= 1200
    assert [word for word in candidates if "@" in word] == rare[:5]


@pytest.mark.parametrize("annotation", ["（算法论文）", " (核心方向)", "(领域=算法)"])
def test_parse_allows_paired_gate_annotations(annotation):
    assert parse(f"领域：计算机\nAI 公共词库：启用{annotation}\nnode=节点").use_library
    assert not parse(f"AI公共词库：停用{annotation}").use_library


def test_truncated_parse_drops_last_nonblank_line_and_rejects_incomplete_gate():
    text = "领域：计算机\nAI公共词库：启用 (算法)\nnode=节点\nedge=边\n \t\n"
    assert parse(text).terms == {"node": "节点", "edge": "边"}
    found = parse(text, truncated=True)
    assert found.use_library and found.terms == {"node": "节点"}
    assert not parse("领域：计算机\nAI公共词库：启用\n", truncated=True).use_library
    found = parse("AI公共词库：启用 (领域=算法")
    assert not found.use_library and found.terms == {}
    assert not parse("AI公共词库：建议启用").use_library
    assert not parse("AI公共词库：启用（未配对)").use_library


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens", "refusal", "content_filter", "stop"])
def test_extract_terms_uses_finish_reason_and_keeps_usage(finish_reason):
    usage = ChatUsage(prompt_tokens=20, completion_tokens=7)

    class Chat:
        model = "fake"
        calls = 0

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            self.calls += 1
            assert opts.max_tokens == 1500
            return ChatResult(text="AI公共词库：启用\nnode=节点\nedge=边", finish_reason=finish_reason, usage=usage)

        async def list_models(self) -> list[str]:
            return []

    chat = Chat()
    found = asyncio.run(extract_terms(chat, title="", abstract="", intro="", headings=[]))
    assert chat.calls == 1 and found.usage == usage
    if finish_reason in ("length", "max_tokens"):
        assert found.use_library and found.terms == {"node": "节点"}
    elif finish_reason == "stop":
        assert found.use_library and found.terms == {"node": "节点", "edge": "边"}
    else:
        assert not found.use_library and not found.terms


def test_clean_zh_strips_parenthetical_slashes_before_splitting_alternatives():
    assert clean_zh("正则化（L1/L2）；规则化") == "正则化"
    assert clean_zh("模型(Transformer/BERT)<sup>1</sup>/网络") == "模型"
    assert clean_zh("Transformer（模型/网络）") == ""

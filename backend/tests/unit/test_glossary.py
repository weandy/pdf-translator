import asyncio
from pathlib import Path

import pytest

from pdftr.config import Settings
from pdftr.domain.llm import ChatOptions, ChatResult
from pdftr.glossary import sources
from pdftr.glossary.extract import PROMPT as EXTRACT_PROMPT
from pdftr.glossary.extract import Extraction, extract_terms, frequent_candidates, parse
from pdftr.glossary.filters import is_lazy_english_self_map
from pdftr.glossary.gist import parse as parse_gist
from pdftr.glossary.matcher import TermMatcher, TermSelection
from pdftr.glossary.normalize import normalize_terms


def test_parse_extraction():
    text = "领域：机器学习优化\nAI公共词库：启用\n- ADAM=ADAM\nstochastic optimization=随机优化\n乱写的一行\n"
    found = parse(text)
    assert found.domain == "机器学习优化" and found.use_library
    assert found.terms == {"ADAM": "ADAM", "stochastic optimization": "随机优化"}


def test_normalize_terms():
    body = "We propose Adam. Adam uses moment estimates. RELATED WORK is not a term here."
    terms = {"Adam": "Adam", "moment estimates": "矩估计", "Related Work": "相关工作", "missing term": "缺失"}
    assert normalize_terms(terms, body, headings=["5 RELATED WORK"]) == {"Adam": "Adam", "moment estimates": "矩估计"}


def test_matcher_longest_and_plural():
    m = TermMatcher({"neural network": "神经网络", "convolutional neural network": "卷积神经网络", "AI": "人工智能"})
    assert len(m) == 2  # 太短的 “AI” 不收
    hits = m.match("Deep convolutional neural networks and recurrent neural networks.")
    assert hits == {"convolutional neural network": "卷积神经网络", "neural network": "神经网络"}


def test_gist_parse():
    terms = parse_gist("Index,English,Chinese\n0,dropout,随机失活\n1,Dropout,丢弃法\n2,,空\n")
    assert terms == {"dropout": "随机失活", "Dropout": "丢弃法"}


def test_parse_aitd():
    md = (
        "索引编号|英文术语|中文翻译|常用缩写|来源&扩展|备注\n---|---|---|---|---|---\n"
        "AITD-00003|Accept-Reject Sampling Method|接受-拒绝抽样法/接受-拒绝采样法||[1]||\n"
        "AITD-01937|Transformer|Transformer||[1]||\n"
    )
    # 多个译法取第一个；没有汉字的“译名”不收
    assert sources.parse_aitd(md.encode()) == {"Accept-Reject Sampling Method": "接受-拒绝抽样法"}


def test_parse_d2l():
    md = "## 英汉术语对照\n\n鞍点，saddle point\n\n长短期记忆网络，long short-term memory (LSTM)\n"
    assert sources.parse_d2l(md.encode()) == {"saddle point": "鞍点", "long short-term memory": "长短期记忆网络"}


def test_parse_ml_technical_term_adds_full_name():
    md = (
        "| EN | ZH | 全称/缩写 | 备注 |\n| --- | --- | --- | --- |\n"
        "| AGI | 通用人工智能 | Artificial General Intelligence | |\n"
    )
    assert sources.parse_ml_technical_term(md.encode()) == {
        "AGI": "通用人工智能",
        "Artificial General Intelligence": "通用人工智能",
    }


def test_parse_ais_vocab_keeps_only_established_terms():
    csv = (
        '\ufeff"id","en","preferred_zh","status","confidence"\n'
        '"a","prompt injection","提示词注入","established","high"\n'
        '"b","vibe term","某个新词","emerging","high"\n'
        '"c","low confidence","低置信","common","low"\n'
    )
    assert sources.parse_ais_vocab(csv.encode()) == {"prompt injection": "提示词注入"}


def test_parse_computerese_multiword_only_and_strips_footnotes():
    md = (
        "| Word | Meaning |\n|---|---|\n| command | 命令 |\n"
        "| Collapse Hierarchy | 折叠继承关系<sup>1</sup> |\n"
        "| COLA | 整洁面向对象分层架构，Clean Object-oriented and Layered Architecture |\n"
    )
    assert sources.parse_computerese(md.encode()) == {"Collapse Hierarchy": "折叠继承关系"}


def test_clean_zh_rejects_explanations():
    assert sources.clean_zh("Transformer 中文为“变换器”") == ""
    assert sources.clean_zh("命令/查询职责分离（CQRS）") == "命令"
    assert sources.clean_zh("S 形曲线") == "S 形曲线"


def test_merge_priority_and_load_all(tmp_path: Path):
    order = sources.parse_ids("aitd,d2l,unknown")
    assert [s.id for s in order] == ["aitd", "d2l"]

    payload = {
        sources.SOURCES["aitd"].url: "AITD-1|Binary Classification|二分类||[1]||\n".encode(),
        sources.SOURCES["d2l"].url: "二元分类，binary classification\n鞍点，saddle point\n".encode(),
    }

    def fetch(url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload[url])

    terms, loaded = sources.load_all(order, tmp_path, fetch)
    # 同一术语以排在前面的来源（机器之心）为准，大小写不同也算同一条
    assert terms == {"Binary Classification": "二分类", "saddle point": "鞍点"}
    assert [s.id for s in loaded] == ["aitd", "d2l"]

    def broken(url: str, dest: Path) -> None:
        raise OSError("network down")

    terms, loaded = sources.load_all([sources.SOURCES["gist"], *order], tmp_path, broken)
    assert loaded == order and terms  # GIST 下载失败只跳过它，已缓存的来源照常使用


@pytest.mark.parametrize("candidates", [(), ("silver chamber", "carrier")])
def test_extract_terms_with_fake_model(candidates):
    class Chat:
        model = "fake"

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            assert "标题：Synthetic study" in user and opts.thinking == "off"
            assert "第二行" in user and "最多 45 个" in user and "Transformer、BERT、ResNet" in user
            assert ("正文高频候选词" in user) is bool(candidates)
            if candidates:
                assert user.endswith("：silver chamber、carrier")
            return ChatResult(text="领域：测量\nAI公共词库：停用\ncarrier=载体")

        async def list_models(self) -> list[str]:
            return []

    got = asyncio.run(
        extract_terms(Chat(), title="Synthetic study", abstract="", intro="", headings=[], candidates=candidates)
    )
    assert got.domain == "测量" and not got.use_library and got.terms == {"carrier": "载体"}


def test_frequent_candidates_clean_and_merge_without_bridging_boundaries():
    repeated = (
        "<b>Silver</b> <i>chamber</i>. Chambers. Box. Boxes. Colony. Colonies. Tile. Tiles. "
        "Paper study section figure table method result results proposed based using shown used however therefore thus "
        "be been being respectively moreover furthermore additionally "
        "et al. The and A 48 10ms. frost, ring; frost <m1/> ring; frost 2 ring. Glass casing."
    )
    candidates = frequent_candidates([repeated] * 4 + ["silver chamber", "frost", "ring"])
    assert set(candidates) == {
        "Silver",
        "chamber",
        "Box",
        "Colony",
        "Tile",
        "frost",
        "ring",
        "Silver chamber",
        "Glass casing",
    }
    assert frequent_candidates(["glass casing"] * 2) == ()
    assert frequent_candidates(["glass casing"] * 3) == ("glass casing",)
    assert frequent_candidates(["glass"] * 5) == ("glass",)


def test_frequent_candidates_cap_ordinary_words_and_phrases():
    words = [f"cell{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(35)]
    candidates = frequent_candidates([f"shell {word}" for i, word in enumerate(words) for _ in range(i + 5)])
    assert len(candidates) == len(set(candidates)) == 40
    assert "cellbi" in candidates and "shell cellbi" in candidates
    assert "cellaa" not in candidates and "shell cellaa" not in candidates


def test_default_sources_filter_single_words_and_known_conflicts(tmp_path):
    assert sources.DEFAULT_ORDER == ["aitd", "d2l", "ml-technical-term", "ais-vocab", "computerese"]
    assert Settings.model_fields["glossary_sources"].default == ",".join(sources.DEFAULT_ORDER)
    ambiguous = [
        "Pool",
        "Operator",
        "Loop",
        "Instance",
        "Range",
        "Setting",
        "Actor",
        "Fluctuation",
        "structure",
        "parallel",
        "Integration",
        "Minimum",
        "support",
        "evaluation",
        "Optimization",
        "Example",
        "Size",
        "Factor",
        "Cost",
        "Mean",
        "Same",
        "Operation",
        "Attention",
    ]
    excluded = dict.fromkeys(ambiguous, "中文") | {
        "global average pooling": "全局平均汇聚",
        "Transformer architecture": "变换器架构",
        "actor network": "演员网络",
        "discount factor": "衰减系数",
        "actor-critic framework": "行动者-评论家框架",
    }
    allowed = {
        "softmax": "归一化指数",
        "1D convolution": "一维卷积",
        "discount factor": "折扣因子",
        "actor-critic framework": "行动者-评价者框架",
        "network aggregation": "网络汇聚",
        "DC converter": "直流变换器",
        "global average pooling": "全局平均池化",
    }
    assert sources.merge([excluded, allowed]) == allowed
    gist = sources.parse_ids("gist")
    (tmp_path / gist[0].cache_name).write_text(
        "Index,English,Chinese\n0,saddle point,鞍点\n1,Pool,汇聚\n", encoding="utf-8"
    )
    assert sources.load_all(gist, tmp_path) == ({"saddle point": "鞍点"}, gist)


def test_uppercase_acronyms_match_only_explicit_uppercase():
    matcher = TermMatcher({"CLIP": "模型", "BERT": "编码模型", "GAN": "生成对抗网络", "Feature map": "特征图"})
    assert matcher.match("clip rate, Clip rate, CliP rate, bert, gan, and FEATURE MAP.") == {"Feature map": "特征图"}
    assert matcher.match("CLIP, BERT, and GAN.") == {"CLIP": "模型", "BERT": "编码模型", "GAN": "生成对抗网络"}


def test_matcher_keeps_three_letter_acronyms_but_excludes_short_common_words():
    matcher = TermMatcher({"map": "映射", "set": "集合", "run": "运行", "GAN": "生成对抗网络"})
    assert matcher.match("map, set, run, and gan") == {}
    assert matcher.match("GAN") == {"GAN": "生成对抗网络"}


@pytest.mark.parametrize(
    "domain",
    [
        "工业工程：使用强化学习算法解决作业车间调度",
        "深度学习算法理论，应用于医学影像",
        "强化学习理论与 LLM 请求调度",
        "自然语言处理",
        "论文领域不明",
    ],
)
def test_library_gate_on_legacy_artifacts_and_missing_model_line(domain):
    legacy = Extraction.model_validate({"domain": domain, "terms": {"node": "节点"}})
    assert not legacy.use_library
    assert legacy.domain == domain and legacy.terms == {"node": "节点"}
    assert not parse(f"领域：{domain}\nnode=节点").use_library
    assert Extraction.model_validate({"domain": domain, "use_library": True}).use_library
    assert parse(f"领域：{domain}\nAI公共词库：启用\nnode=节点").use_library
    assert not parse(f"领域：{domain}\nAI公共词库：停用\nnode=节点").use_library


def test_lazy_english_self_map_filters_compounds_phrases_and_plurals_while_keeping_legit_terms():
    for source, target in [
        ("heavy-tailed", "heavy-tailed"),
        ("non-asymptotic", "non-asymptotic"),
        ("drop-in", "drop-in"),
        ("wall-clock", "wall-clock"),
        ("ad-hoc", "ad-hoc"),
        ("blood pressure", "blood pressure"),
        ("heavy-tailed noise", "heavy-tailed noise"),
        ("heavy-tailed noise", "heavy tailed noise"),
        ("sockets", "sockets"),
        ("sockets", "socket"),
        ("policies", "policy"),
        ("batches", "batch"),
    ]:
        assert is_lazy_english_self_map(source, target), f"should reject {(source, target)}"

    for source, target in [
        ("Transformer", "Transformer"),
        ("ResNet-50", "ResNet-50"),
        ("AdamW", "AdamW"),
        ("Seccomp Notify", "Seccomp Notify"),
        ("TCMs", "TCMs"),
        ("lxc-user-nic", "lxc-user-nic"),
        ("cgroups", "cgroups"),
        ("iptables", "iptables"),
        ("bypass4netns", "bypass4netns"),
        ("top-5", "top-5"),
        ("root", "root"),
        ("nerdctl", "nerdctl"),
        ("socket", "socket"),
        ("news outlets", "new outlet"),
        ("root privileges", "root 权限"),
        ("veth pair", "veth 对"),
    ]:
        assert not is_lazy_english_self_map(source, target), f"should keep {(source, target)}"


def test_parse_drops_lazy_self_maps_before_quota_and_keeps_prior_good_translation():
    assert "heavy-tailed" in EXTRACT_PROMPT
    assert "drop-in=drop-in" in EXTRACT_PROMPT
    assert "无法给出中文译法时省略该词条" in EXTRACT_PROMPT

    good_lines = [f"term_{i}=术语{i}" for i in range(44)]
    text = "\n".join(
        [
            "领域：深度学习优化",
            "AI公共词库：启用",
            "heavy-tailed=heavy-tailed",
            "non-asymptotic=non-asymptotic",
            "wall-clock=wall-clock",
            "drop-in=即插即用",
            "drop-in=drop-in",
            "lxc-user-nic=lxc-user-nic",
            *good_lines,
        ]
    )
    found = parse(text)
    assert len(found.terms) == 45
    assert found.terms["drop-in"] == "即插即用"
    assert found.terms["lxc-user-nic"] == "lxc-user-nic"
    assert "heavy-tailed" not in found.terms
    assert "term_42" in found.terms and "term_43" not in found.terms


def test_normalize_terms_blocks_fake_uppercase_bypass_and_protects_good_translations():
    body = [
        "We use sockets and drop-in with heavy-tailed noise, blood pressure, and lxc-user-nic in Seccomp Notify.",
    ]
    normalized = normalize_terms(
        {
            "Sockets": "Socket",
            "Blood Pressure": "Blood Pressure",
            "Drop-In": "即插即用",
            "Drop-in": "drop-in",
            "Heavy-Tailed Noise": "Heavy-Tailed Noise",
            "lxc-user-nic": "lxc-user-nic",
            "Seccomp Notify": "Seccomp Notify",
        },
        body,
    )
    assert normalized == {
        "drop-in": "即插即用",
        "lxc-user-nic": "lxc-user-nic",
        "Seccomp Notify": "Seccomp Notify",
    }
    # 即使正文仅单数胜出，只要原输入含复数证据仍应拦截。
    assert normalize_terms({"Sockets": "socket"}, ["We use socket."]) == {}
    assert normalize_terms({"Sockets": "Socket"}, ["We use sockets."]) == {}


def test_term_selection_filters_legacy_lazy_self_maps_lets_library_fill_and_keeps_user_terms():
    library = TermMatcher({"drop-in": "即插即用"})
    found = Extraction(
        terms={
            "drop-in": "drop-in",
            "heavy-tailed": "heavy-tailed",
            "lxc-user-nic": "lxc-user-nic",
        },
        use_library=True,
    )
    selection = TermSelection(found, {}, lambda: library)
    assert selection.paper == {"lxc-user-nic": "lxc-user-nic"}
    assert selection.match_segment("A drop-in module with lxc-user-nic.") == (
        {},
        {"lxc-user-nic": "lxc-user-nic"},
        {"drop-in": "即插即用"},
    )

    # 用户显式指定的自映射不受自动抽取过滤影响，且优先于公共词库。
    user_selection = TermSelection(found, {"drop-in": "drop-in"}, lambda: library)
    assert user_selection.match_segment("A drop-in module.") == ({"drop-in": "drop-in"}, {}, {})

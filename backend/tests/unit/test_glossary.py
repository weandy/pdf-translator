import asyncio
from pathlib import Path

from pdftr.domain.llm import ChatOptions, ChatResult
from pdftr.glossary import sources
from pdftr.glossary.extract import extract_terms, parse
from pdftr.glossary.gist import parse as parse_gist
from pdftr.glossary.matcher import TermMatcher
from pdftr.glossary.normalize import normalize_terms


def test_parse_extraction():
    text = "领域：机器学习优化\n- ADAM=ADAM\nstochastic optimization=随机优化\n乱写的一行\n"
    assert parse(text) == ("机器学习优化", {"ADAM": "ADAM", "stochastic optimization": "随机优化"})


def test_normalize_terms():
    body = "We propose Adam. Adam uses moment estimates. RELATED WORK is not a term here."
    terms = {"ADAM": "ADAM", "moment estimates": "矩估计", "Related Work": "相关工作", "missing term": "缺失"}
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


def test_extract_terms_with_fake_model():
    class Chat:
        model = "fake"

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            assert "标题：Adam" in user and opts.thinking == "off"
            return ChatResult(text="领域：优化\nAdam=Adam")

    got = asyncio.run(extract_terms(Chat(), title="Adam", abstract="abs", intro="intro", headings=["1 Intro"]))
    assert got.domain == "优化" and got.terms == {"Adam": "Adam"}

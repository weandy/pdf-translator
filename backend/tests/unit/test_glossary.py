import asyncio
from pathlib import Path

from pdftr.domain.llm import ChatOptions, ChatResult
from pdftr.glossary.extract import extract_terms, parse
from pdftr.glossary.gist import GistSource, load
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


def test_gist_load_and_lookup(tmp_path: Path):
    csv = tmp_path / "zh.csv"
    csv.write_text("Index,English,Chinese\n0,dropout,随机失活\n1,Dropout,丢弃法\n2,,空\n", encoding="utf-8")
    terms = load(csv)
    assert terms == {"dropout": "随机失活", "Dropout": "丢弃法"}
    src = GistSource(terms)
    assert src.lookup(["DROPOUT", "unknown"]) == {"DROPOUT": "丢弃法"}


def test_extract_terms_with_fake_model():
    class Chat:
        model = "fake"

        async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
            assert "标题：Adam" in user and opts.thinking == "off"
            return ChatResult(text="领域：优化\nAdam=Adam")

    got = asyncio.run(extract_terms(Chat(), title="Adam", abstract="abs", intro="intro", headings=["1 Intro"]))
    assert got.domain == "优化" and got.terms == {"Adam": "Adam"}

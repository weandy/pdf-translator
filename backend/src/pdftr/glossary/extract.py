"""用一次小的模型调用抽取文档术语与领域（S10 的 TERMS_V3；参考 MAPS 让模型先给出主题）。

模型只读标题、摘要、正文开头、章节标题和本地统计出的有界候选词。
"""

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, Field

from pdftr.domain.llm import ChatModel, ChatOptions, ChatUsage
from pdftr.domain.text import RICH_TAG, normalize_term_key
from pdftr.glossary.matcher import HORIZONTAL_SPACE, TERM_TOKEN, mask_term_boundaries

log = logging.getLogger(__name__)
_PHRASE_GAP = re.compile(rf"{HORIZONTAL_SPACE}+")
_MEASUREMENT = re.compile(r"\d+(?:[.,]\d+)?(?:ns|μs|µs|ms|s|min|hz|khz|mhz|ghz|mm|cm|km|kg|mg|g|ml|kwh)", re.I)
_ASCII_LETTER = re.compile(r"[A-Za-z]")
_UPPER = re.compile(r"[A-Z]")
_GATE_LINE = re.compile(r"AI\s*公共词库\s*[:：]\s*(.*)", re.I)
_GATE_VALUE = re.compile(r"(启用|停用)(?:\s*(?:\([^（）()\r\n]*\)|（[^（）()\r\n]*）))?")
_MAX_CANDIDATES = 60
_MAX_CANDIDATE_CHARS = 1200
_MAX_TERM_LENGTH = 80
_STOP_WORDS = frozenset({
    "about", "above", "across", "after", "again", "against", "all", "also", "am", "among", "an", "and", "any",
    "are", "around", "as", "at", "be", "been", "before", "behind", "being", "below", "between", "both",
    "but", "by", "can", "cannot",
    "could", "did", "do", "does", "doing", "done", "down", "during", "each", "either", "else", "enough",
    "even", "ever", "every", "few", "for", "former", "from", "further", "get", "got", "had", "has", "have",
    "having", "hence", "her", "here", "hers", "him", "his", "how", "if", "in", "into", "is", "it", "its",
    "just", "latter", "least", "less", "like", "many", "may", "me", "might", "more", "most", "much", "must",
    "my", "near", "neither", "no", "nor", "not", "now", "of", "off", "often", "on", "once", "one", "only",
    "onto", "or", "other", "others", "ought", "our", "ours", "out", "over", "per", "same", "several", "shall",
    "she", "should", "since", "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "then",
    "there", "these", "they", "this", "those", "through", "throughout", "till", "to", "together", "too",
    "toward", "under", "until", "up", "upon", "us", "very", "via", "was", "we", "were", "what", "whatever",
    "when", "where", "whether", "which", "while", "who", "whom", "whose", "why", "will", "with", "within",
    "without", "would", "yet", "you", "your", "yours", "al", "et", "paper", "papers", "study", "studies",
    "section", "sections", "figure", "figures", "fig", "table", "tables", "method", "methods", "result",
    "results", "proposed", "based", "using", "use", "uses", "used", "shown", "shows", "show", "however",
    "therefore", "thus", "example", "examples", "respectively", "moreover", "furthermore", "additionally",
})  # fmt: skip

PROMPT = """阅读下面的论文信息，按顺序输出：
1. 第一行输出「领域：……」，用一句话概括论文所属学科与研究主题。
2. 第二行输出「AI公共词库：启用」或「AI公共词库：停用」。
   仅当论文主领域为 AI、机器学习、深度学习、自然语言处理、计算机视觉、强化学习的算法理论，
   或计算机科学核心方向时启用。工业工程、运筹调度、物理、生物医学、材料、金融、社科等应用或非 AI 学科停用，
   即使这些论文采用了 AI 方法。
3. 之后抽取需要全文统一译法的专业术语，最多 45 个，每行「英文=中文」：
   - 收入概念、方法、指标，以及本文高频出现、有多种译法的核心领域对象词（如 job、instance、operation、agent、node）。
     不收章节名和与本文领域无关的普通词汇。
   - 按术语在摘要或正文中的写法输出，避免沿用标题的大写格式。
   - 模型、架构（如 Transformer、BERT、ResNet）及应保留原文的方法和数据集名，输出「英文=英文」。
   - 使用中文学界通行译法。
不要输出其他内容。

标题：{title}
摘要：{abstract}
正文开头：{intro}
章节标题（仅供判断领域）：{headings}"""


def frequent_candidates(texts: Sequence[str]) -> tuple[str, ...]:
    """统计段内实词与紧邻二、三元词组，按频次和首次出现位置排序。"""
    forms: dict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
    for text in texts:
        visible = mask_term_boundaries(RICH_TAG.sub("", text))
        tokens = list(TERM_TOKEN.finditer(visible))
        keys = [_candidate_key(m[0]) for m in tokens]
        for i, token in enumerate(tokens):
            key = keys[i]
            if not key:
                continue
            window = (key,)
            forms[window][token[0]] += 1
            for j in range(i + 1, min(i + 3, len(tokens))):
                next_key = keys[j]
                gap = visible[tokens[j - 1].end() : tokens[j].start()]
                if not next_key or next_key in window or not _PHRASE_GAP.fullmatch(gap):
                    break
                window += (next_key,)
                written = " ".join(visible[token.start() : tokens[j].end()].split())
                if len(written) <= _MAX_TERM_LENGTH:
                    forms[window][written] += 1

    ranked: list[tuple[str, int, str]] = []
    for keys, spellings in forms.items():
        written = spellings.most_common(1)[0][0]
        count = spellings.total()
        group = _candidate_group(keys, written, count)
        if group is not None:
            ranked.append((written, count, group))
    ranked.sort(key=lambda item: -item[1])

    # 先为末章的低频技术词扣除预算，最终展示仍按全文频次排序。
    quotas = {"word": 20, "technical": 10, "phrase": 25, "rare": 5}
    remaining = _MAX_CANDIDATE_CHARS + 1  # 第一条之前没有顿号。
    selected: set[str] = set()
    for written, _, group in sorted(ranked, key=lambda item: item[2] != "rare"):
        cost = len(written) + 1
        if quotas[group] and len(selected) < _MAX_CANDIDATES and cost <= remaining and written not in selected:
            selected.add(written)
            remaining -= cost
            quotas[group] -= 1
    return tuple(dict.fromkeys(written for written, _, _ in ranked if written in selected))


def _candidate_key(word: str) -> str:
    if not 2 <= len(word) <= _MAX_TERM_LENGTH or not _ASCII_LETTER.search(word) or _MEASUREMENT.fullmatch(word):
        return ""
    key = normalize_term_key(word)
    return "" if word.lower() in _STOP_WORDS or key.removeprefix("word:") in _STOP_WORDS else key


def _candidate_group(keys: tuple[str, ...], written: str, count: int) -> str | None:
    if len(keys) > 1:
        return "phrase" if count >= 3 else None
    if not keys:
        return None
    key = keys[0]
    acronym = key.startswith("acr:")
    mixed_case = not acronym and not written.isupper() and bool(_UPPER.search(written[1:]))
    strong = "@" in written or "-" in written or mixed_case
    technical = strong or acronym or key.startswith("lit:") or any(ch.isdigit() for ch in written)
    if technical:
        if count >= 2:
            return "technical"
        return "rare" if strong else None
    return "word" if count >= 5 else None


class Extraction(BaseModel):
    domain: str = ""
    terms: dict[str, str] = Field(default_factory=dict)
    use_library: bool = False
    usage: ChatUsage = Field(default_factory=ChatUsage)


def parse(text: str, *, truncated: bool = False) -> Extraction:
    domain, terms = "", {}
    use_library = False
    lines = [line for line in text.splitlines() if line.strip()]
    for raw in lines[:-1] if truncated else lines:
        line = raw.strip().lstrip("-*• ").strip()
        if m := re.match(r"^领域\s*[:：]\s*(.+)$", line):
            domain = m.group(1).strip()
        elif m := _GATE_LINE.fullmatch(line):
            value = _GATE_VALUE.fullmatch(m.group(1))
            use_library = value is not None and value.group(1) == "启用"
        elif "=" in line:
            k, v = (p.strip() for p in line.split("=", 1))
            if k and v and len(k) <= _MAX_TERM_LENGTH:
                terms[k] = v
    return Extraction(domain=domain, terms=terms, use_library=use_library)


async def extract_terms(
    chat: ChatModel, *, title: str, abstract: str, intro: str, headings: list[str], candidates: Sequence[str] = ()
) -> Extraction:
    prompt = PROMPT.format(title=title, abstract=abstract, intro=intro[:1500], headings="; ".join(headings))
    if candidates:
        prompt += (
            "\n正文高频候选词（仅供参考，仅筛选需要全文统一译法的领域术语与高频核心对象词，忽略普通词汇）："
            + "、".join(candidates)
        )
    result = await chat.complete("你是学术术语专家。", prompt, ChatOptions(max_tokens=1500))
    truncated = result.finish_reason in ("length", "max_tokens")
    found = (
        Extraction()
        if result.finish_reason in ("refusal", "content_filter")
        else parse(result.text, truncated=truncated)
    )
    if truncated:
        log.info("术语抽取截断，保留 %d 条", len(found.terms))
    found.usage = result.usage
    return found

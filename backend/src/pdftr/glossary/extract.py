"""用一次小的模型调用抽取文档术语与领域（S10 的 TERMS_V3；参考 MAPS 让模型先给出主题）。

模型只读标题、摘要、正文开头、章节标题和本地统计出的有界候选词。
"""

import re
from collections import Counter
from collections.abc import Sequence
from itertools import pairwise

from pydantic import BaseModel, Field

from pdftr.domain.llm import ChatModel, ChatOptions, ChatUsage
from pdftr.domain.text import RICH_TAG

_WORD = re.compile(r"(?<!\w)[a-z]{2,}(?!\w)")
_FORMULA = re.compile(r"<m\d+/>")
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
    """统计全文的高频实词及相邻词组，公式、标点与段落边界阻断词组。"""
    cleaned = [_FORMULA.sub(".", RICH_TAG.sub("", text)).lower() for text in texts]
    counts = Counter(word for text in cleaned for word in _WORD.findall(text) if word not in _STOP_WORDS)
    normalized: dict[str, str] = {}
    unigrams: Counter[str] = Counter()
    for word, count in counts.items():
        forms: list[str] = []
        if word.endswith("ies"):
            forms.append(word[:-3] + "y")
        if re.search(r"(?:s|x|z|ch|sh|o)es$", word):
            forms.append(word[:-2])
        if word.endswith("s") and not word.endswith(("ss", "us", "is")):
            forms.append(word[:-1])
        singular = next((form for form in forms if form in counts), word)
        normalized[word] = singular
        unigrams[singular] += count

    bigrams: Counter[str] = Counter()
    for text in cleaned:
        for left, right in pairwise(_WORD.finditer(text)):
            if left[0] in normalized and right[0] in normalized and text[left.end() : right.start()].isspace():
                bigrams[f"{normalized[left[0]]} {normalized[right[0]]}"] += 1
    return tuple(word for word, count in unigrams.most_common(25) if count >= 5) + tuple(
        phrase for phrase, count in bigrams.most_common(25) if count >= 3
    )


class Extraction(BaseModel):
    domain: str = ""
    terms: dict[str, str] = Field(default_factory=dict)
    use_library: bool = False
    usage: ChatUsage = Field(default_factory=ChatUsage)


def parse(text: str) -> Extraction:
    domain, terms = "", {}
    use_library = False
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if m := re.match(r"^领域\s*[:：]\s*(.+)$", line):
            domain = m.group(1).strip()
        elif m := re.fullmatch(r"AI\s*公共词库\s*[:：]\s*(启用|停用)", line, re.I):
            use_library = m.group(1) == "启用"
        elif "=" in line:
            k, v = (p.strip() for p in line.split("=", 1))
            if k and v and len(k) <= 80:
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
    found = parse(result.text)
    found.usage = result.usage
    return found

"""用一次小的模型调用抽取文档术语与领域（S10 的 TERMS_V3；参考 MAPS 让模型先给出主题）。

模型只读标题、摘要、正文开头、章节标题和本地统计出的有界候选词。
"""

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

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
_MAX_EXTRACTED_TERMS = 45
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
    "work", "works", "approach", "approaches", "different", "large", "small", "high", "low", "term", "terms",
    "element", "elements", "description", "descriptions",
})  # fmt: skip
# 高频通用学术单词：不当作孤立单词候选（避免淹没配额），但仍允许参与构成多词专业术语（如 residual network）。
_GENERIC_UNIGRAM_WORDS = frozenset({
    "model", "models", "network", "networks", "net", "nets", "layer", "layers", "deep", "deeper", "depth",
    "training", "learning", "problem", "problems", "error", "errors", "accuracy",
    "complexity", "plain", "optimize", "optimization", "performance", "communication", "dataset", "datasets",
})  # fmt: skip
# 允许保持全小写以 s 结尾自映射的常见协议/系统/工具名。
_PLURAL_SELF_MAP_ALLOWLIST = frozenset({
    "atlas", "blas", "cgroups", "emacs", "https", "iptables", "keras", "kubernetes",
    "linux", "macos", "nexus", "pandas", "postgres", "procfs", "prometheus", "redis",
    "rootfs", "sysfs", "tmpfs",
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
   - 仅在操作系统/Linux 权限语境下保留 root、sudo、系统调用名与命令行标识符原文
     （如 root privileges=root 权限、root user=root 用户）；树/图/数据结构中的 root node 仍译为「根节点」。
   - 普通可译名词（如 sockets、layers、containers、nodes）必须译为规范中文，
     严禁保留英文单复数自映射（如禁止 sockets=sockets、sockets=socket）。
   - 常规临床/医学名词（如 Blood Pressure、Uterine contractions）应译为规范中文而非保留英文。
   - 严禁输出不伦不类的半中半英目标译法（如 Text信息片段）。
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
    quotas = {"word": 8, "technical": 15, "phrase": 32, "rare": 5}
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
    if written.lower() in _GENERIC_UNIGRAM_WORDS or key.removeprefix("word:") in _GENERIC_UNIGRAM_WORDS:
        return None
    return "word" if count >= 5 else None


_DEF_MINOR_WORDS = frozenset({
    "of", "for", "and", "in", "to", "the", "on", "with", "by", "from", "at", "as", "or", "a", "an", "via", "into",
})  # fmt: skip
_DEF_SKIP_FIRST = _DEF_MINOR_WORDS | frozenset({
    "see", "we", "our", "this", "that", "these", "those", "call", "called", "named", "using", "use", "used",
    "each", "every", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
})  # fmt: skip
_DEF_PAREN = re.compile(r"\(([^()\r\n]{2,80})\)")
_DEF_CLAUSE_SPLIT = re.compile(r"[.,;:!?()\[\]{}—–\r\n]+")
_DEF_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9']*$|^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$")
_DEF_PART_SPLIT = re.compile(r"[\s-]+")
_ROMAN_ONLY = re.compile(r"^[IVX]+$")
_CAMEL_LOWER_START = re.compile(r"^[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*$")


def _normalize_acronym(raw: str) -> str | None:
    if not 2 <= len(raw) <= 8 or not re.fullmatch(r"[A-Za-z0-9]+", raw):
        return None
    stem = raw[:-1] if len(raw) >= 3 and raw.endswith("s") and raw[-2].isupper() else raw
    if _ROMAN_ONLY.fullmatch(stem):
        return None
    uppers = sum(ch.isupper() for ch in stem)
    if uppers >= 2 or (uppers >= 1 and _CAMEL_LOWER_START.fullmatch(stem) is not None):
        return stem
    return None


def _acronym_segments(stem: str) -> list[str]:
    """将缩写按首字母或驼峰段拆成待匹配片段（如 TCM→[t,c,m]、ResNet→[res,net]、qGrams→[q,grams]）。"""
    segs: list[str] = []
    i = 0
    while i < len(stem):
        j = i + 1
        if stem[i].isupper():
            while j < len(stem) and stem[j].islower():
                j += 1
        segs.append(stem[i:j].lower())
        i = j
    return segs


def _matches_acronym(stem: str, full_name: str) -> bool:
    if not full_name or len(full_name) > 80 or re.search(r"[.,;:!?=()\[\]{}]", full_name):
        return False
    raw_words = full_name.split()
    if not 2 <= len(raw_words) <= 7 or not all(_DEF_WORD.fullmatch(w) for w in raw_words):
        return False
    parts = [p for p in _DEF_PART_SPLIT.split(full_name) if p]
    if not 2 <= len(parts) <= 7:
        return False
    if parts[0].lower() in _DEF_SKIP_FIRST or parts[-1].lower() in _DEF_MINOR_WORDS:
        return False
    content_count = sum(p.lower() not in _DEF_MINOR_WORDS for p in parts)
    if not 2 <= content_count <= 6:
        return False
    segs = _acronym_segments(stem)
    low_parts = [p.lower() for p in parts]
    n_segs, n_parts = len(segs), len(low_parts)

    def dfs(si: int, wi: int, skipped_content: int, matched_count: int, matched_last: bool) -> bool:
        if si == n_segs and wi == n_parts:
            return matched_last and matched_count >= 2
        if wi >= n_parts:
            return False
        word = low_parts[wi]
        if si < n_segs:
            seg = segs[si]
            # 整段匹配词首前缀（如 Res→Residual、Net→Network、T→Typed）。
            if word.startswith(seg) and dfs(si + 1, wi + 1, skipped_content, matched_count + 1, wi == n_parts - 1):
                return True
            # 允许混合大小写词内首字母后剩余部分继续归下一个词（仅当本词仅匹配首字母时）。
            if (
                len(seg) > 1
                and word.startswith(seg[0])
                and wi + 1 < n_parts
                and low_parts[wi + 1].startswith(seg[1:])
                and dfs(si + 1, wi + 2, skipped_content, matched_count + 2, wi + 1 == n_parts - 1)
            ):
                return True
        if 0 < wi < n_parts - 1:
            if word in _DEF_MINOR_WORDS:
                if dfs(si, wi + 1, skipped_content, matched_count, matched_last):
                    return True
            elif skipped_content < 1 and dfs(si, wi + 1, skipped_content + 1, matched_count, matched_last):
                return True
        return False

    return dfs(0, 0, 0, 0, False)


def extract_acronym_definitions(
    texts: Sequence[str], candidates: Sequence[str] = (), *, limit: int = 15
) -> dict[str, str]:
    """从正文中提取显式缩写定义（Full Name (ABBR) 与 ABBR (Full Name)），按候选词优先截断。"""
    if limit <= 0:
        return {}
    found: dict[str, str] = {}
    for text in texts:
        visible = mask_term_boundaries(RICH_TAG.sub("", text))
        for match in _DEF_PAREN.finditer(visible):
            inside = " ".join(match.group(1).split())
            before = visible[: match.start()].rstrip()
            if not inside or not before:
                continue
            # 形式 1: Full Name (ABBR)
            if (stem := _normalize_acronym(inside)) is not None:
                if stem in found:
                    continue
                clause = _DEF_CLAUSE_SPLIT.split(before)[-1].strip()
                words = clause.split()
                for size in range(2, min(len(words), 7) + 1):
                    cand_full = " ".join(words[-size:])
                    if _matches_acronym(stem, cand_full):
                        found[stem] = cand_full
                        break
                continue
            # 形式 2: ABBR (Full Name)
            prev_match = re.search(r"(?<![A-Za-z0-9-])([A-Za-z0-9]{2,8})$", before)
            if prev_match is None:
                continue
            stem = _normalize_acronym(prev_match.group(1))
            if stem is None or stem in found:
                continue
            if _matches_acronym(stem, inside):
                found[stem] = inside
    if not found:
        return {}
    cand_keys = {normalize_term_key(c) for c in candidates if c}
    prioritized = [k for k in found if normalize_term_key(k) in cand_keys]
    rest = [k for k in found if normalize_term_key(k) not in cand_keys]
    ordered = (prioritized + rest)[:limit]
    return {k: found[k] for k in ordered}


class Extraction(BaseModel):
    domain: str = ""
    terms: dict[str, str] = Field(default_factory=dict)
    use_library: bool = False
    usage: ChatUsage = Field(default_factory=ChatUsage)


def _skip_extracted_term(k: str, v: str) -> bool:
    """过滤摘要冒出的通用孤立学术泛词与普通小写英文单复数自映射（如 sockets=sockets、sockets=socket）。"""
    if " " not in k and "-" not in k:
        key = normalize_term_key(k)
        if not key.startswith("acr:") and (
            k.lower() in _GENERIC_UNIGRAM_WORDS or key.removeprefix("word:") in _GENERIC_UNIGRAM_WORDS
        ):
            return True
    return (
        k.isascii()
        and v.isascii()
        and k.islower()
        and v.islower()
        and k.isalpha()
        and v.isalpha()
        and len(k) >= 5
        and (k.endswith("s") or v.endswith("s"))
        and k not in _PLURAL_SELF_MAP_ALLOWLIST
        and v not in _PLURAL_SELF_MAP_ALLOWLIST
        and normalize_term_key(k) == normalize_term_key(v)
    )


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
            if (
                k
                and v
                and len(k) <= _MAX_TERM_LENGTH
                and not _skip_extracted_term(k, v)
                and (k in terms or len(terms) < _MAX_EXTRACTED_TERMS)
            ):
                terms[k] = v
    return Extraction(domain=domain, terms=terms, use_library=use_library)


async def extract_terms(
    chat: ChatModel,
    *,
    title: str,
    abstract: str,
    intro: str,
    headings: list[str],
    candidates: Sequence[str] = (),
    definitions: Mapping[str, str] | None = None,
    user_terms: Mapping[str, str] | None = None,
) -> Extraction:
    prompt = PROMPT.format(title=title, abstract=abstract, intro=intro[:1500], headings="; ".join(headings))
    if candidates:
        prompt += (
            "\n正文高频候选词（仅供参考，仅筛选需要全文统一译法的领域术语与高频核心对象词，忽略普通词汇）："
            + "、".join(candidates)
        )
    if definitions:
        formatted_defs = "；".join(f"{k} = {v}" for k, v in definitions.items() if k.strip() and v.strip())
        if formatted_defs:
            prompt += (
                "\n文中明确定义的缩写（若收入相关缩写或其复数形式，必须严格依据正文定义翻译或保留原文，"
                "严禁按外部常见含义臆测）：" + formatted_defs
            )
    if user_terms:
        formatted = "；".join(f"{k}={v}" for k, v in user_terms.items() if k.strip() and v.strip())
        if formatted:
            prompt += "\n用户已指定术语（若抽取包含这些词的复合术语，译法须与之保持一致）：" + formatted
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

"""翻译用例（docs/03-mvp-design.md §3.5）。

查缓存 → 相同原文去重 → 按 token 预算分批 → 并发调用模型 → 校验编号与占位符
→ 不合格的段落单独重试 → 写缓存、汇总用量与费用。
鉴权失败、余额不足直接抛出；连接类临时错误纳入有次数和时限的单段重试。
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from pdftr.domain.ir import BlockType, Segment, SegmentStatus
from pdftr.domain.llm import ChatError, ChatModel, ChatOptions, ChatUsage
from pdftr.domain.text import RICH_TAG, normalize_term_key
from pdftr.translation.batching import (
    BATCH_TOKENS,
    VALIDATION_VERSION,
    NumberedResponseError,
    bold_problems,
    citation_problems,
    estimate_tokens,
    italic_problems,
    make_batches,
    normalize_cjk_spacing,
    normalize_formula_placeholders,
    parse_numbered,
    percentage_problems,
    placeholder_problems,
    prose_problems,
    rich_text_problems,
    script_problems,
    script_value_problems,
)
from pdftr.translation.cost import Price, add_cost
from pdftr.translation.openai_compat import FallbackChat
from pdftr.translation.port import TranslationCache
from pdftr.translation.prompt import (
    PROMPT_VERSION,
    DocContext,
    SegmentPromptMeta,
    expand_formulas,
    system_prompt,
    user_prompt,
)
from pdftr.translation.segmenter import BAD_SOURCE_MESSAGE, bad_source

Quality = Literal["default", "careful"]
TermLookup = Callable[[str], dict[str, str]]
TermHitsLookup = Callable[[str], tuple[dict[str, str], dict[str, str], dict[str, str]]]
Progress = Callable[[int, int, str], None]  # （已完成, 总数, 补充说明；平常为空）
log = logging.getLogger(__name__)


def default_concurrency(base_url: str) -> int:
    """按服务商选默认并发数：DeepSeek 官方不限并发，用 4 路；本机模型（Ollama 等）通常只有一块显卡，
    并发反而更慢，用 1 路；其他服务商限额差别大，保守用 2 路。部署者可用 PDFTR_LLM_CONCURRENCY 覆盖。"""
    host = (urlparse(base_url).hostname or "").lower()
    if host == "deepseek.com" or host.endswith(".deepseek.com"):
        return 4
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal") or host.endswith(".local"):
        return 1
    return 2


class TranslateOptions(BaseModel):
    quality: Quality = "default"  # careful = 精翻（思考·low）
    concurrency: int = Field(default=4, ge=1)
    batch_tokens: int = Field(default=BATCH_TOKENS, ge=1)
    max_attempts: int = Field(default=10, ge=1, le=10)  # 包含首次批量翻译，避免 SDK 与业务重试相乘
    request_timeout_s: float = Field(default=30, gt=0)
    retry_timeout_s: float = Field(default=120, gt=0)  # 首批返回后的全部补救轮次共用，不逐段重新计时
    total_timeout_s: float | None = Field(default=None, gt=0)  # 同步重翻还要覆盖首批和排队，早于网关超时返回
    custom_instruction: str = ""
    use_cache: bool = True  # 单段“重翻”时为 False：用户要的是另一个译文
    price_overrides: Mapping[str, Price] | None = None


class TranslateReport(BaseModel):
    total: int = 0
    translated: int = 0
    cached: int = 0
    failed: int = 0
    requests: int = 0
    usage: ChatUsage = Field(default_factory=ChatUsage)
    cost_cny: float | None = None
    failures: dict[str, str] = Field(default_factory=dict)
    attempts: dict[str, int] = Field(default_factory=dict)
    used_fallback: bool = False
    fallback_models: list[str] = Field(default_factory=list)


_PROTOCOL_TAG = re.compile(r"</?translations\b[^>]*>", re.I)
# 先匹配完整 XML/HTML 风格标签并提取完整标签名（允许 : 与 .），仅当完整标签名严格匹配 ^m\d+$ 时再做公式占位符检查
_GENERIC_TAG = re.compile(r"<(?P<close>/)?\s*(?P<name>[A-Za-z0-9_.:-]+)(?P<rest>[^<>\r\n]*)>")
_FORMULA_NAME = re.compile(r"^m\d+$", re.I)
_VALID_FORMULA_TAG = re.compile(r"^<m\d+/>$")
_OPEN_TAG_ATTRS = re.compile(r'^(?:\s+[A-Za-z_:][\w:.-]*(?:\s*=\s*(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^<>\s"\']+))?)*\s*$')
_MATH_LOGIC_OP = re.compile(r"^\s+(?:and|or)\b", re.I)
_FORMULA_FOLLOWED_BY_EQ = re.compile(r"<(m\d+)/>\s*[=＝]")
_PAREN_NUM_BEFORE_FORMULA = re.compile(r"\(\s*(\d+(?:\.\d+)?)\s*[+\-−×*/]?\s*<(m\d+)/>\s*\)")
_PAREN_NUM_AFTER_FORMULA = re.compile(r"\(\s*<(m\d+)/>\s*[+\-−×*/]?\s*(\d+(?:\.\d+)?)\s*\)")
_TRAILING_LATEX_NOISE = re.compile(r"(?:\s|\\(?:sf|rm|bf|it|tt|quad|qquad|enspace|thinspace|,|;|!|:|\s))+$")
_TRAILING_COMPARATOR = re.compile(r"(?:\\(?:geq|ge|leq|le|neq|ne|approx|equiv)(?![A-Za-z])|[><≥≤≠])$")
_TRAILING_BRACED_COMPARATOR = re.compile(r"(?<![_^])\{\s*([^\{\}]+?)\s*\}$")
_SPLITTING_ADVERBIAL_HEADS = frozenset({"在", "于", "对", "当"})
_LEAKED_LATEX_CMD = re.compile(
    r"\\(?:bar|tilde|hat|vec|dot|ddot|widehat|widetilde|overline|underline|"
    r"mathbb|mathcal|mathsf|mathbf|mathrm|mathit|mathtt|mathfrak|operatorname|"
    r"frac|dfrac|tfrac|sum|int|prod|sqrt|partial|nabla|infty|forall|exists|"
    r"alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|iota|kappa|"
    r"lambda|mu|nu|xi|pi|rho|sigma|tau|upsilon|phi|varphi|chi|psi|omega|"
    r"Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Phi|Psi|Omega|"
    r"leq|le|geq|ge|neq|ne|approx|equiv|sim|simeq|times|cdot|pm|mp|"
    r"subset|subseteq|supset|supseteq|in|notin|cup|cap|"
    r"to|rightarrow|leftarrow|Rightarrow|Leftarrow|mapsto|colon)(?![A-Za-z])"
)
_HIGH_FORMULA_THRESHOLD = 5
_HIGH_FORMULA_SOLO_THRESHOLD = 10
_HIGH_FORMULA_MAX_SEGMENTS = 4
_HIGH_FORMULA_MAX_FORMULAS = 18
_THINKING_MIN_TIMEOUT_S = 60.0
_THINKING_TIMEOUT_SCALE_THRESHOLD_S = 5.0


def _latex_has_top_level_equals(latex: str) -> bool:
    """判断 LaTeX 外层（非花括号内部，如 \\sum_{i=1} 下标）是否已包含等号。"""
    depth = 0
    i = 0
    n = len(latex)
    while i < n:
        ch = latex[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
        elif ch == "=" and depth == 0:
            return True
        i += 1
    return False


def _duplicate_formula_equals_problems(source: str, target: str, formulas: Mapping[str, str] | None) -> list[str]:
    if not formulas:
        return []
    src_counts: dict[str, int] = {}
    for m in _FORMULA_FOLLOWED_BY_EQ.finditer(source):
        key = m.group(1)
        src_counts[key] = src_counts.get(key, 0) + 1
    tgt_counts: dict[str, int] = {}
    for m in _FORMULA_FOLLOWED_BY_EQ.finditer(target):
        key = m.group(1)
        tgt_counts[key] = tgt_counts.get(key, 0) + 1
    problems: list[str] = []
    for key, count in tgt_counts.items():
        if count <= src_counts.get(key, 0):
            continue
        latex = formulas.get(key, "")
        if latex and _latex_has_top_level_equals(latex):
            problems.append(f"公式 <{key}/> 已包含等号，请勿在占位符后重复写出等号和数值")
    return problems


def _adjacent_formula_number_problems(source: str, target: str) -> list[str]:
    """检查源文括号内紧邻公式占位符的字面数值（如 (1 <m5/>)）在译文同侧 6 非空字符内是否保留。"""
    src_plain = RICH_TAG.sub("", source)
    tgt_plain = RICH_TAG.sub("", target)
    checks: list[tuple[str, str, str, str]] = []
    for match in _PAREN_NUM_BEFORE_FORMULA.finditer(src_plain):
        checks.append((match.group(1), match.group(2), "before", match.group(0)))
    for match in _PAREN_NUM_AFTER_FORMULA.finditer(src_plain):
        checks.append((match.group(2), match.group(1), "after", match.group(0)))
    if not checks:
        return []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for num, key, side, matched_text in checks:
        if (key, num) in seen:
            continue
        token = f"<{key}/>"
        num_pat = re.compile(rf"(?<!\d){re.escape(num)}(?!\d)")
        found = False
        pos = tgt_plain.find(token)
        while pos >= 0:
            if side == "before":
                segment = _CONTEXT_FORMULA.sub("\x00", tgt_plain[:pos]).rsplit("\x00", 1)[-1]
                window = "".join(segment.split())[-6:]
            else:
                segment = _CONTEXT_FORMULA.sub("\x00", tgt_plain[pos + len(token) :]).split("\x00", 1)[0]
                window = "".join(segment.split())[:6]
            if num_pat.search(window):
                found = True
                break
            pos = tgt_plain.find(token, pos + len(token))
        if not found:
            seen.add((key, num))
            problems.append(f"请保留公式 <{key}/> 旁的数值 {num}（原文写作 {matched_text}）")
    return problems


def _ends_with_comparator(latex: str) -> bool:
    """剥离末尾空白、裸字体开关与纯外层花括号后，判断公式是否以二元比较符结尾。"""
    s = latex.strip()
    while s:
        prev = s
        s = _TRAILING_LATEX_NOISE.sub("", s).strip()
        if len(s) >= 2 and s[0] == "{" and s[-1] == "}":
            depth = 0
            wraps_all = True
            for idx, ch in enumerate(s):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and idx < len(s) - 1:
                        wraps_all = False
                        break
            if wraps_all and depth == 0:
                s = s[1:-1].strip()
        if s == prev:
            break
    if (braced := _TRAILING_BRACED_COMPARATOR.search(s)) and _TRAILING_COMPARATOR.fullmatch(braced.group(1)):
        return True
    return _TRAILING_COMPARATOR.search(s) is not None


def _trailing_comparator_formula_problems(source: str, target: str, formulas: Mapping[str, str] | None) -> list[str]:
    """比较符结尾的公式在译文中若紧接“在/于/对/当”介词状语会割裂比较关系，强制要求直接衔接右值或括号定语。"""
    del source
    if not formulas:
        return []
    problems: list[str] = []
    for key, latex in formulas.items():
        if not latex or not _ends_with_comparator(latex):
            continue
        pat = re.compile(rf"<{re.escape(key)}/>(?:\s|</?(?:b|i|sup|sub)>)*")
        for match in pat.finditer(target):
            after = target[match.end() :]
            if not after:
                continue
            ch = after[0]
            if ch in _SPLITTING_ADVERBIAL_HEADS and not after.startswith("对应"):
                problems.append(
                    f"公式 <{key}/> 以比较符结尾，其后请直接衔接比较右值或括号定语"
                    f"（如“<{key}/>（……的）临界值”），请勿在比较符后直接接“{ch}……”割裂比较式"
                )
                break
    return problems


def _leaked_latex_command_problems(source: str, target: str) -> list[str]:
    """拦截译文意外泄漏的 LaTeX 数学控制序列（原文正文已包含的命令不误报）。"""
    leaked = list(dict.fromkeys(m[0] for m in _LEAKED_LATEX_CMD.finditer(target) if m[0] not in source))
    if not leaked:
        return []
    sample = "、".join(leaked[:5])
    return [f"译文意外泄漏了 LaTeX 命令（{sample}），公式请仅保留 <mN/> 占位符"]


def _is_malformed_formula_tag(match: re.Match[str]) -> bool:
    raw = match.group(0)
    if _VALID_FORMULA_TAG.match(raw):
        return False
    name = match.group("name")
    if not _FORMULA_NAME.match(name):
        return False
    if match.group("close"):
        return True
    rest = match.group("rest")
    if rest.rstrip().endswith("/"):
        return True
    if not _OPEN_TAG_ATTRS.match(rest):
        return False
    if not rest.strip():
        return True
    # 避开 a<m1 and b>0、x < m1 or b > 0 等数学/逻辑比较，但拦截 A<m1 disabled>B 等带属性畸形标签
    return _MATH_LOGIC_OP.match(rest) is None


def _protocol_tag_problems(source: str, target: str) -> list[str]:
    problems: list[str] = []
    if any(m[0] not in source for m in _PROTOCOL_TAG.finditer(target)):
        problems.append("译文包含协议标签（如 </translations>），请移除后重新输出")
    bad_formulas = [
        m.group(0) for m in _GENERIC_TAG.finditer(target) if _is_malformed_formula_tag(m) and m.group(0) not in source
    ]
    if bad_formulas:
        sample = "、".join(dict.fromkeys(bad_formulas))
        problems.append(f"公式标签格式错误（{sample}），请使用自闭合占位符如 <m1/>")
    return problems


def _target_problems(
    source: str,
    target: str,
    *,
    translated_terms: dict[str, str] | None = None,
    formulas: Mapping[str, str] | None = None,
    prev_context: str = "",
) -> list[str]:
    if problems := _protocol_tag_problems(source, target):
        return problems
    if problems := placeholder_problems(source, target):
        return ["公式占位符不一致：" + "；".join(problems)]
    if problems := (
        _duplicate_formula_equals_problems(source, target, formulas)
        + _adjacent_formula_number_problems(source, target)
        + _trailing_comparator_formula_problems(source, target, formulas)
        + _leaked_latex_command_problems(source, target)
    ):
        return problems
    errors: list[str] = []
    for label, check in (
        ("粗体标记", bold_problems),
        ("斜体标记", italic_problems),
        ("上下标标记", script_problems),
    ):
        if problems := check(source, target):
            errors.append(f"{label}不一致：" + "；".join(problems))
    if structure := errors or rich_text_problems(target):
        return structure
    anchors = (
        citation_problems(source, target)
        + script_value_problems(source, target)
        + percentage_problems(source, target, formulas=formulas)
        + _context_leakage_problems(source, target, prev_context)
    )
    return anchors or prose_problems(source, target, translated_terms=translated_terms)


_BLOCK_PAGE = re.compile(r"p([0-9]{1,12})-b[0-9]+")
_BACK_REFERENCE = re.compile(
    r"^(?:This|These|That|Those|It|Its|They|Their|Such|"
    r"The\s+(?:former|latter|above|aforementioned|same|following)|However,\s+(?:this|it|they))\b",
    re.I,
)
_CONTEXT_FORMULA = re.compile(r"<m\d+/>")
_LOCATOR_NAMES = {"algorithm": "算法", "table": "表", "section": "节", "figure": "图"}
_CONTEXT_COMMON_WORDS = {
    "A",
    "An",
    "The",
    "It",
    "Its",
    "We",
    "Our",
    "He",
    "She",
    "They",
    "Their",
    "This",
    "That",
    "These",
    "Those",
    "In",
    "On",
    "At",
    "For",
    "From",
    "As",
    "By",
    "With",
    "However",
}
_CONTEXT_ANCHOR = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"(?P<locator>(?i:Algorithm|Table|Section|Figure)|算法|表|节|图)\s*(?P<label>\d+(?:\.\d+)*)"
    r"|第\s*(?P<section>\d+(?:\.\d+)*)\s*节"
    r"|(?P<name>[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)"
    r"|(?<!\d\.)(?P<number>\d+(?:\.\d+)?)(?:\s*(?P<unit>%|％|ms|kg|cm|mm|km|kHz|MHz|GHz|Hz))?"
    r")(?![A-Za-z0-9_])"
)
_CONTEXT_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9,;:.'’ ()-]*")
_CONTEXT_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
_SENTENCE_MARK = re.compile(
    r"\b(?:e\.g\.|i\.e\.|et al\.|Fig\.|Eq\.|Sec\.|No\.|vs\.|Dr\.)(?=\s|$)|(?<=\d)\.(?=\d)|"
    r"""(?P<end>[.!?]+["'”’）)」』]*(?:\s+|$)|[。！？]+["'”’）)」』]*\s*)""",
    re.I,
)


def _context_anchors(text: str, *, names_only_if_capitalized: bool) -> set[str]:
    anchors: set[str] = set()
    for match in _CONTEXT_ANCHOR.finditer(text):
        if locator := match["locator"]:
            anchors.add(_LOCATOR_NAMES.get(locator.lower(), locator) + match["label"])
        elif section := match["section"]:
            anchors.add("节" + section)
        elif name := match["name"]:
            significant = len(name) >= 2 and name[0].isupper() and name not in _CONTEXT_COMMON_WORDS
            if not names_only_if_capitalized or significant:
                anchors.add(name.casefold())
        else:
            num = match["number"]
            unit = match["unit"] or ""
            if unit or "." in num or (len(num) >= 2 and num not in {"10", "100"}):
                anchors.add(num + unit.replace("％", "%"))
    return anchors


def _context_leakage_problems(source: str, target: str, prev_context: str) -> list[str]:
    if not prev_context:
        return []
    context = " ".join(_CONTEXT_FORMULA.sub(" ", RICH_TAG.sub("", prev_context)).split())
    if len(context) <= 30:
        return []
    original = " ".join(_CONTEXT_FORMULA.sub(" ", RICH_TAG.sub("", source)).split())
    translated = " ".join(_CONTEXT_FORMULA.sub(" ", RICH_TAG.sub("", target)).split())
    if len(translated) < 50:
        return []
    copied = False
    if prefix := _CONTEXT_PREFIX.match(translated):
        for count, word in enumerate(re.finditer(r"[A-Za-z0-9]+", prefix[0]), 1):
            if count >= 6 and word.end() >= 30:
                phrase = translated[: word.end()]
                copied = phrase in context and phrase not in original
                break
    if not copied:
        # 英文逐字照抄已有短语证据。中文吸纳则以源词数估算篇幅，给正常意译留出余量；
        # 独有锚点仍须同时成立，单纯的长译文不足以触发重试。
        words = re.findall(r"[A-Za-z]+", original)
        if len(_CONTEXT_HAN.findall(translated)) < max(50, 2.8 * len(words), 0.4 * sum(map(len, words))):
            return []
        unique = _context_anchors(context, names_only_if_capitalized=True) - _context_anchors(
            original, names_only_if_capitalized=False
        )
        if len(unique & _context_anchors(translated, names_only_if_capitalized=True)) < 2:
            return []
    return ["译文疑似吸纳了参考上文内容，请仅翻译本段原文，不要重复翻译上文"]


def _last_sentence(source: str) -> str:
    plain = RICH_TAG.sub("", _CONTEXT_FORMULA.sub("[公式]", source))
    text = " ".join(plain.split())
    start = 0
    for mark in _SENTENCE_MARK.finditer(text):
        if mark["end"] and mark.end() < len(text):
            start = mark.end()
    sentence = text[start:]
    return f"…{sentence[-239:]}" if len(sentence) > 240 else sentence


def _previous_context(segments: Sequence[Segment]) -> dict[str, str]:
    contexts: dict[str, str] = {}
    for previous, current in pairwise(segments):
        if (
            previous.role is not BlockType.TEXT
            or current.role is not BlockType.TEXT
            or not current.section
            or current.section != previous.section
            or not previous.block_ids
            or not current.block_ids
        ):
            continue
        before = _BLOCK_PAGE.fullmatch(previous.block_ids[-1])
        after = _BLOCK_PAGE.fullmatch(current.block_ids[0])
        if before and after and 0 <= int(after[1]) - int(before[1]) <= 1:
            contexts[current.id] = _last_sentence(previous.source)
    return contexts


def cache_key(
    seg: Segment,
    model: str,
    terms: dict[str, str],
    quality: Quality,
    *,
    reference_terms: dict[str, str] | None = None,
    domain: str = "",
    section: str = "",
    provider: str = "",
    validation_version: int = VALIDATION_VERSION,
    title: str = "",
    abstract: str = "",
    role: str = "",
    prev_context: str = "",
    user_terms: dict[str, str] | None = None,
) -> str:
    payload = json.dumps(
        [
            seg.source,
            seg.formulas,
            model,
            PROMPT_VERSION,
            sorted(terms.items()),
            quality,
            sorted((reference_terms or {}).items()),
            domain,
            section,
            provider,
            validation_version,
            title,
            abstract,
            role,
            prev_context,
            sorted((user_terms or {}).items()),
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class _Unit(BaseModel):
    """去重后的翻译单元：同一原文的多个段落共享一次翻译。"""

    source: str
    formulas: dict[str, str]
    section: str
    role: BlockType | None
    prev_context: str
    seg_indexes: list[int]
    key: str
    user_terms: dict[str, str]
    reference_terms: dict[str, str]

    @property
    def expanded(self) -> str:
        return expand_formulas(self.source, self.formulas)

    def meta(self, number: int) -> SegmentPromptMeta:
        # 同批普通段也会串入上一段，校验始终用相邻末句；只有指代句需要在提示词中展示上文。
        prompt_context = self.prev_context if _BACK_REFERENCE.match(RICH_TAG.sub("", self.source).lstrip()) else ""
        return SegmentPromptMeta(number, self.section, self.role, prompt_context, self.user_terms, self.reference_terms)


def _is_high_formula(unit: _Unit) -> bool:
    return len(unit.formulas) >= _HIGH_FORMULA_THRESHOLD


def _request_timeout(base_timeout_s: float, thinking: str) -> float:
    """思考模式放宽单次请求上限至至少 60s；极小超时（如单测）按 2 倍放宽以不拖慢验证。"""
    if thinking == "off":
        return base_timeout_s
    if base_timeout_s >= _THINKING_TIMEOUT_SCALE_THRESHOLD_S:
        return max(base_timeout_s, _THINKING_MIN_TIMEOUT_S)
    return base_timeout_s * 2


def _chunk_high_formula(units: list[_Unit]) -> list[list[_Unit]]:
    """对高公式密度段施加独立小批上限：>= 10 式单独成批，其余每批最多 4 段且累计公式数 <= 18。"""
    chunks: list[list[_Unit]] = []
    cur: list[_Unit] = []
    formulas = 0
    for unit in units:
        count = len(unit.formulas)
        if count >= _HIGH_FORMULA_SOLO_THRESHOLD:
            if cur:
                chunks.append(cur)
                cur, formulas = [], 0
            chunks.append([unit])
            continue
        if cur and (len(cur) >= _HIGH_FORMULA_MAX_SEGMENTS or formulas + count > _HIGH_FORMULA_MAX_FORMULAS):
            chunks.append(cur)
            cur, formulas = [], 0
        cur.append(unit)
        formulas += count
    if cur:
        chunks.append(cur)
    return chunks


def _split_high_formula_batches(batches: list[list[_Unit]]) -> list[list[_Unit]]:
    """将普通段与高公式密度段拆开，并对高公式批次施加段数与公式数上限。"""
    result: list[list[_Unit]] = []
    for batch in batches:
        high = [u for u in batch if _is_high_formula(u)]
        if not high:
            result.append(batch)
            continue
        plain = [u for u in batch if not _is_high_formula(u)]
        if plain:
            result.append(plain)
        result.extend(_chunk_high_formula(high))
    return result


async def translate(
    segments: Sequence[Segment],
    *,
    chat: ChatModel,
    cache: TranslationCache,
    ctx: DocContext,
    lookup_terms: TermLookup | None = None,
    lookup_user_terms: TermLookup | None = None,
    lookup_term_hits: TermHitsLookup | None = None,
    context_segments: Sequence[Segment] | None = None,
    opts: TranslateOptions | None = None,
    on_progress: Progress | None = None,
    on_usage: Callable[[float | None, bool], None] | None = None,
) -> tuple[list[Segment], TranslateReport]:
    if bad_source(segments):
        raise ChatError(BAD_SOURCE_MESSAGE)
    o = opts or TranslateOptions()
    use_cache = o.use_cache and not o.custom_instruction
    loop = asyncio.get_running_loop()
    total_deadline = loop.time() + o.total_timeout_s if o.total_timeout_s is not None else None
    out = [s.model_copy() for s in segments]
    report = TranslateReport(total=len(out), attempts={s.id: 0 for s in out})

    previous_context = _previous_context(context_segments if context_segments is not None else segments)
    doc_terms = {(normalize_term_key(k), v) for k, v in ctx.terms.items()}
    legacy_doc_keys = {k.casefold() for k in ctx.terms}
    provider = str(getattr(chat, "provider_id", ""))
    units: dict[str, _Unit] = {}
    for i, seg in enumerate(out):
        if seg.status in (SegmentStatus.DONE, SegmentStatus.EDITED):
            continue
        seg.quality = o.quality
        seg.translation_attempts = 0
        seg.failure_reason = ""
        if lookup_term_hits is not None:
            mine, _, reference_hits = lookup_term_hits(seg.source)
        else:
            mine = lookup_user_terms(seg.source) if lookup_user_terms is not None else {}
            references = lookup_terms(seg.source) if lookup_terms is not None else {}
            locked = legacy_doc_keys | {k.casefold() for k in mine}
            reference_hits = {k: v for k, v in references.items() if k.casefold() not in locked}
        user_hits = {k: v for k, v in mine.items() if (normalize_term_key(k), v) not in doc_terms}
        prev_context = previous_context.get(seg.id, "")
        # 本文术语与额外用户术语的优先级不同，缓存指纹保留两套映射。
        key = cache_key(
            seg,
            chat.model,
            ctx.terms,
            o.quality,
            reference_terms=reference_hits,
            domain=ctx.domain,
            section=seg.section,
            provider=provider,
            title=ctx.title,
            abstract=ctx.abstract,
            role=seg.role.value if seg.role is not None else "",
            prev_context=prev_context,
            user_terms=user_hits,
        )
        active_terms = ctx.terms | user_hits if user_hits else ctx.terms
        if (
            use_cache
            and (hit := cache.get(key))
            and not _target_problems(
                seg.source, hit, translated_terms=active_terms, formulas=seg.formulas, prev_context=prev_context
            )
        ):
            seg.target, seg.status = normalize_cjk_spacing(hit), SegmentStatus.DONE
            report.cached += 1
            continue
        unit = units.setdefault(
            key,
            _Unit(
                source=seg.source,
                formulas=seg.formulas,
                section=seg.section,
                role=seg.role,
                prev_context=prev_context,
                seg_indexes=[],
                key=key,
                user_terms=user_hits,
                reference_terms=reference_hits,
            ),
        )
        unit.seg_indexes.append(i)

    pending = list(units.values())
    done = 0
    total_units = len(pending)
    system = system_prompt(ctx)
    chat_opts = ChatOptions(thinking="low" if o.quality == "careful" else "off", transport_retries=0)
    sem = asyncio.Semaphore(o.concurrency)
    note = f"首次翻译（第 1/{o.max_attempts} 次）"
    reasons: dict[str, str] = {}
    feedback: dict[str, str] = {}
    refused: list[_Unit] = []
    expired_deadlines: set[float] = set()

    def deadline_for(retry_deadline: float | None) -> tuple[float | None, str]:
        if total_deadline is not None and (retry_deadline is None or total_deadline <= retry_deadline):
            return total_deadline, "本次翻译总时限耗尽"
        return retry_deadline, "补救总时限耗尽"

    def fail(batch: list[_Unit], reason: str, *, diagnostic: bool = False) -> list[_Unit]:
        for u in batch:
            reasons[u.key] = reason
            if diagnostic:
                feedback[u.key] = reason
        if on_progress:
            on_progress(done, total_units, note)
        return batch

    async def run(batch: list[_Unit], opts_: ChatOptions, retry_deadline: float | None = None) -> list[_Unit]:
        """翻译一批，返回不合格、需要重试的单元。"""
        nonlocal done
        deadline, exhausted = deadline_for(retry_deadline)
        if deadline is not None and (deadline in expired_deadlines or deadline <= loop.time()):
            return fail(batch, exhausted)
        hint = feedback.get(batch[0].key, "") if len(batch) == 1 else ""
        numbered = list(enumerate(batch, start=1))
        user = user_prompt(
            [(n, u.expanded) for n, u in numbered],
            [],
            {},
            retry_feedback=hint,
            custom_instruction=o.custom_instruction,
            scoped_meta=[u.meta(n) for n, u in numbered],
        )
        # 排队不计为模型尝试；有整次时限时，等待并发名额也必须受限。
        try:
            if deadline is None:
                await sem.acquire()
            else:
                await asyncio.wait_for(sem.acquire(), max(0, deadline - loop.time()))
        except TimeoutError:
            if deadline is not None:
                expired_deadlines.add(deadline)
            return fail(batch, exhausted)
        try:
            call_opts = (
                opts_.model_copy(update={"thinking": "low"})
                if opts_.thinking == "off" and any(_is_high_formula(u) for u in batch)
                else opts_
            )
            req_timeout = _request_timeout(o.request_timeout_s, call_opts.thinking)
            fallback_chat = chat if isinstance(chat, FallbackChat) and chat.has_fallbacks else None
            if fallback_chat is not None:
                if fallback_chat.total_timeout_s is not None:
                    outer_budget = fallback_chat.total_timeout_s
                else:
                    primary_base = (
                        fallback_chat.primary_timeout_s if fallback_chat.primary_timeout_s is not None else req_timeout
                    )
                    outer_budget = max(req_timeout * 2, primary_base * 1.5)
            else:
                chat_call_timeout = getattr(chat, "call_timeout_s", None)
                if isinstance(chat_call_timeout, (int, float)) and chat_call_timeout > 0:
                    outer_budget = max(req_timeout, float(chat_call_timeout))
                else:
                    outer_budget = req_timeout
            remaining = deadline - loop.time() if deadline is not None else outer_budget
            if remaining <= 0 or deadline in expired_deadlines:
                return fail(batch, exhausted)
            call_budget = min(outer_budget, remaining)
            for u in batch:
                for i in u.seg_indexes:
                    report.attempts[out[i].id] += 1
            report.requests += 1
            log.debug("翻译请求段数=%d，估算输入 token=%d", len(batch), system_tokens + estimate_tokens(user))
            try:
                if fallback_chat is not None:
                    primary_cap = (
                        fallback_chat.primary_timeout_s if fallback_chat.primary_timeout_s is not None else req_timeout
                    )
                    primary_slice = min(primary_cap, call_budget * 0.6)
                    result = await asyncio.wait_for(
                        fallback_chat.complete(
                            system,
                            user,
                            call_opts,
                            primary_timeout_s=primary_slice,
                            total_timeout_s=call_budget,
                        ),
                        call_budget,
                    )
                else:
                    result = await asyncio.wait_for(chat.complete(system, user, call_opts), call_budget)
            except TimeoutError:
                if deadline is not None and remaining <= outer_budget:
                    # 事件循环的计时器可能略早于单调时钟触发，不再用几毫秒余量另起一轮。
                    expired_deadlines.add(deadline)
                    return fail(batch, exhausted)
                return fail(batch, "翻译请求超时")
            except ChatError as e:
                if not e.retryable:
                    raise
                return fail(batch, str(e))
        finally:
            sem.release()
        report.usage = report.usage + result.usage
        billing_model = result.actual_model or chat.model
        report.cost_cny = add_cost(report.cost_cny, billing_model, result.usage, prices=o.price_overrides)
        if result.used_fallback:
            report.used_fallback = True
            if result.actual_model and result.actual_model not in report.fallback_models:
                report.fallback_models.append(result.actual_model)
        if on_usage is not None:
            on_usage(report.cost_cny, report.used_fallback)
        if result.finish_reason in ("refusal", "content_filter"):
            failed_batch = fail(batch, f"模型拒绝翻译（{result.finish_reason}）")
            if len(batch) > 1:
                return failed_batch  # 批次拒绝无法归因到某一段，拆开后再确认。
            refused.extend(failed_batch)
            return []
        try:
            got = parse_numbered(result.text, batch_size=len(batch), sources=[u.source for u in batch])
        except NumberedResponseError as e:
            return fail(batch, str(e), diagnostic=True)
        truncated = result.finish_reason in ("length", "max_tokens")
        if truncated and got:
            # 最后一项可能在段内或紧贴下一项编号中断，仅信任有后续完整编号分隔的前缀。
            del got[next(reversed(got))]
        retry = []
        for n, u in enumerate(batch, start=1):
            raw_target = got.get(n, "")
            target = normalize_formula_placeholders(raw_target) if raw_target else ""
            active_terms = ctx.terms | u.user_terms if u.user_terms else ctx.terms
            errors = (
                _target_problems(
                    u.source, target, translated_terms=active_terms, formulas=u.formulas, prev_context=u.prev_context
                )
                if target
                else []
            )
            if target and not errors:
                target = normalize_cjk_spacing(target)
                if use_cache and not result.compatibility_fallback and not result.used_fallback:
                    cache.put(u.key, target)
                for i in u.seg_indexes:
                    out[i].target, out[i].status = target, SegmentStatus.DONE
                done += 1
            else:
                missing = "模型输出截断" if truncated else "返回编号缺失或译文为空"
                reason = "；".join(errors) if target else missing
                reasons[u.key] = reason
                if target or not truncated:
                    feedback[u.key] = reason
                retry.append(u)
        if on_progress:
            on_progress(done, total_units, note)
        return retry

    async def run_all(
        batches: list[list[_Unit]], opts_: ChatOptions, retry_deadline: float | None = None
    ) -> list[_Unit]:
        tasks = [asyncio.create_task(run(b, opts_, retry_deadline)) for b in batches]
        try:
            return [u for r in await asyncio.gather(*tasks) for u in r]
        except BaseException:
            # 鉴权失败或取消时一起结束同轮请求，避免孤儿任务继续消耗模型额度。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    if on_progress:
        on_progress(done, total_units, note)
    system_tokens = estimate_tokens(system)
    sizes = [
        estimate_tokens(
            user_prompt([(n, u.expanded)], [], {}, scoped_meta=[u.meta(n)], custom_instruction=o.custom_instruction)
        )
        for n, u in enumerate(pending, start=1)
    ]
    batches = make_batches(
        pending, sizes, o.batch_tokens, sections=[u.section for u in pending], roles=[u.role for u in pending]
    )
    batches = _split_high_formula_batches(batches)
    failed = await run_all(batches, chat_opts)
    retry_deadline = loop.time() + o.retry_timeout_s
    for attempt in range(2, o.max_attempts + 1):
        if not failed:
            break
        deadline, exhausted = deadline_for(retry_deadline)
        if deadline is not None and (deadline in expired_deadlines or deadline <= loop.time()):
            fail(failed, exhausted)
            break
        note = f"还有 {len(failed)} 段待完成，正在单段重试（第 {attempt}/{o.max_attempts} 次）"
        if on_progress:
            on_progress(done, total_units, note)
        # 首次失败后即拆开，避免同批段落编号错位；按已尝试次数升序排列，优先保障未补救段落。
        failed.sort(key=lambda u: min(report.attempts[out[i].id] for i in u.seg_indexes))
        retry_opts = chat_opts if attempt == 2 else ChatOptions(thinking="low", transport_retries=0)
        failed = await run_all([[u] for u in failed], retry_opts, retry_deadline)
    failed.extend(refused)
    for u in failed:
        for i in u.seg_indexes:
            out[i].status = SegmentStatus.FAILED
            reason = reasons.get(u.key, "未取得可用译文")
            count = report.attempts[out[i].id]
            report.failures[out[i].id] = f"已尝试 {count}/{o.max_attempts} 次：{reason}"

    report.translated = sum(len(u.seg_indexes) for u in pending) - sum(len(u.seg_indexes) for u in failed)
    report.failed = sum(len(u.seg_indexes) for u in failed)
    for unit in pending:
        for i in unit.seg_indexes:
            out[i].translation_attempts = report.attempts[out[i].id]
            out[i].failure_reason = report.failures.get(out[i].id, "")
    return out, report

"""用户上传的术语表：先按常见格式规则识别，识别不了的交给模型读，结果由用户在页面上确认后才保存（保存在浏览器里）。

规则识别不要求固定列顺序：每行里不含汉字、含字母的一格是英文术语，含汉字的第一格是译法，其余非空格合并为说明。
这样“英文,中文”“中文,英文”“术语|译法|说明”都能读；表头行（两格都是汉字）自然跳过。
"""

import csv
import io
import json
import re

from pydantic import BaseModel

from pdftr.domain.llm import ChatModel, ChatOptions, ChatUsage

MAX_ENTRIES = 5000
MAX_TERM_CHARS = 120
_CJK = re.compile(r"[㐀-鿿]")
_LATIN = re.compile(r"[A-Za-z]")
_PAIR = re.compile(r"^\s*(.+?)\s*(?:=|：|:|→|->)\s*(.+?)\s*$")  # “英文=中文”“英文：中文”
_AI_CHUNK = 6000  # 每次交给模型的字符数
_AI_MAX_CHARS = 60000


class TermEntry(BaseModel):
    source: str
    target: str
    note: str = ""


class ParsedGlossary(BaseModel):
    entries: list[TermEntry]
    method: str  # rule | ai
    lines: int  # 文件里的非空行数，用来提示识别了多少
    usage: ChatUsage = ChatUsage()


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _entry(cells: list[str]) -> TermEntry | None:
    cells = [c.strip().strip("`*").strip() for c in cells if c and c.strip()]
    source = next((c for c in cells if _LATIN.search(c) and not _CJK.search(c)), None)
    target = next((c for c in cells if _CJK.search(c)), None)
    if not source or not target or len(source) > MAX_TERM_CHARS or len(target) > MAX_TERM_CHARS:
        return None
    note = "；".join(c for c in cells if c not in (source, target))
    return TermEntry(source=source, target=target, note=note[:300])


def _rows(text: str) -> list[list[str]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:50])
    if sample.count("|") >= len(lines[:50]):  # Markdown 表格
        return [ln.strip().strip("|").split("|") for ln in lines if not re.fullmatch(r"[\s|:-]+", ln)]
    for delim in ("\t", ",", ";"):
        if sum(delim in ln for ln in lines[:50]) >= 0.6 * len(lines[:50]):
            return list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    return [[m.group(1), m.group(2)] if (m := _PAIR.match(ln)) else [ln] for ln in lines]


def _from_json(text: str) -> list[TermEntry] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    rows: list[list[str]] = []
    if isinstance(data, dict):
        rows = [[str(k), str(v)] for k, v in data.items() if isinstance(v, str)]
    elif isinstance(data, list):
        rows = [[str(v) for v in item.values() if isinstance(v, str)] for item in data if isinstance(item, dict)]
    return [e for r in rows if (e := _entry(r))]


def dedupe(entries: list[TermEntry]) -> list[TermEntry]:
    """同一英文术语（不分大小写）只留第一条。"""
    seen: set[str] = set()
    out = []
    for e in entries:
        if e.source.lower() not in seen:
            seen.add(e.source.lower())
            out.append(e)
    return out[:MAX_ENTRIES]


def parse_rules(text: str) -> list[TermEntry]:
    found = _from_json(text) if text.lstrip()[:1] in "[{" else None
    if found is None:
        found = [e for r in _rows(text) if (e := _entry(r))]
    return dedupe(found)


AI_PROMPT = """下面是用户上传的术语表文件的一部分，格式不固定（可能是表格、列表、段落说明）。
逐条找出“英文术语 → 中文译法”的对应关系，文件里有说明或备注的一并保留。每条一行，用制表符分隔：
英文术语<TAB>中文译法<TAB>说明（没有就留空）
说明只写文件里对这个术语的解释、用法或注意事项（如“不要译成……”）；“译作”“统一译为”这类连接词不是说明，留空。
只输出文件里确实写了的对应关系，不要自己补充或翻译新的术语；没有任何术语时输出空。

文件内容：
{chunk}"""


async def parse_with_ai(text: str, chat: ChatModel) -> tuple[list[TermEntry], ChatUsage]:
    usage = ChatUsage()
    entries: list[TermEntry] = []
    text = text[:_AI_MAX_CHARS]
    lines, chunk = text.splitlines(), ""
    chunks = []
    for ln in lines:
        if len(chunk) + len(ln) > _AI_CHUNK and chunk:
            chunks.append(chunk)
            chunk = ""
        chunk += ln + "\n"
    if chunk.strip():
        chunks.append(chunk)
    for c in chunks:
        r = await chat.complete("你是严谨的术语整理助手。", AI_PROMPT.format(chunk=c), ChatOptions(max_tokens=8000))
        usage = usage + r.usage
        entries += [e for ln in r.text.splitlines() if (e := _entry(ln.split("\t")))]
    return dedupe(entries), usage


async def parse_upload(data: bytes, *, chat: ChatModel | None) -> ParsedGlossary:
    """chat 为 None 时按规则识别；否则交给模型读（要花用户的翻译额度，所以只在用户勾选“用 AI 识别”时才传）。"""
    text = decode(data)
    lines = sum(1 for ln in text.splitlines() if ln.strip())
    if chat is not None:
        entries, usage = await parse_with_ai(text, chat)
        return ParsedGlossary(entries=entries, method="ai", lines=lines, usage=usage)
    return ParsedGlossary(entries=parse_rules(text), method="rule", lines=lines)

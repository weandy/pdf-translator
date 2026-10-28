"""论文术语库的来源：每个来源下载一份公开的中英术语表，解析成 {英文: 中文}，再按优先级合并。

数据不随仓库分发，首次使用时下载到 data/glossaries/vendor/（地址固定到具体提交，上游改格式不影响解析）。
部署者用 PDFTR_GLOSSARY_SOURCES 选择来源与优先级（排在前面的优先）。

注意：机器之心术语库为 CC BY-NC-SA 4.0，不允许商业使用；商业部署请从来源列表中去掉 "aitd"。
"""

import csv
import io
import logging
import re
import time
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pdftr.glossary import gist

_CJK = re.compile(r"[\u4e00-\u9fff]")
_TAGS = re.compile(r"<sup>.*?</sup>|<[^>]+>")  # 上标多是脚注编号，连内容一起去掉
_PAREN = re.compile(r"[（(][^）)]*[）)]")  # 全角或半角括号里的内容
_RAW = "https://raw.githubusercontent.com"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    license: str
    homepage: str
    url: str
    parse: Callable[[bytes], dict[str, str]]
    filename: str = ""  # 缓存文件名，默认 “<id>.<原扩展名>”

    @property
    def attribution(self) -> str:
        return f"{self.name}（{self.license}）"

    @property
    def cache_name(self) -> str:
        return self.filename or f"{self.id}{Path(self.url).suffix}"


def clean_zh(text: str) -> str:
    """取第一个译法，去掉注释、括号里的缩写；没有汉字或像解释句子（过长）时返回空串。"""
    text = _TAGS.sub("", text)
    text = re.split(r"[/／；;、，,]|\s+或\s+", text)[0]
    text = _PAREN.sub("", text).strip(" *`")
    if any(q in text for q in '“”"「」'):  # 带引号的多是解释（如“中文为‘变换器’”），不是译名
        return ""
    return text if _CJK.search(text) and len(text) <= 20 else ""


def clean_en(text: str) -> str:
    text = _PAREN.sub("", _TAGS.sub("", text)).strip(" *`")
    return text if text and not _CJK.search(text) and len(text.split()) <= 6 else ""


def _table_rows(markdown: str) -> Iterable[list[str]]:
    """Markdown 表格的数据行（跳过表头分隔行），兼容有无首尾竖线两种写法。"""
    for line in markdown.splitlines():
        if "|" not in line or re.fullmatch(r"[\s|:-]+", line):
            continue
        yield [c.strip() for c in line.strip().strip("|").split("|")]


def _add(terms: dict[str, str], en: str, zh: str) -> None:
    en, zh = clean_en(en), clean_zh(zh)
    if en and zh:
        terms.setdefault(en, zh)


def parse_aitd(data: bytes) -> dict[str, str]:
    """机器之心：索引编号|英文术语|中文翻译|常用缩写|来源&扩展|备注。"""
    terms: dict[str, str] = {}
    for cells in _table_rows(data.decode("utf-8")):
        if len(cells) >= 3 and cells[0].startswith("AITD-"):
            _add(terms, cells[1], cells[2])
    return terms


def parse_d2l(data: bytes) -> dict[str, str]:
    """《动手学深度学习》术语表：每行“中文，english (缩写)”。"""
    terms: dict[str, str] = {}
    for line in data.decode("utf-8").splitlines():
        if "，" in line and not line.startswith("#"):
            zh, en = line.split("，", 1)
            _add(terms, en, zh)
    return terms


def parse_ml_technical_term(data: bytes) -> dict[str, str]:
    """EN | ZH | 全称/缩写 | 备注；EN 是缩写时用全称也登记一条。"""
    terms: dict[str, str] = {}
    for cells in _table_rows(data.decode("utf-8")):
        if len(cells) >= 2 and cells[0] not in ("EN", ""):
            _add(terms, cells[0], cells[1])
            if len(cells) >= 3 and len(cells[2].split()) >= 2:
                _add(terms, cells[2], cells[1])
    return terms


def parse_ais_vocab(data: bytes) -> dict[str, str]:
    """AI 安全术语：只取“通行”“常用”且置信度不低的条目。"""
    terms: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
        if row.get("status") in ("established", "common") and row.get("confidence") in ("high", "medium"):
            _add(terms, row.get("en") or "", row.get("preferred_zh") or "")
    return terms


def parse_computerese(data: bytes) -> dict[str, str]:
    """计算机专业术语对照：Word | Meaning。只取多词术语——单个常用词（如 command、scale）在论文里含义多变。"""
    terms: dict[str, str] = {}
    for cells in _table_rows(data.decode("utf-8")):
        if len(cells) >= 2 and cells[0] != "Word" and len(clean_en(cells[0]).split()) >= 2:
            _add(terms, cells[0], cells[1])
    return terms


def parse_gist(data: bytes) -> dict[str, str]:
    return gist.parse(data.decode("utf-8"))


SOURCES: dict[str, Source] = {
    s.id: s
    for s in [
        Source(
            "d2l",
            "《动手学深度学习》术语表",
            "Apache-2.0",
            "https://github.com/d2l-ai/d2l-zh",
            f"{_RAW}/d2l-ai/d2l-zh/e6b18ccea71451a55fcd861d7b96fddf2587b09a/TERMINOLOGY.md",
            parse_d2l,
        ),
        Source(
            "aitd",
            "机器之心人工智能术语库",
            "CC BY-NC-SA 4.0，不可商用",
            "https://github.com/jiqizhixin/Artificial-Intelligence-Terminology-Database",
            f"{_RAW}/jiqizhixin/Artificial-Intelligence-Terminology-Database/"
            "d4ec80fd9cbca95e77da47c91aaba8c9b6797620/data/All.md",
            parse_aitd,
        ),
        Source(
            "ml-technical-term",
            "机器学习与深度学习术语中英对照（luckycucu/ml-technical-term）",
            "MIT",
            "https://github.com/luckycucu/ml-technical-term",
            f"{_RAW}/luckycucu/ml-technical-term/fafde1b43eac8c9ef47933b7854a1b9d747f2e4e/README.md",
            parse_ml_technical_term,
        ),
        Source(
            "ais-vocab",
            "AI 安全术语中英对照（ais-vocab-cn）",
            "MIT",
            "https://github.com/YixiongHao/ais-vocab-cn",
            f"{_RAW}/YixiongHao/ais-vocab-cn/9904ae1d247a90e3baf6a6243fd637c8a81b2e57/exports/terms.csv",
            parse_ais_vocab,
        ),
        Source(
            "gist",
            "GIST 多语言 AI 术语数据集（Liu et al., ACL Findings 2025）",
            "Apache-2.0",
            "https://huggingface.co/datasets/Jerry999/multilingual-terminology",
            gist.URL,
            parse_gist,
            filename="gist_zh.csv",  # 沿用旧版本的缓存文件
        ),
        Source(
            "computerese",
            "计算机专业术语中英对照（computerese-cross-references）",
            "MIT",
            "https://github.com/EarsEyesMouth/computerese-cross-references",
            f"{_RAW}/EarsEyesMouth/computerese-cross-references/e92ca7dddf6b8122c7c0807f701fa9e237752506/README.md",
            parse_computerese,
        ),
    ]
}

# 默认顺序即优先级：人工整理的在前（机器之心与 d2l 有 16 条译法不同，机器之心的更通行，如“二分类”“隐变量”），
# 自动抽取的 GIST、偏软件工程的计算机术语在后
DEFAULT_ORDER = ["aitd", "d2l", "ml-technical-term", "ais-vocab", "gist", "computerese"]


def parse_ids(spec: str) -> list[Source]:
    """“aitd,d2l,...” → 来源列表（未知的名字忽略并记日志）。"""
    out = []
    for sid in (s.strip() for s in spec.split(",")):
        if sid in SOURCES:
            out.append(SOURCES[sid])
        elif sid:
            log.warning("未知的术语库来源 %r，可选：%s", sid, ", ".join(SOURCES))
    return out


def download(url: str, dest: Path, timeout: float = 20, retry_delays: tuple[float, ...] = (2, 5, 10)) -> None:
    """下载到 dest；网络偶发超时按退避间隔重试（实测连 GitHub 原始文件站点时有发生）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    for delay in (*retry_delays, None):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:  # 固定的 https 地址
                tmp.write_bytes(r.read())
            tmp.replace(dest)
            return
        except OSError:
            if delay is None:
                raise
            log.info("下载 %s 失败，%.0f 秒后重试", url, delay)
            time.sleep(delay)


def load_all(
    sources: Sequence[Source], vendor_dir: Path, fetch: Callable[[str, Path], None] = download
) -> tuple[dict[str, str], list[Source]]:
    """下载（已缓存则跳过）并按顺序合并；某个来源失败只跳过它。返回（合并后的术语, 成功加载的来源）。"""
    tables, loaded = [], []
    for src in sources:
        path = vendor_dir / src.cache_name
        try:
            if not path.exists():
                fetch(src.url, path)
            tables.append(src.parse(path.read_bytes()))
            loaded.append(src)
        except Exception:
            log.warning("术语库 %s 加载失败，本次跳过", src.name, exc_info=True)
    return merge(tables), loaded


def merge(tables: Iterable[dict[str, str]]) -> dict[str, str]:
    """按优先级合并：同一英文术语（不分大小写）以先出现的来源为准。"""
    merged: dict[str, str] = {}
    seen: set[str] = set()
    for table in tables:
        for en, zh in table.items():
            if en.lower() not in seen:
                seen.add(en.lower())
                merged[en] = zh
    return merged

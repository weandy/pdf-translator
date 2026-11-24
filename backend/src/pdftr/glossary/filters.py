"""术语抽取、归一化与选择共用的偷懒英文自映射判定。"""

import re

from pdftr.domain.text import normalize_term_key

# 允许保持全小写自映射的常见协议、系统组件与命令行工具名。
_SELF_MAP_ALLOWLIST = frozenset({
    "atlas", "blas", "cgroups", "emacs", "https", "iptables", "keras", "kubernetes",
    "linux", "lxc-user-nic", "macos", "nexus", "pandas", "postgres", "procfs",
    "prometheus", "redis", "rootfs", "sysfs", "tmpfs",
})  # fmt: skip
_PLAIN_ASCII_PHRASE = re.compile(r"^[A-Za-z]+(?:[\s-]+[A-Za-z]+)*$")
_PART_SPLIT = re.compile(r"[\s-]+")
_ES_PLURAL_STEM = ("s", "x", "z", "ch", "sh")


def _same_english_word(a: str, b: str, *, allow_plural: bool) -> bool:
    la, lb = a.lower(), b.lower()
    if la == lb:
        return True
    if not allow_plural:
        return False
    short, long_ = (la, lb) if len(la) <= len(lb) else (lb, la)
    if len(short) < 4:
        return False
    if long_ == short + "s":
        return True
    if short.endswith(_ES_PLURAL_STEM) and long_ == short + "es":
        return True
    return short.endswith("y") and long_ == short[:-1] + "ies"


def _same_phrase(s_parts: list[str], t_parts: list[str]) -> bool:
    if len(s_parts) != len(t_parts) or not s_parts:
        return False
    for a, b in zip(s_parts[:-1], t_parts[:-1], strict=True):
        if not _same_english_word(a, b, allow_plural=False):
            return False
    return _same_english_word(s_parts[-1], t_parts[-1], allow_plural=True)


def _is_plural_word(word: str) -> bool:
    low = word.lower()
    return len(low) >= 5 and low.endswith("s")


def _canonical_allowlist_form(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def is_lazy_english_self_map(source: str, target: str, *, written: str | None = None) -> bool:
    """判断自动抽取词条是否为应丢弃的纯小写普通英文自映射（含连字符复合词、多词短语与复数单词）。

    当 ``written`` 给定（来自 ``normalize_terms`` 的正文胜出词形）时，以 ``written`` 是否全小写
    判断该词在正文里是否为普通小写词，同时保留原始 ``source``/``target`` 的复数证据，
    从而封堵“标题假大写绕过 parse()、经 normalize_terms() 变回小写”的漏洞。
    """
    s = " ".join(source.strip().split())
    t = " ".join(target.strip().split())
    if not s or not t:
        return False
    if not _PLAIN_ASCII_PHRASE.fullmatch(s) or not _PLAIN_ASCII_PHRASE.fullmatch(t):
        return False
    if normalize_term_key(s).startswith("acr:") or normalize_term_key(t).startswith("acr:"):
        return False

    w = " ".join(written.strip().split()) if written is not None else None
    if w is not None:
        if not w or not w.islower() or not _PLAIN_ASCII_PHRASE.fullmatch(w):
            return False
    elif not (s.islower() and t.islower()):
        return False

    forms = (s, t) if w is None else (s, t, w)
    if any(_canonical_allowlist_form(item) in _SELF_MAP_ALLOWLIST for item in forms):
        return False

    s_parts = _PART_SPLIT.split(s)
    t_parts = _PART_SPLIT.split(t)
    if not _same_phrase(s_parts, t_parts):
        return False
    if len(s_parts) >= 2:
        return True
    return any(_is_plural_word(item) for item in forms)

"""为扁平 ZIP 归档分配可在 Windows 解压的唯一文件名。"""

import re
import unicodedata

_MAX_BYTES = 200
_FORBIDDEN = re.compile(r'[<>:"/\\|?*]')
_DEVICES = re.compile(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]|CONIN\$|CONOUT\$", re.IGNORECASE)


def _clip(text: str, budget: int) -> str:
    return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(" .")


def zip_filenames(names: list[str]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for name in names:
        text = unicodedata.normalize("NFKC", name)
        text = _FORBIDDEN.sub("_", "".join(c for c in text if not unicodedata.category(c).startswith("C")))
        stem = text.rstrip(" .")
        if stem.lower().endswith(".pdf"):
            stem = stem[:-4]
        stem = stem.strip(" .") or "document"
        if _DEVICES.fullmatch(stem.split(".", 1)[0].rstrip(" ")):
            stem = "_" + stem
        suffix = ".pdf"
        number = 1
        while True:
            candidate = _clip(stem, _MAX_BYTES - len(suffix)) + suffix
            key = unicodedata.normalize("NFKC", candidate).casefold()
            if key not in seen:
                seen.add(key)
                results.append(candidate)
                break
            number += 1
            suffix = f" ({number}).pdf"
    return results

"""导出 PDF 用的字体（GB/T 7713.2—2022 附录 B）：正文宋体、标题与图表编号黑体，西文按惯例用 Times New Roman。

商业字体不能随开源项目分发，用开源的同类字体代替（scripts/fetch_fonts.py 下载生成）：
宋体 → 思源宋体，黑体 → 思源黑体，Times New Roman → Tinos（字宽与之一致）。
字体文件缺失时（如开发环境未下载）退回 MuPDF 内置字体，导出照常进行。
"""

import logging
import re
from functools import cache
from pathlib import Path

log = logging.getLogger(__name__)

FILES = {
    "song": "NotoSerifSC-Regular.ttf",
    "hei": "NotoSansSC-Medium.ttf",
    "times": "Tinos-Regular.ttf",
    "times-bold": "Tinos-Bold.ttf",
}

# 用中文字体排的字符：汉字、全角标点、中文引号与破折号、省略号（与前端 shared/text/script.ts 一致）。
# MuPDF 不会按 font-family 列表逐字回退，必须把中文与西文分开包在不同字体里。
CJK_RUN = re.compile(
    "[\u2018\u2019\u201c\u201d\u2014\u2026\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]+"
)


@cache
def available(font_dir: Path) -> bool:
    missing = [name for name in FILES.values() if not (font_dir / name).is_file()]
    if missing:
        log.warning("字体文件缺失（%s），导出 PDF 使用内置字体；运行 scripts/fetch_fonts.py 下载", ", ".join(missing))
    return not missing


def font_faces() -> str:
    """@font-face 规则，字体文件从 Story 的 archive（字体目录）中读取。"""
    return (
        f"@font-face {{font-family: Song; src: url({FILES['song']})}}"
        f" @font-face {{font-family: Hei; src: url({FILES['hei']})}}"
        f" @font-face {{font-family: Times; src: url({FILES['times']})}}"
        f" @font-face {{font-family: Times; font-weight: bold; src: url({FILES['times-bold']})}}"
    )


def wrap_cjk(escaped: str) -> str:
    """把中文片段包进 <span class="zh">（输入是已转义的 HTML 文字，不含标签）。"""
    return CJK_RUN.sub(lambda m: f'<span class="zh">{m.group(0)}</span>', escaped)

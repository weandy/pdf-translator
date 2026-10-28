"""下载并准备导出 PDF 用的字体（GB/T 7713.2—2022 附录 B：正文宋体、标题黑体；西文按惯例用 Times New Roman）。

宋体、黑体、Times New Roman 是商业字体，不能随开源项目分发，这里用开源的同类字体：
  思源宋体 Noto Serif SC（OFL）、思源黑体 Noto Sans SC（OFL）、Tinos（OFL，与 Times New Roman 字宽一致）。
Google Fonts 只提供可变字重的 TrueType，用 fontTools 切出固定字重
（MuPDF 不能正确处理思源字体 OTF 版的 CID 字形映射，必须用 TrueType）。

中文字体精简到 GB 2312 的 6763 个汉字 + 标点 + 西文：完整字体有 3 万多个汉字，导出时每个文本框都要解析一遍，
17 页论文耗时 6 秒、内存 500 MB；精简后 2 秒、240 MB。集外字由后备字体显示。
同一份字体输出两种格式：TrueType 给导出 PDF，WOFF2 给网页阅读器（经 /api/fonts 提供），两边字形一致。

用法（fontTools、brotli 只在这里用到，不是运行时依赖）：
  uv run --with fonttools==4.60.1 --with brotli==1.1.0 python scripts/fetch_fonts.py fonts
"""

import contextlib
import sys
import time
import urllib.request
from pathlib import Path

GOOGLE_FONTS = "https://github.com/google/fonts/raw/main/ofl"
VARIABLE = [  # （可变字体地址, 字重, 输出文件名）
    (f"{GOOGLE_FONTS}/notoserifsc/NotoSerifSC%5Bwght%5D.ttf", 400, "NotoSerifSC-Regular.ttf"),
    (f"{GOOGLE_FONTS}/notosanssc/NotoSansSC%5Bwght%5D.ttf", 500, "NotoSansSC-Medium.ttf"),
]
STATIC = [
    (f"{GOOGLE_FONTS}/tinos/Tinos-Regular.ttf", "Tinos-Regular.ttf"),
    (f"{GOOGLE_FONTS}/tinos/Tinos-Bold.ttf", "Tinos-Bold.ttf"),
    (f"{GOOGLE_FONTS}/tinos/METADATA.pb", "LICENSE-Tinos.txt"),  # 该目录没有 OFL.txt，许可证（OFL）记录在元数据里
    (f"{GOOGLE_FONTS}/notoserifsc/OFL.txt", "OFL-NotoSerifSC.txt"),
    (f"{GOOGLE_FONTS}/notosanssc/OFL.txt", "OFL-NotoSansSC.txt"),
]


def gb2312() -> set[int]:
    chars = set()
    for hi in range(0xB0, 0xF8):
        for lo in range(0xA1, 0xFF):
            with contextlib.suppress(UnicodeDecodeError):
                chars.add(ord(bytes([hi, lo]).decode("gb2312")))
    return chars


# 西文、拉丁补充、常用标点（引号、破折号、省略号）、中文标点、全角字符
EXTRA = (
    set(range(0x20, 0x7F))
    | set(range(0xA1, 0x100))
    | set(range(0x2010, 0x2028))
    | set(range(0x3000, 0x3040))
    | set(range(0xFF00, 0xFFF0))
)


def save_subset(src: Path, target: Path, unicodes: set[int] | None) -> None:
    """输出 TrueType 与 WOFF2 两份；unicodes 为空表示保留全部字符（Tinos 本身很小）。"""
    from fontTools import subset

    for flavor, suffix in ((None, ".ttf"), ("woff2", ".woff2")):
        opts = subset.Options()
        opts.hinting = False
        opts.flavor = flavor
        opts.name_IDs = ["*"]
        opts.notdef_outline = True
        font = subset.load_font(src, opts)
        subsetter = subset.Subsetter(opts)
        # 去掉不间断空格：它与空格共用字形，导出 PDF 里的空格会被记成 U+00A0，复制、搜索时出问题
        keep = set(font.getBestCmap()) if unicodes is None else unicodes
        subsetter.populate(unicodes=keep - {0xA0})
        subsetter.subset(font)
        subset.save_font(font, target / (src.stem + suffix), opts)


def download(url: str, out: Path, attempts: int = 5) -> None:
    """网络偶发连接超时（实测连 GitHub 时有发生），按退避间隔重试。"""
    for n in range(1, attempts + 1):
        print(f"下载 {url}（第 {n} 次）", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                out.write_bytes(r.read())
            return
        except OSError as e:
            if n == attempts:
                raise
            print(f"  失败：{e}，{5 * n} 秒后重试", flush=True)
            time.sleep(5 * n)


def main(target: Path) -> None:
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    target.mkdir(parents=True, exist_ok=True)
    work = target / ".work"
    work.mkdir(exist_ok=True)
    for url, name in STATIC:
        out = (target if name.endswith(".txt") else work) / name
        if not out.exists():
            download(url, out)
        if name.endswith(".ttf"):
            save_subset(out, target, None)
    cjk = gb2312() | EXTRA
    for url, weight, name in VARIABLE:
        variable = work / f"{name}.variable"
        if not variable.exists():
            download(url, variable)
        static = work / name
        instantiateVariableFont(TTFont(variable), {"wght": weight}, updateFontNames=True).save(static)
        save_subset(static, target, cjk)
    for p in work.iterdir():
        p.unlink()
    work.rmdir()
    print("字体已就绪：", ", ".join(f"{p.name} {p.stat().st_size // 1024} KB" for p in sorted(target.iterdir())))


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "fonts"))

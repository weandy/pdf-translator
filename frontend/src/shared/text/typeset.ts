/**
 * 译文排版规则（W3C《中文排版需求》clreq），与后端 rendering/typeset.py 一致：
 * 汉字与西文、数字之间加空格（§6.3.3），正文段首缩进两字（§6.2.1.1），正文两端对齐（§6.2）。
 */
import type { BlockData, SegmentData } from "@/shared/api/client";

const HAN = "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]";
const LATIN = "[A-Za-z0-9]";
const HAN_LATIN = new RegExp(`(${HAN})(${LATIN}|<m\\d+/>)`, "g");
const LATIN_HAN = new RegExp(`(${LATIN}|%|<m\\d+/>)(${HAN})`, "g");
const LIST_ITEM = /^\s*(?:[•·▪◦‣\-–—*]|\(?\d+\)|\d+[.)、]|\[\d+\]|[a-z][.)]\s)/;

/** 键盘可操作的段落：Tab 聚焦，回车或空格打开（与点击相同）。 */
export function keyActivate(run: () => void) {
  return (e: { key: string; preventDefault: () => void }) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      run();
    }
  };
}

/** 读屏与悬停提示用的段落简称：去掉公式占位与格式标签，取开头。 */
export function shortLabel(text: string, n = 40): string {
  const plain = text
    .replace(/<m\d+\/>/g, "…")
    .replace(/<\/?(?:sup|sub|b|i)>/g, "")
    .trim();
  return plain.length > n ? `${plain.slice(0, n)}…` : plain;
}

/** 解析器偶尔把整行文字标成上下标（如列表项的第一行被包在 <sub> 里），真正的上下标都很短。 */
const FAKE_SCRIPT = /<(sup|sub)>([^<]{13,})<\/\1>/g;

export function dropFakeScripts(text: string): string {
  return text.replace(FAKE_SCRIPT, "$2");
}

/** 行距：放得下时优先宽松，依次收紧，最后才缩小字号（与导出 PDF 一致）。 */
export const LINE_HEIGHTS = [1.5, 1.4, 1.3, 1.2];

export function spaceCjkLatin(text: string): string {
  for (let i = 0; i < 2; i++) {
    text = text.replace(HAN_LATIN, "$1 $2").replace(LATIN_HAN, "$1 $2");
  }
  return text;
}

export function wantsIndent(block: BlockData, segment: SegmentData, first: boolean): boolean {
  if (block.type !== "text" || !first || block.bold) return false;
  const source = segment.source.trimStart();
  return !(LIST_ITEM.test(source) || /^[a-z]/.test(source));
}

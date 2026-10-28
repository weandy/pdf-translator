/**
 * 按文字种类切分：中文（汉字、全角标点、中文引号与破折号、省略号）用中文字体，其余用西文字体。
 * 与后端 rendering/fonts.py 的 CJK_RUN 一致，网页与导出 PDF 的字体分配相同。
 */
const CJK_RUN =
  /[\u2018\u2019\u201c\u201d\u2014\u2026\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]+/g;

export interface ScriptRun {
  text: string;
  cjk: boolean;
}

export function splitScript(text: string): ScriptRun[] {
  const out: ScriptRun[] = [];
  let last = 0;
  for (const m of text.matchAll(CJK_RUN)) {
    if (m.index > last) out.push({ text: text.slice(last, m.index), cjk: false });
    out.push({ text: m[0], cjk: true });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), cjk: false });
  return out;
}

/** 图表标题开头的编号（“图 4.”“表2：”），按 GB/T 7713.2 用黑体（与后端 rendering/export.py 一致）。 */
const CAPTION_NO = /^\s*(?:图|表)\s*[A-Z]?\d+(?:\.\d+)*[.:：．。]?/;

export function splitCaptionNo(text: string): [string, string] {
  const m = CAPTION_NO.exec(text);
  return m ? [m[0], text.slice(m[0].length)] : ["", text];
}

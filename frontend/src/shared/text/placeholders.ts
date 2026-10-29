/**
 * 公式占位符检查（与后端 translation/batching.py 的 placeholder_problems 规则一致）：
 * 修改译文时每个公式占位符要各保留一次，少了公式会从阅读器与导出 PDF 里消失。
 */
const TAG = /<m(\d+)\/>/g;

export interface PlaceholderCheck {
  missing: string[]; // 缺少的占位符，如 "m2"
  repeated: string[]; // 出现多于一次的
  unknown: string[]; // 原文里没有的编号
}

function counts(text: string): Map<string, number> {
  const out = new Map<string, number>();
  for (const m of text.matchAll(TAG)) out.set(`m${m[1]}`, (out.get(`m${m[1]}`) ?? 0) + 1);
  return out;
}

export function checkPlaceholders(source: string, text: string): PlaceholderCheck {
  const want = counts(source);
  const got = counts(text);
  const byNumber = (a: string, b: string) => Number(a.slice(1)) - Number(b.slice(1));
  return {
    missing: [...want]
      .filter(([k, n]) => (got.get(k) ?? 0) < n)
      .map(([k]) => k)
      .sort(byNumber),
    repeated: [...got]
      .filter(([k, n]) => want.has(k) && n > (want.get(k) ?? 0))
      .map(([k]) => k)
      .sort(byNumber),
    unknown: [...got.keys()].filter((k) => !want.has(k)).sort(byNumber),
  };
}

export function placeholdersOk(c: PlaceholderCheck): boolean {
  return !c.missing.length && !c.repeated.length && !c.unknown.length;
}

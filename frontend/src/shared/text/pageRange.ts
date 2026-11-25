/**
 * 解析“页码范围”输入（1 起，如 “1-10, 15, 20-22”，也接受中文逗号、顿号、全角短横）。
 * 返回 0 起的页码（升序、去重）；留空返回 null 表示全部；格式不对或超出总页数返回错误说明。
 */
export function parsePageRange(text: string, total: number): number[] | null | { error: string } {
  const raw = text.trim();
  if (!raw) return null;
  const pages = new Set<number>();
  const joined = raw.replace(/\s*([-–—－~～])\s*/g, "$1"); // “8 ~ 10” 两边的空格不算分隔
  for (const part of joined.split(/[,，、;；\s]+/).filter(Boolean)) {
    const m = /^(\d+)(?:[-–—－~～](\d+))?$/.exec(part);
    if (!m) return { error: `看不懂“${part}”，请写成 1-10, 15 这样` };
    const start = Number(m[1]);
    const end = m[2] ? Number(m[2]) : start;
    if (start < 1 || end < start) return { error: `“${part}”不是有效的范围` };
    if (end > total) return { error: `这份 PDF 只有 ${total} 页` };
    for (let p = start; p <= end; p++) pages.add(p - 1);
  }
  return [...pages].sort((a, b) => a - b);
}

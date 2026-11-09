/** 译文差异：中文按字、英文按词、公式占位符整体比较，不解析或执行文本标记。 */
export interface TextChange {
  kind: "equal" | "removed" | "added";
  text: string;
}

export interface TextDiff {
  changes: TextChange[];
  coarse: boolean;
}

const MAX_MATRIX_CELLS = 250_000;
const MAX_TOKENS = 6_000;
const tokens = (text: string) => text.match(/<m\d+\/>|[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*|\s+|[\s\S]/gu) ?? [];

export function diffText(before: string | null, after: string | null): TextDiff {
  const oldText = before ?? "";
  const newText = after ?? "";
  if (oldText === newText) return { changes: [{ kind: "equal", text: oldText }], coarse: false };
  const changes: TextChange[] = [];
  const append = (kind: TextChange["kind"], text: string) => {
    if (!text) return;
    const last = changes.at(-1);
    if (last?.kind === kind) last.text += text;
    else changes.push({ kind, text });
  };
  const left = tokens(oldText);
  const right = tokens(newText);
  let start = 0;
  while (start < left.length && start < right.length && left[start] === right[start]) start++;
  let oldEnd = left.length;
  let newEnd = right.length;
  while (oldEnd > start && newEnd > start && left[oldEnd - 1] === right[newEnd - 1]) {
    oldEnd--;
    newEnd--;
  }
  append("equal", left.slice(0, start).join(""));
  const oldMiddle = left.slice(start, oldEnd);
  const newMiddle = right.slice(start, newEnd);
  const width = newMiddle.length + 1;
  const cells = (oldMiddle.length + 1) * width;
  const coarse = Math.max(oldMiddle.length, newMiddle.length) > MAX_TOKENS || cells > MAX_MATRIX_CELLS;
  if (coarse || !oldMiddle.length || !newMiddle.length) {
    // 巨大或完全改写的段落只标整块变化，避免平方级比较阻塞阅读器。
    append("removed", oldMiddle.join(""));
    append("added", newMiddle.join(""));
  } else {
    const lengths = new Uint16Array(cells);
    for (let i = oldMiddle.length - 1; i >= 0; i--) {
      for (let j = newMiddle.length - 1; j >= 0; j--) {
        lengths[i * width + j] =
          oldMiddle[i] === newMiddle[j]
            ? lengths[(i + 1) * width + j + 1]! + 1
            : Math.max(lengths[(i + 1) * width + j]!, lengths[i * width + j + 1]!);
      }
    }
    let i = 0;
    let j = 0;
    while (i < oldMiddle.length || j < newMiddle.length) {
      if (i < oldMiddle.length && j < newMiddle.length && oldMiddle[i] === newMiddle[j]) {
        append("equal", oldMiddle[i++]!);
        j++;
      } else if (
        i < oldMiddle.length &&
        (j === newMiddle.length || lengths[(i + 1) * width + j]! >= lengths[i * width + j + 1]!)
      ) {
        append("removed", oldMiddle[i++]!);
      } else append("added", newMiddle[j++]!);
    }
  }
  append("equal", left.slice(oldEnd).join(""));
  return { changes, coarse };
}

/** 折叠时直接展示第一处修改附近的内容，避免段尾修改被开头的相同文本淹没。 */
export function diffPreview(changes: TextChange[], context = 28, changeLimit = 80): TextChange[] {
  const first = changes.findIndex((change) => change.kind !== "equal");
  if (first < 0) return [];
  const clip = (text: string, limit: number, tail = false) => {
    const parts = text.match(/<m\d+\/>|[\s\S]/gu) ?? [];
    if (parts.length <= limit) return text;
    return tail ? `…${parts.slice(-limit).join("")}` : `${parts.slice(0, limit).join("")}…`;
  };
  const preview: TextChange[] = [];
  if (first > 0) preview.push({ kind: "equal", text: clip(changes[first - 1]!.text, context, true) });
  let next = first;
  while (next < changes.length && changes[next]!.kind !== "equal") {
    const change = changes[next++]!;
    preview.push({ ...change, text: clip(change.text, changeLimit) });
  }
  if (next < changes.length) preview.push({ kind: "equal", text: clip(changes[next]!.text, context) });
  if (next < changes.length - 1 && !preview.at(-1)!.text.endsWith("…")) preview.at(-1)!.text += "…";
  return preview;
}

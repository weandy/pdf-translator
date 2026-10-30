import type { DocumentData } from "@/shared/api/client";

export interface SearchHit {
  segmentId: string;
  page: number;
  source: string;
  target: string;
}

/** 搜索时去掉公式、格式标记和多余空白，保留用户可理解的正文。 */
export function searchableText(text: string): string {
  return text
    .replace(/<m\d+\/>/g, " ")
    .replace(/<\/?(?:sup|sub|b|i)>/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase();
}

export function searchDocument(doc: DocumentData, query: string): SearchHit[] {
  const q = searchableText(query);
  if (!q) return [];
  const pageByBlock = new Map(doc.pages.flatMap((p) => p.blocks.map((b) => [b.id, p.index] as const)));
  return doc.segments.flatMap((segment) => {
    const source = searchableText(segment.source);
    const target = searchableText(segment.target ?? "");
    if (!source.includes(q) && !target.includes(q)) return [];
    return [
      {
        segmentId: segment.id,
        page: pageByBlock.get(segment.block_ids[0] ?? "") ?? 0,
        source: segment.source,
        target: segment.target ?? "",
      },
    ];
  });
}

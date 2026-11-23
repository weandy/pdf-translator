import type { DocumentData } from "@/shared/api/client";

export interface SearchHit {
  segmentId: string;
  page: number;
  source: string;
  target: string;
}

export interface SearchEntry extends SearchHit {
  sourceNorm: string;
  targetNorm: string;
}

export interface SearchIndex {
  entries: SearchEntry[];
}

/** 搜索时去掉公式、格式标记和多余空白，保留用户可理解的正文。 */
export function searchableText(text: string): string {
  return text
    .replace(/<m\d+\/>/g, " ")
    .replace(/<\/?(?:sup|sub|b|i)>/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

/** 预构建文档搜索索引，仅在文档变化时清洗一次全部段落与页号映射。 */
export function buildSearchIndex(doc: DocumentData): SearchIndex {
  const pageByBlock = new Map(doc.pages.flatMap((p) => p.blocks.map((b) => [b.id, p.index] as const)));
  const fallbackPage = doc.pages[0]?.index ?? 0;
  const entries: SearchEntry[] = doc.segments.map((segment) => {
    const target = segment.target ?? "";
    const matchedPage = segment.block_ids.map((id) => pageByBlock.get(id)).find((p) => p !== undefined);
    return {
      segmentId: segment.id,
      page: matchedPage ?? fallbackPage,
      source: segment.source,
      target,
      sourceNorm: searchableText(segment.source),
      targetNorm: searchableText(target),
    };
  });
  return { entries };
}

/** 基于预构建索引执行纯子串匹配。 */
export function querySearchIndex(index: SearchIndex, query: string): SearchHit[] {
  const q = searchableText(query);
  if (!q) return [];
  const hits: SearchHit[] = [];
  for (const entry of index.entries) {
    if (!entry.sourceNorm.includes(q) && !entry.targetNorm.includes(q)) continue;
    hits.push({
      segmentId: entry.segmentId,
      page: entry.page,
      source: entry.source,
      target: entry.target,
    });
  }
  return hits;
}

export function searchDocument(doc: DocumentData, query: string): SearchHit[] {
  return querySearchIndex(buildSearchIndex(doc), query);
}

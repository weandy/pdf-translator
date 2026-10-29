/**
 * 阅读视图：不保持原版式，把译文按阅读顺序排成正常字号的文字流。手机上把整页缩到屏幕宽，正文只剩 4–5 像素，
 * 只有这样才能读（docs/04-ux-round-1.md §1）。图、表、算法框插入原文裁图，独立公式用 KaTeX；点段落打开编辑。
 */
import { useMemo } from "react";

import { roleOf } from "@/entities/document/model";
import { blockImageUrl, formulaUrl, type BlockData, type DocumentData, type SegmentData } from "@/shared/api/client";
import { InlineMathText, renderLatex } from "@/shared/math/inline";
import { dropFakeScripts, keyActivate, shortLabel, spaceCjkLatin } from "@/shared/text/typeset";

type Item =
  | { kind: "page"; index: number }
  | { kind: "text"; segment: SegmentData; block: BlockData }
  | { kind: "figure"; block: BlockData };

const PICTURES = new Set(["figure", "table", "code", "formula"]);

function items(doc: DocumentData): Item[] {
  const first = new Map(doc.segments.map((s) => [s.block_ids[0], s]));
  const out: Item[] = [];
  for (const page of doc.pages) {
    out.push({ kind: "page", index: page.index });
    for (const block of page.blocks) {
      const segment = first.get(block.id);
      if (segment) out.push({ kind: "text", segment, block });
      else if (PICTURES.has(block.type)) out.push({ kind: "figure", block });
    }
  }
  return out;
}

interface Props {
  doc: DocumentData;
  jobId: string;
  selected: string | null;
  onSelect: (segmentId: string) => void;
}

export function ReadingView({ doc, jobId, selected, onSelect }: Props) {
  const list = useMemo(() => items(doc), [doc]);
  return (
    <article className="flow">
      {list.map((it) => {
        if (it.kind === "page") {
          return (
            <p key={`p${it.index}`} className="flow-page" aria-hidden="true" data-page={it.index}>
              第 {it.index + 1} 页
            </p>
          );
        }
        if (it.kind === "figure") {
          const html = it.block.type === "formula" && it.block.latex ? renderLatex(it.block.latex, true) : null;
          return html ? (
            <div key={it.block.id} className="flow-formula" dangerouslySetInnerHTML={{ __html: html }} />
          ) : (
            <img
              key={it.block.id}
              className={`flow-figure ${it.block.type}`}
              src={blockImageUrl(jobId, it.block.id)}
              alt={it.block.type === "table" ? "表格" : it.block.type === "formula" ? "公式" : "图"}
              loading="lazy"
            />
          );
        }
        const { segment, block } = it;
        const translated = Boolean(segment.target) && segment.status !== "failed";
        const text = translated ? spaceCjkLatin(segment.target ?? "") : segment.source;
        const role = roleOf(block);
        const Tag = role === "heading" ? "h3" : "p";
        return (
          <Tag
            key={segment.id}
            className={`flow-${role}${translated ? "" : " untranslated"}${segment.id === selected ? " selected" : ""}`}
            onClick={() => onSelect(segment.id)}
            tabIndex={0}
            role="button"
            aria-label={`段落：${shortLabel(segment.source)}`}
            data-seg={segment.id}
            onKeyDown={keyActivate(() => onSelect(segment.id))}
          >
            <InlineMathText
              text={dropFakeScripts(text)}
              formulas={segment.formulas}
              fallbackUrl={(k) => formulaUrl(segment.id, k)}
            />
          </Tag>
        );
      })}
    </article>
  );
}

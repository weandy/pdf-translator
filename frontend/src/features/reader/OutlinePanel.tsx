/**
 * 目录侧栏（以原文为准）：每项显示原文标题，下面小字是译文；点一项跳到对应位置。
 */
import type { OutlineData } from "@/shared/api/client";

interface Props {
  items: OutlineData[];
  current: number; // 当前页（0 起），用来标出读到哪一节
  picked: number | null; // 刚点过、且仍在当前页的一项
  onGo: (item: OutlineData) => void;
  onClose: () => void;
}

export function OutlinePanel({ items, current, picked, onGo, onClose }: Props) {
  // 当前所在的一节：刚点过的那项；否则页码不超过当前页的最后一项
  const active = picked ?? items.reduce((at, it, i) => (it.page <= current ? i : at), -1);
  return (
    <nav className="outline" aria-label="目录">
      <div className="outline-head">
        <strong>目录</strong>
        <span className="spacer" />
        <button type="button" className="quiet" onClick={onClose}>
          收起
        </button>
      </div>
      <ol>
        {items.map((it, i) => (
          <li key={i} style={{ paddingLeft: `${(Math.min(it.level, 4) - 1) * 14}px` }}>
            <button
              type="button"
              className={`outline-item${i === active ? " on" : ""}`}
              aria-current={i === active ? "location" : undefined}
              onClick={() => onGo(it)}
            >
              <span className="outline-title">{it.title}</span>
              {it.translated && <small>{it.translated}</small>}
              <small className="outline-page num">{it.page + 1}</small>
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
}

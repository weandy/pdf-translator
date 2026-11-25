import { useLayoutEffect, type RefObject } from "react";
import { createPortal } from "react-dom";

import { useAnchoredMenu } from "@/shared/feedback/useAnchoredMenu";

import { type ZoomSpec } from "./zoom";

const OPTIONS: { title: string; zoom: ZoomSpec }[] = [
  { title: "智能自适应", zoom: { kind: "auto" } },
  { title: "适合宽度", zoom: { kind: "fit-width" } },
  ...[80, 100, 125, 150].map((value) => ({
    title: value + "%",
    zoom: { kind: "fixed" as const, value: value / 100 },
  })),
];

export function ZoomMenu({ zoom, scale, open, trigger, onOpen, onClose, onChange }: {
  zoom: ZoomSpec;
  scale: number;
  open: boolean;
  trigger: RefObject<HTMLButtonElement | null>;
  onOpen: () => void;
  onClose: () => void;
  onChange: (zoom: ZoomSpec) => void;
}) {
  const menu = useAnchoredMenu(open, trigger, onClose, "center");
  useLayoutEffect(() => {
    if (!open) return;
    const options = menu.current?.querySelectorAll<HTMLButtonElement>("button");
    (menu.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]') ?? options?.[0])
      ?.focus({ preventScroll: true });
  }, [open, menu]);

  const percent = Math.round(scale * 100) + "%";
  const label = (zoom.kind === "auto" ? "自适应 " : zoom.kind === "fit-width" ? "适合宽度 " : "固定 ") + percent;
  return (
    <div className="zoom-picker">
      <button type="button" className="quiet zoom-label" ref={trigger} title={label} aria-label={label}
        aria-haspopup="menu" aria-expanded={open} onClick={open ? onClose : onOpen}>
        {percent}
      </button>
      {open && createPortal(
        <div className="zoom-menu" role="menu" aria-label="缩放档位" ref={menu}
          style={{ position: "fixed" }}
          onBlur={(e) => {
            if (e.relatedTarget && e.relatedTarget !== trigger.current && !e.currentTarget.contains(e.relatedTarget)) onClose();
          }}
          onKeyDown={(e) => {
            const buttons = Array.from(e.currentTarget.querySelectorAll("button"));
            const index = buttons.indexOf(e.target as HTMLButtonElement);
            const next = e.key === "ArrowDown" ? (index + 1) % buttons.length
              : e.key === "ArrowUp" ? (index - 1 + buttons.length) % buttons.length
                : e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1 : -1;
            if (next < 0) return;
            e.preventDefault();
            buttons[next]?.focus();
          }}>
          {OPTIONS.map((option) => (
            <button type="button" key={option.title} role="menuitemradio" aria-checked={
              zoom.kind === option.zoom.kind && (zoom.kind !== "fixed" || (option.zoom.kind === "fixed" && zoom.value === option.zoom.value))
            } onClick={() => { onChange(option.zoom); onClose(); trigger.current?.focus(); }}>
              {option.title}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </div>
  );
}

import { useLayoutEffect, useRef, type RefObject } from "react";

import { hasOpenModal } from "./ConfirmDialog";

const EDGE = 8;
const GAP = 6;

export function useAnchoredMenu(
  open: boolean,
  trigger: RefObject<HTMLButtonElement | null>,
  onClose: () => void,
  align: "center" | "right",
  container: RefObject<HTMLElement | null> = trigger,
) {
  const menu = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const panel = menu.current;
    const button = trigger.current;
    if (!open || !panel || !button) return;

    const position = () => {
      const anchor = button.getBoundingClientRect();
      const width = document.documentElement.clientWidth || window.innerWidth;
      const height = document.documentElement.clientHeight || window.innerHeight;
      panel.style.maxWidth = `${Math.max(0, width - 2 * EDGE)}px`;
      const panelWidth = panel.getBoundingClientRect().width;
      const left = align === "right" ? anchor.right - panelWidth : (anchor.left + anchor.right - panelWidth) / 2;
      const below = Math.max(0, height - Math.max(EDGE, anchor.bottom + GAP) - EDGE);
      const above = Math.max(0, Math.min(height - EDGE, anchor.top - GAP) - EDGE);
      const flip = panel.scrollHeight > below && above > below;
      panel.style.maxHeight = `${flip ? above : below}px`;
      const top = flip
        ? Math.min(height - EDGE, anchor.top - GAP) - panel.getBoundingClientRect().height
        : anchor.bottom + GAP;
      panel.style.left = `${Math.max(EDGE, Math.min(left, width - EDGE - panelWidth))}px`;
      panel.style.top = `${Math.max(EDGE, Math.min(top, height - EDGE))}px`;
    };
    const outside = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Node && !panel.contains(target) && !container.current?.contains(target)) onClose();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.isComposing || hasOpenModal()) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      onClose();
      button.focus();
    };
    const scroll = (event: Event) => {
      if (!(event.target instanceof Node && panel.contains(event.target))) onClose();
    };
    position();
    document.addEventListener("pointerdown", outside);
    window.addEventListener("keydown", escape, true);
    window.addEventListener("resize", position);
    window.addEventListener("scroll", scroll, true);
    return () => {
      document.removeEventListener("pointerdown", outside);
      window.removeEventListener("keydown", escape, true);
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", scroll, true);
    };
  }, [open, trigger, container, onClose, align]);
  return menu;
}

import { useEffect, useRef, type RefObject } from "react";

import { hasOpenModal } from "@/shared/feedback/ConfirmDialog";

type ElementRef<T extends HTMLElement> = RefObject<T | null>;

function selectionInPages(pages: HTMLElement | null): string {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !pages?.contains(selection.anchorNode) ||
      !pages.contains(selection.focusNode)) return "";
  const raw = selection.toString();
  const text = raw.trim();
  return text.length <= 80 && !/[\r\n]/.test(raw) ? text : "";
}

function visible(element: HTMLElement): boolean {
  return element.isConnected && !element.closest("[hidden], [inert]") &&
    (element.checkVisibility?.({ checkVisibilityCSS: true }) ?? true) &&
    window.getComputedStyle(element).visibility !== "hidden" &&
    window.getComputedStyle(element).display !== "none";
}

export function useReaderShortcuts({ searchOpen, disabled, input, pages, openSearch, closeSearch }:{
  searchOpen: boolean;
  disabled?: boolean;
  input: ElementRef<HTMLInputElement>;
  pages: ElementRef<HTMLDivElement>;
  openSearch: (selected: string) => void;
  closeSearch: () => void;
}) {
  const previous = useRef<HTMLElement | null>(null);
  const frame = useRef(0);
  const focusSearch = () => {
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      input.current?.focus();
      input.current?.select();
    });
  };
  const show = (trigger: Element | null, selected = "") => {
    if (!searchOpen || trigger !== input.current) previous.current = trigger instanceof HTMLElement ? trigger : null;
    openSearch(selected);
    focusSearch();
  };
  const hide = () => {
    closeSearch();
    cancelAnimationFrame(frame.current);
    const trigger = previous.current;
    (trigger && visible(trigger) ? trigger : pages.current)?.focus();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing || e.keyCode === 229 || hasOpenModal()) return;
      if (e.key === "Escape" && searchOpen) {
        e.preventDefault();
        e.stopImmediatePropagation();
        hide();
        return;
      }
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || e.key.toLowerCase() !== "f" || disabled) return;
      const active = document.activeElement;
      const inSearch = searchOpen && active === input.current;
      if (!inSearch && active?.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), .editor, .terms-panel')) return;
      e.preventDefault();
      if (inSearch) input.current?.select();
      else show(active, selectionInPages(pages.current));
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  });
  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  return { show, hide };
}

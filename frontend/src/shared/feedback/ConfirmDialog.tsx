import { createContext, useCallback, useContext, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import "./feedback.css";

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
}

type Pending = {
  owner: symbol;
  options: ConfirmOptions;
  trigger: HTMLElement | null;
  resolve: (accepted: boolean) => void;
};

const ConfirmContext = createContext<{
  open: (owner: symbol, options: ConfirmOptions) => Promise<boolean>;
  cancel: (owner: symbol) => void;
} | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const active = useRef<Pending | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const finish = useCallback((request: Pending, accepted: boolean) => {
    if (active.current !== request) return;
    active.current = null;
    setPending(null);
    request.resolve(accepted);
  }, []);
  const open = useCallback((owner: symbol, options: ConfirmOptions) => {
    if (active.current) return Promise.resolve(false);
    return new Promise<boolean>((resolve) => {
      const trigger = document.activeElement;
      const request = { owner, options, trigger: trigger instanceof HTMLElement ? trigger : null, resolve };
      active.current = request;
      setPending(request);
    });
  }, []);
  const cancel = useCallback((owner: symbol) => {
    if (active.current?.owner === owner) finish(active.current, false);
  }, [finish]);
  useLayoutEffect(() => () => {
    active.current?.resolve(false);
    active.current = null;
  }, []);

  return (
    <ConfirmContext.Provider value={{ open, cancel }}>
      {children}
      {pending && createPortal(<ConfirmDialog pending={pending} finish={finish} />, document.body)}
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): (options: ConfirmOptions) => Promise<boolean> {
  const context = useContext(ConfirmContext);
  const open = context?.open;
  const cancel = context?.cancel;
  const [owner] = useState(() => Symbol());
  useLayoutEffect(() => () => cancel?.(owner), [cancel, owner]);
  return useCallback((options) => open?.(owner, options) ?? Promise.resolve(false), [open, owner]);
}

export function hasOpenModal(): boolean {
  return Boolean(document.querySelector('dialog[open], [aria-modal="true"]'));
}

function ConfirmDialog({ pending, finish }: { pending: Pending; finish: (pending: Pending, result: boolean) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const initial = useRef<HTMLButtonElement>(null);
  const { options, trigger } = pending;
  const danger = options.tone === "danger";
  useLayoutEffect(() => {
    const el = dialog.current!;
    const overflow = document.body.style.overflow;
    const cancel = (e: Event) => {
      e.preventDefault();
      e.stopImmediatePropagation();
      finish(pending, false);
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.isComposing) cancel(e);
    };
    document.body.style.overflow = "hidden";
    if (typeof el.showModal === "function") el.showModal();
    else {
      el.setAttribute("open", "");
      el.dataset.fallback = "";
    }
    initial.current?.focus();
    el.addEventListener("cancel", cancel);
    window.addEventListener("keydown", escape, true);
    return () => {
      el.removeEventListener("cancel", cancel);
      window.removeEventListener("keydown", escape, true);
      if (typeof el.close === "function") el.close();
      else el.removeAttribute("open");
      document.body.style.overflow = overflow;
      if (trigger?.isConnected) trigger.focus();
    };
  }, [finish, pending, trigger]);

  return (
    <>
    <dialog
      ref={dialog}
      className="confirm-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
      aria-describedby="confirm-message"
      onClick={(e) => { if (e.target === e.currentTarget) finish(pending, false); }}
    >
      <div className="confirm-content">
        <h2 id="confirm-title">{options.title}</h2>
        <p id="confirm-message">{options.message}</p>
        <div className="confirm-actions">
          <button ref={danger ? initial : undefined} type="button" onClick={() => finish(pending, false)}>
            {options.cancelLabel ?? "取消"}
          </button>
          <button ref={danger ? undefined : initial} type="button" className={danger ? "danger" : "primary"}
            onClick={() => finish(pending, true)}>
            {options.confirmLabel ?? "确定"}
          </button>
        </div>
      </div>
    </dialog>
    <div className="confirm-fallback" aria-hidden="true" onClick={() => finish(pending, false)} />
    </>
  );
}

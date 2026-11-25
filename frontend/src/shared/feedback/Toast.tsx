import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import "./feedback.css";

type Tone = "success" | "info" | "error";
export type ToastApi = Record<Tone, (message: string) => void>;
type Notice = { id: number; tone: Tone; message: string };

const ToastContext = createContext<ToastApi>({ success: () => undefined, info: () => undefined, error: () => undefined });

export function useToast(): ToastApi {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Notice[]>([]);
  const { api, dismiss } = useMemo(() => {
    let nextId = 0;
    const add = (tone: Tone, message: string) => {
      const id = nextId++;
      setItems((items) => [...items.slice(-2), { id, tone, message }]);
    };
    return {
      api: {
        success: (message: string) => add("success", message),
        info: (message: string) => add("info", message),
        error: (message: string) => add("error", message),
      },
      dismiss: (id: number) => setItems((items) => items.filter((item) => item.id !== id)),
    };
  }, []);
  return (
    <ToastContext.Provider value={api}>
      {children}
      {items.length > 0 && createPortal(
        <div className="toast-stack">
          {items.map((item) => <Toast key={item.id} item={item} dismiss={dismiss} />)}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  );
}

function Toast({ item, dismiss }: { item: Notice; dismiss: (id: number) => void }) {
  const { id, tone, message } = item;
  useEffect(() => {
    const timer = setTimeout(() => dismiss(id), tone === "error" ? 5000 : 3000);
    return () => clearTimeout(timer);
  }, [id, tone, dismiss]);
  return (
    <div className={`toast toast-${tone}`}>
      <span role={tone === "error" ? "alert" : "status"}>{message}</span>
      <button type="button" className="quiet" aria-label="关闭提示" onClick={() => dismiss(id)}>×</button>
    </div>
  );
}

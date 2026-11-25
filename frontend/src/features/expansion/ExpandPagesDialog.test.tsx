import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type Expansion, type Job } from "@/shared/api/client";

import { ExpandPagesDialog } from "./ExpandPagesDialog";

const job: Job = {
  id: "synthetic", filename: "synthetic.pdf", status: "done", stage: "done", progress: 1,
  page_count: 3, content_version: 1, channel_mode: "free", model: "", message: "", error: "",
  cost_cny: null, fallback_used: false, created_at: "2026-01-01T00:00:00Z", stage_started_at: null, expires_at: null,
  options: {
    parser: "mineru-cloud", quality: "default", pages: [0],
    include_references: false, translate_figures: false, cloud_consent: true,
  },
};

describe("补翻弹窗的关闭行为", () => {
  let host: HTMLDivElement;
  let root: Root;
  let client: QueryClient;
  let overflow: string;
  const showModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "showModal");

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    overflow = document.body.style.overflow;
    document.body.style.overflow = "scroll";
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    client = new QueryClient();
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    client.clear();
    host.remove();
    document.body.style.overflow = overflow;
    if (showModal) Object.defineProperty(HTMLDialogElement.prototype, "showModal", showModal);
    else Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
    vi.restoreAllMocks();
  });

  async function setup(native = false) {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: native ? function (this: HTMLDialogElement) { this.setAttribute("open", ""); } : undefined,
    });
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>打开</button>
          {open && <ExpandPagesDialog job={job} published={new Set([0])}
            onClose={() => setOpen(false)} onStarted={() => setOpen(false)} />}
        </>
      );
    }
    await act(async () => root.render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>));
    const trigger = host.querySelector("button")!;
    trigger.focus();
    await act(async () => trigger.click());
    return { trigger, dialog: document.querySelector("dialog")! };
  }

  it("锁定滚动，点击表单外的原生遮罩关闭并恢复滚动与焦点", async () => {
    const { trigger, dialog } = await setup(true);
    expect(document.body.style.overflow).toBe("hidden");
    await act(async () => dialog.querySelector("h2")!.click());
    expect(dialog.open).toBe(true);
    await act(async () => dialog.click());
    expect(dialog.isConnected).toBe(false);
    expect(document.body.style.overflow).toBe("scroll");
    expect(document.activeElement).toBe(trigger);
  });

  it("降级时拦截 Escape，提交期间忽略关闭，提交失败后恢复关闭", async () => {
    let reject!: (reason: Error) => void;
    vi.spyOn(api, "expandPages").mockImplementation(() => new Promise<Expansion>((_, fail) => { reject = fail; }));
    const { trigger, dialog } = await setup();
    expect(dialog.dataset.fallback).toBe("");
    const escape = () => new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    const downstream = vi.fn();
    document.addEventListener("keydown", downstream);
    try {
      await act(async () => dialog.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
      await act(async () => {
        dialog.click();
        document.querySelector<HTMLDivElement>(".confirm-fallback")!.click();
        dialog.dispatchEvent(escape());
      });
      expect(dialog.isConnected).toBe(true);
      expect(document.body.style.overflow).toBe("hidden");
      await act(async () => reject(new Error("合成提交失败")));
      expect(dialog.textContent).toContain("合成提交失败");
      const event = escape();
      await act(async () => dialog.dispatchEvent(event));
      expect(event.defaultPrevented).toBe(true);
      expect(downstream).not.toHaveBeenCalled();
      expect(dialog.isConnected).toBe(false);
      expect(document.body.style.overflow).toBe("scroll");
      expect(document.activeElement).toBe(trigger);
    } finally {
      document.removeEventListener("keydown", downstream);
    }
  });
});

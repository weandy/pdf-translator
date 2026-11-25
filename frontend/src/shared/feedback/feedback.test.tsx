import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";

import { ConfirmProvider, useConfirm } from "./ConfirmDialog";
import { ToastProvider, useToast } from "./Toast";

let host: HTMLDivElement;
let root: Root;
beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function Trigger({ answer }: { answer: (result: boolean) => void }) {
  const confirm = useConfirm();
  return <button type="button" onClick={() => {
    void confirm({ title: "删除", message: "删除这篇论文？", tone: "danger" }).then(answer);
  }}>打开</button>;
}

it.each([true, false])("确认弹窗独占 Esc、互斥并恢复焦点（原生 showModal: %s）", async (native) => {
  const prototype = HTMLDialogElement.prototype;
  const wasShowModal = Object.getOwnPropertyDescriptor(prototype, "showModal");
  const wasClose = Object.getOwnPropertyDescriptor(prototype, "close");
  const show = vi.fn(function (this: HTMLDialogElement) { this.setAttribute("open", ""); });
  const close = function (this: HTMLDialogElement) { this.removeAttribute("open"); };
  Object.defineProperty(prototype, "showModal", { configurable: true, value: native ? show : undefined });
  Object.defineProperty(prototype, "close", { configurable: true, value: native ? close : undefined });
  const underlying = vi.fn();
  document.addEventListener("keydown", underlying);
  const answer = vi.fn();
  document.body.style.overflow = "auto";
  try {
    await act(() => root.render(<ConfirmProvider><Trigger answer={answer} /></ConfirmProvider>));
    const trigger = host.querySelector("button")!;
    trigger.focus();
    await act(() => { trigger.click(); trigger.click(); });
    expect(answer.mock.calls).toEqual([[false]]);
    expect(show).toHaveBeenCalledTimes(native ? 1 : 0);
    const dialog = document.querySelector("dialog[open]")!;
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.activeElement?.textContent).toBe("取消");
    expect(document.body.style.overflow).toBe("hidden");
    await act(() => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown",
      { key: "Escape", bubbles: true, cancelable: true })));
    expect(underlying).not.toHaveBeenCalled();
    expect(answer.mock.calls).toEqual([[false], [false]]);
    expect(document.activeElement).toBe(trigger);
    expect(document.body.style.overflow).toBe("auto");
  } finally {
    document.removeEventListener("keydown", underlying);
    document.body.style.overflow = "";
    if (wasShowModal) Object.defineProperty(prototype, "showModal", wasShowModal);
    else Reflect.deleteProperty(prototype, "showModal");
    if (wasClose) Object.defineProperty(prototype, "close", wasClose);
    else Reflect.deleteProperty(prototype, "close");
  }
});

it("调用方卸载时只关闭所属弹窗并 resolve false", async () => {
  const answer = vi.fn();
  function Owner() {
    const [enabled, setEnabled] = useState(true);
    return <><button type="button" onClick={() => setEnabled(false)}>卸载</button>{enabled && <Trigger answer={answer} />}</>;
  }
  await act(() => root.render(<ConfirmProvider><Owner /></ConfirmProvider>));
  const [unmount, trigger] = host.querySelectorAll("button");
  await act(() => trigger!.click());
  expect(document.querySelector("dialog[open]")).not.toBeNull();
  await act(() => unmount!.click());
  expect(answer).toHaveBeenCalledExactlyOnceWith(false);
  expect(document.querySelector("dialog")).toBeNull();
});

it("轻提示保留三条，按类型超时并允许手动关闭", async () => {
  vi.useFakeTimers();
  function Buttons() {
    const toast = useToast();
    return <button type="button" onClick={() => {
      toast.info("移除最旧");
      toast.success("成功");
      toast.info("消息");
      toast.error("失败");
    }}>提示</button>;
  }
  await act(() => root.render(<ToastProvider><Buttons /></ToastProvider>));
  await act(() => host.querySelector("button")!.click());
  expect([...document.querySelectorAll(".toast span")].map((node) => node.textContent)).toEqual(["成功", "消息", "失败"]);
  await act(() => document.querySelector<HTMLButtonElement>(".toast-info button")!.click());
  expect(document.querySelectorAll(".toast")).toHaveLength(2);
  await act(() => vi.advanceTimersByTimeAsync(3000));
  expect(document.querySelector(".toast-success")).toBeNull();
  expect(document.querySelector('.toast [role="alert"]')?.textContent).toBe("失败");
  await act(() => vi.advanceTimersByTimeAsync(2000));
  expect(document.querySelector(".toast")).toBeNull();
});

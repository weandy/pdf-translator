import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/shared/api/client";

import { DownloadChoice } from "./DownloadChoice";

describe("任务下载类型选择", () => {
  let host: HTMLDivElement;
  let root: Root;

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.restoreAllMocks();
  });

  async function render() {
    await act(async () => root.render(<DownloadChoice jobId="synthetic" />));
  }

  async function download() {
    await act(async () =>
      host.querySelector("a")!.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })),
    );
  }

  async function select(mode: "mono" | "dual") {
    const control = host.querySelector("select")!;
    await act(async () => {
      control.value = mode;
      control.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  it("默认双语右侧译文，切换仅译文及切回双语均使用对应参数并重置下载状态", async () => {
    const prepare = vi.spyOn(api, "prepareExport").mockResolvedValue({
      filename: "synthetic.pdf",
      download_url: "/api/jobs/synthetic/export",
      size_bytes: 100,
    });
    await render();
    expect(host.querySelector("select")!.value).toBe("dual");
    expect(host.querySelector("label")!.htmlFor).toBe(host.querySelector("select")!.id);
    await download();
    expect(prepare).toHaveBeenLastCalledWith("synthetic", "dual", "right");
    expect(host.textContent).toContain("已开始下载");
    await select("mono");
    expect(host.querySelector("a")!.textContent).toBe("下载 PDF");
    await download();
    expect(prepare).toHaveBeenLastCalledWith("synthetic", "mono", "right");
    await select("dual");
    await download();
    expect(prepare).toHaveBeenLastCalledWith("synthetic", "dual", "right");
    expect(prepare).toHaveBeenCalledTimes(3);
  });

  it("准备期间锁定类型并阻止重复下载，失败后可切换类型", async () => {
    let reject!: (reason: Error) => void;
    const prepare = vi.spyOn(api, "prepareExport").mockImplementation(
      () =>
        new Promise((_, fail) => {
          reject = fail;
        }),
    );
    await render();
    await download();
    expect(host.querySelector("select")!.disabled).toBe(true);
    expect(host.querySelector("a")!.getAttribute("aria-busy")).toBe("true");
    await download();
    expect(prepare).toHaveBeenCalledOnce();
    await act(async () => reject(new Error("合成准备失败")));
    expect(host.querySelector("select")!.disabled).toBe(false);
    expect(host.textContent).toContain("合成准备失败");
    await select("mono");
    expect(host.querySelector("a")!.textContent).toBe("下载 PDF");
  });
});

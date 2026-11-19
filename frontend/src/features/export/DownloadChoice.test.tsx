import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/shared/api/client";

import { DownloadChoice } from "./DownloadChoice";
import { DownloadMenu } from "./DownloadMenu";
import { ExportDownloadLink } from "./ExportDownloadLink";

const preparedPdf = {
  filename: "synthetic.pdf",
  download_url: "/api/jobs/synthetic/export",
  size_bytes: 100,
};

describe("任务下载", () => {
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

  function clickDownload(link = host.querySelector("a")!) {
    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  }

  async function download() {
    await act(async () => clickDownload());
  }

  async function select(mode: "mono" | "dual") {
    const control = host.querySelector("select")!;
    await act(async () => {
      control.value = mode;
      control.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  it("默认双语右侧译文，切换仅译文及切回双语均使用对应参数并重置下载状态", async () => {
    const prepare = vi.spyOn(api, "prepareExport").mockResolvedValue(preparedPdf);
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

  it("同格式可再次下载，反馈始终在按钮外更新", async () => {
    const prepare = vi.spyOn(api, "prepareExport").mockResolvedValue(preparedPdf);
    await render();
    const link = host.querySelector("a")!;
    const status = host.querySelector('[role="status"]')!;
    expect(status.textContent).toBe("");
    expect(link.getAttribute("aria-describedby")).toBe(status.id);

    await download();
    expect(status.textContent).toBe("已开始下载");
    expect(link.textContent).toBe("下载 PDF");
    expect(link.hasAttribute("aria-disabled")).toBe(false);
    await download();

    expect(prepare.mock.calls).toEqual([
      ["synthetic", "dual", "right"],
      ["synthetic", "dual", "right"],
    ]);
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(2);
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
    await act(async () => {
      clickDownload();
      clickDownload();
    });
    expect(host.querySelector("select")!.disabled).toBe(true);
    expect(host.querySelector("a")!.getAttribute("aria-busy")).toBe("true");
    expect(host.querySelector("a")!.textContent).toBe("下载 PDF");
    expect(host.querySelector('[role="status"]')!.textContent).toBe("正在准备 PDF…");
    await download();
    expect(prepare).toHaveBeenCalledOnce();
    await act(async () => reject(new Error("合成准备失败")));
    expect(host.querySelector("select")!.disabled).toBe(false);
    expect(host.querySelector("a")!.getAttribute("aria-disabled")).toBeNull();
    expect(host.querySelector("a")!.textContent).toBe("下载 PDF");
    expect(host.textContent).toContain("合成准备失败");
    await select("mono");
    expect(host.querySelector("a")!.textContent).toBe("下载 PDF");
  });

  it("控件卸载时释放准备锁，晚完成的请求不会重复释放", async () => {
    let complete!: (result: typeof preparedPdf) => void;
    vi.spyOn(api, "prepareExport").mockImplementation(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    const onPreparingChange = vi.fn();
    await act(async () =>
      root.render(
        <StrictMode>
          <ExportDownloadLink jobId="synthetic" mode="dual" onPreparingChange={onPreparingChange}>
            下载 PDF
          </ExportDownloadLink>
        </StrictMode>,
      ),
    );
    await download();
    expect(onPreparingChange.mock.calls).toEqual([[true]]);

    await act(async () => root.render(null));
    expect(onPreparingChange.mock.calls).toEqual([[true], [false]]);

    await act(async () => complete(preparedPdf));
    expect(onPreparingChange.mock.calls).toEqual([[true], [false]]);
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce();
  });

  it("阅读器关闭准备中的菜单后，可再次打开并下载同一种格式", async () => {
    let complete!: (result: typeof preparedPdf) => void;
    const prepare = vi
      .spyOn(api, "prepareExport")
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            complete = resolve;
          }),
      )
      .mockResolvedValue(preparedPdf);
    await act(async () => root.render(<DownloadMenu jobId="synthetic" />));
    const toggle = host.querySelector("button")!;
    const status = host.querySelector('[role="status"]')!;
    await act(async () => toggle.click());
    await download();
    expect(toggle.disabled).toBe(true);
    expect(toggle.textContent).toBe("下载 PDF");
    expect(status.textContent).toBe("正在准备 PDF…");
    expect([...host.querySelectorAll("a")].every((link) => link.getAttribute("aria-disabled") === "true")).toBe(true);

    await act(async () => document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    expect(host.querySelector('[role="menu"]')).toBeNull();
    await act(async () => complete(preparedPdf));
    expect(toggle.disabled).toBe(false);
    expect(status.textContent).toBe("已开始下载");

    await act(async () => toggle.click());
    await download();
    expect(prepare.mock.calls).toEqual([
      ["synthetic", "mono", "right"],
      ["synthetic", "mono", "right"],
    ]);
  });
});

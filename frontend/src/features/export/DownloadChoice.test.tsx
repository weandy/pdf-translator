import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/shared/api/client";

import { DownloadFormatSelect, useDownloadChoice } from "./DownloadChoice";
import { DownloadMenu } from "./DownloadMenu";
import { ExportDownloadLink } from "./ExportDownloadLink";

const preparedPdf = {
  status: "ready" as const,
  detail: "",
  snapshot: "0123456789abcdef",
  filename: "synthetic.pdf",
  download_url: "/api/jobs/synthetic/export",
  size_bytes: 100,
};

function DownloadControls({ cardKey }: { cardKey: string }) {
  const { mode, setMode, preparing, onPreparingChange } = useDownloadChoice(cardKey);
  return (
    <>
      <DownloadFormatSelect mode={mode} onChange={setMode} disabled={preparing} />
      <ExportDownloadLink key={mode} jobId="synthetic" mode={mode} onPreparingChange={onPreparingChange}>
        下载 PDF
      </ExportDownloadLink>
    </>
  );
}

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

  async function render(cardKey: string) {
    await act(async () => root.render(<DownloadControls key={cardKey} cardKey={cardKey} />));
  }

  function clickDownload(link = host.querySelector("a")!) {
    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  }

  async function download(link?: HTMLAnchorElement) {
    await act(async () => clickDownload(link));
  }

  async function select(mode: "mono" | "dual") {
    const control = host.querySelector("select")!;
    await act(async () => {
      control.value = mode;
      control.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  it("默认双语右侧译文，切换仅译文及切回双语均使用对应参数并重置下载状态", async () => {
    const prepare = vi.spyOn(api, "startExport").mockResolvedValue(preparedPdf);
    await render("switch-format");
    expect(host.querySelector("select")!.value).toBe("dual");
    expect(host.querySelector("label")!.htmlFor).toBe(host.querySelector("select")!.id);
    await download();
    expect(prepare).toHaveBeenLastCalledWith("synthetic", "dual", "right", expect.any(AbortSignal));
    expect(host.textContent).toContain("已开始下载");
    await select("mono");
    expect(host.querySelector("a")!.textContent).toBe("下载 PDF");
    await download();
    expect(prepare).toHaveBeenLastCalledWith("synthetic", "mono", "right", expect.any(AbortSignal));
    await select("dual");
    await download();
    expect(prepare).toHaveBeenLastCalledWith("synthetic", "dual", "right", expect.any(AbortSignal));
    expect(prepare).toHaveBeenCalledTimes(3);
  });

  it("同格式可再次下载，反馈始终在按钮外更新", async () => {
    const prepare = vi.spyOn(api, "startExport").mockResolvedValue(preparedPdf);
    await render("repeat-download");
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
      ["synthetic", "dual", "right", expect.any(AbortSignal)],
      ["synthetic", "dual", "right", expect.any(AbortSignal)],
    ]);
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(2);
  });

  it("用准备结果的快照链接下载，提示最多八个截断页码与总页数", async () => {
    const downloadUrl = "/api/jobs/synthetic/export?mode=dual&side=right&snapshot=0123456789abcdef";
    const withOverflow = { ...preparedPdf, download_url: downloadUrl, overflow_pages: [1, 2, 3, 4, 5, 7, 9, 12, 13] };
    vi.spyOn(api, "startExport").mockResolvedValue(withOverflow);
    const click = vi.mocked(HTMLAnchorElement.prototype.click);
    await render("overflow");
    await download();
    const status = host.querySelector('[role="status"]')!;
    expect(status.textContent).toBe("已开始下载，第 1、2、3、4、5、7、9、12 页等共 9 页部分译文未写入 PDF");
    expect(click).toHaveBeenCalledOnce();
    expect((click.mock.contexts[0] as HTMLAnchorElement).getAttribute("href")).toBe(downloadUrl);

    await select("mono");
    expect(host.querySelector('[role="status"]')!.textContent).toBe("");
  });

  it("再次准备或失败时清除上次的截断提示", async () => {
    let reject!: (reason: Error) => void;
    const withOverflow = { ...preparedPdf, overflow_pages: [3, 7] };
    vi.spyOn(api, "startExport")
      .mockResolvedValueOnce(withOverflow)
      .mockImplementationOnce(
        () =>
          new Promise((_, fail) => {
            reject = fail;
          }),
      );
    await render("overflow-retry");
    const status = host.querySelector('[role="status"]')!;
    await download();
    expect(status.textContent).toBe("已开始下载，第 3、7 页部分译文未写入 PDF");
    await act(async () => clickDownload());
    expect(status.textContent).toBe("正在准备 PDF…");
    await act(async () => reject(new Error("合成准备失败")));
    expect(status.textContent).toBe("合成准备失败");
  });

  it("返回任务列表后恢复所选格式，各卡片的格式独立", async () => {
    const prepare = vi.spyOn(api, "startExport").mockResolvedValue(preparedPdf);
    await render("saved-format");
    await select("mono");
    await act(async () => root.render(null));

    await render("other-card");
    expect(host.querySelector("select")!.value).toBe("dual");
    await render("saved-format");
    expect(host.querySelector("select")!.value).toBe("mono");
    await download();
    expect(prepare).toHaveBeenCalledWith("synthetic", "mono", "right", expect.any(AbortSignal));

    await select("dual");
    await act(async () => root.render(null));
    await render("saved-format");
    expect(host.querySelector("select")!.value).toBe("dual");
  });

  it("准备期间锁定类型并阻止重复下载，失败后可切换类型", async () => {
    let reject!: (reason: Error) => void;
    const prepare = vi.spyOn(api, "startExport").mockImplementation(
      () =>
        new Promise((_, fail) => {
          reject = fail;
        }),
    );
    await render("preparing");
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
    vi.spyOn(api, "startExport").mockImplementation(
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
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it("溢出页支持连续点击，切换任务时清空", async () => {
    vi.spyOn(api, "startExport").mockResolvedValue({ ...preparedPdf, overflow_pages: [5] });
    const locate = vi.fn();
    await act(async () => root.render(<DownloadMenu jobId="synthetic" onLocatePage={locate} />));
    await act(async () => host.querySelector("button")!.click());
    await download(document.querySelector<HTMLAnchorElement>(".download-menu a")!);
    const button = host.querySelector<HTMLButtonElement>('[aria-label="定位到第 5 页"]')!;
    await act(async () => {
      button.click();
      button.click();
    });
    expect(locate.mock.calls).toEqual([[5], [5]]);
    await act(async () => root.render(<DownloadMenu jobId="other" onLocatePage={locate} />));
    expect(host.querySelector('[aria-label="定位到第 5 页"]')).toBeNull();
  });

  it("下载菜单挂在 body 上，点击菜单内保持打开，Escape 关闭并归还焦点", async () => {
    await act(async () => root.render(<DownloadMenu jobId="synthetic" />));
    const toggle = host.querySelector("button")!;
    await act(async () => toggle.click());
    const menu = document.querySelector<HTMLDivElement>('.download-menu[role="menu"]')!;
    expect(menu.parentElement).toBe(document.body);
    expect(menu.style.position).toBe("fixed");
    expect(menu.querySelectorAll('[role="menuitem"]')).toHaveLength(3);
    await act(async () => menu.firstElementChild!.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true })));
    expect(menu.isConnected).toBe(true);
    await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Escape", bubbles: true, cancelable: true,
    })));
    expect(menu.isConnected).toBe(false);
    expect(document.activeElement).toBe(toggle);
  });

  it("阅读器关闭准备中的菜单后，可再次打开并下载同一种格式", async () => {
    let complete!: (result: typeof preparedPdf) => void;
    const prepare = vi
      .spyOn(api, "startExport")
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
    const menu = () => document.querySelector<HTMLDivElement>(".download-menu")!;
    await download(menu().querySelector("a")!);
    expect(toggle.disabled).toBe(true);
    expect(toggle.textContent).toBe("下载 PDF");
    expect(status.textContent).toBe("正在准备 PDF…");
    expect([...menu().querySelectorAll("a")].map((link) => link.getAttribute("aria-disabled"))).toEqual([
      "true", "true", "true",
    ]);

    await act(async () => document.body.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true })));
    expect(menu().hidden).toBe(true);
    expect(menu().getAttribute("role")).toBeNull();
    await act(async () => complete(preparedPdf));
    expect(toggle.disabled).toBe(false);
    expect(status.textContent).toBe("已开始下载");

    await act(async () => toggle.click());
    await download(menu().querySelector("a")!);
    expect(prepare.mock.calls).toEqual([
      ["synthetic", "mono", "right", expect.any(AbortSignal)],
      ["synthetic", "mono", "right", expect.any(AbortSignal)],
    ]);
  });
});

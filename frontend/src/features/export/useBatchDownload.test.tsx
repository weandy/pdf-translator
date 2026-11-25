import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterAll, beforeAll, expect, it, vi } from "vitest";

import { api, ApiError, type ExportStatus } from "@/shared/api/client";

import { useBatchDownload } from "./useBatchDownload";

beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));

const jobs = ["one", "two"].map((jobId) => ({ jobId, mode: "mono", side: "right" }) as const);
const ready = (snapshot: string): ExportStatus => ({
  status: "ready", detail: "", snapshot, size_bytes: 5, filename: "a.pdf", download_url: "/export",
});

it("依次准备，签发冲突时仅重做失效项一次并启动原生下载", async () => {
  const host = document.createElement("div");
  const root = createRoot(host);
  const preparing = vi.fn();
  const start = vi.spyOn(api, "startExport")
    .mockResolvedValueOnce(ready("first")).mockResolvedValueOnce(ready("second")).mockResolvedValueOnce(ready("updated"));
  const issue = vi.spyOn(api, "createExportBundle")
    .mockRejectedValueOnce(new ApiError(409, "合成快照失效", [1]))
    .mockResolvedValueOnce({ download_url: "/bundle", filename: "translations.zip", size_bytes: 100, item_count: 2, expires_in_s: 180 });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  function Probe() {
    const batch = useBatchDownload(preparing);
    return <button onClick={() => void batch.start(jobs)}>批量下载</button>;
  }
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => host.querySelector("button")!.click());
    expect(start.mock.calls.map(([id]) => id)).toEqual(["one", "two", "two"]);
    expect(issue.mock.calls[1]?.[0].map((item) => item.snapshot)).toEqual(["first", "updated"]);
    expect(click).toHaveBeenCalledTimes(1);
    expect(preparing.mock.calls).toEqual([[true], [false]]);
  } finally {
    await act(async () => root.unmount());
    vi.restoreAllMocks();
  }
});

it("取消后清除进度并忽略迟到的准备结果", async () => {
  const host = document.createElement("div");
  const root = createRoot(host);
  const preparing = vi.fn();
  let complete!: (result: ExportStatus) => void;
  const start = vi.spyOn(api, "startExport").mockImplementation(() => new Promise((resolve) => { complete = resolve; }));
  const issue = vi.spyOn(api, "createExportBundle");
  function Probe() {
    const batch = useBatchDownload(preparing);
    return <>
      <button onClick={() => void batch.start(jobs)}>批量下载</button>
      {batch.progress && <button onClick={batch.cancel}>取消 {batch.progress.current}/{batch.progress.total}</button>}
    </>;
  }
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => host.querySelector("button")!.click());
    const cancel = host.querySelectorAll("button")[1]!;
    expect(cancel.textContent).toBe("取消 1/2");
    await act(async () => cancel.click());
    expect(start.mock.calls[0]?.[3].aborted).toBe(true);
    expect(host.querySelectorAll("button")).toHaveLength(1);
    await act(async () => complete(ready("late")));
    expect(start).toHaveBeenCalledTimes(1);
    expect(issue).not.toHaveBeenCalled();
    expect(preparing.mock.calls).toEqual([[true], [false]]);
  } finally {
    await act(async () => root.unmount());
    vi.restoreAllMocks();
  }
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULTS, saveSettings } from "@/entities/settings/model";
import { api, type Job, type ServerConfig } from "@/shared/api/client";

import { UploadPanel } from "./UploadPanel";

vi.mock("@/shared/pdf/pdf", () => ({ countPages: vi.fn().mockResolvedValue(3) }));
vi.mock("@/shared/file/fingerprint", () => ({ fingerprint: vi.fn().mockResolvedValue(null) }));
vi.mock("@/entities/job/model", () => ({ rememberJob: vi.fn() }));

describe("共享 MinerU 上传流程", () => {
  let host: HTMLDivElement;
  let root: Root;
  let client: QueryClient;
  const onCreated = vi.fn();
  beforeAll(() => {
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  });
  afterAll(() => {
    Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
    Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
  });
  beforeEach(() => {
    saveSettings({ ...DEFAULTS, llmKey: "synthetic-llm-key", cloudConsent: true });
    host = document.createElement("div");
    root = createRoot(host);
    client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    vi.spyOn(api, "estimate").mockResolvedValue({ pages: 3, cny_low: 0.01, cny_high: 0.02, peak: false, minutes: 1 });
    onCreated.mockClear();
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    client.clear();
    saveSettings({ ...DEFAULTS });
    vi.restoreAllMocks();
  });

  async function render(shared: boolean) {
    const config: ServerConfig = {
      version: "test",
      default_base_url: "https://example.invalid",
      default_model: "synthetic-model",
      default_protocol: "openai",
      server_has_llm_key: false,
      server_has_parser_token: shared,
      server_has_paddle_token: false,
      max_upload_mb: 50,
      max_pages: 200,
      retention_hours: 24,
      submission_audit_enabled: false,
      attributions: [],
    };
    client.setQueryData(["config"], config);
    await act(async () =>
      root.render(
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <UploadPanel onCreated={onCreated} onBatchCreated={vi.fn()} onNeedSettings={vi.fn()} />
          </MemoryRouter>
        </QueryClientProvider>,
      ),
    );
    await act(async () => {
      const input = host.querySelector('input[type="file"]')!;
      Object.defineProperty(input, "files", {
        value: [new File(["synthetic"], "synthetic.pdf", { type: "application/pdf" })],
      });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  const startButton = () => [...host.querySelectorAll("button")].find((button) => button.textContent === "开始翻译")!;

  it("共享 Token 可用时只填写翻译 Key 即可提交 PDF", async () => {
    const job = { id: "synthetic-job" } as Job;
    const create = vi.spyOn(api, "createJob").mockResolvedValue(job);
    await render(true);
    expect(host.querySelector(".setup-hint")).toBeNull();
    expect(startButton().disabled).toBe(false);
    await act(async () => startButton().click());
    expect(create).toHaveBeenCalledOnce();
    expect(onCreated).toHaveBeenCalledWith(job);
  });

  it("没有共享 Token 时仍阻止缺少个人 Token 的上传", async () => {
    const create = vi.spyOn(api, "createJob");
    await render(false);
    expect(host.querySelector(".setup-hint")?.textContent).toContain("MinerU Token");
    expect(host.querySelector(".setup-hint")?.textContent).not.toContain("两者");
    expect(startButton().disabled).toBe(true);
    expect(create).not.toHaveBeenCalled();
  });

  it("只缺翻译 Key 时明确站点已提供解析，避免要求用户再申请 Token", async () => {
    saveSettings({ ...DEFAULTS });
    await render(true);
    expect(host.querySelector(".setup-hint")?.textContent).toContain("翻译服务的 API Key");
    expect(host.querySelector(".setup-hint")?.textContent).toContain("本站已提供 MinerU 版面解析");
    expect(host.querySelector(".setup-hint")?.textContent).not.toContain("免费申请");
  });

  it.each([false, true])("已有个人解析 Token 时正确显示来源（共享解析：%s）", async (shared) => {
    saveSettings({ ...DEFAULTS, parserKey: "synthetic-parser-token" });
    await render(shared);
    expect(host.querySelector(".setup-hint")?.textContent).toContain("版面解析将使用你自己的 MinerU Token");
    expect(host.querySelector(".setup-hint")?.textContent).not.toContain("本站已提供 MinerU 版面解析");
    expect(host.querySelector(".setup-hint")?.textContent).not.toContain("MinerU Token 在 MinerU API 管理中申请");
  });
});

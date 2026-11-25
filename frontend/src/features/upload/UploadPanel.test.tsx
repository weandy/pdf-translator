import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { addHistory, clearHistory } from "@/entities/history/model";
import { DEFAULTS, getSettings, saveSettings } from "@/entities/settings/model";
import { api, type Job, type ServerConfig } from "@/shared/api/client";
import { fingerprint } from "@/shared/file/fingerprint";
import { countPages } from "@/shared/pdf/pdf";

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
    vi.mocked(countPages).mockReset().mockResolvedValue(3);
    vi.mocked(fingerprint).mockReset().mockResolvedValue(null);
    saveSettings({
      ...DEFAULTS,
      llmKey: "synthetic-llm-key",
      llmKeyBoundUrl: "https://example.invalid",
      llmKeyBoundProtocol: "openai",
      cloudConsent: true,
    });
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

  function makeConfig(shared: boolean, audit = false, overrides: Partial<ServerConfig> = {}): ServerConfig {
    return {
      version: "test",
      default_base_url: "https://example.invalid",
      default_mineru_base_url: "https://mineru.net",
      default_model: "synthetic-model",
      default_protocol: "openai",
      admin_path: "/weandy",
      server_has_llm_key: false,
      server_has_parser_token: shared,
      server_has_paddle_token: false,
      paddle_public_enabled: false,
      maintenance_mode: false,
      site_notice: "",
      max_upload_mb: 50,
      max_pages: 200,
      retention_hours: 24,
      submission_audit_enabled: audit,
      attributions: [],
      ...overrides,
    };
  }

  async function render(shared: boolean, audit = false, overrides: Partial<ServerConfig> = {}) {
    const config = makeConfig(shared, audit, overrides);
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
        configurable: true,
        value: [new File(["synthetic"], "synthetic.pdf", { type: "application/pdf" })],
      });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  const startButton = () => [...host.querySelectorAll("button")].find((button) => button.textContent === "开始翻译")!;
  const findButton = (text: string) =>
    [...host.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent?.trim() === text);

  it("只有明确开启提交账本时展示有限性告知（含 IP 与 User-Agent 说明）", async () => {
    await render(true);
    expect(host.textContent).not.toContain("本站长期保存论文标题");
    await act(async () => root.unmount());
    root = createRoot(host);
    await render(true, true);
    expect(host.querySelector('[role="note"]')?.textContent).toContain("必要信息");
    expect(host.querySelector('[role="note"]')?.textContent).toContain("User-Agent");
    expect(host.querySelector('[role="note"]')?.textContent).toContain("公开统计仅展示聚合数量");
    expect(host.querySelector('a[href="/guide#privacy"]')).not.toBeNull();
  });

  it("指纹和历史查询期间禁止提交，忙态冻结拖放与选文件并拦截双击", async () => {
    let finishHash!: (hash: string) => void;
    vi.mocked(fingerprint).mockImplementationOnce(() => new Promise((resolve) => { finishHash = resolve; }));
    addHistory({ id: "history", hash: "synthetic-hash", filename: "旧样本.pdf", pages: 3, createdAt: "2026-01-01" });
    let finishHistory!: (job: Job) => void;
    const history = vi.spyOn(api, "job").mockImplementationOnce(() => new Promise((resolve) => { finishHistory = resolve; }));
    let finishUpload!: (job: Job) => void;
    const create = vi.spyOn(api, "createJob").mockImplementationOnce(() => new Promise((resolve) => { finishUpload = resolve; }));
    try {
      await render(true);
      await vi.waitFor(() => expect(host.textContent).toContain("3 页 · 检查历史…"));
      const forceClick = async () => {
        const button = startButton();
        expect(button.disabled).toBe(true);
        await act(() => {
          button.disabled = false;
          button.click();
          button.disabled = true;
        });
      };
      await forceClick();
      expect(create).not.toHaveBeenCalled();
      await act(() => finishHash("synthetic-hash"));
      expect(history).toHaveBeenCalledExactlyOnceWith("history");
      await forceClick();
      expect(create).not.toHaveBeenCalled();
      await act(() => finishHistory({ id: "history" } as Job));
      await act(() => findButton("仍要重新翻译")!.click());
      const start = startButton();
      await act(() => { start.click(); start.click(); });
      expect(create).toHaveBeenCalledTimes(1);
      const fileInput = host.querySelector<HTMLInputElement>('input[type="file"]')!;
      expect(fileInput.disabled).toBe(true);
      const next = new File(["next"], "第二篇.pdf", { type: "application/pdf" });
      const drop = new Event("drop", { bubbles: true, cancelable: true });
      Object.defineProperty(drop, "dataTransfer", { value: { files: [next] } });
      await act(() => {
        fileInput.closest("label")!.dispatchEvent(drop);
        Object.defineProperty(fileInput, "files", { configurable: true, value: [next] });
        fileInput.dispatchEvent(new Event("change", { bubbles: true }));
      });
      expect(drop.defaultPrevented).toBe(true);
      expect(vi.mocked(countPages)).toHaveBeenCalledTimes(1);
      expect(host.textContent).not.toContain("第二篇.pdf");
      await act(() => finishUpload({ id: "created" } as Job));
    } finally {
      clearHistory();
    }
  });

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

  it("一次选择多个 PDF 时限制 countPages 并发度不超过 2，避免浏览器内存尖峰", async () => {
    const pdfMod = await import("@/shared/pdf/pdf");
    let active = 0;
    let peak = 0;
    const resolvers: Array<() => void> = [];
    vi.mocked(pdfMod.countPages).mockImplementation(
      () =>
        new Promise<number>((resolve) => {
          active += 1;
          peak = Math.max(peak, active);
          resolvers.push(() => {
            active -= 1;
            resolve(5);
          });
        }),
    );

    const config: ServerConfig = {
      version: "test",
      default_base_url: "https://example.invalid",
      default_mineru_base_url: "https://mineru.net",
      default_model: "synthetic-model",
      default_protocol: "openai",
      admin_path: "/weandy",
      server_has_llm_key: false,
      server_has_parser_token: true,
      server_has_paddle_token: false,
      paddle_public_enabled: false,
      maintenance_mode: false,
      site_notice: "",
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

    const files = Array.from(
      { length: 6 },
      (_, i) => new File(["synthetic"], `paper-${i + 1}.pdf`, { type: "application/pdf" }),
    );
    await act(async () => {
      const input = host.querySelector('input[type="file"]')!;
      Object.defineProperty(input, "files", { configurable: true, value: files });
      input.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(peak).toBeLessThanOrEqual(2);
    expect(resolvers).toHaveLength(2);

    while (resolvers.length > 0) {
      await act(async () => {
        resolvers.shift()?.();
        await Promise.resolve();
        await Promise.resolve();
      });
    }
    expect(peak).toBe(2);
    expect(host.textContent).toContain("共 30 页");
  });

  it("快速重新选择文件时丢弃上一批延迟返回的计页结果", async () => {
    const pdfMod = await import("@/shared/pdf/pdf");
    let resolveFirst: ((n: number) => void) | null = null;
    vi.mocked(pdfMod.countPages).mockImplementation((file: File) => {
      if (file.name === "old.pdf") {
        return new Promise<number>((resolve) => {
          resolveFirst = resolve;
        });
      }
      return Promise.resolve(9);
    });

    const config: ServerConfig = {
      version: "test",
      default_base_url: "https://example.invalid",
      default_mineru_base_url: "https://mineru.net",
      default_model: "synthetic-model",
      default_protocol: "openai",
      admin_path: "/weandy",
      server_has_llm_key: false,
      server_has_parser_token: true,
      server_has_paddle_token: false,
      paddle_public_enabled: false,
      maintenance_mode: false,
      site_notice: "",
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

    const fpMod = await import("@/shared/file/fingerprint");
    vi.mocked(fpMod.fingerprint).mockClear();

    const input = host.querySelector('input[type="file"]')!;
    await act(async () => {
      Object.defineProperty(input, "files", {
        configurable: true,
        value: [new File(["old"], "old.pdf", { type: "application/pdf" })],
      });
      input.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });
    await act(async () => {
      Object.defineProperty(input, "files", {
        configurable: true,
        value: [new File(["fresh"], "fresh.pdf", { type: "application/pdf" })],
      });
      input.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      resolveFirst?.(999);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(host.textContent).toContain("fresh.pdf");
    expect(host.textContent).toContain("9 页");
    expect(host.textContent).not.toContain("999");
    expect(vi.mocked(fpMod.fingerprint)).toHaveBeenCalledTimes(1);
    expect((vi.mocked(fpMod.fingerprint).mock.calls[0]?.[0] as File | undefined)?.name).toBe("fresh.pdf");
  });

  it("组件卸载后立即中止多选 PDF 队列中尚未开始的计页检查", async () => {
    const pdfMod = await import("@/shared/pdf/pdf");
    const resolvers: Array<() => void> = [];
    vi.mocked(pdfMod.countPages).mockImplementation(
      () =>
        new Promise<number>((resolve) => {
          resolvers.push(() => resolve(4));
        }),
    );

    const config: ServerConfig = {
      version: "test",
      default_base_url: "https://example.invalid",
      default_mineru_base_url: "https://mineru.net",
      default_model: "synthetic-model",
      default_protocol: "openai",
      admin_path: "/weandy",
      server_has_llm_key: false,
      server_has_parser_token: true,
      server_has_paddle_token: false,
      paddle_public_enabled: false,
      maintenance_mode: false,
      site_notice: "",
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

    const files = Array.from(
      { length: 5 },
      (_, i) => new File(["x"], `leave-${i + 1}.pdf`, { type: "application/pdf" }),
    );
    await act(async () => {
      const input = host.querySelector('input[type="file"]')!;
      Object.defineProperty(input, "files", { configurable: true, value: files });
      input.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(resolvers).toHaveLength(2);

    // 卸载后再结束前两个正在进行的 countPages，不应再启动第 3~5 个文件
    await act(async () => {
      root.unmount();
    });
    while (resolvers.length > 0) {
      await act(async () => {
        resolvers.shift()?.();
        await Promise.resolve();
        await Promise.resolve();
      });
    }
    expect(vi.mocked(pdfMod.countPages)).toHaveBeenCalledTimes(2);
  });

  it("老用户未绑定或管理员修改默认地址时拦截 BYOK 提交，确认绑定后恢复提交", async () => {
    saveSettings({
      ...DEFAULTS,
      llmKey: "synthetic-llm-key",
      baseUrl: "",
      llmKeyBoundUrl: "https://old-default.example.invalid",
      llmKeyBoundProtocol: "openai",
      cloudConsent: true,
    });
    const job = { id: "bound-job" } as Job;
    const create = vi.spyOn(api, "createJob").mockResolvedValue(job);
    await render(true, false, { default_base_url: "https://new-default.example.invalid" });

    expect(host.querySelector(".key-rebind-notice")?.textContent).toContain("https://new-default.example.invalid");
    expect(startButton().disabled).toBe(true);
    expect(create).not.toHaveBeenCalled();

    await act(async () => findButton("确认发送到此地址")!.click());
    expect(getSettings().llmKeyBoundUrl).toBe("https://new-default.example.invalid");
    expect(host.querySelector(".key-rebind-notice")).toBeNull();
    expect(startButton().disabled).toBe(false);

    await act(async () => startButton().click());
    expect(create).toHaveBeenCalledOnce();
  });

  it("/api/config 加载中或加载失败时禁用提交避免误用 BYOK，点击重试加载后恢复", async () => {
    const create = vi.spyOn(api, "createJob");
    let resolveConfig!: (cfg: ServerConfig) => void;
    const configSpy = vi
      .spyOn(api, "config")
      .mockImplementationOnce(
        () =>
          new Promise<ServerConfig>((resolve) => {
            resolveConfig = resolve;
          }),
      )
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce(makeConfig(true));

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
        configurable: true,
        value: [new File(["synthetic"], "wait.pdf", { type: "application/pdf" })],
      });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(host.textContent).toContain("正在加载站点配置");
    expect(startButton().disabled).toBe(true);
    expect(create).not.toHaveBeenCalled();

    // 卸载并模拟首次加载直接失败
    await act(async () => {
      resolveConfig(makeConfig(true));
      root.unmount();
    });
    client.clear();
    root = createRoot(host);
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
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await act(async () => {
      const input = host.querySelector('input[type="file"]')!;
      Object.defineProperty(input, "files", {
        configurable: true,
        value: [new File(["synthetic"], "err.pdf", { type: "application/pdf" })],
      });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(host.textContent).toContain("无法获取站点配置，为避免误用计费通道已暂停提交");
    expect(startButton().disabled).toBe(true);
    expect(create).not.toHaveBeenCalled();

    await act(async () => findButton("重试加载配置")!.click());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(configSpy).toHaveBeenCalledTimes(3);
    expect(startButton().disabled).toBe(false);
  });

  it("免费通道关闭且用户此前选过免费模式时明确提示将使用自备 Key", async () => {
    saveSettings({
      ...DEFAULTS,
      llmKey: "synthetic-llm-key",
      llmKeyBoundUrl: "https://example.invalid",
      llmKeyBoundProtocol: "openai",
      translationMode: "free",
      cloudConsent: true,
    });
    await render(true, false, {
      free_channel: { enabled: false, model_display: "", max_pages: null, notice: "" },
    });
    expect(host.textContent).toContain("免费体验通道当前未开放");
    expect(host.textContent).toContain("自备 API Key");
  });

  it("paddle_public_enabled 开启且有服务 Token 时支持 PaddleOCR-VL；仅 Paddle 可用时默认自动选中并可切换", async () => {
    await render(true, false, { paddle_public_enabled: false, server_has_paddle_token: false });
    expect(host.querySelector('[aria-label="版面解析服务"]')).toBeNull();

    await act(async () => root.unmount());
    root = createRoot(host);
    const job = { id: "paddle-job" } as Job;
    const create = vi.spyOn(api, "createJob").mockResolvedValue(job);
    // 关闭 MinerU 共享 Token 且无个人 Token 时，默认自动选用 PaddleOCR-VL 且不误报缺少 MinerU Token
    await render(false, false, { paddle_public_enabled: true, server_has_paddle_token: true });
    expect(host.querySelector('[aria-label="版面解析服务"]')).not.toBeNull();
    expect(host.querySelector(".consent")?.textContent).toContain("我知道文件会上传到 PaddleOCR 云端解析");
    expect(host.querySelector(".setup-hint")).toBeNull();
    expect(startButton().disabled).toBe(false);

    // 手动切回 MinerU 时才提示缺少 MinerU Token 并禁用提交
    await act(async () => findButton("MinerU")!.click());
    expect(host.querySelector(".consent")?.textContent).toContain("MinerU");
    expect(startButton().disabled).toBe(true);

    // 切回 PaddleOCR-VL 恢复并提交
    await act(async () => findButton("PaddleOCR-VL")!.click());
    expect(host.querySelector(".consent")?.textContent).toContain("我知道文件会上传到 PaddleOCR 云端解析");
    expect(host.querySelector(".setup-hint")).toBeNull();
    expect(startButton().disabled).toBe(false);

    await act(async () => startButton().click());
    expect(create).toHaveBeenCalledWith(
      expect.any(File),
      expect.objectContaining({ parser: "paddleocr-vl", cloud_consent: true }),
      expect.objectContaining({ mode: "byok" }),
    );
  });

  it("仅公开 PaddleOCR-VL（无服务端 MinerU Token）时免费模式默认自动选 paddleocr-vl 且可直接提交", async () => {
    saveSettings({
      ...DEFAULTS,
      translationMode: "free",
      cloudConsent: true,
    });
    const job = { id: "free-paddle-job" } as Job;
    const create = vi.spyOn(api, "createJob").mockResolvedValue(job);

    await render(false, false, {
      free_channel: { enabled: true, model_display: "demo-free", max_pages: 40, notice: "" },
      paddle_public_enabled: true,
      server_has_paddle_token: true,
    });

    expect(host.querySelector(".setup-hint")).toBeNull();
    expect(host.querySelector(".consent")?.textContent).toContain("我知道文件会上传到 PaddleOCR 云端解析");
    const freeStart = findButton("开始免费翻译")!;
    expect(freeStart).toBeTruthy();
    expect(freeStart.disabled).toBe(false);

    await act(async () => freeStart.click());
    expect(create).toHaveBeenCalledWith(
      expect.any(File),
      expect.objectContaining({ parser: "paddleocr-vl", cloud_consent: true }),
      expect.objectContaining({
        mode: "free",
        headers: { "X-Translation-Mode": "free" },
      }),
    );
  });

  it("方案 A 纯白虚线卡片精简大段正文，勾选框可直接触发服务条款弹窗回调", async () => {
    const onOpenTerms = vi.fn();
    client.setQueryData(["config"], makeConfig(true, false));
    await act(async () =>
      root.render(
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <UploadPanel
              onCreated={onCreated}
              onBatchCreated={vi.fn()}
              onNeedSettings={vi.fn()}
              onOpenTerms={onOpenTerms}
            />
          </MemoryRouter>
        </QueryClientProvider>,
      ),
    );
    expect(host.querySelector(".upload-card")).not.toBeNull();
    expect(host.querySelector(".dropzone")).not.toBeNull();
    expect(host.querySelector(".spread")).toBeNull();
    expect(host.querySelector(".how-to")).toBeNull();
    const tosBtn = findButton("《服务条款与隐私说明》")!;
    expect(tosBtn).toBeTruthy();
    await act(async () => tosBtn.click());
    expect(onOpenTerms).toHaveBeenCalledOnce();
  });

  it("BYOK 使用个人 MinerU Token 且未绑定当前解析地址时拦截提交，确认后恢复并附带 X-Parser-Base-URL", async () => {
    saveSettings({
      ...DEFAULTS,
      llmKey: "synthetic-llm-key",
      llmKeyBoundUrl: "https://example.invalid",
      llmKeyBoundProtocol: "openai",
      parserKey: "personal-mineru-token",
      parserKeyBoundUrl: "mineru-cloud",
      cloudConsent: true,
    });
    const job = { id: "parser-bound-job" } as Job;
    const create = vi.spyOn(api, "createJob").mockResolvedValue(job);

    await render(true, false, { default_mineru_base_url: "https://mineru-alt.example.invalid" });
    expect(host.querySelector(".key-rebind-notice")?.textContent).toContain("https://mineru-alt.example.invalid");
    expect(startButton().disabled).toBe(true);
    expect(create).not.toHaveBeenCalled();

    await act(async () => findButton("确认发送到此解析地址")!.click());
    expect(getSettings().parserKeyBoundUrl).toBe("https://mineru-alt.example.invalid");
    expect(host.querySelector(".key-rebind-notice")).toBeNull();
    expect(startButton().disabled).toBe(false);

    await act(async () => startButton().click());
    expect(create).toHaveBeenCalledWith(
      expect.any(File),
      expect.objectContaining({ cloud_consent: true }),
      expect.objectContaining({
        mode: "byok",
        headers: expect.objectContaining({
          "X-Parser-Key": "personal-mineru-token",
          "X-Parser-Base-URL": "https://mineru-alt.example.invalid",
        }),
      }),
    );
  });
});

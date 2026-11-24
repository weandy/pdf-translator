import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminConsolePage, AdminLoginPage, AdminSubmissionsPage } from "@/pages/AdminPage";
import {
  adminApi,
  AdminApiError,
  type AdminConfigView,
  type AdminOverview,
  type AdminSession,
  type ConfigRevisionsOut,
  type SubmissionPage,
} from "@/shared/api/admin";

function makeSession(offsetMs = 3600000): AdminSession {
  return {
    csrf_token: "synthetic-csrf",
    expires_at: new Date(Date.now() + offsetMs).toISOString(),
  };
}

function makeOverview(overrides?: Partial<AdminOverview>): AdminOverview {
  return {
    queues: {
      parse: { in_use: 1, limit: 4, waiting: 0 },
      free: { in_use: 1, limit: 1, waiting: 2 },
      byok: { in_use: 0, limit: 2, waiting: 0 },
      parse_reserved_for_byok: true,
    },
    services: {
      free_enabled: true,
      free_ready: true,
      usable_providers: 1,
      mineru_configured: true,
      paddle_configured: false,
      glossary_terms: 14820,
      glossary_reloading: false,
    },
    warnings: [],
    stats: {
      today_submitted: 18,
      today_completed: 16,
      period_submitted: 120,
      period_completed: 114,
      failed_24h: 1,
      recent_failures: [
        {
          job_id: "fail-job-1",
          title: "Long-Context Scaling Laws",
          submitted_at: "2026-11-24T05:00:00Z",
          channel_mode: "free",
          failure_reason: "上游超时",
          retryable: true,
        },
      ],
      client_breakdown: {
        total: 100,
        devices: [
          { label: "desktop", count: 78, share: 0.78 },
          { label: "mobile", count: 16, share: 0.16 },
          { label: "tablet", count: 4, share: 0.04 },
          { label: "bot", count: 2, share: 0.02 },
        ],
        platforms: [
          { label: "Windows · Chrome", count: 45, share: 0.45 },
          { label: "macOS · Safari", count: 33, share: 0.33 },
        ],
      },
      client_breakdown_source: "job",
    },
    active_jobs: [
      {
        job_id: "abcdef1234567890",
        page_count: 12,
        stage: "translate",
        progress: 0.45,
        channel_mode: "free",
        model: "nvidia/nemotron-3-ultra-550b",
        status: "running",
      },
    ],
    maintenance_mode: false,
    revision: 15,
    secret_key_ready: true,
    ...overrides,
  };
}

function makeConfig(overrides?: Partial<AdminConfigView>): AdminConfigView {
  return {
    revision: 15,
    updated_at: "2026-11-24T10:00:00Z",
    secret_key_ready: true,
    warnings: [],
    free_translation: {
      enabled: true,
      max_pages: 40,
      notice: "免费体验仅建议公开论文。",
      ready: true,
      providers: [
        {
          id: "nim-main",
          name: "NVIDIA NIM 主力",
          enabled: true,
          protocol: "openai",
          base_url: "https://integrate.api.nvidia.com/v1",
          model: "nvidia/nemotron-3-ultra-550b",
          fallback_model: "nvidia/nemotron-super-120b",
          timeout_s: 65,
          api_key: {
            mode: "custom",
            configured: true,
            mask: "nvap...nv7_",
            bound_url: "https://integrate.api.nvidia.com/v1",
            bound_protocol: "openai",
            corrupted: false,
          },
          usable: true,
        },
      ],
    },
    parser: {
      mineru_base_url: "https://mineru.net",
      mineru_token: {
        mode: "custom",
        configured: true,
        mask: "eyJ0...9kQw",
        bound_url: "https://mineru.net",
        bound_protocol: "",
        corrupted: false,
      },
      mineru_proxy: {
        mode: "custom",
        configured: true,
        mask: "socks5h://***@proxy.example:1080",
        bound_url: "",
        bound_protocol: "",
        corrupted: false,
      },
      paddle_public_enabled: false,
      paddle_token: {
        mode: "inherit_env",
        configured: false,
        mask: "",
        bound_url: "",
        bound_protocol: "",
        corrupted: false,
      },
      paddle_proxy: {
        mode: "inherit_env",
        configured: false,
        mask: "",
        bound_url: "",
        bound_protocol: "",
        corrupted: false,
      },
    },
    default_llm: {
      base_url: "https://api.deepseek.com",
      protocol: "openai",
      model: "deepseek-flash",
      presets: [
        {
          id: "deepseek",
          name: "DeepSeek",
          protocol: "openai",
          base_url: "https://api.deepseek.com",
          models: ["deepseek-flash", "deepseek-v4-pro"],
        },
      ],
    },
    pricing: {
      custom_configured: true,
      models: [
        { model: "deepseek-flash", cache_hit: 0.02, cache_miss: 1.0, output: 4.0, peak_factor: 2.0 },
      ],
    },
    glossary: {
      sources: ["aitd", "d2l"],
      available: [
        {
          id: "aitd",
          name: "机器之心 AI 术语库",
          license: "CC BY-NC-SA 4.0",
          commercial_ok: false,
          enabled: true,
          priority: 1,
          cached: true,
        },
        {
          id: "d2l",
          name: "《动手学深度学习》术语表",
          license: "Apache-2.0",
          commercial_ok: true,
          enabled: true,
          priority: 2,
          cached: true,
        },
        {
          id: "ml-technical-term",
          name: "ML 中英术语表",
          license: "MIT",
          commercial_ok: true,
          enabled: false,
          priority: null,
          cached: true,
        },
        {
          id: "ais-vocab",
          name: "AI 安全词库",
          license: "MIT",
          commercial_ok: true,
          enabled: false,
          priority: null,
          cached: true,
        },
        {
          id: "computerese",
          name: "计算机专业词库",
          license: "MIT",
          commercial_ok: true,
          enabled: false,
          priority: null,
          cached: true,
        },
        {
          id: "gist",
          name: "GIST 多语言术语集",
          license: "Apache-2.0",
          commercial_ok: true,
          enabled: false,
          priority: null,
          cached: false,
        },
      ],
      loaded_terms: 14820,
      reloading: false,
    },
    system: {
      max_upload_mb: 200,
      max_pages: 200,
      max_concurrent_jobs: 4,
      max_concurrent_translations: 2,
      free_max_concurrent_translations: 1,
      max_queued_jobs: 10,
      free_max_queued_jobs: 6,
      retention_hours: 24,
      min_free_disk_mb: 1024,
      maintenance_mode: false,
      site_notice: "",
    },
    env_locks: [
      { name: "ADMIN_PASSWORD_HASH", status: "已锁定", detail: "仅限环境变量修改" },
      { name: "CONFIG_SECRET_KEY", status: "已就绪", detail: "256-bit AES-GCM 主密钥" },
    ],
    ...overrides,
  };
}

function makeRevisions(): ConfigRevisionsOut {
  return {
    limit: 10,
    offset: 0,
    items: [
      {
        revision: 15,
        section: "free_translation",
        changed_fields: ["providers[0].model"],
        summary: "更新免费翻译主力模型",
        created_at: "2026-11-24T10:00:00Z",
      },
      {
        revision: 14,
        section: "system",
        changed_fields: ["max_pages"],
        summary: "调整全局页数上限",
        created_at: "2026-11-24T08:30:00Z",
      },
    ],
  };
}

function makeSubmissionPage(): SubmissionPage {
  return {
    generated_at: "2026-11-11T06:00:00Z",
    total: 26,
    page: 1,
    page_size: 25,
    items: [
      {
        job_id: "synthetic-job",
        title: "合成论文标题 <script>不会执行</script>",
        title_source: "parsed",
        submitted_at: "2026-11-11T05:00:00Z",
        submitter_ip: "2001:db8:1111:2222:3333:4444:5555:6666",
        ip_source: "trusted_proxy",
        page_count: 8,
        status: "failed",
        first_completed_at: "2026-11-11T05:30:00Z",
        updated_at: "2026-11-11T06:00:00Z",
        record_source: "live",
        notice_version: "synthetic-notice",
        current_stage: "translate",
        failure_code: "upstream_timeout",
        failure_reason: "上游超时",
        retryable: true,
        failure_updated_at: "2026-11-11T05:45:00Z",
        channel_mode: "free",
        model_name: "nvidia/nemotron-3-ultra-550b",
        fallback_used: true,
        user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
        client_device: "desktop",
        client_os: "Windows",
        client_browser: "Chrome",
      },
    ],
  };
}

describe("专业管理后台与登录组件", () => {
  let host: HTMLDivElement;
  let root: Root;
  let renderSeq = 0;

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));

  beforeEach(() => {
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    vi.spyOn(adminApi, "session").mockResolvedValue(makeSession());
    vi.spyOn(adminApi, "login").mockResolvedValue(makeSession());
    vi.spyOn(adminApi, "logout").mockResolvedValue(undefined);
    vi.spyOn(adminApi, "overview").mockResolvedValue(makeOverview());
    vi.spyOn(adminApi, "search").mockResolvedValue(makeSubmissionPage());
    vi.spyOn(adminApi, "getConfig").mockResolvedValue(makeConfig());
    vi.spyOn(adminApi, "updateConfig").mockResolvedValue(makeConfig({ revision: 16 }));
    vi.spyOn(adminApi, "testConnection").mockResolvedValue({
      ok: true,
      status_code: 200,
      latency_ms: 420,
      detail: "连接正常",
    });
    vi.spyOn(adminApi, "listRevisions").mockResolvedValue(makeRevisions());
    vi.spyOn(adminApi, "rollbackConfig").mockResolvedValue(makeConfig({ revision: 16 }));
    vi.spyOn(adminApi, "reloadGlossary").mockResolvedValue(makeConfig().glossary);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function renderConsole(initialPath = "/admin/overview", submissionAuditEnabled = true) {
    const seq = ++renderSeq;
    await act(async () =>
      root.render(
        <MemoryRouter key={seq} initialEntries={[initialPath]}>
          <Routes>
            <Route path="/" element={<p>主站首页</p>} />
            <Route path="/stats" element={<p>公开统计</p>} />
            <Route path="/admin/login" element={<AdminLoginPage adminPath="/admin" />} />
            <Route
              path="/admin/overview"
              element={
                <AdminConsolePage
                  adminPath="/admin"
                  section="overview"
                  submissionAuditEnabled={submissionAuditEnabled}
                />
              }
            />
            <Route
              path="/admin/records"
              element={
                <AdminConsolePage
                  adminPath="/admin"
                  section="records"
                  submissionAuditEnabled={submissionAuditEnabled}
                />
              }
            />
            <Route
              path="/admin/submissions"
              element={
                <AdminSubmissionsPage
                  adminPath="/admin"
                  submissionAuditEnabled={submissionAuditEnabled}
                />
              }
            />
            <Route
              path="/admin/services"
              element={
                <AdminConsolePage
                  adminPath="/admin"
                  section="services"
                  submissionAuditEnabled={submissionAuditEnabled}
                />
              }
            />
            <Route
              path="/admin/settings"
              element={
                <AdminConsolePage
                  adminPath="/admin"
                  section="settings"
                  submissionAuditEnabled={submissionAuditEnabled}
                />
              }
            />
          </Routes>
        </MemoryRouter>,
      ),
    );
  }

  const findButton = (label: string) =>
    [...host.querySelectorAll<HTMLButtonElement>("button")].find((btn) => btn.textContent?.trim() === label)!;

  const findLink = (label: string) =>
    [...host.querySelectorAll<HTMLAnchorElement>("a")].find((link) => link.textContent?.includes(label))!;

  async function clickButton(label: string) {
    const target = findButton(label);
    expect(target, `未找到按钮: ${label}`).toBeTruthy();
    await act(async () => target.click());
  }

  async function clickLink(label: string) {
    const target = findLink(label);
    expect(target, `未找到链接: ${label}`).toBeTruthy();
    await act(async () => target.click());
  }

  async function setInput(selector: string, value: string) {
    await act(async () => {
      const field = host.querySelector<HTMLInputElement>(selector)!;
      expect(field, `未找到输入框: ${selector}`).toBeTruthy();
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  async function setSelect(selector: string, value: string) {
    await act(async () => {
      const field = host.querySelector<HTMLSelectElement>(selector)!;
      expect(field, `未找到下拉框: ${selector}`).toBeTruthy();
      field.value = value;
      field.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  async function submitForm() {
    await act(async () => {
      host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
  }

  function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((done) => {
      resolve = done;
    });
    return { promise, resolve };
  }

  it("未登录直达管理页先校验会话，401 自动跳登录且不发业务请求", async () => {
    vi.mocked(adminApi.session).mockRejectedValue(new AdminApiError(401));
    await renderConsole("/admin/records");
    expect(adminApi.search).not.toHaveBeenCalled();
    expect(host.textContent).toContain("管理登录");
    expect(host.textContent).toContain("管理会话已失效，请重新登录。");
  });

  it("会话检查未完成时不发起业务查询，404 显示未开启提示", async () => {
    const pending = deferred<AdminSession>();
    vi.mocked(adminApi.session).mockReturnValueOnce(pending.promise);
    await renderConsole("/admin/overview");
    expect(host.textContent).toContain("正在验证管理会话");
    expect(adminApi.overview).not.toHaveBeenCalled();
    await act(async () => pending.resolve(makeSession()));
    expect(adminApi.overview).toHaveBeenCalledOnce();

    vi.mocked(adminApi.session).mockRejectedValueOnce(new AdminApiError(404));
    await renderConsole("/admin/services");
    expect(host.textContent).toContain("本站未开启管理功能");
    expect(adminApi.getConfig).not.toHaveBeenCalled();
  });

  it("登录表单支持显示密码、限流与错误提示，登录成功后进入运行概况", async () => {
    vi.mocked(adminApi.session).mockRejectedValueOnce(new AdminApiError(401));
    vi.mocked(adminApi.login)
      .mockRejectedValueOnce(new AdminApiError(401))
      .mockRejectedValueOnce(new AdminApiError(429))
      .mockResolvedValueOnce(makeSession());
    await renderConsole("/admin/login");
    await setInput("#admin-password", "wrong-pass");
    await clickButton("显示密码");
    expect(host.querySelector<HTMLInputElement>("#admin-password")!.type).toBe("text");
    await submitForm();
    expect(host.textContent).toContain("管理密码不正确或暂时无法登录");
    expect(host.querySelector<HTMLInputElement>("#admin-password")!.value).toBe("");

    await setInput("#admin-password", "wrong-again");
    await submitForm();
    expect(host.textContent).toContain("登录尝试过于频繁");

    vi.mocked(adminApi.session).mockResolvedValue(makeSession());
    await setInput("#admin-password", "valid-password");
    await submitForm();
    expect(adminApi.login).toHaveBeenLastCalledWith({ password: "valid-password" }, expect.any(AbortSignal));
    expect(host.querySelector("h1")?.textContent).toBe("运行概况");
  });

  it("顶栏常态保持极简无状态/commit徽章，仅异常时展示告警，且 4 个一级菜单可自由切换", async () => {
    await renderConsole("/admin/overview");
    const topbarText = host.querySelector(".admin-topbar")?.textContent ?? "";
    expect(topbarText).toContain("返回主站");
    expect(topbarText).not.toContain("免费体验可用");
    expect(topbarText).not.toContain("维护模式已开启");
    expect(topbarText).not.toContain("加密主密钥未就绪");

    // 切换到翻译记录、服务设置、系统设置
    await clickLink("翻译记录");
    expect(host.querySelector("h1")?.textContent).toBe("翻译记录");
    await clickLink("服务设置");
    expect(host.querySelector("h1")?.textContent).toBe("服务设置");
    await clickLink("系统设置");
    expect(host.querySelector("h1")?.textContent).toBe("系统设置");

    // 异常状态出现时顶栏显示告警
    vi.mocked(adminApi.overview).mockResolvedValueOnce(
      makeOverview({
        maintenance_mode: true,
        secret_key_ready: false,
        warnings: ["mineru_token 解密失败"],
      }),
    );
    await renderConsole("/admin/overview");
    const alertTopbar = host.querySelector(".admin-topbar")?.textContent ?? "";
    expect(alertTopbar).toContain("维护模式已开启");
    expect(alertTopbar).toContain("加密主密钥未就绪");
    expect(alertTopbar).toContain("密文解密告警");
  });

  it("运行概况展示 3 队列动态预留、按来源标注终端分布、在途任务，并可一键跳转失败记录", async () => {
    await renderConsole("/admin/overview");
    expect(host.textContent).toContain("为自备 Key 预留 1 席");
    expect(host.textContent).toContain("实时展示队列占用、提交终端分布、核心服务状态与当前在途任务");
    expect(host.textContent).not.toContain("访客终端分布");
    expect(host.textContent).toContain("提交终端分布（近 7 日）");
    expect(host.querySelector(".ac-ua-bar")?.getAttribute("aria-label")).toBe("近 7 日提交终端分布占比条");
    expect(host.textContent).toContain("100 篇");
    expect(host.textContent).toContain("Windows · Chrome");
    expect(host.textContent).toContain("abcdef12");
    expect(host.textContent).toContain("最近 24 小时有 1 条失败任务");

    // 切换为单并发无预留 + 访问来源统计
    const visitOverview = makeOverview();
    visitOverview.queues.parse.limit = 1;
    visitOverview.queues.parse_reserved_for_byok = false;
    visitOverview.stats.client_breakdown_source = "visit";
    vi.mocked(adminApi.overview).mockResolvedValueOnce(visitOverview);
    await clickButton("刷新概况");
    expect(host.textContent).toContain("当前解析并发为 1，无独立预留");
    expect(host.textContent).toContain("实时展示队列占用、访问终端分布、核心服务状态与当前在途任务");
    expect(host.textContent).toContain("访问终端分布（近 7 日）");
    expect(host.querySelector(".ac-ua-bar")?.getAttribute("aria-label")).toBe("近 7 日访问终端分布占比条");
    expect(host.textContent).toContain("100 次");

    await clickButton("查看失败记录");
    expect(host.querySelector("h1")?.textContent).toBe("翻译记录");
    expect(adminApi.search).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "failed" }),
      "synthetic-csrf",
      expect.any(AbortSignal),
    );
  });

  it("翻译记录支持旧书签、多维筛选、IP 与 UA 展示复制、不写存储，且切窗不清空已展示数据", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const copy = vi.fn().mockRejectedValueOnce(new Error("denied")).mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText: copy } });

    await renderConsole("/admin/submissions", false);
    expect(host.textContent).toContain("未开启提交明细采集");
    expect(host.querySelector(".admin-ip")?.textContent).toBe("2001:db8:1111:2222:3333:4444:5555:6666");
    expect(host.querySelector("script")).toBeNull();
    expect(host.textContent).toContain("桌面 · Windows · Chrome");
    expect(host.textContent).toContain("备选");

    // 切出窗口与隐藏标签页不再清空 DOM 或重验会话
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("hidden");
    await act(async () => {
      window.dispatchEvent(new Event("blur"));
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(host.querySelectorAll(".admin-record")).toHaveLength(1);
    expect(host.querySelector(".admin-ip")?.textContent).toBe("2001:db8:1111:2222:3333:4444:5555:6666");

    // 复制 IP（失败后成功）与展开完整 UA
    await clickButton("复制");
    expect(host.textContent).toContain("复制失败，请手动选择完整 IP");
    await clickButton("复制");
    expect(copy).toHaveBeenLastCalledWith("2001:db8:1111:2222:3333:4444:5555:6666");
    expect(host.textContent).toContain("已复制");

    await clickButton("完整 UA");
    expect(host.textContent).toContain("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0");

    // 多维筛选提交与翻页
    await setInput("#admin-title", "  Transformer  ");
    await setInput("#admin-ip", " 2001:db8::1 ");
    await setSelect("#admin-channel", "free");
    await setSelect("#admin-device", "desktop");
    await setSelect("#admin-status", "failed");
    await submitForm();
    expect(adminApi.search).toHaveBeenLastCalledWith(
      expect.objectContaining({
        title_query: "Transformer",
        ip: "2001:db8::1",
        channel_mode: "free",
        client_device: "desktop",
        status: "failed",
        page: 1,
        page_size: 25,
      }),
      "synthetic-csrf",
      expect.any(AbortSignal),
    );

    await clickButton("下一页");
    expect(adminApi.search).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 2 }),
      "synthetic-csrf",
      expect.any(AbortSignal),
    );
    expect(storage).not.toHaveBeenCalled();
  });

  it("服务设置 4 分区独立切换、连接测试随表单变更自动中止、409 冲突保留草稿、aitd 标注不可商用", async () => {
    await renderConsole("/admin/services");
    expect(host.textContent).not.toContain("兼容方言");
    expect(host.textContent).not.toContain("思考模式策略");

    // 测试连接进行中修改字段，应自动中止过时测试请求
    let capturedSignal: AbortSignal | undefined;
    vi.mocked(adminApi.testConnection).mockImplementationOnce(
      (_body, _csrf, signal) =>
        new Promise((_resolve, reject) => {
          capturedSignal = signal;
          signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
        }),
    );
    await clickButton("测试连接");
    expect(capturedSignal?.aborted).toBe(false);
    await setInput("#provider-model-nim-main", "nvidia/new-model-70b");
    expect(capturedSignal?.aborted).toBe(true);

    // 409 冲突提示且保留管理员草稿
    vi.mocked(adminApi.updateConfig).mockRejectedValueOnce(new AdminApiError(409));
    await clickButton("保存免费翻译池");
    expect(host.textContent).toContain("配置已在其他窗口更新");
    expect(host.querySelector<HTMLInputElement>("#provider-model-nim-main")!.value).toBe("nvidia/new-model-70b");

    // 切换到版面解析、自备预设与计价、公共术语库
    await clickButton("版面解析");
    expect(host.textContent).toContain("MinerU 云端版面解析");
    expect(host.textContent).toContain("PaddleOCR-VL");

    await clickButton("自备预设与计价");
    expect(host.textContent).toContain("后台绝不保存访客密钥");
    expect(host.textContent).toContain("模型单价热表");

    // 计价热表使用 Number.isFinite 严格拦截空值、Infinity 与非正高峰倍率
    const updateCallsBefore = vi.mocked(adminApi.updateConfig).mock.calls.length;
    await setInput('input[aria-label="缓存命中单价 1"]', "");
    await clickButton("保存模型单价");
    expect(host.textContent).toContain("单价或高峰倍率格式无效");
    expect(vi.mocked(adminApi.updateConfig)).toHaveBeenCalledTimes(updateCallsBefore);

    await setInput('input[aria-label="缓存命中单价 1"]', "1e309");
    await clickButton("保存模型单价");
    expect(host.textContent).toContain("单价或高峰倍率格式无效");
    expect(vi.mocked(adminApi.updateConfig)).toHaveBeenCalledTimes(updateCallsBefore);

    await setInput('input[aria-label="缓存命中单价 1"]', "0.02");
    await setInput('input[aria-label="高峰倍率 1"]', "0");
    await clickButton("保存模型单价");
    expect(host.textContent).toContain("单价或高峰倍率格式无效");
    expect(vi.mocked(adminApi.updateConfig)).toHaveBeenCalledTimes(updateCallsBefore);

    await setInput('input[aria-label="高峰倍率 1"]', "2");
    await clickButton("保存模型单价");
    expect(adminApi.updateConfig).toHaveBeenLastCalledWith(
      "pricing",
      expect.objectContaining({ expected_revision: 15 }),
      "synthetic-csrf",
    );

    await clickButton("公共术语库");
    expect(host.textContent).toContain("不可商用");
    await clickButton("立即重新加载");
    expect(adminApi.reloadGlossary).toHaveBeenCalledWith("synthetic-csrf");
  });

  it("系统设置支持保存、二次确认回滚非敏感项并展示环境变量锁定项", async () => {
    await renderConsole("/admin/settings");
    expect(host.textContent).toContain("仅影响新任务与清理阈值");
    expect(host.textContent).toContain("ADMIN_PASSWORD_HASH");

    await setInput("#sys-max-pages", "300");
    vi.mocked(adminApi.listRevisions).mockResolvedValueOnce({
      limit: 10,
      offset: 0,
      items: [
        {
          revision: 16,
          section: "system",
          changed_fields: ["max_pages"],
          summary: "调整全局页数上限至 300",
          created_at: "2026-11-24T11:00:00Z",
        },
        {
          revision: 14,
          section: "system",
          changed_fields: ["max_pages"],
          summary: "调整全局页数上限",
          created_at: "2026-11-24T08:30:00Z",
        },
      ],
    });
    await clickButton("保存系统设置");
    expect(adminApi.updateConfig).toHaveBeenLastCalledWith(
      "system",
      expect.objectContaining({ expected_revision: 15, max_pages: 300 }),
      "synthetic-csrf",
    );

    // 回滚非敏感项需要二次确认
    await clickButton("回滚非敏感项");
    expect(adminApi.rollbackConfig).not.toHaveBeenCalled();
    expect(host.textContent).toContain("确认回滚到 v14");
    await clickButton("确认回滚");
    expect(adminApi.rollbackConfig).toHaveBeenCalledWith(
      { target_revision: 14, expected_revision: 16 },
      "synthetic-csrf",
    );
  });

  it("系统设置遇 409 冲突刷新时按字段三方合并，既保留本地修改又同步另一窗口修改，同字段冲突显式提示", async () => {
    await renderConsole("/admin/settings");

    // 窗口 A 修改全局页数上限为 320，另一窗口 B 同时把 site_notice 改成了“今晚维护”并升到 v16
    await setInput("#sys-max-pages", "320");
    vi.mocked(adminApi.updateConfig).mockRejectedValueOnce(new AdminApiError(409));
    await clickButton("保存系统设置");
    expect(host.textContent).toContain("配置已在其他窗口更新");

    const remoteV16 = makeConfig({
      revision: 16,
      system: { ...makeConfig().system, max_pages: 200, site_notice: "今晚维护" },
    });
    vi.mocked(adminApi.getConfig).mockResolvedValueOnce(remoteV16);
    await clickButton("加载最新并合并");

    // A 的 max_pages=320 被保留，B 的 site_notice="今晚维护" 自动合入，不丢失也不覆盖
    expect(host.querySelector<HTMLInputElement>("#sys-max-pages")!.value).toBe("320");
    expect(host.querySelector<HTMLInputElement>("#sys-notice")!.value).toBe("今晚维护");
    expect(host.textContent).toContain("已自动同步其他窗口对「全站公告」的修改");

    // 再次保存应携带新版本 v16 以及合并后的完整字段
    vi.mocked(adminApi.updateConfig).mockResolvedValueOnce(
      makeConfig({ revision: 17, system: { ...remoteV16.system, max_pages: 320 } }),
    );
    await clickButton("保存系统设置");
    expect(adminApi.updateConfig).toHaveBeenLastCalledWith(
      "system",
      expect.objectContaining({ expected_revision: 16, max_pages: 320, site_notice: "今晚维护" }),
      "synthetic-csrf",
    );

    // 双方同时修改同一字段（max_pages）时显式列出冲突并支持一键采用最新覆盖
    await setInput("#sys-max-pages", "400");
    vi.mocked(adminApi.updateConfig).mockRejectedValueOnce(new AdminApiError(409));
    await clickButton("保存系统设置");
    const remoteV18 = makeConfig({
      revision: 18,
      system: { ...remoteV16.system, max_pages: 500 },
    });
    vi.mocked(adminApi.getConfig).mockResolvedValueOnce(remoteV18);
    await clickButton("加载最新并合并");
    expect(host.textContent).toContain("全局页数上限（页）");
    expect(host.textContent).toContain("400");
    expect(host.textContent).toContain("500");
    expect(host.querySelector<HTMLInputElement>("#sys-max-pages")!.value).toBe("400");

    await clickButton("全部采用最新覆盖草稿");
    expect(host.querySelector<HTMLInputElement>("#sys-max-pages")!.value).toBe("500");
  });

  it("409 合并安全配对拦截：另一窗口修改 MinerU 地址后绝不与本地待保存 Token 自动配对", async () => {
    const initialDisabled = makeConfig({
      revision: 15,
      parser: {
        ...makeConfig().parser,
        mineru_base_url: "https://mineru.net",
        mineru_token: {
          mode: "disabled",
          configured: false,
          mask: "",
          bound_url: "",
          bound_protocol: "",
          corrupted: false,
        },
      },
    });
    vi.mocked(adminApi.getConfig).mockResolvedValueOnce(initialDisabled);
    await renderConsole("/admin/services");
    await clickButton("版面解析");

    // 窗口 A 为原地址 https://mineru.net 输入新的 MinerU Token
    await clickButton("更换Token");
    await setInput('input[placeholder="输入新的 MinerU API Token"]', "token-for-old-mineru");

    // 窗口 B 已把 mineru_base_url 改为新地址并保存 v16，窗口 A 提交遇 409
    vi.mocked(adminApi.updateConfig).mockRejectedValueOnce(new AdminApiError(409));
    await clickButton("保存版面解析");
    expect(host.textContent).toContain("配置已在其他窗口更新");

    const remoteV16 = makeConfig({
      revision: 16,
      parser: {
        ...initialDisabled.parser,
        mineru_base_url: "https://mineru-new.example.invalid",
      },
    });
    vi.mocked(adminApi.getConfig).mockResolvedValueOnce(remoteV16);
    await clickButton("加载最新并合并");

    // 断言已清空待保存旧 Token、产生明确配对冲突并阻止直接保存或连接测试
    expect(host.textContent).toContain("已拦截自动配对");
    expect(host.textContent).toContain("https://mineru-new.example.invalid");
    expect(host.querySelector('input[placeholder="输入新的 MinerU API Token"]')).toBeNull();
    expect(findButton("保存版面解析").disabled).toBe(true);
    expect(findButton("测试解析连接").disabled).toBe(true);

    const callsBefore = vi.mocked(adminApi.updateConfig).mock.calls.length;
    await act(async () => {
      findButton("保存版面解析").click();
      findButton("测试解析连接").click();
    });
    expect(adminApi.updateConfig).toHaveBeenCalledTimes(callsBefore);
    expect(adminApi.testConnection).not.toHaveBeenCalled();

    // 显式重新更换为新地址对应 Token 后解除阻断并可正常保存
    await clickButton("更换Token");
    await setInput('input[placeholder="输入新的 MinerU API Token"]', "token-for-new-mineru");
    expect(findButton("保存版面解析").disabled).toBe(false);
    vi.mocked(adminApi.updateConfig).mockResolvedValueOnce(
      makeConfig({
        revision: 17,
        parser: {
          ...remoteV16.parser,
          mineru_token: {
            mode: "custom",
            configured: true,
            mask: "toke...neru",
            bound_url: "https://mineru-new.example.invalid",
            bound_protocol: "",
            corrupted: false,
          },
        },
      }),
    );
    await clickButton("保存版面解析");
    expect(adminApi.updateConfig).toHaveBeenLastCalledWith(
      "parser",
      expect.objectContaining({
        expected_revision: 16,
        mineru_base_url: "https://mineru-new.example.invalid",
        mineru_token: { action: "replace", value: "token-for-new-mineru" },
      }),
      "synthetic-csrf",
    );
  });

  it("多脏分区防静默覆盖：在版面解析遇 409 合并时同步三方合并已脏的模型计价分区", async () => {
    await renderConsole("/admin/services");

    // 1. 修改「版面解析」的 Paddle 公开开关（使 parser 分区变脏）
    await clickButton("版面解析");
    const paddleCheckbox = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].find((el) =>
      el.closest("label")?.textContent?.includes("向前台开放 PaddleOCR-VL"),
    )!;
    await act(async () => paddleCheckbox.click());

    // 2. 切到「自备预设与计价」，新增一行模型单价（使 pricing 分区变脏）
    await clickButton("自备预设与计价");
    await clickButton("新增模型单价");
    await setInput('input[aria-label="计价模型 2"]', "claude-new");
    await setInput('input[aria-label="输出单价 2"]', "12");

    // 3. 另一窗口已把 deepseek-flash 的输出单价从 4 改成 5.5（升至 v16）；切回版面解析提交遇 409 并合并
    await clickButton("版面解析");
    vi.mocked(adminApi.updateConfig).mockRejectedValueOnce(new AdminApiError(409));
    await clickButton("保存版面解析");

    const remoteV16 = makeConfig({
      revision: 16,
      pricing: {
        custom_configured: true,
        models: [{ model: "deepseek-flash", cache_hit: 0.02, cache_miss: 1.0, output: 5.5, peak_factor: 2.0 }],
      },
    });
    vi.mocked(adminApi.getConfig).mockResolvedValueOnce(remoteV16);
    await clickButton("加载最新并合并");

    // 4. 切回「自备预设与计价」：应已同步合入远端的 5.5 并保留本地新增的 claude-new，且携带新 revision 16 保存
    await clickButton("自备预设与计价");
    expect(host.querySelector<HTMLInputElement>('input[aria-label="输出单价 1"]')!.value).toBe("5.5");
    expect(host.querySelector<HTMLInputElement>('input[aria-label="计价模型 2"]')!.value).toBe("claude-new");
    expect(host.textContent).toContain("已自动同步其他窗口对「模型「deepseek-flash」单价」的修改");

    vi.mocked(adminApi.updateConfig).mockResolvedValueOnce(makeConfig({ revision: 17 }));
    await clickButton("保存模型单价");
    expect(adminApi.updateConfig).toHaveBeenLastCalledWith(
      "pricing",
      expect.objectContaining({
        expected_revision: 16,
        models: [
          expect.objectContaining({ model: "deepseek-flash", output: 5.5 }),
          expect.objectContaining({ model: "claude-new", output: 12 }),
        ],
      }),
      "synthetic-csrf",
    );
  });

  it("免费渠道按稳定 ID 字段级三方合并，且拦截远端改地址与本地换密钥的自动配对", async () => {
    await renderConsole("/admin/services");

    // 窗口 A 修改 nim-main 的 timeout_s 为 90
    await setInput("#provider-timeout-nim-main", "90");
    vi.mocked(adminApi.updateConfig).mockRejectedValueOnce(new AdminApiError(409));
    await clickButton("保存免费翻译池");

    // 窗口 B 修改同一渠道 nim-main 的 fallback_model，并新增备用渠道 backup-2
    const baseCfg = makeConfig();
    const remoteV16 = makeConfig({
      revision: 16,
      free_translation: {
        ...baseCfg.free_translation,
        providers: [
          {
            ...baseCfg.free_translation.providers[0]!,
            fallback_model: "nvidia/other-70b",
          },
          {
            id: "backup-2",
            name: "备用渠道 B",
            enabled: true,
            protocol: "openai",
            base_url: "https://backup.example.invalid/v1",
            model: "backup-model",
            fallback_model: "",
            timeout_s: 60,
            api_key: {
              mode: "custom",
              configured: true,
              mask: "sk-...bk2",
              bound_url: "https://backup.example.invalid/v1",
              bound_protocol: "openai",
              corrupted: false,
            },
            usable: true,
          },
        ],
      },
    });
    vi.mocked(adminApi.getConfig).mockResolvedValueOnce(remoteV16);
    await clickButton("加载最新并合并");

    // 双方无冲突字段均保留：A 的 timeout_s=90，B 的 fallback_model 和 backup-2
    expect(host.querySelector<HTMLInputElement>("#provider-timeout-nim-main")!.value).toBe("90");
    expect(host.querySelector<HTMLInputElement>("#provider-fallback-nim-main")!.value).toBe("nvidia/other-70b");
    expect(host.textContent).toContain("备用渠道 B");
    expect(host.querySelector(".ac-conflict-list")).toBeNull();

    // 进一步验证：窗口 A 更换 nim-main 的密钥，窗口 B 同时改了 nim-main 的 base_url → 必须清空密钥并拦截自动配对
    const replaceKeyButtons = [...host.querySelectorAll<HTMLButtonElement>("button")].filter(
      (b) => b.textContent?.trim() === "更换密钥",
    );
    await act(async () => replaceKeyButtons[0]!.click());
    await setInput('input[placeholder="输入该渠道的新 API Key"]', "old-url-provider-secret");
    vi.mocked(adminApi.updateConfig).mockRejectedValueOnce(new AdminApiError(409));
    await clickButton("保存免费翻译池");

    const remoteV17 = makeConfig({
      revision: 17,
      free_translation: {
        ...remoteV16.free_translation,
        providers: [
          {
            ...remoteV16.free_translation.providers[0]!,
            base_url: "https://moved-upstream.example.invalid/v1",
          },
          remoteV16.free_translation.providers[1]!,
        ],
      },
    });
    vi.mocked(adminApi.getConfig).mockResolvedValueOnce(remoteV17);
    await clickButton("加载最新并合并");

    expect(host.textContent).toContain("已拦截自动配对");
    expect(host.querySelector('input[placeholder="输入该渠道的新 API Key"]')).toBeNull();
    expect(findButton("保存免费翻译池").disabled).toBe(true);
  });

  it("退出登录销毁会话跳登录，到期定时器自动跳登录并提示会话失效", async () => {
    await renderConsole("/admin/overview");
    // 退出后后端会话 Cookie 已销毁，登录页检查 session 应返回 401
    vi.mocked(adminApi.session).mockRejectedValue(new AdminApiError(401));
    await clickButton("退出登录");
    expect(adminApi.logout).toHaveBeenCalledWith("synthetic-csrf", expect.any(AbortSignal));
    expect(host.textContent).toContain("管理登录");

    // 固定到期时间到达后自动跳登录
    vi.useFakeTimers();
    vi.mocked(adminApi.session).mockResolvedValueOnce(makeSession(1000));
    await renderConsole("/admin/overview");
    expect(host.querySelector("h1")?.textContent).toBe("运行概况");
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(host.textContent).toContain("管理登录");
    expect(host.textContent).toContain("管理会话已失效，请重新登录。");
  });
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULTS, getSettings, saveSettings } from "@/entities/settings/model";
import { api, type ServerConfig } from "@/shared/api/client";

import { SettingsPanel } from "./SettingsPanel";

describe("共享 MinerU 设置引导", () => {
  let host: HTMLDivElement;
  let root: Root;
  let client: QueryClient;
  const close = vi.fn();

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    saveSettings({ ...DEFAULTS });
    host = document.createElement("div");
    root = createRoot(host);
    client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    close.mockClear();
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
      admin_path: "/weandy",
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
          <SettingsPanel onClose={close} />
        </QueryClientProvider>,
      ),
    );
  }

  const parserInput = () =>
    [...host.querySelectorAll("label")]
      .find((label) => label.textContent?.startsWith("MinerU Token"))!
      .querySelector("input")!;

  const field = (prefix: string) =>
    [...host.querySelectorAll("label")]
      .find((label) => label.textContent?.startsWith(prefix))!
      .querySelector("input, select") as HTMLInputElement;
  const button = (text: string) => [...host.querySelectorAll("button")].find((b) => b.textContent === text)!;
  async function change(element: HTMLInputElement, value: string) {
    await act(async () => {
      const prototype = element.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(element, value);
      element.dispatchEvent(new Event(element.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
    });
  }

  it("按未保存的自定义配置获取模型，去重排序并选择保存，不发送解析 Token", async () => {
    const list = vi.spyOn(api, "llmModels").mockResolvedValue(["z-model", "a-model", "a-model"]);
    saveSettings({ ...DEFAULTS, parserKey: "synthetic-parser-token", model: "manual-model" });
    await render(true);
    await change(field("翻译服务 API Key"), "synthetic-key");
    await change(field("接口地址"), "https://custom.example.invalid/v1");
    expect(list).not.toHaveBeenCalled();
    await act(async () => button("获取模型列表").click());
    expect(list).toHaveBeenCalledWith(
      {
        "X-LLM-Protocol": "openai",
        "X-LLM-Base-URL": "https://custom.example.invalid/v1",
        "X-LLM-Key": "synthetic-key",
        "X-LLM-Model": "manual-model",
      },
      expect.any(AbortSignal),
    );
    expect(field("模型名称").value).toBe("manual-model");
    expect([...field("从列表选择模型").querySelectorAll("option")].map((o) => o.value)).toEqual([
      "",
      "a-model",
      "z-model",
    ]);
    await change(field("从列表选择模型"), "z-model");
    expect(field("模型名称").value).toBe("z-model");
    await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true })));
    expect(getSettings().model).toBe("z-model");
    expect(getSettings().baseUrl).toBe("https://custom.example.invalid/v1");
  });

  it("失败后同配置可以重试，空列表保留手动模型", async () => {
    saveSettings({ ...DEFAULTS, llmKey: "synthetic-key", model: "manual-model" });
    const list = vi
      .spyOn(api, "llmModels")
      .mockRejectedValueOnce(new Error("服务不支持查询"))
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(["other-model"]);
    await render(true);
    await act(async () => button("获取模型列表").click());
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("可以重试，或手动填写");
    await act(async () => button("获取模型列表").click());
    expect(host.textContent).toContain("没有返回可选模型");
    expect(field("模型名称").value).toBe("manual-model");
    await act(async () => button("获取模型列表").click());
    expect(list).toHaveBeenCalledTimes(3);
    expect(host.textContent).toContain("已获取 1 个模型");
  });

  it("切换接口取消旧请求，晚到的响应不覆盖新列表，协议和 Key 变化也清空列表", async () => {
    saveSettings({ ...DEFAULTS, llmKey: "synthetic-key" });
    let oldResolve!: (models: string[]) => void;
    const list = vi
      .spyOn(api, "llmModels")
      .mockImplementationOnce(
        () =>
          new Promise((r) => {
            oldResolve = r;
          }),
      )
      .mockResolvedValue(["new-model"]);
    await render(true);
    await act(async () => button("获取模型列表").click());
    const signal = list.mock.calls[0]![1]!;
    expect(button("获取中…").disabled).toBe(true);
    await change(field("接口地址"), "https://other.example.invalid/v1");
    expect(signal.aborted).toBe(true);
    await act(async () => button("获取模型列表").click());
    await act(async () => oldResolve(["old-model"]));
    expect(host.textContent).not.toContain("old-model");
    expect(host.textContent).toContain("new-model");
    await change(field("接口协议"), "anthropic");
    expect(host.textContent).not.toContain("new-model");
    await act(async () => button("获取模型列表").click());
    expect(list.mock.calls.at(-1)?.[0]["X-LLM-Protocol"]).toBe("anthropic");
    await change(field("翻译服务 API Key"), "changed-key");
    expect(host.querySelector('select option[value="new-model"]')).toBeNull();
  });

  it("未填 Key 时禁用查询，关闭面板取消正在查询的请求", async () => {
    const list = vi.spyOn(api, "llmModels").mockImplementation(() => new Promise(() => {}));
    await render(true);
    expect(button("获取模型列表").disabled).toBe(true);
    await change(field("翻译服务 API Key"), "synthetic-key");
    await act(async () => button("获取模型列表").click());
    const signal = list.mock.calls[0]![1]!;
    await act(async () => root.render(null));
    expect(signal.aborted).toBe(true);
  });

  it("站点已提供解析时，翻译 Key 仍可填写，个人 Token 默认折叠且不是第二个必填步骤", async () => {
    await render(true);
    expect(host.querySelector("h3")?.textContent).toBe("填写翻译 API Key 即可开始");
    expect(host.textContent).toContain("本站已提供 MinerU 版面解析，无需再填写 Token");
    expect(host.querySelector('input[placeholder="sk-..."]')).not.toBeNull();
    expect(parserInput().closest("details")?.open).toBe(false);
    expect(host.querySelectorAll(".step-no")).toHaveLength(1);
    expect(host.textContent).not.toContain("1000");
  });

  it("更换服务或模型后，旧连接测试不能误报新配置已连通", async () => {
    saveSettings({ ...DEFAULTS, llmKey: "synthetic-key" });
    let finish!: (result: Awaited<ReturnType<typeof api.llmCheck>>) => void;
    const check = vi
      .spyOn(api, "llmCheck")
      .mockImplementationOnce(
        () =>
          new Promise((r) => {
            finish = r;
          }),
      )
      .mockResolvedValue({ ok: true, model: "new-model", latency_ms: 1, reply: "OK", detail: "" });
    await render(true);
    await act(async () => button("测试连接").click());
    const signal = check.mock.calls[0]![1]!;
    await change(field("接口地址"), "https://other.example.invalid/v1");
    expect(signal.aborted).toBe(true);
    await act(async () => button("测试连接").click());
    await act(async () => finish({ ok: true, model: "old-model", latency_ms: 1, reply: "OK", detail: "" }));
    expect(host.textContent).toContain("已连通：new-model");
    expect(host.textContent).not.toContain("已连通：old-model");
    await change(field("模型名称"), "manual-model");
    expect(host.textContent).not.toContain("已连通");
  });

  it("已有个人 Token 时明确优先使用，清空并保存可以恢复站点默认", async () => {
    saveSettings({ ...DEFAULTS, parserKey: "synthetic-parser-token", llmKey: "synthetic-llm-key" });
    await render(true);
    expect(host.textContent).toContain("当前选择：使用你自己的 MinerU Token");
    expect(parserInput().value).toBe("synthetic-parser-token");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(parserInput(), "");
      parserInput().dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(host.textContent).toContain("无需再填写 Token");
    await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true })));
    expect(getSettings().parserKey).toBe("");
    expect(getSettings().llmKey).toBe("synthetic-llm-key");
    expect(close).toHaveBeenCalledOnce();
  });

  it("站点没有提供解析时仍展示必填 Token 输入框", async () => {
    await render(false);
    expect(host.textContent).toContain("MinerU Token（版面解析，必填）");
    expect(parserInput().closest("details")).toBeNull();
    expect(host.textContent).not.toContain("无需再填写 Token");
    expect(host.querySelectorAll(".step-no")).toHaveLength(2);
  });
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULTS, getSettings, saveSettings } from "@/entities/settings/model";
import type { ServerConfig } from "@/shared/api/client";

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
      server_has_llm_key: false,
      server_has_parser_token: shared,
      server_has_paddle_token: false,
      max_upload_mb: 50,
      max_pages: 200,
      retention_hours: 24,
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

  it("站点已提供解析时，翻译 Key 仍可填写，个人 Token 默认折叠且不是第二个必填步骤", async () => {
    await render(true);
    expect(host.querySelector("h3")?.textContent).toBe("填写翻译 API Key 即可开始");
    expect(host.textContent).toContain("本站已提供 MinerU 版面解析，无需再填写 Token");
    expect(host.querySelector('input[placeholder="sk-..."]')).not.toBeNull();
    expect(parserInput().closest("details")?.open).toBe(false);
    expect(host.querySelectorAll(".step-no")).toHaveLength(1);
    expect(host.textContent).not.toContain("1000");
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

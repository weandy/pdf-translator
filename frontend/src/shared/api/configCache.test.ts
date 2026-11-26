import { beforeEach, describe, expect, it } from "vitest";

import type { ServerConfig } from "./client";
import { clearCachedConfig, readCachedConfig, writeCachedConfig } from "./configCache";

const SAMPLE: ServerConfig = {
  version: "0.2.0",
  default_base_url: "https://api.deepseek.com/v1",
  default_mineru_base_url: "https://mineru.net",
  default_model: "deepseek-chat",
  default_protocol: "openai",
  admin_path: "/secret-admin",
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
  free_channel: { enabled: true, model_display: "demo-free", max_pages: 30, notice: "" },
};

describe("configCache", () => {
  beforeEach(() => {
    clearCachedConfig();
    window.localStorage.clear();
  });

  it("缓存公开配置快照且不持久化 admin_path", () => {
    expect(readCachedConfig()).toBeUndefined();
    writeCachedConfig(SAMPLE);
    const raw = window.localStorage.getItem("pdftr.public_config.v1") ?? "";
    expect(raw).not.toContain("/secret-admin");
    const restored = readCachedConfig();
    expect(restored?.free_channel?.enabled).toBe(true);
    expect(restored?.free_channel?.model_display).toBe("demo-free");
    expect(restored?.admin_path).toBe("");
  });

  it("本地缓存损坏或字段不全时安全回退 undefined", () => {
    window.localStorage.setItem("pdftr.public_config.v1", '{"broken":true}');
    expect(readCachedConfig()).toBeUndefined();
    window.localStorage.setItem("pdftr.public_config.v1", "not-json");
    expect(readCachedConfig()).toBeUndefined();
  });
});

import { expect, it } from "vitest";

import { normalizeLlmTarget } from "./target";

it("规范化主机及默认端口并保持协议、非默认端口和租户路径身份", () => {
  expect(normalizeLlmTarget("openai", " HTTPS://Proxy.Example.INVALID.:443/TenantA/v1/// ")).toEqual({
    protocol: "openai",
    url: "https://proxy.example.invalid/TenantA/v1",
    key: "openai|https://proxy.example.invalid/TenantA/v1",
  });
  expect(normalizeLlmTarget("anthropic", "http://Proxy.Example.INVALID:8443/tenantA/V1")?.key)
    .toBe("anthropic|http://proxy.example.invalid:8443/tenantA/V1");
  expect(normalizeLlmTarget("openai", "https://proxy.example.invalid/A/../B")?.url)
    .toBe("https://proxy.example.invalid/A/../B");
});

it.each([
  "https://u:pass@proxy.invalid", "https://@proxy.invalid", "https://proxy.invalid?",
  "https://proxy.invalid/#", "https://proxy.invalid/a b", "\nhttps://proxy.invalid",
  "https://proxy.invalid/a\u0000", "https:proxy.invalid", "https:///proxy.invalid",
  "https://proxy.invalid:0", "https://proxy.invalid:65536", "ftp://proxy.invalid", "https://proxy.invalid\\tenant",
])("拒绝无效地址 %j", (url) => expect(normalizeLlmTarget("openai", url)).toBeNull());

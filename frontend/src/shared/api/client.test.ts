import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, setAuthHeaders } from "@/shared/api/client";

function headerKeys(call: unknown[]): string[] {
  const init = (call[1] ?? {}) as RequestInit;
  const headers = new Headers(init.headers);
  return [...headers.keys()].map((key) => key.toLowerCase());
}

function hasLlmHeader(call: unknown[]): boolean {
  return headerKeys(call).some((key) => key.startsWith("x-llm-"));
}

function hasPersonalCredentialHeader(call: unknown[]): boolean {
  return headerKeys(call).some(
    (key) => key.startsWith("x-llm-") || key.startsWith("x-parser-") || key.startsWith("x-paddle-"),
  );
}

describe("接口客户端凭据隔离（默认不发送个人 Key）", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    setAuthHeaders(() => ({
      "X-LLM-Protocol": "anthropic",
      "X-LLM-Base-URL": "https://personal.example.invalid",
      "X-LLM-Model": "claude-personal",
      "X-LLM-Key": "sk-personal-secret",
      "X-Parser-Key": "mineru-personal-token",
      "X-Parser-Base-URL": "https://mineru.net",
      "X-Paddle-Key": "paddle-personal-token",
    }));
    fetchMock.mockReset();
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/visits") || url.includes("/api/jobs/del-job")) {
        return new Response(null, { status: 204 });
      }
      return new Response(JSON.stringify({ ok: true, models: [], entries: [], segments: [] }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    setAuthHeaders(() => ({}));
    vi.unstubAllGlobals();
  });

  it("只读与非模型接口默认不发送任何 X-LLM-* 或 X-Parser-Key 头", async () => {
    await api.health();
    await api.config();
    await api.stats(7);
    await api.recordVisit({
      event_id: "00000000-0000-4000-8000-000000000001",
      visitor_id: "00000000-0000-4000-8000-000000000002",
    });
    await api.estimate(10, "default", "deepseek-flash");
    await api.job("job-1");
    await api.deleteJob("del-job");
    await api.cancelJob("job-1");
    await api.document("job-1");
    await api.editSegment("seg-1", "新译文");
    await api.jobTerms("job-1");
    await api.prepareExport("job-1", "mono");
    await api.revisions("job-1");
    await api.revert(1);

    expect(fetchMock).toHaveBeenCalledTimes(14);
    for (const call of fetchMock.mock.calls) {
      expect(hasLlmHeader(call), String(call[0])).toBe(false);
      expect(headerKeys(call), String(call[0])).not.toContain("x-parser-key");
    }
  });

  it("免费任务全生命周期（建任务、重试、单段重翻、批量重翻）与规则解析术语均不发送 X-LLM-* / X-Parser-* / X-Paddle-* 头", async () => {
    const file = new File(["pdf"], "paper.pdf", { type: "application/pdf" });
    const glossary = new File(["term,术语"], "terms.csv", { type: "text/csv" });

    await api.createJob(file, { cloud_consent: true }, { mode: "free" });
    await api.createJob(
      file,
      { cloud_consent: true },
      {
        mode: "free",
        headers: {
          "X-LLM-Key": "sk-explicit",
          "X-Parser-Key": "tok-explicit",
          "X-Parser-Base-URL": "https://mineru.net",
          "X-Paddle-Key": "pad-explicit",
        },
      },
    );
    await api.retryJob("job-free", { mode: "free" });
    await api.retryJob("job-free", { channelMode: "free" });
    await api.retranslate("seg-1", "default", { a: "甲" }, { mode: "free" });
    await api.retranslate("seg-1", "careful", { a: "甲" }, { channelMode: "free" });
    await api.retranslateMany("job-free", ["seg-1"], { a: "甲" }, { mode: "free" });
    await api.retranslateMany("job-free", ["seg-1"], { a: "甲" }, { channelMode: "free" });
    await api.parseGlossary(glossary, false);

    expect(fetchMock).toHaveBeenCalledTimes(9);
    for (const call of fetchMock.mock.calls) {
      expect(hasLlmHeader(call), String(call[0])).toBe(false);
      expect(hasPersonalCredentialHeader(call), String(call[0])).toBe(false);
    }

    // 免费任务创建携带 X-Translation-Mode: free，但不携带任何个人解析头
    const createHeaders = new Headers(fetchMock.mock.calls[0]![1].headers);
    expect(createHeaders.get("X-Translation-Mode")).toBe("free");
    expect(createHeaders.get("X-Parser-Key")).toBeNull();
    expect(createHeaders.get("X-Parser-Base-URL")).toBeNull();

    const explicitFreeHeaders = new Headers(fetchMock.mock.calls[1]![1].headers);
    expect(explicitFreeHeaders.get("X-Translation-Mode")).toBe("free");
    expect(explicitFreeHeaders.get("X-Parser-Key")).toBeNull();
    expect(explicitFreeHeaders.get("X-Paddle-Key")).toBeNull();

    const retryHeaders = new Headers(fetchMock.mock.calls[2]![1].headers);
    expect(retryHeaders.get("X-Parser-Key")).toBeNull();
    expect(retryHeaders.get("X-Parser-Base-URL")).toBeNull();

    // 规则解析术语既不发 X-LLM-* 也不发 X-Parser-*
    expect(headerKeys(fetchMock.mock.calls[8]!)).not.toContain("x-parser-key");
  });

  it("BYOK 模式与 AI 解析术语正常携带浏览器中的 X-LLM-Key", async () => {
    const file = new File(["pdf"], "paper.pdf", { type: "application/pdf" });
    const glossary = new File(["term: 术语"], "terms.txt", { type: "text/plain" });

    await api.createJob(file, { cloud_consent: true }, { mode: "byok" });
    await api.retryJob("job-byok");
    await api.retryJob("job-byok", { channelMode: "byok" });
    await api.retranslate("seg-1", "default", { a: "甲" }, { channelMode: "byok" });
    await api.retranslateMany("job-byok", ["seg-1"], { a: "甲" }, { channelMode: "byok" });
    await api.parseGlossary(glossary, true);

    expect(fetchMock).toHaveBeenCalledTimes(6);
    for (const call of fetchMock.mock.calls) {
      const headers = new Headers(call[1].headers);
      expect(headers.get("X-LLM-Key"), String(call[0])).toBe("sk-personal-secret");
    }
  });
});

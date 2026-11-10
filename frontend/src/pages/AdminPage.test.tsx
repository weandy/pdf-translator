import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminLoginPage, AdminSubmissionsPage } from "@/pages/AdminPage";
import { adminApi, AdminApiError, type AdminSession, type SubmissionPage } from "@/shared/api/admin";

describe("管理登录与提交明细真实组件", () => {
  let host: HTMLDivElement;
  let root: Root;
  const identity = (): AdminSession => ({
    csrf_token: "synthetic-csrf",
    expires_at: new Date(Date.now() + 3600000).toISOString(),
  });
  const page = (): SubmissionPage => ({
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
      },
    ],
  });

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    vi.spyOn(adminApi, "session").mockResolvedValue(identity());
    vi.spyOn(adminApi, "search").mockResolvedValue(page());
    vi.spyOn(adminApi, "logout").mockResolvedValue(undefined);
    vi.spyOn(adminApi, "login").mockResolvedValue(identity());
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function render(mode: "login" | "submissions" = "submissions") {
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={[`/admin/${mode}`]}>
          <Routes>
            <Route path="/admin/login" element={mode === "login" ? <AdminLoginPage /> : <p>管理登录入口</p>} />
            <Route
              path="/admin/submissions"
              element={mode === "submissions" ? <AdminSubmissionsPage /> : <p>提交明细入口</p>}
            />
          </Routes>
        </MemoryRouter>,
      ),
    );
  }

  const button = (label: string) =>
    [...host.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent === label)!;
  async function click(label: string) {
    await act(async () => button(label).click());
  }
  async function input(id: string, value: string) {
    await act(async () => {
      const field = host.querySelector<HTMLInputElement>(`#${id}`)!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }
  async function submit() {
    await act(async () =>
      host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })),
    );
  }
  function deferred<T>() {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>((done) => {
      resolve = done;
    });
    return { promise, resolve };
  }

  it("未登录直达明细时先校验会话，绝不查询或展示敏感列表", async () => {
    vi.mocked(adminApi.session).mockRejectedValue(new AdminApiError(401));
    await render();
    expect(adminApi.search).not.toHaveBeenCalled();
    expect(host.textContent).toContain("管理登录入口");
    expect(host.textContent).not.toContain("合成论文标题");
  });

  it("会话检查未完成时不发起明细请求", async () => {
    const session = deferred<AdminSession>();
    vi.mocked(adminApi.session).mockReturnValue(session.promise);
    await render();
    expect(host.textContent).toContain("正在验证管理会话");
    expect(adminApi.search).not.toHaveBeenCalled();
    await act(async () => session.resolve(identity()));
    expect(adminApi.search).toHaveBeenCalledOnce();
  });

  it("显示完整 IPv6、当前状态和独立首次完成时间，标题仅按文本渲染", async () => {
    await render();
    expect(host.querySelector(".admin-ip")?.textContent).toBe(page().items[0]!.submitter_ip);
    expect(host.querySelector("script")).toBeNull();
    expect(host.textContent).toContain("失败");
    expect(host.textContent).toContain("首次完成：2026/11/11 13:30:00");
    expect(host.querySelectorAll(".admin-record")).toHaveLength(1);
    expect(host.querySelector(".admin-cell-label")?.textContent).toBe("提交时间");
    expect(host.querySelectorAll("a[href*='/jobs/']")).toHaveLength(0);
  });

  it("标题、IP 筛选经请求体提交，分页与每页数量重置正确，不写浏览器存储", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    await render();
    await input("admin-title", "  合成关键词  ");
    await input("admin-ip", " 2001:db8::1 ");
    await submit();
    expect(adminApi.search).toHaveBeenLastCalledWith(
      expect.objectContaining({ title_query: "合成关键词", ip: "2001:db8::1", page: 1, page_size: 25 }),
      "synthetic-csrf",
      expect.any(AbortSignal),
    );
    await click("下一页");
    expect(adminApi.search).toHaveBeenLastCalledWith(
      expect.objectContaining({ title_query: "合成关键词", page: 2 }),
      "synthetic-csrf",
      expect.any(AbortSignal),
    );
    await act(async () => {
      const select = host.querySelector<HTMLSelectElement>("#admin-page-size")!;
      select.value = "50";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(adminApi.search).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, page_size: 50 }),
      "synthetic-csrf",
      expect.any(AbortSignal),
    );
    expect(storage).not.toHaveBeenCalled();
  });

  it("快捷全部清除日期，重置恢复含今天的七天窗口", async () => {
    await render();
    const original = vi.mocked(adminApi.search).mock.calls[0]![0];
    expect(original.date_from).toBeTruthy();
    expect(original.date_to).toBeTruthy();
    await click("全部");
    expect(adminApi.search).toHaveBeenLastCalledWith(
      expect.objectContaining({ date_from: null, date_to: null, page: 1 }),
      "synthetic-csrf",
      expect.any(AbortSignal),
    );
    await click("重置");
    expect(vi.mocked(adminApi.search).mock.lastCall![0]).toEqual(original);
  });

  it("日期倒置在字段旁提示，服务端 IP 校验错误保留上一次结果", async () => {
    await render();
    await input("admin-date-from", "2026-11-12");
    await input("admin-date-to", "2026-11-11");
    await submit();
    expect(host.querySelector("#admin-date-to-error")?.textContent).toContain("不能早于");
    expect(adminApi.search).toHaveBeenCalledOnce();
    await input("admin-date-from", "2026-11-01");
    vi.mocked(adminApi.search).mockRejectedValue(
      new AdminApiError(422, { ip: "请输入完整、有效的 IPv4 或 IPv6 地址。" }),
    );
    await submit();
    expect(host.querySelector("#admin-ip-error")?.textContent).toContain("IPv4");
    expect(host.textContent).toContain("当前保留上一次查询结果");
    expect(host.querySelectorAll(".admin-record")).toHaveLength(1);
  });

  it("首次查询失败不会伪装成空数据，刷新失败保留成功结果", async () => {
    vi.mocked(adminApi.search).mockRejectedValueOnce(new Error("synthetic-network"));
    await render();
    expect(host.textContent).toContain("请求失败");
    expect(host.textContent).not.toContain("暂无提交");
    await click("重试查询");
    expect(host.querySelectorAll(".admin-record")).toHaveLength(1);
    vi.mocked(adminApi.search).mockRejectedValueOnce(new Error("synthetic-network"));
    await click("刷新记录");
    expect(host.querySelectorAll(".admin-record")).toHaveLength(1);
    expect(host.textContent).toContain("当前保留上一次查询结果");
  });

  it("401 后清空已有列表并转到登录", async () => {
    await render();
    vi.mocked(adminApi.search).mockRejectedValueOnce(new AdminApiError(401));
    await click("刷新记录");
    expect(host.textContent).toContain("管理登录入口");
    expect(host.textContent).not.toContain("合成论文标题");
  });

  it("CSRF 验证失败遮蔽结果，提供重新校验会话的入口", async () => {
    await render();
    vi.mocked(adminApi.search).mockRejectedValueOnce(new AdminApiError(403));
    await click("刷新记录");
    expect(host.querySelectorAll(".admin-record")).toHaveLength(0);
    expect(host.textContent).toContain("请求验证未通过");
    await click("重新验证会话");
    expect(adminApi.session).toHaveBeenCalledTimes(2);
    expect(host.querySelectorAll(".admin-record")).toHaveLength(1);
  });

  it("pagehide 同步遮蔽，pageshow 必须验证会话后才能重新查询", async () => {
    await render();
    await act(async () => window.dispatchEvent(new Event("pagehide")));
    expect(host.querySelector<HTMLDivElement>(".admin-sensitive")!.hidden).toBe(true);
    expect(host.querySelectorAll(".admin-record")).toHaveLength(0);
    const session = deferred<AdminSession>();
    vi.mocked(adminApi.session).mockReturnValueOnce(session.promise);
    await act(async () => window.dispatchEvent(new Event("pageshow")));
    expect(adminApi.search).toHaveBeenCalledOnce();
    expect(host.querySelector<HTMLDivElement>(".admin-sensitive")!.hidden).toBe(true);
    await act(async () => session.resolve(identity()));
    expect(adminApi.search).toHaveBeenCalledTimes(2);
    expect(host.querySelector<HTMLDivElement>(".admin-sensitive")!.hidden).toBe(false);
  });

  it("失焦后焦点恢复会话失效时不恢复旧结果", async () => {
    await render();
    await act(async () => window.dispatchEvent(new Event("blur")));
    vi.mocked(adminApi.session).mockRejectedValueOnce(new AdminApiError(401));
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(host.textContent).toContain("管理登录入口");
    expect(adminApi.search).toHaveBeenCalledOnce();
  });

  it("隐藏标签页时清空并遮蔽，恢复可见时重新验证权限", async () => {
    await render();
    const visibility = vi.spyOn(document, "visibilityState", "get");
    visibility.mockReturnValue("hidden");
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(host.querySelector<HTMLDivElement>(".admin-sensitive")!.hidden).toBe(true);
    expect(host.querySelectorAll(".admin-record")).toHaveLength(0);
    visibility.mockReturnValue("visible");
    vi.mocked(adminApi.session).mockRejectedValueOnce(new AdminApiError(401));
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(host.textContent).toContain("管理登录入口");
    expect(adminApi.search).toHaveBeenCalledOnce();
  });

  it("固定到期时间到达后自动清除列表并返回登录", async () => {
    vi.useFakeTimers();
    vi.mocked(adminApi.session).mockResolvedValue({
      csrf_token: "synthetic-csrf",
      expires_at: new Date(Date.now() + 1000).toISOString(),
    });
    await render();
    expect(host.querySelectorAll(".admin-record")).toHaveLength(1);
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(host.textContent).toContain("管理登录入口");
    expect(host.querySelectorAll(".admin-record")).toHaveLength(0);
  });

  it("页面离开前的迟到查询响应不能重新显示敏感内容", async () => {
    const result = deferred<SubmissionPage>();
    vi.mocked(adminApi.search).mockReturnValueOnce(result.promise);
    await render();
    await act(async () => window.dispatchEvent(new Event("pagehide")));
    await act(async () => result.resolve(page()));
    expect(host.querySelectorAll(".admin-record")).toHaveLength(0);
    expect(host.querySelector<HTMLDivElement>(".admin-sensitive")!.hidden).toBe(true);
  });

  it("退出立即清空页面并发送 CSRF，退出中焦点恢复不重新加载列表", async () => {
    await render();
    const logout = deferred<void>();
    vi.mocked(adminApi.logout).mockReturnValueOnce(logout.promise);
    await click("退出管理");
    expect(host.querySelectorAll(".admin-record")).toHaveLength(0);
    expect(adminApi.logout).toHaveBeenCalledWith("synthetic-csrf", expect.any(AbortSignal));
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(adminApi.search).toHaveBeenCalledOnce();
    await act(async () => logout.resolve(undefined));
    expect(host.textContent).toContain("管理登录入口");
  });

  it("退出请求失败保留明确提示与重试，焦点恢复不再次加载列表", async () => {
    await render();
    vi.mocked(adminApi.logout).mockRejectedValueOnce(new Error("synthetic-network"));
    await click("退出管理");
    expect(host.textContent).toContain("未能确认会话已退出");
    expect(host.querySelectorAll(".admin-record")).toHaveLength(0);
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(adminApi.search).toHaveBeenCalledOnce();
    await click("退出管理");
    expect(host.textContent).toContain("管理登录入口");
  });

  it("复制完整 IP，剪贴板失败时明确提示", async () => {
    const copy = vi.fn().mockRejectedValueOnce(new Error("synthetic-denied")).mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText: copy } });
    await render();
    await click("复制");
    expect(host.textContent).toContain("复制失败，请手动选择完整 IP");
    await click("复制");
    expect(copy).toHaveBeenLastCalledWith(page().items[0]!.submitter_ip);
    expect(host.textContent).toContain("已复制");
  });

  it("后端未启用时显示说明，不查询明细", async () => {
    vi.mocked(adminApi.session).mockRejectedValueOnce(new AdminApiError(404));
    await render();
    expect(host.textContent).toContain("本站未开启管理功能");
    expect(adminApi.search).not.toHaveBeenCalled();
  });

  it("登录可主动显示密码，错误提示不泄漏服务端详情，成功后转入明细", async () => {
    vi.mocked(adminApi.session).mockRejectedValue(new AdminApiError(401));
    vi.mocked(adminApi.login).mockRejectedValueOnce(new AdminApiError(401));
    await render("login");
    await input("admin-password", "synthetic-password");
    await click("显示密码");
    expect(host.querySelector<HTMLInputElement>("#admin-password")!.type).toBe("text");
    await submit();
    expect(host.textContent).toContain("管理密码不正确或暂时无法登录");
    expect(host.querySelector<HTMLInputElement>("#admin-password")!.value).toBe("");
    await input("admin-password", "synthetic-password");
    await submit();
    expect(adminApi.login).toHaveBeenLastCalledWith({ password: "synthetic-password" }, expect.any(AbortSignal));
    expect(host.textContent).toContain("提交明细入口");
  });

  it("登录限流与未启用有独立提示", async () => {
    vi.mocked(adminApi.session).mockRejectedValue(new AdminApiError(401));
    vi.mocked(adminApi.login).mockRejectedValueOnce(new AdminApiError(429));
    await render("login");
    await input("admin-password", "synthetic-password");
    await submit();
    expect(host.textContent).toContain("登录尝试过于频繁");
    vi.mocked(adminApi.login).mockRejectedValueOnce(new AdminApiError(404));
    await input("admin-password", "synthetic-password");
    await submit();
    expect(host.textContent).toContain("本站未开启管理功能");
  });
});

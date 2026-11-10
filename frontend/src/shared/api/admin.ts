/** 管理接口独立使用同源 Cookie，绝不读取翻译或解析凭据。 */
import type { components } from "@/shared/api/schema";

export type AdminSession = components["schemas"]["AdminSessionOut"];
export type SubmissionSearch = components["schemas"]["SubmissionSearch"];
export type SubmissionPage = components["schemas"]["SubmissionPage"];
export type SubmissionRecord = components["schemas"]["SubmissionRecord"];

export class AdminApiError extends Error {
  constructor(
    readonly status: number,
    readonly fields: Record<string, string> = {},
    readonly retryAfter: string | null = null,
  ) {
    super(status === 408 ? "管理请求超时，请重试。" : "管理请求未能完成。");
  }
}

const fieldMessages: Record<string, string> = {
  title_query: "标题关键词最多 100 个字符。",
  ip: "请输入完整、有效的 IPv4 或 IPv6 地址。",
  date_from: "请输入有效的起始日期。",
  date_to: "请输入有效的结束日期，且不能早于起始日期。",
  page: "页码无效，请刷新查询。",
  page_size: "每页可选择 25、50 或 100 条。",
};

async function request<T>(
  path: string,
  method: "GET" | "POST" | "DELETE",
  body?: unknown,
  csrfToken?: string,
  signal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  const cancel = () => controller.abort(signal?.reason);
  if (signal?.aborted) cancel();
  else signal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 20000);
  try {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(method !== "GET" ? { "Content-Type": "application/json" } : {}),
        ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
      },
      ...(method !== "GET" ? { body: JSON.stringify(body ?? {}) } : {}),
    });
    if (!response.ok) {
      const fields: Record<string, string> = {};
      if (response.status === 422) {
        try {
          const payload = (await response.json()) as { detail?: { loc?: unknown[] }[] };
          if (Array.isArray(payload.detail)) {
            for (const item of payload.detail) {
              const field = item.loc?.at(-1);
              if (typeof field === "string" && fieldMessages[field]) fields[field] = fieldMessages[field];
              else if (field === "body") fields.date_to = fieldMessages.date_to!;
            }
          }
        } catch {
          // 不把可能包含输入值的后端原始错误内容展示或写入日志。
        }
      }
      throw new AdminApiError(response.status, fields, response.headers.get("Retry-After"));
    }
    return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
  } catch (error) {
    if (timedOut) throw new AdminApiError(408);
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

export const adminApi = {
  login: (body: components["schemas"]["AdminLoginIn"], signal?: AbortSignal) =>
    request<AdminSession>("/api/admin/session", "POST", body, undefined, signal),
  session: (signal?: AbortSignal) => request<AdminSession>("/api/admin/session", "GET", undefined, undefined, signal),
  logout: (csrfToken: string, signal?: AbortSignal) =>
    request<void>("/api/admin/session", "DELETE", {}, csrfToken, signal),
  search: (body: SubmissionSearch, csrfToken: string, signal?: AbortSignal) =>
    request<SubmissionPage>("/api/admin/submissions/search", "POST", body, csrfToken, signal),
};

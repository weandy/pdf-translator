/** 管理接口独立使用同源 Cookie 与 CSRF Token，绝不读取前台翻译或解析凭据。 */
import type { components } from "@/shared/api/schema";

export type AdminSession = components["schemas"]["AdminSessionOut"];
export type AdminLoginIn = components["schemas"]["AdminLoginIn"];
export type SubmissionSearch = components["schemas"]["SubmissionSearch"];
export type SubmissionPage = components["schemas"]["SubmissionPage"];
export type SubmissionRecord = components["schemas"]["SubmissionRecord"];
export type AdminOverview = components["schemas"]["AdminOverviewOut"];
export type AdminQueues = components["schemas"]["AdminQueuesOut"];
export type QueueSlotStats = components["schemas"]["QueueSlotStats"];
export type AdminServiceSummary = components["schemas"]["AdminServiceSummaryOut"];
export type AdminStatsSnapshot = components["schemas"]["AdminStatsSnapshot"];
export type OverviewFailureSummary = components["schemas"]["OverviewFailureSummary"];
export type ClientBreakdown = components["schemas"]["ClientBreakdown"];
export type ClientBucket = components["schemas"]["ClientBucket"];
export type ActiveJobSummary = components["schemas"]["ActiveJobSummaryOut"];
export type AdminConfigView = components["schemas"]["AdminConfigView"];
export type FreeTranslationView = components["schemas"]["FreeTranslationView"];
export type FreeProviderView = components["schemas"]["FreeProviderView"];
export type ParserServiceView = components["schemas"]["ParserServiceView"];
export type DefaultLlmConfig = components["schemas"]["DefaultLlmConfig"];
export type LlmPreset = components["schemas"]["LlmPreset"];
export type PricingConfig = components["schemas"]["PricingConfig"];
export type ModelPriceEntry = components["schemas"]["ModelPriceEntry"];
export type GlossaryAdminView = components["schemas"]["GlossaryAdminView"];
export type GlossarySourceView = components["schemas"]["GlossarySourceView"];
export type SystemConfig = components["schemas"]["SystemConfig"];
export type EnvLockItem = components["schemas"]["EnvLockItem"];
export type SecretView = components["schemas"]["SecretView"];
export type SecretUpdate = components["schemas"]["SecretUpdate"];
export type ConnectionTestIn = components["schemas"]["ConnectionTestIn"];
export type ConnectionTestOut = components["schemas"]["ConnectionTestOut"];
export type ConfigRevisionSummary = components["schemas"]["ConfigRevisionSummary"];
export type ConfigRevisionsOut = components["schemas"]["ConfigRevisionsOut"];
export type RollbackIn = components["schemas"]["RollbackIn"];

export type FreeProviderPatch = components["schemas"]["FreeProviderPatch"];
export type FreeTranslationPatch = components["schemas"]["FreeTranslationPatch"];
export type ParserPatch = components["schemas"]["ParserPatch"];
export type DefaultLlmPatch = components["schemas"]["DefaultLlmPatch"];
export type PricingPatch = components["schemas"]["PricingPatch"];
export type GlossaryPatch = components["schemas"]["GlossaryPatch"];
export type SystemPatch = components["schemas"]["SystemPatch"];

export interface ConfigSectionPayloadMap {
  free_translation: FreeTranslationPatch;
  parser: ParserPatch;
  default_llm: DefaultLlmPatch;
  pricing: PricingPatch;
  glossary: GlossaryPatch;
  system: SystemPatch;
}

export type ConfigSection = keyof ConfigSectionPayloadMap;

const SAFE_400_MESSAGES: readonly { keyword: string; message: string }[] = [
  {
    keyword: "修改服务地址或协议时必须同时更换或禁用原密钥",
    message: "修改服务地址或协议时，必须同时更换或禁用原密钥，避免旧密钥发送到新地址。",
  },
  {
    keyword: "开启免费翻译前请至少启用并配置一个有效上游渠道及密钥",
    message: "开启免费翻译前，请至少启用并配置一个有效的上游渠道及密钥。",
  },
  {
    keyword: "主密钥",
    message: "服务端加密主密钥未就绪，暂时无法保存加密凭据或代理，请检查数据目录权限或主密钥配置。",
  },
  {
    keyword: "HTTPS",
    message: "服务地址必须使用合法的公网 HTTPS 地址。",
  },
  {
    keyword: "内网",
    message: "服务地址不允许指向本机或内网地址。",
  },
  {
    keyword: "代理",
    message: "代理地址格式无效，仅支持 http、https、socks5 或 socks5h 协议。",
  },
  {
    keyword: "重复",
    message: "列表中存在重复标识或模型名，请去重后再保存。",
  },
  {
    keyword: "回滚",
    message: "指定的历史版本无法回滚，请刷新配置列表后重试。",
  },
  {
    keyword: "术语库",
    message: "包含未知的术语库来源，请刷新配置后重试。",
  },
];

function defaultStatusMessage(status: number): string {
  switch (status) {
    case 400:
      return "提交的配置或参数不符合安全校验要求，请检查地址、模型、密钥绑定与数值范围后重试。";
    case 401:
      return "管理会话已失效，请重新登录。";
    case 403:
      return "请求验证未通过，请重新验证管理会话。";
    case 404:
      return "本站未开启管理功能或请求的资源不存在。";
    case 408:
      return "管理请求超时，请重试。";
    case 409:
      return "配置已在其他窗口更新，请刷新最新配置后重试。";
    case 413:
      return "提交内容超出大小限制，请精简后重试。";
    case 422:
      return "提交的参数格式或范围无效，请检查提示字段后重试。";
    case 429:
      return "操作过于频繁，请稍后重试。";
    default:
      return "管理请求未能完成，请稍后重试。";
  }
}

function safeBusinessMessage(detail: unknown): string | null {
  if (typeof detail !== "string") return null;
  for (const rule of SAFE_400_MESSAGES) {
    if (detail.includes(rule.keyword)) return rule.message;
  }
  return null;
}

export class AdminApiError extends Error {
  constructor(
    readonly status: number,
    readonly fields: Record<string, string> = {},
    readonly retryAfter: string | null = null,
    message?: string,
  ) {
    super(message ?? defaultStatusMessage(status));
    this.name = "AdminApiError";
  }
}

const fieldMessages: Record<string, string> = {
  title_query: "标题关键词最多 100 个字符。",
  ip: "请输入完整、有效的 IPv4 或 IPv6 地址。",
  date_from: "请输入有效的起始日期。",
  date_to: "请输入有效的结束日期，且不能早于起始日期。",
  page: "页码无效，请刷新查询。",
  page_size: "每页可选择 25、50 或 100 条。",
  channel_mode: "请选择有效的通道方式。",
  status: "请选择有效的任务状态。",
  client_device: "请选择有效的终端类型。",
  expected_revision: "配置版本信息无效，请刷新最新配置后重试。",
  target_revision: "目标回滚版本号无效。",
  max_pages: "页数上限须为合法正整数。",
  notice: "提示文案最多 500 个字符。",
  base_url: "请输入合法的 HTTPS 服务地址。",
  mineru_base_url: "请输入合法的 MinerU 服务地址。",
  model: "请输入有效的模型名称。",
  fallback_model: "备选模型名称长度超出限制。",
  timeout_s: "超时时间须在 15 至 300 秒之间。",
  protocol: "协议仅支持 OpenAI 兼容或 Anthropic 兼容。",
  max_upload_mb: "上传大小上限须在 1 至 2048 MB 之间。",
  max_concurrent_jobs: "版面解析总并发须在 1 至 64 之间。",
  max_concurrent_translations: "自备 Key 并发须在 1 至 64 之间。",
  free_max_concurrent_translations: "免费翻译并发须在 1 至 32 之间。",
  max_queued_jobs: "自备 Key 排队上限须在 1 至 10000 之间。",
  free_max_queued_jobs: "免费排队上限须在 1 至 10000 之间。",
  retention_hours: "文件保留时长须在 0 至 8760 小时之间。",
  min_free_disk_mb: "最小剩余磁盘空间须为非负整数。",
  site_notice: "全站公告最多 500 个字符。",
  sources: "术语库来源配置无效，请刷新后重试。",
};

type HttpMethod = "GET" | "POST" | "PUT" | "DELETE";

async function request<T>(
  path: string,
  method: HttpMethod,
  body?: unknown,
  csrfToken?: string,
  signal?: AbortSignal,
  timeoutMs = 20000,
): Promise<T> {
  const controller = new AbortController();
  const cancel = () => controller.abort(signal?.reason);
  if (signal?.aborted) cancel();
  else signal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
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
      let resolvedMessage: string | undefined;
      if (response.status === 422 || response.status === 400) {
        try {
          const payload = (await response.json()) as { detail?: unknown };
          if (response.status === 422 && Array.isArray(payload.detail)) {
            for (const rawItem of payload.detail) {
              const item = rawItem as { loc?: unknown[] };
              const field = item.loc?.at(-1);
              if (typeof field === "string" && fieldMessages[field]) {
                fields[field] = fieldMessages[field]!;
              } else if (field === "body") {
                fields.date_to = fieldMessages.date_to!;
              }
            }
          } else if (response.status === 400) {
            resolvedMessage = safeBusinessMessage(payload.detail) ?? undefined;
          }
        } catch {
          // 绝不将可能包含输入值的后端原始错误正文直接展示或写入日志。
        }
      }
      throw new AdminApiError(response.status, fields, response.headers.get("Retry-After"), resolvedMessage);
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
  login: (body: AdminLoginIn, signal?: AbortSignal) =>
    request<AdminSession>("/api/admin/session", "POST", body, undefined, signal),
  session: (signal?: AbortSignal) => request<AdminSession>("/api/admin/session", "GET", undefined, undefined, signal),
  logout: (csrfToken: string, signal?: AbortSignal) =>
    request<void>("/api/admin/session", "DELETE", {}, csrfToken, signal),
  overview: (signal?: AbortSignal) => request<AdminOverview>("/api/admin/overview", "GET", undefined, undefined, signal),
  getConfig: (signal?: AbortSignal) => request<AdminConfigView>("/api/admin/config", "GET", undefined, undefined, signal),
  updateConfig: <S extends ConfigSection>(
    section: S,
    body: ConfigSectionPayloadMap[S],
    csrfToken: string,
    signal?: AbortSignal,
  ) => request<AdminConfigView>(`/api/admin/config/${section}`, "PUT", body, csrfToken, signal),
  testConnection: (body: ConnectionTestIn, csrfToken: string, signal?: AbortSignal) =>
    request<ConnectionTestOut>("/api/admin/config/test", "POST", body, csrfToken, signal, 35000),
  listRevisions: (params?: { limit?: number; offset?: number }, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    if (params?.limit !== undefined) query.set("limit", String(params.limit));
    if (params?.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString();
    return request<ConfigRevisionsOut>(
      suffix ? `/api/admin/config/revisions?${suffix}` : "/api/admin/config/revisions",
      "GET",
      undefined,
      undefined,
      signal,
    );
  },
  rollbackConfig: (body: RollbackIn, csrfToken: string, signal?: AbortSignal) =>
    request<AdminConfigView>("/api/admin/config/rollback", "POST", body, csrfToken, signal),
  reloadGlossary: (csrfToken: string, signal?: AbortSignal) =>
    request<GlossaryAdminView>("/api/admin/glossary/reload", "POST", {}, csrfToken, signal),
  search: (body: SubmissionSearch, csrfToken: string, signal?: AbortSignal) =>
    request<SubmissionPage>("/api/admin/submissions/search", "POST", body, csrfToken, signal),
};

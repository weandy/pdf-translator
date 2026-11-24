import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { useAdminConsole } from "@/pages/admin/AdminLayout";
import {
  formatMergeSummary,
  mergeFields,
  type FieldConflict,
  type MergeFieldSpec,
} from "@/pages/admin/mergeConfig";
import {
  adminApi,
  AdminApiError,
  type AdminConfigView,
  type ConfigRevisionSummary,
  type SystemConfig,
} from "@/shared/api/admin";

const dateTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const SECTION_LABELS: Record<string, string> = {
  free_translation: "免费翻译池",
  parser: "版面解析",
  default_llm: "自备预设",
  pricing: "模型计价",
  glossary: "公共术语库",
  system: "系统设置",
  rollback: "配置回滚",
};

interface SystemFormDraft {
  free_max_concurrent_translations: string;
  max_concurrent_translations: string;
  max_concurrent_jobs: string;
  free_max_queued_jobs: string;
  max_queued_jobs: string;
  max_upload_mb: string;
  max_pages: string;
  min_free_disk_mb: string;
  retention_hours: string;
  maintenance_mode: boolean;
  site_notice: string;
}

function toSystemDraft(sys: SystemConfig): SystemFormDraft {
  return {
    free_max_concurrent_translations: String(sys.free_max_concurrent_translations),
    max_concurrent_translations: String(sys.max_concurrent_translations),
    max_concurrent_jobs: String(sys.max_concurrent_jobs),
    free_max_queued_jobs: String(sys.free_max_queued_jobs),
    max_queued_jobs: String(sys.max_queued_jobs),
    max_upload_mb: String(sys.max_upload_mb),
    max_pages: String(sys.max_pages),
    min_free_disk_mb: String(sys.min_free_disk_mb),
    retention_hours: String(sys.retention_hours),
    maintenance_mode: sys.maintenance_mode,
    site_notice: sys.site_notice,
  };
}

const SYSTEM_FIELD_SPECS: readonly MergeFieldSpec<SystemFormDraft>[] = [
  { key: "free_max_concurrent_translations", label: "免费翻译并发上限" },
  { key: "max_concurrent_translations", label: "自备 Key 翻译并发上限" },
  { key: "max_concurrent_jobs", label: "版面解析总并发" },
  { key: "free_max_queued_jobs", label: "免费排队上限" },
  { key: "max_queued_jobs", label: "自备 Key 排队上限" },
  { key: "max_upload_mb", label: "上传大小上限（MB）" },
  { key: "max_pages", label: "全局页数上限（页）" },
  { key: "min_free_disk_mb", label: "最小剩余磁盘（MB）" },
  { key: "retention_hours", label: "文件保留时长（小时）" },
  { key: "maintenance_mode", label: "维护模式" },
  { key: "site_notice", label: "全站公告" },
];

function parseStrictNonNegativeInt(raw: string): number {
  const trimmed = raw.trim();
  if (!trimmed) return Number.NaN;
  const value = Number(trimmed);
  return Number.isInteger(value) && value >= 0 ? value : Number.NaN;
}

interface SettingsBanner {
  tone: "ok" | "err" | "warn" | "info";
  message: string;
  conflict?: boolean;
  conflicts?: FieldConflict[];
}

export function SettingsPage() {
  const { csrfToken, handleUnauthorized, syncSummary } = useAdminConsole();
  const [config, setConfig] = useState<AdminConfigView | null>(null);
  const [draft, setDraft] = useState<SystemFormDraft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [banner, setBanner] = useState<SettingsBanner | null>(null);

  const [revisions, setRevisions] = useState<ConfigRevisionSummary[]>([]);
  const [revisionsLoading, setRevisionsLoading] = useState(false);
  const [revisionsOffset, setRevisionsOffset] = useState(0);
  const [hasMoreRevisions, setHasMoreRevisions] = useState(false);
  const [confirmRollbackRev, setConfirmRollbackRev] = useState<number | null>(null);
  const [rollingBack, setRollingBack] = useState(false);
  const [rollbackBanner, setRollbackBanner] = useState<{ tone: "ok" | "err" | "warn"; message: string } | null>(null);

  const controllerRef = useRef<AbortController | null>(null);
  const baselineRef = useRef<SystemFormDraft | null>(null);
  const draftRef = useRef<SystemFormDraft | null>(null);
  const PAGE_SIZE = 10;

  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);

  const loadRevisions = useCallback(
    async (offset: number, append: boolean, signal?: AbortSignal) => {
      setRevisionsLoading(true);
      try {
        const res = await adminApi.listRevisions({ limit: PAGE_SIZE, offset }, signal);
        if (signal?.aborted) return;
        setRevisions((prev) => (append ? [...prev, ...res.items] : res.items));
        setRevisionsOffset(offset);
        setHasMoreRevisions(res.items.length >= PAGE_SIZE);
      } catch (reason) {
        if (signal?.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          handleUnauthorized();
        }
      } finally {
        if (!signal?.aborted) setRevisionsLoading(false);
      }
    },
    [handleUnauthorized],
  );

  const loadAll = useCallback(
    async (mode: "reset" | "merge" = "reset") => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setLoading(true);
      setLoadError("");
      try {
        const view = await adminApi.getConfig(controller.signal);
        if (controller.signal.aborted) return;
        setConfig(view);
        syncSummary({
          revision: view.revision,
          secretKeyReady: view.secret_key_ready,
          maintenanceMode: view.system.maintenance_mode,
          warnings: view.warnings,
        });
        const remoteDraft = toSystemDraft(view.system);
        if (mode === "merge" && baselineRef.current && draftRef.current) {
          const outcome = mergeFields(baselineRef.current, draftRef.current, remoteDraft, SYSTEM_FIELD_SPECS);
          baselineRef.current = remoteDraft;
          setDraft(outcome.merged);
          setDirty(outcome.dirty);
          setBanner({
            tone: outcome.conflicts.length > 0 ? "warn" : "info",
            message: formatMergeSummary(view.revision, outcome),
            conflicts: outcome.conflicts.length > 0 ? outcome.conflicts : undefined,
          });
        } else {
          baselineRef.current = remoteDraft;
          setDraft(remoteDraft);
          setDirty(false);
          if (mode === "reset") {
            setBanner(null);
          }
        }
        await loadRevisions(0, false, controller.signal);
      } catch (reason) {
        if (controller.signal.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          handleUnauthorized();
          return;
        }
        setLoadError(reason instanceof AdminApiError ? reason.message : "读取系统设置失败，请重试。");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    },
    [handleUnauthorized, loadRevisions, syncSummary],
  );

  useEffect(() => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    void adminApi.getConfig(controller.signal).then(
      async (view) => {
        if (controller.signal.aborted) return;
        setConfig(view);
        setLoadError("");
        syncSummary({
          revision: view.revision,
          secretKeyReady: view.secret_key_ready,
          maintenanceMode: view.system.maintenance_mode,
          warnings: view.warnings,
        });
        const initialDraft = toSystemDraft(view.system);
        baselineRef.current = initialDraft;
        setDraft(initialDraft);
        setDirty(false);
        await loadRevisions(0, false, controller.signal);
        if (!controller.signal.aborted) setLoading(false);
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          handleUnauthorized();
          return;
        }
        setLoadError(reason instanceof AdminApiError ? reason.message : "读取系统设置失败，请重试。");
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [handleUnauthorized, loadRevisions, syncSummary]);

  async function handleSave(event: FormEvent) {
    event.preventDefault();
    if (!config || !draft || saving) return;

    const payload = {
      expected_revision: config.revision,
      free_max_concurrent_translations: parseStrictNonNegativeInt(draft.free_max_concurrent_translations),
      max_concurrent_translations: parseStrictNonNegativeInt(draft.max_concurrent_translations),
      max_concurrent_jobs: parseStrictNonNegativeInt(draft.max_concurrent_jobs),
      free_max_queued_jobs: parseStrictNonNegativeInt(draft.free_max_queued_jobs),
      max_queued_jobs: parseStrictNonNegativeInt(draft.max_queued_jobs),
      max_upload_mb: parseStrictNonNegativeInt(draft.max_upload_mb),
      max_pages: parseStrictNonNegativeInt(draft.max_pages),
      min_free_disk_mb: parseStrictNonNegativeInt(draft.min_free_disk_mb),
      retention_hours: parseStrictNonNegativeInt(draft.retention_hours),
      maintenance_mode: draft.maintenance_mode,
      site_notice: draft.site_notice.trim(),
    };

    const numericFields = [
      payload.free_max_concurrent_translations,
      payload.max_concurrent_translations,
      payload.max_concurrent_jobs,
      payload.free_max_queued_jobs,
      payload.max_queued_jobs,
      payload.max_upload_mb,
      payload.max_pages,
      payload.min_free_disk_mb,
      payload.retention_hours,
    ];
    if (numericFields.some((num) => !Number.isInteger(num) || num < 0)) {
      setBanner({ tone: "err", message: "所有并发、队列、容量与时长项须填写合法整数。" });
      return;
    }

    setSaving(true);
    setBanner(null);
    try {
      const updated = await adminApi.updateConfig("system", payload, csrfToken);
      const nextDraft = toSystemDraft(updated.system);
      baselineRef.current = nextDraft;
      setConfig(updated);
      setDraft(nextDraft);
      setDirty(false);
      syncSummary({
        revision: updated.revision,
        secretKeyReady: updated.secret_key_ready,
        maintenanceMode: updated.system.maintenance_mode,
        warnings: updated.warnings,
      });
      setBanner({ tone: "ok", message: `系统设置已保存并热生效（v${updated.revision}）。` });
      void loadRevisions(0, false);
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      if (reason instanceof AdminApiError && reason.status === 409) {
        setBanner({
          tone: "warn",
          message: "配置已在其他窗口更新，请加载最新并合并后再保存（当前草稿已保留）。",
          conflict: true,
        });
      } else if (reason instanceof AdminApiError) {
        const firstField = Object.values(reason.fields)[0];
        setBanner({ tone: "err", message: firstField ?? reason.message });
      } else {
        setBanner({ tone: "err", message: "保存系统设置失败，请稍后重试。" });
      }
    } finally {
      setSaving(false);
    }
  }

  async function executeRollback(targetRevision: number) {
    if (!config || rollingBack) return;
    setRollingBack(true);
    setRollbackBanner(null);
    try {
      const updated = await adminApi.rollbackConfig(
        { target_revision: targetRevision, expected_revision: config.revision },
        csrfToken,
      );
      const nextDraft = toSystemDraft(updated.system);
      baselineRef.current = nextDraft;
      setConfig(updated);
      setDraft(nextDraft);
      setDirty(false);
      setBanner(null);
      setConfirmRollbackRev(null);
      syncSummary({
        revision: updated.revision,
        secretKeyReady: updated.secret_key_ready,
        maintenanceMode: updated.system.maintenance_mode,
        warnings: updated.warnings,
      });
      setRollbackBanner({
        tone: "ok",
        message: `已按 v${targetRevision} 回滚非敏感配置，当前版本为 v${updated.revision}（密钥与代理未受影响）。`,
      });
      void loadRevisions(0, false);
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      if (reason instanceof AdminApiError && reason.status === 409) {
        setRollbackBanner({
          tone: "warn",
          message: "配置已在其他窗口更新，请刷新最新配置后再执行回滚。",
        });
      } else {
        setRollbackBanner({
          tone: "err",
          message: reason instanceof AdminApiError ? reason.message : "回滚失败，请重试。",
        });
      }
    } finally {
      setRollingBack(false);
    }
  }

  if (loading && !config) {
    return (
      <div className="ac-card" role="status">
        正在读取系统设置…
      </div>
    );
  }

  if (loadError && !config) {
    return (
      <div className="ac-alert err" role="alert">
        <span>{loadError}</span>
        <button type="button" className="ac-btn sm" onClick={() => void loadAll("reset")}>
          重试
        </button>
      </div>
    );
  }

  if (!config || !draft) return null;

  const adoptLatestSystemDraft = () => {
    const latest = toSystemDraft(config.system);
    baselineRef.current = latest;
    setDraft(latest);
    setDirty(false);
    setBanner({ tone: "info", message: `已采用最新配置（v${config.revision}）覆盖本地草稿。` });
  };

  return (
    <>
      <section className="ac-card" aria-labelledby="settings-system-heading">
        <form onSubmit={(event) => void handleSave(event)}>
          <div className="ac-card-hd">
            <div>
              <h2 className="ac-card-title" id="settings-system-heading">
                队列并发、上传限制与维护模式
              </h2>
              <p className="ac-card-sub">
                并发与队列上限平滑生效不中断在途任务；文件保留时长调整仅影响新任务与清理阈值。
              </p>
            </div>
            <label className="ac-toggle">
              <input
                id="sys-maintenance"
                type="checkbox"
                checked={draft.maintenance_mode}
                onChange={(event) => {
                  setDirty(true);
                  setBanner(null);
                  setDraft({ ...draft, maintenance_mode: event.target.checked });
                }}
              />
              <span>{draft.maintenance_mode ? "维护模式：已开启" : "维护模式：关闭"}</span>
            </label>
          </div>

          {banner && (
            <div
              className={`ac-alert ${banner.tone}`}
              role={banner.tone === "ok" || banner.tone === "info" ? "status" : "alert"}
              style={{ marginBottom: 14 }}
            >
              <div className="ac-alert-body">
                <span>{banner.message}</span>
                {banner.conflicts && banner.conflicts.length > 0 && (
                  <ul className="ac-conflict-list">
                    {banner.conflicts.map((item) => (
                      <li key={item.label}>
                        <strong>{item.label}</strong>：你的草稿「{item.localValue}」 / 最新配置「{item.remoteValue}」
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="ac-actions">
                {banner.conflict && (
                  <button type="button" className="ac-btn sm" onClick={() => void loadAll("merge")}>
                    加载最新并合并
                  </button>
                )}
                {(banner.conflict || (banner.conflicts && banner.conflicts.length > 0)) && (
                  <button
                    type="button"
                    className="ac-btn sm"
                    onClick={() => (banner.conflict ? void loadAll("reset") : adoptLatestSystemDraft())}
                  >
                    全部采用最新覆盖草稿
                  </button>
                )}
              </div>
            </div>
          )}

          <div className="ac-grid-3">
            <div className="ac-field">
              <label htmlFor="sys-free-conc">免费翻译并发上限</label>
              <input
                id="sys-free-conc"
                type="number"
                min={1}
                max={32}
                value={draft.free_max_concurrent_translations}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, free_max_concurrent_translations: event.target.value });
                }}
              />
            </div>

            <div className="ac-field">
              <label htmlFor="sys-byok-conc">自备 Key 翻译并发上限</label>
              <input
                id="sys-byok-conc"
                type="number"
                min={1}
                max={64}
                value={draft.max_concurrent_translations}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, max_concurrent_translations: event.target.value });
                }}
              />
            </div>

            <div className="ac-field">
              <label htmlFor="sys-parse-conc">
                <span>版面解析总并发</span>
                <span className="ac-hint">自备 Key 预留 1 席</span>
              </label>
              <input
                id="sys-parse-conc"
                type="number"
                min={1}
                max={64}
                value={draft.max_concurrent_jobs}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, max_concurrent_jobs: event.target.value });
                }}
              />
            </div>

            <div className="ac-field">
              <label htmlFor="sys-free-queue">免费排队上限</label>
              <input
                id="sys-free-queue"
                type="number"
                min={1}
                max={10000}
                value={draft.free_max_queued_jobs}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, free_max_queued_jobs: event.target.value });
                }}
              />
            </div>

            <div className="ac-field">
              <label htmlFor="sys-byok-queue">自备 Key 排队上限</label>
              <input
                id="sys-byok-queue"
                type="number"
                min={1}
                max={10000}
                value={draft.max_queued_jobs}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, max_queued_jobs: event.target.value });
                }}
              />
            </div>

            <div className="ac-field">
              <label htmlFor="sys-max-upload">上传大小上限（MB）</label>
              <input
                id="sys-max-upload"
                type="number"
                min={1}
                max={2048}
                value={draft.max_upload_mb}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, max_upload_mb: event.target.value });
                }}
              />
            </div>

            <div className="ac-field">
              <label htmlFor="sys-max-pages">全局页数上限（页）</label>
              <input
                id="sys-max-pages"
                type="number"
                min={1}
                max={10000}
                value={draft.max_pages}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, max_pages: event.target.value });
                }}
              />
            </div>

            <div className="ac-field">
              <label htmlFor="sys-min-disk">最小剩余磁盘（MB）</label>
              <input
                id="sys-min-disk"
                type="number"
                min={0}
                value={draft.min_free_disk_mb}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, min_free_disk_mb: event.target.value });
                }}
              />
            </div>

            <div className="ac-field">
              <label htmlFor="sys-retention">
                <span>文件保留时长（小时）</span>
                <span className="ac-hint">仅影响新任务与清理阈值</span>
              </label>
              <input
                id="sys-retention"
                type="number"
                min={0}
                max={8760}
                value={draft.retention_hours}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, retention_hours: event.target.value });
                }}
              />
            </div>

            <div className="ac-field span-2">
              <label htmlFor="sys-notice">全站公告（留空则前台不显示）</label>
              <input
                id="sys-notice"
                type="text"
                maxLength={500}
                placeholder="例如：今晚 23:00 例行维护，期间暂停新任务上传"
                value={draft.site_notice}
                onChange={(event) => {
                  setDirty(true);
                  setDraft({ ...draft, site_notice: event.target.value });
                }}
              />
            </div>
          </div>

          <div className="ac-card-ft">
            <span className="ac-hint">当前配置版本 v{config.revision}，提交携带版本乐观锁。</span>
            <div className="ac-actions">
              <button
                type="button"
                className="ac-btn sm"
                disabled={!dirty || saving}
                onClick={() => {
                  const latest = toSystemDraft(config.system);
                  baselineRef.current = latest;
                  setDraft(latest);
                  setDirty(false);
                  setBanner(null);
                }}
              >
                还原更改
              </button>
              <button type="submit" className="ac-btn primary sm" disabled={saving}>
                {saving ? "保存中…" : "保存系统设置"}
              </button>
            </div>
          </div>
        </form>
      </section>

      <section className="ac-card" aria-labelledby="settings-revisions-heading">
        <div className="ac-card-hd">
          <div>
            <h2 className="ac-card-title" id="settings-revisions-heading">
              最近配置变更记录
            </h2>
            <p className="ac-card-sub">仅回滚非敏感配置项，已加密的 API Key、Token 与网络代理不受回滚影响。</p>
          </div>
          <div className="ac-actions">
            <span className="ac-badge mono pen">当前 v{config.revision}</span>
            <button
              type="button"
              className="ac-btn sm"
              disabled={revisionsLoading}
              onClick={() => void loadRevisions(0, false)}
            >
              {revisionsLoading ? "刷新中…" : "刷新记录"}
            </button>
          </div>
        </div>

        {rollbackBanner && (
          <div className={`ac-alert ${rollbackBanner.tone}`} role={rollbackBanner.tone === "ok" ? "status" : "alert"} style={{ marginBottom: 12 }}>
            <span>{rollbackBanner.message}</span>
          </div>
        )}

        {revisions.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {revisions.map((rev) => {
              const isCurrent = rev.revision === config.revision;
              const confirming = confirmRollbackRev === rev.revision;
              return (
                <div key={rev.revision} className="ac-rev-row">
                  <div>
                    <strong>
                      v{rev.revision} · {SECTION_LABELS[rev.section] ?? rev.section}
                      {rev.summary ? ` — ${rev.summary}` : ""}
                    </strong>
                    <div className="ac-rev-meta">
                      {dateTime.format(new Date(rev.created_at))}
                      {rev.changed_fields.length > 0 ? ` · 变更字段：${rev.changed_fields.join(", ")}` : ""}
                    </div>
                  </div>
                  <div className="ac-actions">
                    {isCurrent ? (
                      <span className="ac-badge ok">当前版本</span>
                    ) : confirming ? (
                      <>
                        <span className="ac-hint">确认回滚到 v{rev.revision} 的非敏感项？</span>
                        <button
                          type="button"
                          className="ac-btn sm danger"
                          disabled={rollingBack}
                          onClick={() => void executeRollback(rev.revision)}
                        >
                          {rollingBack ? "回滚中…" : "确认回滚"}
                        </button>
                        <button
                          type="button"
                          className="ac-btn sm"
                          disabled={rollingBack}
                          onClick={() => setConfirmRollbackRev(null)}
                        >
                          取消
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        className="ac-btn sm"
                        disabled={rollingBack}
                        onClick={() => {
                          setRollbackBanner(null);
                          setConfirmRollbackRev(rev.revision);
                        }}
                      >
                        回滚非敏感项
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
            {hasMoreRevisions && (
              <div>
                <button
                  type="button"
                  className="ac-btn sm"
                  disabled={revisionsLoading}
                  onClick={() => void loadRevisions(revisionsOffset + PAGE_SIZE, true)}
                >
                  {revisionsLoading ? "加载中…" : "加载更多变更记录"}
                </button>
              </div>
            )}
          </div>
        ) : (
          <p className="ac-empty">暂无历史配置变更记录。</p>
        )}
      </section>

      <section className="ac-card" aria-labelledby="settings-env-heading">
        <div className="ac-card-hd">
          <div>
            <h2 className="ac-card-title" id="settings-env-heading">
              环境变量锁定项（只读）
            </h2>
            <p className="ac-card-sub">由部署环境直接控制的安全边界项，仅在服务端环境变量中修改。</p>
          </div>
        </div>
        {config.env_locks.length > 0 ? (
          <div className="ac-env-grid">
            {config.env_locks.map((item) => (
              <div key={item.name} className="ac-env-item">
                <div>
                  <strong>{item.name}</strong>
                  {item.detail && <div className="ac-hint">{item.detail}</div>}
                </div>
                <span className="ac-badge">{item.status}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="ac-empty">暂无环境变量锁定项。</p>
        )}
      </section>
    </>
  );
}

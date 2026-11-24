# 免费翻译通道与管理后台重构实施计划

> **For Antigravity:** 采用多 Neon 子 Agent 按非重叠文件分片并行实施，主 Agent 串行把控所有构建/测试门禁，收口后派 Codex (`apice/gpt-6.2-astra` xhigh) 独立审核并将审核意见回传 Neon 修复闭环。

**Goal:** 在独立 worktree（`D:\01\pdf-translator-admin-free`，分支 `feat/admin-console-free-channel`，基线 `a7fe439`）完整落地 v3.0 架构设计：站点内置多上游免费翻译通道、AES-256-GCM 加密热配置与零重启快照、全链路 UA 采集统计、4 菜单专业管理后台重构（移除切窗隐藏、无损继承并升级翻译记录、4 大服务设置架构分区）、前台双模式上传切换，以及公式占位标签正则修复。

**Architecture:** 严守后端 `import-linter` 四层依赖（`api → pipeline → 能力层 → domain`，能力层互不导入，`domain` 零外部依赖）与前端 ESLint 五层依赖（`app → pages → features → entities → shared`）。单进程 `uvicorn` 下通过 SQLite `BEGIN IMMEDIATE` 乐观锁持久化 + 不可变内存快照 + 事件循环内 `DynamicSlots` 实现零重启热重载。

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite (WAL), cryptography (AES-256-GCM), React 19, TypeScript, Vite, TanStack Query, OpenAPI (`openapi-typescript`).

---

## Wave 1：底层基建（3 个 Neon 子 Agent 并行，零文件重叠）

### 分片 A（Neon-A）：配置模型、AES-256-GCM 持久化与出站 URL/代理校验
- **Create**:
  - `backend/src/pdftr/domain/runtime_config.py`
  - `backend/src/pdftr/storage/runtime_config.py`
  - `backend/src/pdftr/pipeline/url_guard.py`
  - `backend/scripts/generate_config_key.py`
  - `backend/tests/unit/test_runtime_config_storage.py`
  - `backend/tests/unit/test_url_guard.py`
- **Modify**: `backend/src/pdftr/config.py`（增加 `config_secret_key` 32 字节 Base64 校验）
- **核心契约**:
  - `SecretSlot` 支持三态 `custom | disabled | inherit_env`，含 `enc`、`mask`、`bound_url`、`bound_protocol`；
  - `AESGCM` 每次写入随机 12B Nonce，AAD 绑定字段路径；解密损坏返回降级状态而不抛崩溃异常；
  - `runtime_config`（`id = 1` + `revision`）与 `runtime_config_revisions`（绝不含明文/密文/代理账密）在 `BEGIN IMMEDIATE` 中校验 `expected_revision`；
  - `url_guard.py` 分流校验翻译 URL（HTTPS、禁内网/元数据）与代理 URL（`http/https/socks5/socks5h`，允许账密，禁链路本地与云元数据）。

### 分片 B（Neon-B）：User-Agent 解析、审计与统计扩展、元数据备份白名单
- **Create**:
  - `backend/src/pdftr/domain/user_agent.py`
  - `backend/tests/unit/test_user_agent.py`
- **Modify**:
  - `backend/src/pdftr/domain/submissions.py`、`backend/src/pdftr/domain/stats.py`
  - `backend/src/pdftr/storage/submissions.py`、`backend/src/pdftr/storage/stats.py`、`backend/src/pdftr/storage/sqlite.py`、`backend/src/pdftr/storage/port.py`、`backend/src/pdftr/storage/metadata_backup.py`
  - `backend/tests/unit/test_metadata_backup.py`
- **核心契约**:
  - `parse_user_agent(raw)` 零第三方依赖，清洗截断 512 字符，输出 `ClientInfo(raw, device, os, browser)`；
  - `submission_audit` 通过 `_MIGRATION_COLUMNS` 追加 `channel_mode`、`model_name`、`fallback_used`、`user_agent`、`client_device`、`client_os`、`client_browser`；`SubmissionSearch` 支持 `channel_mode`、`status`、`client_device`；
  - `analytics_client_daily(day, metric_type, device, os, browser, count)` 匿名聚合访问与提交终端分布，提供近 7 日汇总查询；
  - `metadata_backup.py` 白名单纳入新增非正文审计列与 `analytics_client_daily`，单测断言绝不导出 `runtime_config` 与 `runtime_config_revisions`。

### 分片 C（Neon-C）：翻译适配器、免费容灾缓存保护、热计价、公式正则修复与解析端点
- **Create**:
  - `backend/tests/unit/test_formula_tag_guard.py`
- **Modify**:
  - `backend/src/pdftr/domain/llm.py`（`ChatResult.actual_model`、`ChatError.status_code / error_code`）
  - `backend/src/pdftr/translation/openai_compat.py`（NVIDIA Nemotron 550B / 120B 精确模型适配、`follow_redirects=False`、禁用 120B 的 `_disabled` 拆 `extra_body`、结构化 429 分类与 `FallbackChat`）
  - `backend/src/pdftr/translation/service.py`（三段式公式正则修复、保留 `VALIDATION_VERSION = 5`、备选出字跳过主模型缓存）
  - `backend/src/pdftr/translation/cost.py`（支持传入热配置单价表覆盖默认 `PRICES`）
  - `backend/src/pdftr/parsing/mineru_cloud.py`、`backend/src/pdftr/parsing/paddle_vl.py`（支持自定义 `base_url` 与轻量连通性测试辅助）
  - `backend/tests/contract/test_openai_compat.py`

---

## Wave 2：后端热重载编排、双队列与全量管理接口（Neon-D）
- **Create**:
  - `backend/src/pdftr/pipeline/runtime_config.py`
  - `backend/src/pdftr/api/routes_admin_config.py`
  - `backend/tests/test_admin_runtime_config.py`
  - `backend/tests/test_free_channel_flow.py`
- **Modify**:
  - `backend/src/pdftr/domain/job.py`（`channel_mode`、`channel_provider_id`、`fallback_used`）
  - `backend/src/pdftr/pipeline/secrets.py`、`runner.py`、`usecases.py`
  - `backend/src/pdftr/container.py`
  - `backend/src/pdftr/api/deps.py`、`schemas.py`、`routes_meta.py`、`routes_jobs.py`、`routes_segments.py`、`routes_stats.py`、`routes_admin_submissions.py`、`app.py`
- **主 Agent 串行门禁 1**：`ruff format --check` → `ruff check` → `pyright` → `lint-imports` → `pytest -q` → 导出 `frontend/openapi.json` → `npm run gen:api`。

---

## Wave 3：前端并行实现（2 个 Neon 子 Agent 并行，零文件重叠）

### 分片 E（Neon-E）：4 菜单专业管理后台（无切窗隐藏 + IP/UA 记录 + 4 大服务架构分区）
- **Create**: `frontend/src/pages/admin/`（`AdminLayout.tsx`、`AdminLoginPage.tsx`、`OverviewPage.tsx`、`RecordsPage.tsx`、`ServicesPage.tsx`、`SettingsPage.tsx`、`admin-console.css`）
- **Modify**: `frontend/src/shared/api/admin.ts`、`frontend/src/pages/AdminPage.tsx`、`frontend/src/app/App.tsx`、`frontend/src/pages/AdminPage.test.tsx`、`frontend/src/shared/api/admin.test.ts`

### 分片 F（Neon-F）：前台双模式上传、快捷预设与维护公告
- **Modify**: `frontend/src/entities/settings/model.ts`、`frontend/src/shared/api/client.ts`、`frontend/src/features/upload/UploadPanel.tsx`、`frontend/src/features/settings/SettingsPanel.tsx`、`frontend/src/pages/HomePage.tsx`、`frontend/src/app/styles.css`、`frontend/src/features/settings/SettingsPanel.test.tsx`
- **主 Agent 串行门禁 2**：`npm run lint` → `npm run typecheck` → `npm test` → `npm run build` + 后端复验 `pytest -q`。

---

## Wave 4：Codex 独立审核 → Neon 修复闭环
1. 派 Codex (`codex` / `apice/gpt-6.2-astra` / `reasoningEffort: xhigh`) 对完整 `git diff main..HEAD` 进行严格审查；
2. 将审核意见核实并传递给 Neon 子 Agent 修复；
3. 主 Agent 串行复跑前后端全量门禁，输出最终交付报告。

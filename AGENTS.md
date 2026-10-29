# AGENTS.md — 接手说明

给接手本项目的编程代理（及人类开发者）看的工作说明。先读这一份，再按需读 `docs/` 里的设计文档。

## 1. 项目是什么

开源（AGPL-3.0）、可自部署的**网页端 PDF 保版式翻译**：上传英文论文 PDF，在浏览器里与原文并排阅读按原版式排好的中文译文，可逐段修改、下载中文版或双语对照 PDF。

- 后端：Python 3.12 + uv + FastAPI（`backend/`），HTTP 客户端用 `httpx2`（openai、anthropic SDK 也基于它）
- 前端：React 19 + Vite + TypeScript（`frontend/`），PDF 渲染用 pdf.js（legacy 构建），公式用 KaTeX
- 版面解析：MinerU 云端 API（默认且界面唯一选项）；PaddleOCR-VL 适配器保留在后端但界面不开放（解析慢）
- 翻译：任意 OpenAI 兼容接口（默认 DeepSeek）或 Anthropic Messages 接口
- 仓库：https://github.com/weandy/pdf-translator（`main` 分支，推送即触发 CI）

## 2. 目录

```
backend/src/pdftr/
  domain/        领域模型（IR：Document/Page/Block/Segment、Job、版面结果等），不依赖任何模块
  extraction/    PyMuPDF 字符提取、页面分类（原生/扫描/乱码，含隐形 OCR 层识别）
  parsing/       版面解析服务适配器（mineru_cloud、paddle_vl）与端口
  fusion/        解析结果 × PDF 字符融合成 IR（跨栏段落、行内公式定位 formula_align 等）
  glossary/      术语：自动抽取、聚合术语库、用户术语库导入（user_import）
  translation/   切分、提示词、批量翻译与重试、费用、OpenAI/Anthropic 适配器
  rendering/     背景页（去字/扫描页纸色覆盖）、排版器、导出 PDF、链接定位（links.py）
  storage/       SQLite（任务、段落、修改记录、译文缓存）与文件存储
  pipeline/      流水线 runner（分阶段名额调度、取消、重试）、usecases（接口用例）
  api/           FastAPI 路由与 schema
  container.py   组合根：在这里创建适配器并注入
backend/tests/   unit / contract（适配器，不联网）/ e2e（假解析器、假模型走完整接口）/ regression
frontend/src/    app → pages → features → entities → shared（见 §3）
docs/            调研、选型、验证实验、设计文档（见 §9）
deploy/          docker-compose（hostnet 叠加文件用于本机 Docker 网桥不可用的情况）
spikes/          早期验证实验脚本与 Playwright 浏览器测试环境（不属于产品代码）
```

## 3. 架构规则（CI 会检查）

**后端**（`lint-imports`，规则在 `backend/pyproject.toml`）
- 分层：`api → pipeline → 能力模块 → domain`，只能向下依赖。
- 能力模块（extraction、parsing、fusion、glossary、translation、rendering、storage）之间**互不导入**，只经 `domain` 的 IR 与端口（Protocol）交互。需要两边共用的模型放 `domain`。
- 适配器只在 `container.py` 组装；测试用 `build_container(settings, deps_override)` 替换解析器、模型等。

**前端**（ESLint 规则）
- `app → pages → features → entities → shared` 只能向下依赖；**features 之间互不导入**，需要组合的在 pages 里组合。
- 跨目录导入一律用 `@/` 别名，不要用 `../`。
- 接口类型由后端 OpenAPI 生成（`src/shared/api/schema.d.ts`），不要手写。

## 4. 流水线

阶段：`parse → fuse → segment → glossary → translate → render`（`domain/job.py: STAGES`）。每个阶段的产物落盘（`layout.json`、`ir.json`、段落表、`glossary.json`、`background.pdf`），**重试从中断的阶段继续**；已翻译的段落有译文缓存，不重复计费。

调度（`pipeline/runner.py: Slots`）：
- 上传与解析：最多 `PDFTR_MAX_CONCURRENT_JOBS`（默认 4）篇同时
- 对齐与生成背景页（PyMuPDF，耗内存）：一次一篇
- 术语抽取 + 翻译：`PDFTR_MAX_CONCURRENT_TRANSLATIONS`（默认 1）篇，每篇内部按服务商并发（DeepSeek 4 路）
- 排队的任务消息显示“排在第 N 位 / 前面还有 N 篇”

任务状态：`queued / running / done / failed / cancelled`。用户取消走 `Pipeline.cancel`（记入集合后取消 asyncio 任务）；服务停止时的取消不算用户取消，启动时由中断恢复标为失败、可重试。

## 5. 开发与检查

```bash
# 后端
cd backend
uv sync
uv run uvicorn pdftr.api.app:create_app --factory --reload      # http://127.0.0.1:8000
uv run --frozen ruff format . && uv run --frozen ruff check .
uv run --frozen pyright && uv run --frozen lint-imports && uv run --frozen pytest -q

# 前端
cd frontend
npm install
npm run dev                        # /api 转发到 8000，可用 PDFTR_API 覆盖
npm run lint && npm run typecheck && npm test && npm run build
```

- **改了接口（schema、路由）后**：`cd backend && uv run python scripts/export_openapi.py ../frontend/openapi.json`，再 `cd frontend && npm run gen:api`。后端测试 `test_openapi_sync` 会检查两边一致。
- CI（`.github/workflows`）跑的就是上面这些；提交前本地全部通过。
- 回归样本不入库：`backend/.samples/`（`scripts/fetch_samples.py` 生成，需要 MinerU Token）；扫描件样例在 `backend/.samples/scans/`（NASA NTRS 公有领域扫描件 + 电子版渲染的模拟扫描件）。
- 导出 PDF 用的字体由 `scripts/fetch_fonts.py` 下载到 `backend/fonts/`（Docker 构建时自动下载）。
- 浏览器验收：`spikes/.venv` 里装有 Playwright（`spikes/.venv/bin/python`），用它截图、量尺寸，手机视口用 390×844。

## 6. 约定

**语言**：代码注释、文档、提交说明、界面文字一律中文；标识符用英文。注释说明“为什么”，尤其是实测得出的取舍（附数据）。

**提交**
- 每完成一个逻辑步骤就提交并推送到 `origin/main`，一次提交只做一件事。
- 提交说明全中文，前缀如 `功能：`、`修复：`、`改进：`、`性能：`、`界面：`、`文档：`、`测试：`、`安全：`；正文写清楚为什么改、实测数据。
- 作者邮箱用 `84604972+weandy@users.noreply.github.com`（user.name `weandy`）。
- 提交前检查暂存区里没有密钥（API Key、Token、代理凭据、服务器密码），例如 `git diff --cached | grep -E "sk-[0-9a-zA-Z]{20}"`。

**格式**
- 后端：ruff（行宽 120）。
- 前端：项目没有 prettier 配置，行宽约 120。**只格式化改动过的文件**（`npx prettier --print-width 120 --write <文件>`），不要对整个目录跑默认 80 列的 prettier；格式化后看一眼 diff，混入无关改动就还原。

**测试**：新功能、修复都要有测试（单元或 e2e）。e2e 用 `tests/e2e/test_api_flow.py` 里的 `FakeParser`、`FakeChat` 与 `_upload` / `_wait`；测试目录不是 Python 包，不能跨测试文件导入。

**数据与隐私**
- 代码、测试、文档里**不要放真实用户的论文内容或用户的文件名**；测试数据用合成文本或公开论文（arXiv、公有领域扫描件）。
- 密钥只存在用户浏览器（请求头传给后端）或部署机本地的 `.env`；`.env` 已在 `.gitignore`，不要提交、不要打印它的内容。
- 用户术语库、Key 都只存浏览器；服务端不保存用户数据之外的东西（任务文件按保留期自动清理）。

**质量优先**：为省 token 改提示词、模型、思考强度等，必须同时给出翻译质量对比数据（盲评），不能只看成本。

## 7. 部署（项目维护者的服务器）

- 公网实例跑在 Docker 里（`pdf-translator-app-1`、`pdf-translator-web-1`），经 Cloudflare 快速隧道对外，systemd 服务名 `pdftr-tunnel`（cloudflared → `127.0.0.1:18080`）。快速隧道地址重启会变，查看：`journalctl -u pdftr-tunnel | grep trycloudflare`。固定域名尚未配置（等维护者提供 Cloudflare 上的域名）。
- 部署配置在仓库根目录的 `.env`（不入库，含代理等敏感配置）。
- **部署前必须硬性检查没有进行中的任务**，有就中止，连镜像都先不要构建（构建会占用带宽和 CPU，拖慢正在上传的任务）：

```bash
cd /root/pdf-translator
CHECK='import sqlite3,sys;db=sqlite3.connect("/data/app.db");r=db.execute("select id from jobs where status in (\"running\",\"queued\")").fetchall();print("进行中:",r);sys.exit(1 if r else 0)'
docker exec pdf-translator-app-1 python -c "$CHECK" \
  && docker build --network host -q -t pdf-translator-app backend \
  && docker build --network host -q -t pdf-translator-web frontend \
  && docker exec pdf-translator-app-1 python -c "$CHECK" \
  && docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d
```

- 只改了前端时只构建 `pdf-translator-web`。部署后在公网验收，测试任务用完删除（`DELETE /api/jobs/{id}`）。
- 服务停止时会等进行中的任务最多 180 秒（`shutdown_grace_s`），Compose 的 `stop_grace_period` 为 200 秒。
- MinerU 上海存储从这台机器直连很慢（约 25 KB/s），线上通过 `PDFTR_MINERU_PROXY`（SOCKS5）转发；代理机停用时删掉 `.env` 里这一项再重启。

## 8. 已知的坑

- **本机内存只有约 4 GB**：内存紧张时 pyright 会跑十几分钟（正常十几秒）、Chromium 会崩溃。跑检查前停掉不用的本地 vite / uvicorn。
- **停止本地进程**：`pkill -f` / `pgrep -f` 会匹配到命令自身把 shell 杀掉。用 `ps -eo pid,args | awk '/[u]vicorn/ && /1800[1]/ {print $1}' | xargs -r kill`，而且**停止和启动分开两条命令**。
- **Docker**：构建必须加 `--network host`（本机网桥不可用），运行叠加 `deploy/docker-compose.hostnet.yml`。8080 端口被别的服务占用，本项目用 18080。磁盘空间紧张，构建后 `docker image prune -f`。
- **heredoc 写前端代码**：`\u` 转义会被写成真实字符，正则转义请用 Python 脚本写入。
- **pdf.js**：用 `pdfjs-dist/legacy/build`（默认构建在稍旧的 Chrome 上整页画不出来）；CCITT / JBIG2 / JPEG 2000 图片要从 `wasmUrl` 加载解码器，前端用纯 JS 解码器放在 `/pdfjs/`（`vite.config.ts` 里的插件按原名输出），不给就会把扫描页画成空白。
- **KaTeX 新版类名**：片段是 `.katex-base`、`.katex-strut`（不是旧版的 `.base`）。行内公式与 `<sub>/<sup>` 设了不计入行高，避免行距不齐。
- **MinerU**：电子版有行级坐标（借用 PDF 文字层），扫描件只有块级坐标；PaddleOCR-VL 只有块级坐标，行内公式位置由 `fusion/formula_align.py` 推算。
- **扫描件**：网上的扫描版论文多带隐形 OCR 层（渲染模式 3，质量差），分类器据此判为扫描页并丢弃这层文字。
- **原文链接缓存**：`links.v{N}.json`，改了 `rendering/links.py` 的提取规则要把 `VERSION` 加一。
- **导出文件缓存**：`exports/{方式}-{译文侧}-{摘要}.pdf`，摘要含段落译文；改了导出的生成方式（排版、字体、链接等）要把 `pipeline/usecases.py` 的 `EXPORT_CACHE_VERSION` 加一。
- **保留期**：服务器只保留 24 小时（`PDFTR_RETENTION_HOURS`），每个任务有 `expires_at`；没有到期时间的旧任务在第一次清理时补上“现在 + 保留期”，不会立即删除。
- **本机数据**（只在浏览器里）：设置与 Key、术语库、我的任务、翻译历史（文件指纹，用于重复上传提醒）、阅读位置。不要把这些改成存服务器。
- **本文术语**：`GET /api/jobs/{id}/terms` 只给出处与段落；“可能不一致”在前端 `features/job-terms/terms.ts` 按浏览器里最新的术语库和当前译文计算，不要在后端再算一遍。批量重翻 `POST /api/jobs/{id}/retranslate` 一次最多 20 段（要在网关约 100 秒超时前返回）。
- **anthropic SDK 2.x** 默认用 `Authorization: Bearer` 传 Key，兼容接口另加 `x-api-key` 头；新版接口不接受 `temperature`。

## 9. 文档索引

- `docs/00-research.md`：调研（沉浸式翻译 / BabelDOC 原理、开源与商业方案、解析服务、术语库）
- `docs/01-tech-stack.md`：技术选型
- `docs/02-spikes.md`：验证实验记录（解析、对齐、导出、成本、翻译质量、提示词）
- `docs/03-mvp-design.md`：第一阶段设计与开发记录（含 PaddleOCR、扫描件的实测结论）
- `docs/04-ux-round-1.md`：体验改进第一轮（手机与阅读视图、解析自动重试、只翻部分页、收尾提速、完成提醒、取消任务）
- `docs/05-ux-round-2.md`：体验改进第二轮（设置两步开始、操作提示、分段加载与续传、目录、键盘、公式编辑校验、24 小时保留、重复上传提醒）
- `docs/06-ux-round-3.md`：体验改进第三轮（使用指南页、章节跳转高亮、移除阅读视图、术语库页面与本文术语、名称与 logo）
- `README.md`：面向用户的功能、部署与常见问题

## 10. 当前状态与待办（2026-10-29）

**已上线（v0.2.0 之后）**：使用指南页（`/guide`）、目录跳转高亮、术语库页面（`/glossary`）、阅读器“术语”面板与批量重翻；“阅读”视图已按用户要求移除（不要加回）。

**v0.2.0**：保版式翻译与网页阅读（对照 / 译文 / 原文）、逐段修改与按步撤回、公式编辑校验与预览、用户术语库（导入、AI 识别、确认）、Anthropic 协议与模型列表、批量上传与分阶段排队、取消任务、只翻部分页、扫描件翻译、导出 PDF（中文版 / 双语对照）保留链接并可续传、阅读器分段加载、目录侧栏与页码跳转、键盘操作、解析自动重试、完成提醒、手机适配、24 小时保留与提醒、重复上传提醒（本机）。

**等维护者**：网站名称与 logo（“PDF 保版式翻译”要换，按“品牌名 + 在线可编辑的 PDF 翻译”组织，等定名）；固定域名隧道（需要 Cloudflare 上的域名，并运行 `cloudflared tunnel login` 授权）。

**候选待办**（来自整体分析，尚未开工，开工前先写设计文档）：
- 示例文档：不填 Key 先看效果（需维护者挑一篇允许转载的公开论文）
- 边翻边读：解析完就能打开阅读器，已翻译的段落随时出现
- 原文文字层：原文页可选中、复制、Ctrl+F 搜索
- 选中文字“解释 / 问 AI”（带本段与论文背景）
- 大图页面降低渲染分辨率（个别页对照模式约占 230 MB 内存）
- Zotero 插件（右键 PDF → 翻译 → 译文挂回条目）
- 更多导出格式：交替页双语、Markdown、Word
- 公网访问控制：访问密码或按 IP 限流
- 账号与多设备同步、其他语言方向（改动大，需维护者先定方向）

**设计取舍（不要改回）**：图表里的文字不翻、只翻图表标题；不加表格翻译开关；不做“翻译前先总结全文”（实测对质量没有可测量的提升）。

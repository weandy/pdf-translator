# pdf-translator

[![CI](https://github.com/weandy/pdf-translator/actions/workflows/ci.yml/badge.svg)](https://github.com/weandy/pdf-translator/actions/workflows/ci.yml)

开源、可自部署的网页端 PDF 保版式翻译工具。上传论文 PDF 后，在浏览器中按原版式对照阅读中文译文，公式、图表、表格和引用位置尽量保持不变。

> 当前版本：v0.2.0

## 一键部署（Docker）

适用于已安装 Docker Engine 和 Docker Compose v2 的 Linux 云服务器：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/weandy/pdf-translator/main/deploy/install.sh)
```

脚本会把项目安装到 `/opt/pdf-translator`，创建 `.env`，并使用 Docker Compose 构建和启动服务。默认将宿主机的 `127.0.0.1:18765` 映射到前端容器的 80 端口，公网通过宿主机已有的 Nginx、Caddy 或其他网关反代到这个地址。已有安装会先检查工作区，再同步代码；已有 `.env` 和 Docker 数据卷会保留。

自定义安装目录或端口：

```bash
PDFTR_INSTALL_DIR=/srv/pdf-translator \
PDFTR_PORT=18765 \
bash <(curl -fsSL https://raw.githubusercontent.com/weandy/pdf-translator/main/deploy/install.sh)
```

首次安装时脚本只交互式询问 GitHub 下载加速和可选的 MinerU 网络代理。MinerU 代理地址直接回车表示保持当前值，输入 `direct` 表示直连；代理为空时不会经过代理。部署者可以在服务器 `.env` 配置共享的 MinerU Token，配置后普通用户只需在网页填写翻译服务 API Key；用户也可以在网页设置中填写自己的 MinerU Token，个人 Token 会优先使用。已有安装默认保留原配置，可设置 `PDFTR_CONFIGURE_ENV=yes` 再次修改 MinerU 代理。

如果没有 Docker，脚本会先询问是否在 Debian/Ubuntu 系统上运行 Docker 官方安装脚本；不支持的发行版会提示手动安装。脚本不会修改系统防火墙。首次启动后，在网页右上角“设置”中仍可以为当前浏览器填写个人 API Key。脚本源码见 [deploy/install.sh](deploy/install.sh)。

国内服务器可以在脚本提示时选择 GitHub 加速，并在下一步输入自己的加速地址；直接回车使用 `https://ghfast.top/`，输入 `no` 则直连 GitHub。也可以通过环境变量预先指定：

```bash
PDFTR_USE_GITHUB_PROXY=yes \
PDFTR_GITHUB_PROXY=https://ghfast.top/ \
bash <(curl -fsSL https://ghfast.top/https://raw.githubusercontent.com/weandy/pdf-translator/main/deploy/install.sh)
```

加速地址是第三方服务，脚本只用它下载公开仓库代码；生产环境建议固定提交版本并检查代码来源。

GitHub 加速只作用于仓库和脚本下载，Docker 基础镜像和 Python/Node 依赖仍由 Docker、uv 和 npm 按各自地址下载；这些下载需要单独配置 Docker registry 或系统网络代理。

## 适合什么场景

- 英文论文、技术报告和其他以 PDF 为主的文档
- 希望在网页中逐段阅读、搜索、复制和修改译文
- 希望使用自己的翻译服务 API Key
- 希望在个人电脑、NAS 或云服务器上自部署

当前版本定位为个人或受控团队使用。任务、SQLite 数据库、SSE 进度事件和任务队列都在单个后端实例中，适合单实例部署；开放给陌生用户前，应先增加登录、权限和限流。

## 功能

### 翻译与版面

- MinerU 云端解析正文、公式、表格和版面结构
- 译文按照原文坐标排回页面，图片、矢量图形、独立公式、算法框和表格保持原样
- 跨栏、跨页段落作为完整段落翻译后再拆回原位置
- 行内公式用 LaTeX 交给模型理解，浏览器用 KaTeX 重排；公式占位符严格校验，公式渲染失败时使用原图裁剪
- 原文正文中的局部粗体会随翻译保留，网页和导出 PDF 使用对应的中文粗体字形；整块粗体正文的译文仍使用普通正文字体，标题和图表编号按结构使用黑体；扫描页因没有可靠的字符字重信息，只能保留块级标题样式
- 失败段落自动单独补救，每段最多尝试 10 次（含首次），单次模型请求最多 30 秒，首轮后的补救阶段共用 120 秒预算
- 次数或时间耗尽后明确提示未完成，任务不会带着未翻译的正文标记为完成；重试仅继续未完成段落
- 手动重翻的整次操作最多 45 秒，失败原因和尝试次数可见；已有可用译文不会因重翻失败退回原文
- 自动抽取论文术语，并结合公共术语库和“我的术语库”保持译法一致
- 支持 OpenAI 兼容接口和 Anthropic 接口，默认配置为 DeepSeek

### 阅读器

- 对照、译文、原文三种显示方式，手机默认显示译文
- PDF 按需加载，并在切换原文/译文后释放不再使用的 PDF 资源
- 文字可选择、复制，支持全文搜索
- 目录、章节标题和文章内部引用可以跳转
- 跳转目标会短暂高亮并自动淡出，搜索结果也会定位到对应段落
- 支持页码跳转、阅读位置记忆和最近任务缓存
- 公式、引用、图表和网页链接可以点击

### 修改与任务管理

- 点击段落后可查看原文、编辑译文、重翻或精修
- 编辑时检查公式占位符，防止误删或重复公式
- 修改、重翻和精修都会留下记录，并支持撤回
- 重翻后即时更新费用、状态和译文
- 支持一次选择多个 PDF，并按批次查看上传和翻译进度
- 失败任务可以从中断阶段重试
- 错误信息统一显示，并给出重试或设置提示

### 下载与缓存

- 翻译完成后可直接阅读
- 支持下载中文版 PDF 和双语对照 PDF
- 下载文件自动以识别到的论文原文标题命名，空格和文件名中的特殊字符转为 `_`，分别加上 `_译文.pdf` 或 `_双语对照.pdf`；未识别到可靠标题时使用上传文件名。
- 导出文件包含网页中的手动修改
- 下载支持 HTTP Range，中断后可以继续
- 服务端默认保留任务 24 小时，并自动清理过期文件和译文缓存

### 访问与使用统计

- 从顶部“统计”入口进入 `/stats`，首页不展示统计卡片
- 统计页仅展示最近 7 天汇总、今日数据、每日趋势与明细，按北京时间划分日期，并显示数据更新时间
- 指标包括访问次数、访问浏览器数、提交任务数、完成任务数和上传页数；最近 7 天浏览器数跨日去重，不累加日值
- 每天的日期和数值直接显示，手机可点按查看完整数值；可见页面每分钟刷新，刷新失败保留旧值
- 访问只在打开页面或站内导航时记录；健康检查、进度轮询和 SSE 心跳不计入访问
- 同一任务重复保存、失败重试不会重复计入提交数；首次完成时计入完成数
- 上传页数按完整 PDF 页数计算；仅翻译其中部分页面时，也计入整份上传文件的页数
- 统计从功能启用时开始；历史统计长期保留，页面展示最近 7 天不会清除更早的数据，任务到期或手动删除也不影响已记录的统计

访问浏览器按浏览器中的随机匿名标识去重，不代表真实人数或浏览器种类。换设备、无痕浏览、清除浏览器数据可能影响去重。公开接口固定七个北京时间自然日，不返回历史累计、论文名、提交 IP 或任务/访客标识。每日汇总、任务去重标记和累计访客哈希长期保留；访问事件及每日访客明细保留约 7 天，在后续访问时清理。管理页面不计入公开访问。

### 管理员提交明细（可选）

`/weandy/login` 为独立管理入口，登录后可按标题关键词、完整 IP、提交日期筛选记录并分页查看。入口路径可通过 `PDFTR_ADMIN_PATH` 修改，只有管理 API 验证过的有效会话可查询；普通用户无需登录即可使用翻译。管理功能默认关闭。

在 `backend/` 执行 `uv run python scripts/hash_admin_password.py`，交互输入至少 16 字符的管理密码，将输出的哈希用**单引号**包裹后写入部署 `.env` 的 `PDFTR_ADMIN_PASSWORD_HASH`；不要将明文密码写入命令行或仓库。设置 `PDFTR_ADMIN_ORIGIN=https://你的域名`（无尾斜杠或路径）后可启用管理登录。Cookie 仅在 HTTPS 下工作，会话默认有效 30 天，可通过 `PDFTR_ADMIN_SESSION_DAYS` 设置为 1～30 天；不会自动滑动续期，退出或更换密码哈希可使会话失效。

采集需额外设置 `PDFTR_SUBMISSION_AUDIT_ENABLED=true`，并按[可信代理与发布方案](docs/07-statistics-and-submission-audit-design.md)核验 OpenResty → Caddy → FastAPI 的地址链，配置 `PDFTR_WEB_TRUSTED_PROXIES` 与 `PDFTR_TRUSTED_PROXY_CIDRS`。不能照抄其他机器的 Docker 地址，也不要信任所有私网或任意 X-Forwarded-For。缺少管理、HTTPS Origin 或可信代理配置时，开启采集将拒绝启动。上传处会明确显示长期留存说明。

启用后长期记录论文标题（识别失败则回退去路径、去扩展名的上传名）、提交时间、提交 IP、页数与状态；正文/PDF仍按保留期清理。删除任务不会删除该元数据。现有任务可由部署者显式补录标题，旧提交 IP 和首次完成时间无法可靠补回。关闭采集停止新增及更新，不自动删除既有账本。

## 工作方式与隐私

默认流程如下：

```text
浏览器上传 PDF
        ↓
后端保存任务并上传 PDF 到 MinerU 云端解析
        ↓
PyMuPDF 对齐文字与坐标，翻译正文并生成背景页
        ↓
浏览器按原版式显示译文
        ↓
用户阅读、编辑、重翻或导出 PDF
```

- PDF 会发送到 MinerU 进行版面解析。
- 正文和术语抽取内容会发送到用户选择的翻译服务。
- 浏览器中的 API Key 和“我的术语库”默认只保存在浏览器本地。
- 服务端会在配置的保留期内保存原文、解析结果、译文和导出文件。
- 公开统计保存聚合计数与匿名去重信息。若部署者开启管理员提交明细，会按上传处的说明长期保存标题、提交 IP 和少量任务元数据，仅供管理员查看；不在长期账本中保存论文内容或 API Key。宿主机反向代理日志由部署者另行管理。
- 请勿上传涉密、未公开或受第三方协议限制的文档。

当前 Docker Compose 部署使用云端解析，不需要 GPU，也不会在服务器上运行本地大模型。后端仍保留 PaddleOCR-VL 适配器，但前端不开放该选项；默认解析器是 MinerU。

任务、译文段落、修改记录、翻译缓存、统计和可选的提交账本保存在 SQLite `/data/app.db`，PDF等文件在 `/data/jobs/`。Docker 使用命名数据卷持久化 `/data`，无需额外部署 MySQL、PostgreSQL 或 Redis。到期清理失败会排队重试并巡检孤儿目录；运行中的任务受保护。手动删除任务后的共享译文缓存仍按缓存保留期清理，不承诺立即删除所有共享命中。

短期恢复备份应使用 SQLite在线备份并配套任务文件，避免遗漏 WAL；完整备份含正文，必须单独设定到期时间，不随任务 TTL 自动消失。SQLite启用 `secure_delete`、清理后执行被动 WAL checkpoint，这不能替代旧备份治理，也不是底层磁盘擦除保证。

长期只保留元数据时，可在 `backend/` 执行 `uv run python scripts/backup_metadata.py /data/app.db /data/backups/metadata-日期.db`（路径按部署调整）。脚本从一致只读快照把允许的统计/提交表写入全新数据库，拒绝覆盖文件，排除正文、译文、修订、缓存、管理会话和原数据库空闲页。不要把完整库复制后仅 DELETE 正文表当作无正文备份；脚本不自动删除历史完整备份。

## 快速部署

需要 Linux、Docker 和 Docker Compose。

```bash
git clone https://github.com/weandy/pdf-translator.git
cd pdf-translator
cp .env.example .env
docker compose -f deploy/docker-compose.yml up -d --build
```

配置宿主机反向代理，将域名转发到：

```text
http://127.0.0.1:18765
```

本机测试可以直接访问 `http://127.0.0.1:18765`。端口可以在 `.env` 中修改：

```env
PDFTR_PORT=18765
```

项目的 `web` 容器内部自带 Caddy，用来提供静态前端、处理单页路由，并把 `/api` 和 SSE 请求转发给 `app:8000`。宿主机不需要另外安装 Caddy；已有 Nginx、Caddy 或其他网关只需要反代 `127.0.0.1:18765`。

脚本首次运行会从 [.env.example](.env.example) 创建 `.env`。默认关键配置如下，Key 和代理都为空：

```env
PDFTR_PORT=18765
PDFTR_LLM_BASE_URL=https://api.deepseek.com
PDFTR_LLM_MODEL=deepseek-flash
PDFTR_LLM_API_KEY=
PDFTR_LLM_PROTOCOL=openai
PDFTR_MINERU_API_TOKEN=
PDFTR_MINERU_PROXY=
PDFTR_MAX_UPLOAD_MB=200
PDFTR_MAX_PAGES=200
PDFTR_RETENTION_HOURS=24
```

用户自己的翻译 Key、模型和 MinerU Token 可以在网页“设置”中保存；`.env` 中的同名配置只作为部署者共享的默认值。部署者配置 `PDFTR_MINERU_API_TOKEN` 后，普通用户只需提供一个翻译服务 API Key；留空个人 MinerU Token 会使用站点默认值，填写后则覆盖站点默认值。

然后在网页右上角“设置”中填写：

- 翻译服务 API Key、接口地址和模型名
- 自定义服务可点击“获取模型列表”后选择模型，无需先保存；接口不支持查询时仍可手动填写。模型列表不代表使用权限，选择后可测试连接。
- MinerU Token（仅当站点未提供共享 Token，或需要使用个人额度时）

也可以在 `.env` 中配置部署者共享的 Key。共享 Key 会由服务器承担调用费用，应配合访问认证和使用限额。

### 让用户只填写一个 API Key

在 [MinerU API 管理](https://mineru.net/apiManage/token) 创建解析 API Token，将它填入服务器 `.env` 的 `PDFTR_MINERU_API_TOKEN`，然后重新创建 `app` 容器使配置生效：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d app
```

请先等待进行中的任务结束。共享 Token 只存服务器，网页只获知解析服务是否已配置。用户在网页留空 MinerU Token 时使用共享值；填写个人 Token 时优先使用个人值，清空并保存即可恢复站点默认。用户填写的个人凭据仍按浏览器设置保存。

MinerU 只负责版面解析，翻译服务 API Key 的用量和计费独立。按 [官方 API 文档](https://mineru.net/apiManage/docs)，每个账号每天享有 1000 页最高优先级解析额度，超出部分优先级降低；另有任务数量限制。官网 API 管理页当前说明解析 Token 有效期 90 天，到期需创建新 Token 并更新服务器配置。网页登录 Cookie/JWT 不能代替此配置。

当前未集成剩余额度显示：已实测官网账号页面使用的 `GET /api/v4/extract/status` 可以用解析 API Token 鉴权，返回 `daily.used`、`daily.left`、`daily.allow_file_num` 和 `daily.used_file_num`，无需保存官网登录 Cookie。其中页数剩余表示优先解析额度，不是翻译余额。公开解析 API 文档未列出此查询接口的稳定合同，后续若接入显示，应兼容查询失败或字段变化，并避免据此拦截上传。

### 公网部署建议

1. 使用域名和 HTTPS。
2. 在宿主机前置网关配置登录认证；也可以打开项目内部 [frontend/Caddyfile](frontend/Caddyfile) 的 `basic_auth`。
3. 给 Docker 的 `pdftr-data` 卷做定期备份。
4. 保持单个 `app` 实例，不要直接扩展多个副本。
5. 监控 `/data` 磁盘空间、内存和外部 API 错误率。

### 服务器访问 MinerU 很慢时

只给 MinerU 配置代理：

```env
PDFTR_MINERU_PROXY=socks5h://用户名:密码@代理主机:端口
```

也支持：

```env
PDFTR_MINERU_PROXY=http://代理主机:端口
```

PaddleOCR 的代理配置为 `PDFTR_PADDLE_PROXY`。这些配置只影响解析服务，翻译服务仍访问 `PDFTR_LLM_BASE_URL`。

如果 Docker 桥接网络无法访问外网，可以尝试主机网络模式：

```bash
docker compose \
  -f deploy/docker-compose.yml \
  -f deploy/docker-compose.hostnet.yml \
  up -d --build
```

## 主要配置

所有配置都在项目根目录的 `.env` 中，完整示例见 [.env.example](.env.example)。

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `PDFTR_PORT` | `18765` | 宿主机本地反代端口 |
| `PDFTR_LLM_BASE_URL` | `https://api.deepseek.com` | 共享翻译服务地址 |
| `PDFTR_LLM_MODEL` | `deepseek-flash` | 共享翻译模型 |
| `PDFTR_LLM_PROTOCOL` | `openai` | `openai` 或 `anthropic` |
| `PDFTR_LLM_API_KEY` | 空 | 共享翻译服务 Key，可留空改用网页设置 |
| `PDFTR_MINERU_API_TOKEN` | 空 | 共享 MinerU Token，可留空改用网页设置 |
| `PDFTR_MINERU_PROXY` | 空 | MinerU 上传、轮询和下载代理 |
| `PDFTR_PADDLE_TOKEN` | 空 | 后端保留的 PaddleOCR Token，前端暂不开放 |
| `PDFTR_PADDLE_PROXY` | 空 | PaddleOCR 请求代理 |
| `PDFTR_MAX_UPLOAD_MB` | `200` | 单个 PDF 的大小上限 |
| `PDFTR_MAX_PAGES` | `200` | 单个 PDF 的页数上限 |
| `PDFTR_MAX_CONCURRENT_JOBS` | `4` | 同时上传、解析的任务数 |
| `PDFTR_MAX_CONCURRENT_TRANSLATIONS` | `1` | 同时翻译的任务数 |
| `PDFTR_MAX_QUEUED_JOBS` | `10` | 最大排队任务数 |
| `PDFTR_RETENTION_HOURS` | `24` | 任务和译文缓存保留时间，`0` 表示不自动删除 |
| `PDFTR_MIN_FREE_DISK_MB` | `1024` | 剩余空间低于此值时拒绝新上传 |
| `PDFTR_LLM_CONCURRENCY` | `0` | 每个任务请求翻译服务的并发数，`0` 为自动选择 |

小型云服务器可以先使用：

```env
PDFTR_MAX_UPLOAD_MB=100
PDFTR_MAX_CONCURRENT_JOBS=2
PDFTR_MAX_CONCURRENT_TRANSLATIONS=1
PDFTR_LLM_CONCURRENCY=2
PDFTR_MAX_QUEUED_JOBS=5
PDFTR_RETENTION_HOURS=24
```

## 资源与费用估算

- 2 vCPU、2–4 GB 内存、40 GB SSD：适合个人或内网使用。
- 4 vCPU、8 GB 内存、80–160 GB SSD：适合小团队使用。
- 后端空闲内存通常约 150–250 MB；导出 PDF 时会额外占用内存。
- 上传接口会读取完整 PDF，多个大文件同时上传会明显增加内存压力。
- 一个任务通常保存原 PDF、背景 PDF、解析结果和可选导出文件，磁盘占用约为原 PDF 的 2–4 倍。
- DeepSeek Flash 翻译一篇约 15 页论文，项目实测约 ¥0.03–0.07；精翻会更贵，最终以服务商账单为准。

## 已知限制

- 只支持翻译成简体中文，英文论文效果最好。
- 扫描件依赖 MinerU OCR，纸张底纹、倾斜和复杂背景可能留下边缘痕迹。
- 图表内文字默认保留原文，只翻译图表标题。
- 当前任务队列、SSE 事件和任务密钥保存在进程内存中，服务重启会中断进行中的任务。
- 当前使用 SQLite 和本地文件卷，不适合直接做多副本、多租户 SaaS。
- 当前没有完整的用户账号、项目归属、接口限流和计费隔离。
- 机器之心术语库 `aitd` 使用 CC BY-NC-SA 4.0，商业部署时应从 `PDFTR_GLOSSARY_SOURCES` 中移除。

## 本地开发

后端需要 Python 3.12 和 uv，前端需要 Node 22。

```bash
# 后端
cd backend
uv sync
uv run uvicorn pdftr.api.app:create_app --factory --reload
uv run pytest
uv run ruff check .
uv run pyright
uv run lint-imports

# 前端
cd frontend
npm install
npm run dev
npm run lint
npm run typecheck
npm test
npm run build
```

前端开发服务器会把 `/api` 请求转发到 `127.0.0.1:8000`。

接口变更后，重新生成 OpenAPI 类型：

```bash
cd backend
uv run python scripts/export_openapi.py ../frontend/openapi.json
cd ../frontend
npm run gen:api
```

## 项目结构

```text
backend/src/pdftr/
├── api/          FastAPI 路由和数据结构
├── parsing/      MinerU、PaddleOCR 云端解析适配器
├── extraction/   PDF 文字、目录和页面分类
├── fusion/       解析结果与 PDF 坐标融合
├── translation/  LLM 接口、批处理、缓存和费用
├── rendering/    背景页、网页叠加层和 PDF 导出
├── pipeline/     任务流水线、进度事件和密钥生命周期
└── storage/      SQLite 与任务文件

frontend/src/
├── pages/        首页、任务页、统计、使用指南和术语库
├── features/     上传、阅读器、编辑、设置、下载
├── entities/     任务、文档、批次和术语数据模型
└── shared/       API、PDF.js、文本处理和通用组件
```

## 文档

- [docs/00-research.md](docs/00-research.md)：调研报告
- [docs/01-tech-stack.md](docs/01-tech-stack.md)：技术选型与架构
- [docs/02-spikes.md](docs/02-spikes.md)：版面、导出、费用和质量验证
- [docs/03-mvp-design.md](docs/03-mvp-design.md)：MVP 设计与验收记录
- [docs/04-ux-round-1.md](docs/04-ux-round-1.md)：第一轮体验设计
- [docs/05-ux-round-2.md](docs/05-ux-round-2.md)：第二轮体验设计
- [docs/06-ux-round-3.md](docs/06-ux-round-3.md)：第三轮体验设计

## 术语库来源

首次使用时按 `PDFTR_GLOSSARY_SOURCES` 下载并合并术语库。默认来源包括：机器之心术语库、D2L、机器学习术语、AI 安全术语、GIST 和计算机专业术语。

商业部署请移除 `aitd`：

```env
PDFTR_GLOSSARY_SOURCES=d2l,ml-technical-term,ais-vocab,gist,computerese
```

## 许可证

[AGPL-3.0](LICENSE)

如果把本项目修改后部署成对外提供服务，需要按 AGPL-3.0 履行源码公开义务。

## 致谢

感谢 [MinerU](https://github.com/opendatalab/MinerU)、[PyMuPDF](https://github.com/pymupdf/PyMuPDF)、[pdf.js](https://github.com/mozilla/pdf.js)、[KaTeX](https://katex.org) 以及 BabelDOC、PDFMathTranslate、KISS Translator、STranslate 等项目。

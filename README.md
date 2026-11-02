# pdf-translator

[![CI](https://github.com/weandy/pdf-translator/actions/workflows/ci.yml/badge.svg)](https://github.com/weandy/pdf-translator/actions/workflows/ci.yml)

开源、可自部署的网页端 PDF 保版式翻译工具。上传论文 PDF 后，在浏览器中按原版式对照阅读中文译文，公式、图表、表格和引用位置尽量保持不变。

> 当前版本：v0.2.0

## 一键部署（Docker）

适用于已安装 Docker Engine 和 Docker Compose v2 的 Linux 云服务器：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/weandy/pdf-translator/main/deploy/install.sh)
```

脚本会把项目安装到 `/opt/pdf-translator`，创建 `.env`，并使用 Docker Compose 构建和启动服务。已有安装会先检查工作区，再同步代码；已有 `.env` 和 Docker 数据卷会保留。

自定义安装目录或端口：

```bash
PDFTR_INSTALL_DIR=/srv/pdf-translator \
PDFTR_PORT=8080 \
bash <(curl -fsSL https://raw.githubusercontent.com/weandy/pdf-translator/main/deploy/install.sh)
```

脚本不会自动安装 Docker，也不会修改系统防火墙。首次启动后，在网页右上角“设置”中填写翻译服务 API Key 和 MinerU Token。脚本源码见 [deploy/install.sh](deploy/install.sh)。

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
- 行内公式用 LaTeX 交给模型理解，浏览器用 KaTeX 重排；校验失败时保留原文或公式裁剪图
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
- 导出文件包含网页中的手动修改
- 下载支持 HTTP Range，中断后可以继续
- 服务端默认保留任务 24 小时，并自动清理过期文件和译文缓存

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
- 请勿上传涉密、未公开或受第三方协议限制的文档。

当前 Docker Compose 部署使用云端解析，不需要 GPU，也不会在服务器上运行本地大模型。后端仍保留 PaddleOCR-VL 适配器，但前端不开放该选项；默认解析器是 MinerU。

## 快速部署

需要 Linux、Docker 和 Docker Compose。

```bash
git clone https://github.com/weandy/pdf-translator.git
cd pdf-translator
cp .env.example .env
docker compose -f deploy/docker-compose.yml up -d --build
```

浏览器访问：

```text
http://服务器地址:8080
```

端口可以在 `.env` 中修改：

```env
PDFTR_PORT=8080
```

然后在网页右上角“设置”中填写：

- 翻译服务 API Key、接口地址和模型名
- MinerU Token

也可以在 `.env` 中配置部署者共享的 Key。共享 Key 会由服务器承担调用费用，应配合访问认证和使用限额。

### 公网部署建议

1. 使用域名和 HTTPS。
2. 打开 [frontend/Caddyfile](frontend/Caddyfile) 中的 `basic_auth`，或在前置网关配置登录认证。
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
├── pages/        首页、任务页、使用指南和术语库
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

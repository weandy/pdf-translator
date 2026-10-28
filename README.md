# pdf-translator

开源、可自部署的网页端 PDF 保版式翻译工具：上传英文论文 PDF，在浏览器里与原文并排阅读按原版式排好的中文译文，公式、图表、表格保持原样，行内公式用 KaTeX 重新渲染。

> 当前版本：v0.1.0（MVP 第一阶段）

## 功能

- **保留原版式**：译文排回原位置，图片、矢量图形、独立公式、算法框、表格保持不动；跨栏、跨页的段落作为一段翻译后再拆回各自位置
- **公式不被翻坏**：行内公式以 LaTeX 交给模型理解、译文中只保留占位符，逐段校验后用 KaTeX 重新渲染（失败时回退为原文裁剪图）
- **论文术语一致**：自动抽取论文领域与术语，并匹配 [GIST](https://huggingface.co/datasets/Jerry999/multilingual-terminology) AI 术语库
- **可接外部 API**：翻译走任意 OpenAI 兼容接口（默认 DeepSeek，也支持 OpenAI、通义千问、GLM、Ollama 等），版面解析用 [MinerU](https://mineru.net) 云端（免费）
- **省钱**：默认不开模型思考、按约 1500 token 分批、只翻需要翻的内容、译文缓存；一篇 10–16 页论文约 ¥0.03–0.07（DeepSeek 空闲时段）
- **可修改**：点击任一段落查看原文、修改译文、重翻或“精修”（开启低强度思考）
- **可下载**：导出中文版 PDF（保留原版式）或双语对照版（每页左原文、右译文），包含你做的修改
- **小机器能跑**：本地不跑模型，后端翻译时内存约 250 MB，2 核 2 GB 即可
- **隐私**：Key 只保存在你的浏览器里，服务端不落盘；上传前明确提示文件会发送到 MinerU 云端

## 快速开始

需要 Docker（含 Compose）。

```bash
git clone https://github.com/weandy/pdf-translator.git
cd pdf-translator
cp .env.example .env        # 可选：修改端口、配置共享 Key
docker compose -f deploy/docker-compose.yml up -d --build
```

浏览器打开 `http://localhost:8080`（端口可在 `.env` 的 `PDFTR_PORT` 修改），点右上角 **设置** 填写：

| 项 | 获取方式 |
|---|---|
| 翻译服务 API Key | 默认使用 DeepSeek：[platform.deepseek.com](https://platform.deepseek.com)；其他 OpenAI 兼容服务需同时填写接口地址与模型名 |
| MinerU Token | 在 [mineru.net](https://mineru.net/apiManage) 免费申请（每天 1000 页优先额度） |

然后上传 PDF、勾选上传确认、开始翻译即可。部署者也可以在 `.env` 里配置共享的 Key，用户留空时使用。

### 部署在公网

编辑 `frontend/Caddyfile` 末尾的 `basic_auth` 段开启访问密码（用 `caddy hash-password` 生成哈希），并在前面加一层 HTTPS（反向代理，或把 Caddy 的站点地址改成你的域名）。

### 常见问题

- **任务失败，提示“无法连接 MinerU 云端”**：容器访问不了外网。若 `docker run` 时出现 `IPv4 forwarding is disabled`，可开启宿主机的 `net.ipv4.ip_forward=1`，或改用主机网络模式：
  ```bash
  docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.hostnet.yml up -d --build
  ```
- **一直停在“版面解析”**：MinerU 免费额度在高峰时需要排队，实测从 30 秒到 10 分钟以上不等；进度页会显示 MinerU 的实时状态（排队中、解析到第几页），可以关闭页面稍后在“我的任务”里查看。
- **个别段落显示为灰色原文**：该段译文没有通过公式占位符校验，点击段落后“重翻”或“精修”。

## 费用与质量

以下数据来自 [验证实验](docs/02-spikes.md) 与 [第一阶段验收](docs/03-mvp-design.md)（DeepSeek Flash，2026-10）：

| | 说明 |
|---|---|
| 费用 | 5 篇论文实测每篇 ¥0.033–0.073（空闲时段）；DeepSeek 工作日 9–12、14–18 点（北京时间）为高峰，价格翻倍 |
| 默认档 | 不开思考。在论文样本与 ACL 60/60 学术数据集上，开思考没有稳定收益 |
| 精翻 | 开启低强度思考，约 4 倍费用；在通用、文学类文本上更好 |
| 提示词 | 公式以 LaTeX 交给模型理解，是质量提升最大的一条（公式段评分 91.7 → 97.5） |

## 已知限制（第一阶段）

- 只处理有文字层的 PDF；扫描页、文字提取乱码的页面会提示并跳过
- 表格内文字、图片内文字暂不翻译；首页作者、单位、邮箱保留原文
- MinerU 偶尔把公式符号识别成普通字母，这类原文错误会带到译文里
- 只支持翻译为简体中文，以英文论文为主

## 本地开发

```bash
# 后端（Python 3.12 + uv）
cd backend
uv sync
uv run uvicorn pdftr.api.app:create_app --factory --reload   # http://127.0.0.1:8000
uv run pytest && uv run ruff check . && uv run pyright && uv run lint-imports

# 前端（Node 22）
cd frontend
npm install
npm run dev        # http://localhost:5173，/api 自动转发到 8000
npm run lint && npm run typecheck && npm test
```

- 接口变更后：`cd backend && uv run python scripts/export_openapi.py ../frontend/openapi.json`，再 `cd frontend && npm run gen:api` 重新生成前端类型（后端测试会检查两边是否一致）
- 不经过前端单独跑解析与翻译：`uv run pdftr-dev parse|fuse|translate --help`
- 回归样本：`PDFTR_MINERU_API_TOKEN=... uv run python scripts/fetch_samples.py`（下载 5 篇 arXiv 论文并解析，不入库）
- 模块依赖规则由 `lint-imports`（后端）和 ESLint（前端）检查，见 [docs/03-mvp-design.md](docs/03-mvp-design.md) §2.1、§4.1

## 文档

- [docs/00-research.md](docs/00-research.md)：调研报告（沉浸式翻译/BabelDOC 原理、开源方案、解析服务、术语库、STranslate 架构）
- [docs/01-tech-stack.md](docs/01-tech-stack.md)：技术选型
- [docs/02-spikes.md](docs/02-spikes.md)：验证实验记录（版面解析、对齐、导出、成本、翻译质量、提示词、公开数据集对照）
- [docs/03-mvp-design.md](docs/03-mvp-design.md)：MVP 第一阶段设计与开发记录（含验收结果）

## 致谢

- [MinerU](https://github.com/opendatalab/MinerU)：版面解析
- [PyMuPDF](https://github.com/pymupdf/PyMuPDF)（AGPL-3.0）：PDF 文字提取与背景页生成
- [GIST](https://huggingface.co/datasets/Jerry999/multilingual-terminology)（Apache-2.0）：AI 术语库
- [pdf.js](https://github.com/mozilla/pdf.js)、[KaTeX](https://katex.org)：阅读器渲染
- 参考了 [BabelDOC](https://github.com/funstory-ai/BabelDOC)、[PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate)、[KISS Translator](https://github.com/fishjar/kiss-translator)、[STranslate](https://github.com/STranslate/STranslate) 等项目的设计

相关资源：[机器之心 AI 术语库](https://github.com/jiqizhixin/Artificial-Intelligence-Terminology-Database)（约 2400 条人工智能英汉术语，CC BY-NC-SA 4.0，本项目未集成，可人工查阅）。

## 许可证

[AGPL-3.0](LICENSE)。如果你把本项目部署成对外的在线服务，需要按 AGPL 公开你修改后的源码。

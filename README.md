# pdf-translator

开源、可自部署的网页端 PDF 保版式翻译工具：上传 PDF，在浏览器里按原版式对照阅读中文译文，公式、图表按原位置重新渲染，可导出纯译文或双语 PDF。

> 当前阶段：MVP 第一阶段开发中（M1 工程骨架已完成）。

## 目标

- **保留原版式**：译文排回原位置，图片、矢量图形、独立公式保持不动
- **公式图表重新渲染**：行内公式用 KaTeX 随译文重排，表格、图内文字可翻译
- **可接外部 API**：翻译走任意 OpenAI 兼容接口（DeepSeek、通义千问、GLM、Ollama 等），解析可选 MinerU 云端 API、Doc2X；Key 由部署者自己提供
- **论文术语一致**：领域术语库 + 文档级术语自动抽取
- **小机器就能跑**：默认轻量模式把版面解析交给 MinerU 云端 API（免费）或 Doc2X（付费），本地不跑模型、不需要 GPU；上传前会提醒。也可选本地模式，文件不出本机（需要更大内存）
- **通用输入**：原生文本 PDF、扫描件、混合文档
- **翻译方向**：其他语言 → 简体中文（以英文为主）

## 快速开始

需要 Docker（含 Compose）。

```bash
git clone https://github.com/weandy/pdf-translator.git
cd pdf-translator
cp .env.example .env        # 可选：修改端口、配置共享 Key
docker compose -f deploy/docker-compose.yml up -d --build
```

浏览器打开 `http://localhost:8080`（端口可在 `.env` 的 `PDFTR_PORT` 修改）。翻译服务的 Key 可以在网页设置里填写，只保存在你的浏览器中。

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

模块依赖规则由 `lint-imports`（后端）和 ESLint（前端）检查，见 [docs/03-mvp-design.md](docs/03-mvp-design.md) §2.1、§4.1。

## 文档

- [docs/00-research.md](docs/00-research.md)：调研报告（沉浸式翻译/BabelDOC 原理、开源方案、不足、解析服务、术语库）
- [docs/01-tech-stack.md](docs/01-tech-stack.md)：技术选型
- [docs/02-spikes.md](docs/02-spikes.md)：验证实验记录
- [docs/03-mvp-design.md](docs/03-mvp-design.md)：MVP 第一阶段设计（模块划分、接口、流水线、前端结构、里程碑）

## 相关资源

- [机器之心 AI 术语库](https://github.com/jiqizhixin/Artificial-Intelligence-Terminology-Database)：约 2400 条人工智能英汉术语，可人工查阅（CC BY-NC-SA 4.0，本项目未集成）
- [GIST 多语言 AI 术语数据集](https://huggingface.co/datasets/Jerry999/multilingual-terminology)：本项目计划内置的 AI 领域术语来源（Apache-2.0）
- [BabelDOC](https://github.com/funstory-ai/BabelDOC) / [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate)：同类开源项目，本项目的对比基线
- [MinerU](https://github.com/opendatalab/MinerU)：文档解析

## 许可证

[AGPL-3.0](LICENSE)。如果你把本项目部署成对外的在线服务，需要按 AGPL 公开你修改后的源码。

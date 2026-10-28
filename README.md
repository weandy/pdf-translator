# pdf-translator

开源、可自部署的网页端 PDF 保版式翻译工具：上传 PDF，在浏览器里按原版式对照阅读中文译文，公式、图表按原位置重新渲染，可导出纯译文或双语 PDF。

> 当前阶段：调研、技术选型与验证实验，尚未开始编码。

## 目标

- **保留原版式**：译文排回原位置，图片、矢量图形、独立公式保持不动
- **公式图表重新渲染**：行内公式用 KaTeX 随译文重排，表格、图内文字可翻译
- **可接外部 API**：翻译走任意 OpenAI 兼容接口（DeepSeek、通义千问、GLM、Ollama 等），解析可选 MinerU 云端 API、Doc2X；Key 由部署者自己提供
- **论文术语一致**：领域术语库 + 文档级术语自动抽取
- **不需要 GPU**：本地只用 CPU，重型解析可交给可选的云端 API（使用前会提醒上传风险）
- **通用输入**：原生文本 PDF、扫描件、混合文档
- **翻译方向**：其他语言 → 简体中文（以英文为主）

## 文档

- [docs/00-research.md](docs/00-research.md)：调研报告（沉浸式翻译/BabelDOC 原理、开源方案、不足、解析服务、术语库）
- [docs/01-tech-stack.md](docs/01-tech-stack.md)：技术选型
- [docs/02-spikes.md](docs/02-spikes.md)：验证实验记录

## 相关资源

- [机器之心 AI 术语库](https://github.com/jiqizhixin/Artificial-Intelligence-Terminology-Database)：约 2400 条人工智能英汉术语，可人工查阅（CC BY-NC-SA 4.0，本项目未集成）
- [GIST 多语言 AI 术语数据集](https://huggingface.co/datasets/Jerry999/multilingual-terminology)：本项目计划内置的 AI 领域术语来源（Apache-2.0）
- [BabelDOC](https://github.com/funstory-ai/BabelDOC) / [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate)：同类开源项目，本项目的对比基线
- [MinerU](https://github.com/opendatalab/MinerU)：文档解析

## 许可证

[AGPL-3.0](LICENSE)。如果你把本项目部署成对外的在线服务，需要按 AGPL 公开你修改后的源码。

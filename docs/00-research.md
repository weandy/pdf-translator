# PDF 保留版式翻译 · 调研报告

> 调研日期：2026-10-27 ~ 2026-10-28
> 目的：搞清楚沉浸式翻译这类"在线 PDF 翻译"是怎么实现的，梳理现有开源/免费方案和它们的不足，为开源项目 pdf-translator（见 [01-tech-stack.md](01-tech-stack.md)）提供依据。
> 说明：GitHub 数据取自 2026-10-27 的 GitHub API；"不足"部分主要依据官方文档、论文自评和 GitHub issue，**未做本地实测**。

---

## 1. 沉浸式翻译的 PDF 翻译：三种模式

| 模式 | 实现思路 | 能力边界 |
|---|---|---|
| **免费版 PDF 翻译**（浏览器扩展内） | 浏览器里用 pdf.js 类阅读器渲染 PDF → 读取文本层（字符 + 坐标）→ 拼成段落 → 翻译 → 在原文旁/上叠一层译文 DOM | 官方文档称是"纯文本识别"，**公式、表格处理不了**；扫描件不支持（原文必须可复制）；段落切错、位置偏移、字号不对要用户手动拖滑块调 |
| **BabelDOC（保留排版翻译）** | 服务端解析 PDF → 中间表示 IR → 翻译 → 按原坐标**重绘一份新 PDF**，引擎已开源 | 原版式基本不变，公式保留，可输出双语/纯译文/隔页交替 PDF |
| **PDF Pro** | 官方称识别公式，并把多栏**重排为单栏**。内部流程未公开，推测是"PDF → 结构化内容（类似 MinerU/Doc2X）→ 翻译 → 重新排版"（**未证实**） | 牺牲原版式换可读性 |

另外：[immersive-translate 仓库](https://github.com/immersive-translate/immersive-translate)（19.5k⭐）无 license，主要用于发布和收集反馈，扩展本体不是开源的；真正开源的只有 BabelDOC 引擎。

### 1.1 BabelDOC 的流水线

来源：BabelDOC README、ACL 2026 Demo 论文（arXiv 2605.10845）。

核心难点：**PDF 里没有"段落"概念**，只有"在坐标 (x,y) 用某字体画某字形"的绘制指令。整个流程就是"先把结构还原出来，翻完再画回去"。

```
PDF ──解析 (pdfminer + PyMuPDF)───► 字符级数据：坐标、字体、字号、颜色
    ──版面检测 (DocLayout-YOLO)───► 正文 / 标题 / 公式 / 图表 / 页眉页脚
    ──构建中间表示 IR─────────────► 页 → 段落 → 字符/公式/图片；版式与文字分开存
    ──段落重组─────────────────► 拼接被分栏、分页切断的段落
    ──公式/引用占位─────────────► "as shown in {v1}"，不交给 LLM
    ──术语抽取 + 术语表──────────► 注入提示词，保证长文档术语一致
    ──LLM 翻译─────────────────► 占位符换回原公式
    ──自适应排版────────────────► 放不下就把缩放系数从 1.0 按 0.05/0.1 递减，直到放下或触底
    ──PDF 重绘─────────────────► 按原 CTM/裁剪路径重画，嵌入 CJK 字体子集
    ──输出─────────────────────► 纯译文 / 双语对照 / 隔页交替
```

关键细节：

- **IR**：按页组织，含字符、文本行、图形块、内联图像，每个元素带坐标、bbox 和样式（论文附录给出 `page_layout`、`pdf_font`、`pdf_character`、`paragraph` 的 JSON 示例）。
- **公式识别**：靠相邻字符字号差判断上下标，基线坐标计算偏移，矢量公式重建；CLI 有 `--formular-font-pattern` / `--formular-char-pattern`，即**主要靠字体名和字符特征规则**识别公式。
- **重绘**：用 XObject 栈和图形状态/裁剪路径栈成对压栈出栈，保证字体、颜色、坐标系互不干扰。
- **外部接入**：只支持 OpenAI 兼容 LLM；版面分析可通过 `--rpc-doclayout` 走外部 RPC 服务；官方声明所有 Python API 都是内部接口，推荐经 pdf2zh-next 集成。

PDFMathTranslate v1 是它的前身，思路相同但更粗糙：公式靠字体名/字符类别猜，没有 IR，也没有跨页段落合并。

### 1.2 BabelDOC 论文自评数据

基准 200 页（arXiv 论文 80 + 技术文档 60 + 专利 60）：

| 系统 | BIoU（版式保真） | 人工评分 LF / TP / VA / TC | 未翻译块数 UTB（越低越好） |
|---|---|---|---|
| DeepL（文档翻译） | 19.8% | 3.44 / 3.62 / 3.63 / 4.21 | **2.33** |
| PDFMathTranslate | 48.7% | 3.29 / 3.40 / 3.28 / 3.34 | 6.25 |
| BabelDOC | **50.0%** | **4.59 / 4.28 / 4.46 / 4.47** | 2.85 |

- 速度（秒/页）：BabelDOC 1.63，PDFMathTranslate 1.47，Doc2X 1.86，Google 0.38。
- 消融：去掉自适应排版，LF 4.50→3.00、VA 4.50→2.50；去掉术语/上下文控制，TC 5.00→3.00。
- 解读：BIoU 只比 PDFMathTranslate 高 1.3 个百分点；**漏翻比 DeepL 多**；样本小、未做显著性检验。

---

## 2. 开源 / 免费方案盘点

### 2.1 整篇翻译 + 保留版式

| 项目 | Stars | 许可证 | 说明 |
|---|---|---|---|
| [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate)（pdf2zh） | 37.9k | AGPL-3.0 | 最主流，EMNLP 2025 Demo；CLI/GUI/Docker/MCP；Google、DeepL、OpenAI、Ollama 等 20+ 翻译服务；默认 v1，`--babeldoc` 为实验后端；OCR 实验性 |
| [BabelDOC](https://github.com/funstory-ai/BabelDOC) | 9.8k | AGPL-3.0 | 沉浸式翻译团队开源引擎；仅 OpenAI 兼容接口；CLI 主要用于调试 |
| [PDFMathTranslate-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next) | 769 | AGPL-3.0 | 2.0 分支，调用 BabelDOC 的官方参考实现；不提供使用帮助 |
| [DocuTranslate](https://github.com/xunbu/docutranslate) | 1.3k | MPL-2.0 | PDF/Word/Excel/EPUB/字幕等多格式 |
| [LinguaHaru](https://github.com/YANG-Haruka/LinguaHaru) | 308 | GPL-3.0 | LLM 翻译 Office 文档和 PDF |
| [PolyglotPDF](https://github.com/DsCodeStudioxhpott/PolyglotPDF) | — | — | 自称保留版式 + OCR，规模小，未核实 |

### 2.2 "PDF → Markdown → 翻译"路线（不保留原版式）

| 项目 | Stars | 许可证 | 说明 |
|---|---|---|---|
| [MinerU](https://github.com/opendatalab/MinerU) | 82.6k | MinerU Open Source License（Apache 2.0 + 附加条件） | 已到 4.0；输出 Markdown/HTML/LaTeX/DOCX/JSON/Content List 等；本地 CLI/WebUI/server/SDK，也可走 mineru.net 远程 API |
| [marker](https://github.com/datalab-to/marker) | 40.7k | Apache-2.0 | PDF/图片/Office → Markdown/JSON，Surya OCR，可选 LLM 增强 |
| [MDTrans](https://pypi.org/project/mdtrans/) | — | — | MinerU 转 Markdown + LangChain/OpenAI 兼容接口翻译 |
| Doc2X（商业） | — | 闭源 | PDF → Markdown/LaTeX/DOCX，公式识别强，有 API（详见 §4） |

适合二次编辑或喂给知识库，代价是原版式丢失。

### 2.3 阅读时辅助翻译

- [zotero-pdf-translate](https://github.com/windingwind/zotero-pdf-translate)（12.2k，AGPL-3.0）：Zotero 内划词/划段翻译，不做整篇转换。

### 2.4 免费在线可用

- [pdf2zh.com](https://pdf2zh.com)、[HuggingFace Space](https://huggingface.co/spaces/reycn/PDFMathTranslate-Docker)、[ModelScope](https://www.modelscope.cn/studios/AI-ModelScope/PDFMathTranslate)（演示资源有限）
- [沉浸式翻译 BabelDOC 在线版](https://app.immersivetranslate.com/babel-doc/)：README 称免费每月 1000 页；第三方报道称免费用户用 GLM-4-Flash，DeepSeek 仅限 Pro（可能过时）。

---

## 3. 主要不足

### 3.1 保留版式路线的固有问题

| 问题 | 表现 / 证据 |
|---|---|
| **译文长度变化** | 框放不下就缩字号 → BabelDOC issue"翻译后的字变得异常小，几乎看不见"；中译英更严重 |
| **文字提取不出** | 字体缺 ToUnicode 映射（CID 字体）→ issue"The document contains too many CID paragraphs""对于有文本映射问题的 PDF，无法准确翻译" |
| **扫描件** | OCR 均为实验功能，只适合白底黑字；PDFMathTranslate"翻译扫描档存在重影"issue 至今未关（31 条评论） |
| **结构识别错误向下传递** | 公式和文字重叠、作者与参考文献合并为一段、流程图打乱、原文译文混杂、漏翻（均有对应 issue） |

### 3.2 功能未完成（BabelDOC README / issue）

- 表格翻译为实验功能；不支持线条、首字下沉；超大页面跳过。
- 跨页/跨栏段落、目录、书签跳转还在路线图上。
- 主要测试英译中，其他方向"have not been tested"；阿拉伯语等 RTL、印度语系为待办。
- 输出文件偏大；大文件卡死或丢页；图片内文字不翻译。

### 3.3 产品与部署

- **隐私**：在线版需上传文件到云端。
- **额度**：按页限额，免费用户模型档次低；浏览器免费版对公式表格基本无效。
- **部署门槛**：issue 中占比最高的是安装问题——HuggingFace 模型下载失败、Windows DLL 加载失败、依赖版本冲突、字体下载链接失效。
- **许可**：AGPL 对外提供网络服务需开源修改版（自用无影响）。
- **支持有限**：官方声明不提供使用帮助，API 视为内部接口。

---

## 4. 补充调研：解析服务与相关组件

### 4.1 MinerU 4.0

- **解析等级与硬件**：

  | 等级 / 引擎 | 模型体积 | 内存 | 设备 |
  |---|---|---|---|
  | `basic` + ONNX | ~0.8 GB | ≥2 GB | CPU 可用 |
  | `basic` + PyTorch | ~0.8 GB | 8 GB | 建议 GPU/MPS |
  | `standard`/`advanced` + ONNX + llama.cpp | ~2 GB | 8 GB | CPU 可用，建议 Vulkan |
  | `standard`/`advanced` + vLLM/LMDeploy/mlx | — | 16 GB | GPU ≥8 GB 显存 |

- **部署形态**：`mineru-kit parse`（CLI）、`mineru-kit webui`（Gradio）、`mineru server start`（服务）、Python SDK、V1 API、Router；`--remote` + `MINERU_API_KEY` 走 mineru.net 云端。
- **输出**：Markdown、HTML、LaTeX、DOCX、EPUB、PDF、Structured Content、Content List V1/V2；JSON 带 `schema`（`docvortex.model` / `docvortex.middle`），`page_idx` 从 0 开始；PDF 页尺寸在 `extensions.docvortex_layout` 中给出（`width_pt`/`height_pt`）。
- ⚠️ **坐标单位、块类型枚举、公式/表格字段名官方文档未写清**。旧版资料称 content_list 的 bbox 为 0–1000 归一化、pipeline 后端为像素坐标，4.0 的 schema 已变，**必须用真实样例确认**。
- **许可附加条件**：月活 >1 亿或月收入 >2000 万美元需商业授权；向第三方提供在线服务需显著署名。自用无额外限制。
- **mineru.net 云端额度**（2026-10-28 查官方 API 管理页）：
  - **精准解析 API**（需 Token）：**免费**，页面未列任何价格；每账号**每天 1000 页最高优先级**，超出仍可解析但排队靠后；单文件 ≤200 MB、≤200 页；单次批量 ≤200 个文件；可选 pipeline / vlm / MinerU-HTML 模型，可开公式、表格识别和 OCR；返回 Zip（Markdown + JSON）。
  - **Agent 轻量解析 API**（免登录）：单文件 ≤10 MB、≤20 页，**禁用公式和表格识别**，只返回 Markdown，对本项目基本无用。
  - 限流：提交任务 300 次/分钟，查询结果 1000 次/分钟，每天最多上传 10000 个文件。
  - 旧版 beta 文档写的是"每天 2000 页、单文件 600 页"，已过时。

### 4.2 Doc2X API（open.noedgeai.com）

- 流程：`POST /api/v2/parse/preupload` 拿上传地址（`model` 可选 `v2` / `v3-2026`）→ 上传 → 轮询 `/api/v2/parse/status?uid=` → 取结果；结果仅保留 24 小时。
- 输出：Markdown（逐页）、DOCX、LaTeX；**`v3-2026` 额外返回结构化版面块（类型、坐标、阅读顺序、父子关系）**。
- 价格（API 价格页）：**没有免费额度**，资源包 ¥10/500 页、¥20/1000 页、¥100/5000 页、¥200/1 万页（均 ¥0.02/页），量大递减到 ¥5000/50 万页（¥0.01/页）；也可充值按量扣费。资源包有效期、v2 与 v3-2026 是否同价未写明。
- 并发：默认同时处理 10 个 PDF；平均约 10 页/秒；不支持旋转 PDF、页边空白过多的文档。API 额度与网页版不共享。
- 翻译 API "即将上线"，目前不可用。

### 4.3 文档解析 VLM 排行（OmniDocBench v1.6，二手资料）

| 模型 | 参数 | 得分 | 许可 |
|---|---|---|---|
| PaddleOCR-VL-1.6 | 0.9B | 96.34 | Apache-2.0 |
| MinerU2.5-Pro | 1.2B | 95.75 | Apache 2.0 + 条件 |
| GLM-OCR | 0.9B | 95.22 | 权重 MIT / 代码 Apache-2.0 |
| dots.ocr | 3B | 90.77 | MIT |

这些模型可作为 OCR/版面识别的备选后端（主要用于扫描件）。

### 4.4 PyMuPDF 重绘相关接口（已核对官方文档）

- `Page.apply_redactions(images, graphics, text)`：可**只删文字、不动图片和矢量图形**（`images=PDF_REDACT_IMAGE_NONE`、`graphics=PDF_REDACT_LINE_ART_NONE`）。按字符 bbox 判定是否删除。
- `Page.insert_htmlbox(rect, html, css=, scale_low=, archive=)`：把 HTML 排进矩形，放不下自动缩放（`scale_low` 设缩放下限），返回 `(spare_height, scale)`；支持自定义字体（经 `Archive`）、表格、RTL。
- `Page.show_pdf_page(rect, src, pno, clip=)`：把另一 PDF 的页面（可裁剪）以矢量形式叠到当前页。
- 许可 AGPL-3.0（自用无影响）。

### 4.5 论文术语库（2026-10-28 补充）

**其他项目怎么做术语一致性**

| 项目 | 做法 |
|---|---|
| BabelDOC | 翻译前扫描全文，LLM **自动抽取文档术语**生成动态词表；用户可用 `--glossary-files` 导入 CSV（`source,target,tgt_lng`），`--save-auto-extracted-glossary` 导出自动抽取结果。论文消融：去掉术语/上下文控制，术语一致性 TC 从 5.00 降到 3.00 |
| PDFMathTranslate-next | 沿用 BabelDOC 的术语表 CSV |
| 沉浸式翻译 | "AI 专家"按领域定制提示词（学术论文专家等）；官方术语库功能细节未查到公开说明 |
| GIST（ACL 2025 Findings） | 从 879 篇顶会获奖论文抽取 AI 术语 + 人工翻译；把术语通过提示词注入 LLM，BLEU/COMET 稳定提升，无需微调 |
| Kim et al.（arXiv 2410.15690） | 用 **trie 树**在原文中匹配术语，只把命中的术语连同原文一起交给 LLM |
| Retrieval vs Generation（arXiv 2503.05010） | 对比"检索已有术语库"和"让 LLM 先生成术语再翻"，给出双语术语抽取提示词 |

**可直接使用的英→中术语数据**

| 数据 | 规模 | 许可 | 说明 |
|---|---|---|---|
| [GIST 中文](https://huggingface.co/datasets/Jerry999/multilingual-terminology)（[代码](https://github.com/jiarui-liu/MultilingualAITerminology)） | 6426 条 | Apache-2.0 | AI 领域，CSV：`Index,English,Chinese`（已下载核对，如 `1D convolution → 一维卷积`）；中文部分参考了机器之心术语库 |
| [机器之心 AI 术语库](https://github.com/jiqizhixin/Artificial-Intelligence-Terminology-Database) | ~2442 条 | **CC BY-NC-SA 4.0** | AI 基础术语，含缩写和出处；2022 年后未更新；非商用、相同方式共享，**本项目不集成**，只在 README 中给链接 |
| [ECDICT](https://github.com/skywind3000/ECDICT) | 数百万词条 | MIT | 通用英汉词典，含大量专业词，适合作冷门词兜底，不是领域术语库 |
| Wikidata | 覆盖全学科 | CC0 | 用英文标签查中文标签（`zh-hans`/`zh-cn`），适合 AI 以外的领域按需在线查询 |
| [术语在线](https://www.termonline.cn)（全国科学技术名词审定委员会） | 全学科规范名词 | — | 最权威，但**未找到开放 API**，只能人工参考，不宜爬取 |
| ACL 60-60 | 1000 个 NLP 高频术语 × 多语言 | — | 未找到可下载的数据仓库 |

---

### 4.6 STranslate 的架构（2026-10-28 补充）

[STranslate](https://github.com/STranslate/STranslate)（8.4k⭐，MIT，C# / WPF）是 Windows 上“即用即走”的翻译 + OCR 小工具：输入、划词、截图、剪贴板监听都能触发翻译。仓库 `src/docs/` 有完整的架构文档，以下据此和源码整理。

**整体：宿主 + 插件 SDK**

```
STranslate（WPF 宿主）                         STranslate.Plugin（SDK，NuGet 包）
 ├ PluginManager  扫描插件目录、读 plugin.json、反射加载程序集      ITranslatePlugin  翻译（含回译）
 ├ ServiceManager 插件 → “服务实例”（每个实例一份独立配置）          IDictionaryPlugin 词典
 ├ MainWindowViewModel 翻译编排                                   IOcrPlugin        OCR（可声明是否返回坐标框）
 ├ ImageTranslateWindowViewModel 截图翻译编排                     ITtsPlugin        朗读
 └ SqlService     SQLite 历史 / 缓存                              IVocabularyPlugin 生词本
                                                                  ILlm              大模型插件的提示词管理
Plugins/  Translate.OpenAI / DeepL / Baidu / Youdao / BigModel / Google… ；Ocr.* ；Tts.* ；Vocabulary.*
```

- 插件是独立程序集，打包成 `.spkg`，有插件市场（下载、升级、卸载需重启生效）。
- **插件 ≠ 服务**：同一个插件可以“复制”出多个服务实例（如两个不同 base_url/模型的 OpenAI），各有独立配置文件。

**文本翻译链路**

1. 输入防抖 → 创建 `TranslationOperation`（快照文本、语种、取消令牌，带递增操作号）。
2. **缓存优先**：用 `(原文, 源语言, 目标语言)` 查 SQLite 历史，命中的服务直接出结果。
3. 语言检测，推导目标语种。
4. **多服务并发扇出**：所有启用的翻译/词典服务用信号量并发执行，结果并排显示；可选自动回译。
5. 旧请求被新请求取代后，旧结果不能再覆盖界面、复制或写历史（操作号 + 取消令牌保证）。

**大模型插件**：流式输出；提示词是可编辑的 system/user 模板，变量 `$source`、`$target`、`$content`，内置“翻译 / 润色 / 总结”三套；OpenAI 插件默认 `gpt-4o`、温度 0.7、**不发送任何思考参数**；智谱 BigModel 插件默认 `glm-4-flash`、`Thinking = false`。没有术语表和文档级上下文，每次只翻一段文本。

**截图翻译链路**（与本项目的“去字 + 叠加”思路相同，只是对象是图片）：

1. 截图 → 必须用**能返回坐标框**的 OCR 插件。
2. 分段（`OcrLayoutAnalyzer`）：优先用 OCR 服务商返回的段落结构；没有就本地 `Smart` 分段，按 Y 坐标恢复视觉行、按列聚合区域、识别表格（多行多列、列边缘重复对齐）后禁止跨行合并。
3. 每个块并发翻译。
4. `ImageTranslateTextOverlayLayout` 计算擦除矩形、字号、多行回退和裁剪，生成**矢量文字覆盖层**叠在原图上，原文/译文可切换、可选中复制。

**和本项目的对比与可借鉴之处**

| | STranslate | 本项目 |
|---|---|---|
| 翻译单位 | 一段短文本，实时 | 整篇文档，批量 |
| 引擎用法 | 多引擎同时翻、并排对比 | 一篇文档用一个引擎，控制成本 |
| 上下文 / 术语 | 无 | 标题、摘要、章节、术语表 |
| 版式 | 截图：OCR 坐标 + 擦除 + 矢量覆盖 | PDF：字符坐标 + 去字 + HTML 覆盖 + 矢量导出 |

可借鉴：①“插件 / 服务实例”分离，同一适配器可配多套参数；②单段“多引擎对比”可作为精修时的辅助；③操作号 + 取消令牌，防止单段重翻时旧结果覆盖新结果；④缓存键包含服务实例；⑤扫描页 OCR 后的本地分段规则（视觉行恢复、列聚合、表格识别）。

---

## 5. 结论（对自建项目的启示）

1. **"字符级坐标 + 版面语义"必须同时拿到**：PyMuPDF 给精确坐标和样式，MinerU/Doc2X 给块类型、阅读顺序、公式 LaTeX、表格结构，两者融合才能既保版式又懂结构。
2. **中间表示（IR）是核心资产**：解析器、翻译服务、渲染器都围绕 IR 插拔，才能同时接 MinerU / Doc2X / 外部 LLM。
3. **网页端天然适合"叠加渲染"**：浏览器的排版能力（自动换行、CSS 缩放、KaTeX 公式）比在 PDF 里手写绘制指令强得多，也便于逐段编辑和重翻。
4. **BabelDOC 可以直接拿来当基线和备选引擎**：本项目同为 AGPL-3.0，许可兼容，先用它评估样本、对比效果。
5. **保留版式的难点都在边角**：字号溢出、CID 字体、行内公式、表格、扫描件，要按优先级分阶段做。
6. **论文术语一致性靠"领域词库 + 文档级自动抽取 + 命中注入"**：现成的 GIST（Apache-2.0）可直接用，其他领域用 Wikidata 兜底。

---

## 参考来源

- BabelDOC：[GitHub](https://github.com/funstory-ai/BabelDOC) · [论文 arXiv 2605.10845](https://arxiv.org/html/2605.10845) · [ACL 2026 Demo](https://preview.aclanthology.org/ingest-acl/2026.acl-demo.25/)
- PDFMathTranslate：[GitHub](https://github.com/PDFMathTranslate/PDFMathTranslate) · [论文 arXiv 2507.03009](https://arxiv.org/html/2507.03009v1) · [PDFMathTranslate-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next)
- 沉浸式翻译：[PDF 帮助文档](https://immersivetranslate.com/zh-Hans/docs/pdf-helper/) · [BabelDOC 介绍](https://immersivetranslate.com/zh-Hans/document/babel-doc-translator/) · [PDF Pro 介绍](https://immersivetranslate.com/zh-Hans/document/pdf-pro-translator/) · [koc.com.tw 额度报道](https://koc.com.tw/?p=594414) · [ai-bot.cn 介绍](https://ai-bot.cn/babeldoc/)
- MinerU：[GitHub](https://github.com/opendatalab/MinerU) · [LICENSE](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md) · [输出文件说明](https://opendatalab.github.io/MinerU/reference/output_files/) · [mineru.net 限额](https://mineru.net/doc/docs/limit_en)
- Doc2X：[开放平台](https://open.noedgeai.com) · [API 价格](https://open.noedgeai.com/pricing.html) · [常见问题](https://doc2x.noedgeai.com/help/en/faq/doc2x-common-faq.html) · [积分说明](https://doc2x.noedgeai.com/help/en/faq/doc2x-points.html)
- MinerU API：[API 管理页](https://mineru.net/apiManage) · [限流说明](https://mineru.net/doc/docs/limit_en)
- 术语：[GIST 论文](https://arxiv.org/abs/2412.18367) · [GIST 数据集](https://huggingface.co/datasets/Jerry999/multilingual-terminology) · [机器之心 AI 术语库](https://github.com/jiqizhixin/Artificial-Intelligence-Terminology-Database) · [ECDICT](https://github.com/skywind3000/ECDICT) · [Kim et al. 2410.15690](https://arxiv.org/abs/2410.15690v1) · [Retrieval vs Generation 2503.05010](https://arxiv.org/pdf/2503.05010) · [术语在线介绍](https://www.iikx.com/web/scientific/tool/214.html) · [ACL 60-60](https://2022.aclweb.org/dispecialinitiative.html) · [linux.do 沉浸式翻译 AI 专家讨论](https://linux.do/t/517192)
- STranslate：[GitHub](https://github.com/STranslate/STranslate) · 架构文档 `src/docs/flow-main-translation.md`、`runtime-plugin-service.md`、`flow-image-translation.md`
- 其他：[marker](https://github.com/datalab-to/marker) · [MDTrans](https://pypi.org/project/mdtrans/) · [PolyglotPDF](https://github.com/DsCodeStudioxhpott/PolyglotPDF) · [PDF 转 Markdown 工具对比](https://jimmysong.io/blog/pdf-to-markdown-open-source-deep-dive/) · [PaddleOCR-VL-1.6 论文](https://arxiv.org/pdf/2606.03264) · [本地 OCR 2026 对比](https://pasqualepillitteri.it/zh/news/25574/zui-jia-ben-di-ocr-pc-fuwuqi) · [PyMuPDF 文档](https://pymupdf.readthedocs.io/en/latest/page.html)

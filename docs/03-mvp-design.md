# MVP 第一阶段设计

> 日期：2026-10-28
> 依据：[01-tech-stack.md](01-tech-stack.md)（选型 v9）、[02-spikes.md](02-spikes.md)（S1–S12 实验结论）
> 设计原则：**高内聚、低耦合**——每个模块只负责一件事，模块之间只通过稳定的接口（端口）和数据结构（IR）交互

---

## 1. 第一阶段范围

**目标**：上传一篇原生文本的英文论文 PDF，在浏览器里得到左右对照、保留版式的中文译文，可逐段修改和重翻。

| 包含（P1） | 不包含（后续阶段） |
|---|---|
| 原生文本 PDF，≤200 页（MinerU 单文件上限） | 扫描页 / 乱码页 OCR 路径与背景擦除（P3） |
| 版面解析：MinerU 云端精准解析 API（上传前确认） | Doc2X、本地解析（P2 / P4） |
| 翻译：任意 OpenAI 兼容接口；v2 提示词；默认不思考，可选“精翻”（思考·low） | DeepL、多模型对比（P2+） |
| 术语：文档术语抽取 + 程序校正 + GIST 词库匹配 | 用户术语表编辑界面、Wikidata 兜底（P2 / P4） |
| 翻译缓存、占位符校验、失败段重试 | |
| 网页阅读器：左原文 / 右译文（去字背景 + 叠加层 + KaTeX 行内公式），同步滚动、悬停联动 | 导出 PDF（P2） |
| 单段编辑、单段重翻 | 表格单元格翻译、图内文字（P3） |
| API Key 存浏览器 localStorage，随请求发送 | 账号系统 |
| Docker Compose 一键启动（web + app） | |

**验收标准**

1. S7–S12 用过的 5 篇 arXiv 论文都能完整走通：解析 → 翻译 → 阅读器展示，无漏页、无公式丢失（占位符校验 100% 通过或明确标出失败段）。
2. 公式密集页（1412.6980 第 2–3 页）排版不重叠，行内公式由 KaTeX 渲染，失败时回退为原图裁剪。
3. 一篇 15 页论文，DeepSeek 默认档费用 ≤ ¥0.1，端到端耗时 ≤ 3 分钟（其中 MinerU 约 30–50 秒）。
4. 同一文档第二次打开不再调用 LLM；单段重翻只发这一段。
5. 机器 2 核 / 2 GB 内存可运行（轻量模式不在本地跑模型）。

---

## 2. 总体架构

```mermaid
flowchart TB
  subgraph FE[前端 React]
    U[upload 上传] --> JP[job-progress 进度]
    JP --> R[reader 阅读器]
    S[settings 设置/Key]
  end
  subgraph BE[后端 FastAPI]
    API[api 接口层] --> PL[pipeline 流水线编排]
    PL --> P1[parsing 版面解析]
    PL --> P2[extraction 字符提取]
    PL --> P3[fusion 融合]
    PL --> P4[translation 翻译]
    PL --> P5[glossary 术语]
    PL --> P6[rendering 背景页]
    P1 & P2 & P3 & P4 & P5 & P6 --> D[(domain：IR 数据结构)]
    PL --> ST[storage 存储]
  end
  FE -- HTTP / SSE --> API
  P1 -- HTTPS --> M[MinerU 云端]
  P4 -- HTTPS --> L[OpenAI 兼容 LLM]
```

### 2.1 依赖规则（保证低耦合）

```
api  →  pipeline  →  { parsing, extraction, fusion, translation, glossary, rendering }  →  domain
                ↘  storage（经端口）
```

- **domain** 只有数据结构和纯函数，不依赖任何其他模块、不做 IO。
- 六个能力模块**互不导入**：它们之间的数据只经 domain 的 IR 传递，由 pipeline 串起来。
- 能力模块对外部服务（MinerU、LLM、文件、数据库）只依赖自己声明的**端口（Protocol）**；具体实现（适配器）在启动时由 `container.py` 注入。
- api 只做“HTTP ↔ 用例”转换，不含业务逻辑。
- 违反规则的导入用 `import-linter` 在 CI 中检查。

### 2.2 一次翻译任务的时序

```mermaid
sequenceDiagram
  participant B as 浏览器
  participant A as api
  participant P as pipeline
  participant M as MinerU
  participant L as LLM
  B->>A: POST /api/jobs（PDF、选项、Key 放请求头、cloud_consent）
  A->>P: 创建任务（Key 只放内存）
  A-->>B: 202 {job_id}
  B->>A: GET /api/jobs/{id}/events（SSE）
  P->>P: classify：逐页分类
  P->>M: parse：上传 → 轮询 → 下载结果
  P->>P: extract：PyMuPDF 字符；fuse：生成 IR
  P->>L: glossary：抽术语（1 次小调用）
  P->>L: translate：按批翻译（并发、缓存、校验、重试）
  P->>P: render：生成去字背景页
  P-->>B: 各阶段进度事件 … done
  B->>A: GET /api/jobs/{id}/document（IR + 译文）
  B->>A: GET /api/jobs/{id}/files/original.pdf、background.pdf
```

---

## 3. 后端设计

### 3.1 目录结构

```
backend/
  pyproject.toml                  # uv 管理；Python 3.12
  src/pdftr/
    domain/                       # 纯数据结构与纯函数
      geometry.py                 #   Rect、坐标换算（0–1000 归一化 ↔ pt）
      ir.py                       #   Document / Page / Block / Span / InlineFormula / Segment
      ids.py                      #   稳定 ID 生成
    parsing/                      # 版面解析
      port.py                     #   LayoutParser 协议、LayoutResult
      mineru_cloud.py             #   MinerU 精准解析 API 适配器
      mineru_normalize.py         #   layout.json → LayoutResult（跨页合并块、discarded 中的脚注）
    extraction/                   # PyMuPDF 侧
      chars.py                    #   rawdict 字符/样式提取
      classify.py                 #   逐页分类 native / scanned / garbled
    fusion/                       # 融合为 IR
      align.py                    #   字符归属到块（中心点 + 容差，S4 验证 99.8%）
      cross_page.py               #   跨页段落拆分信息（续行属于下一页空块）
      text_fix.py                 #   以 PyMuPDF 文字纠正 MinerU 文本（如 “≲ 1” 被识别为 “b1”）
      style.py                    #   块主字号/字体/粗体统计
    glossary/                     # 术语
      port.py                     #   TermSource 协议
      extract.py                  #   LLM 抽取（TERMS_V3：领域 + 术语）
      normalize.py                #   程序校正：正文大小写、剔除章节名
      gist.py                     #   GIST 词库加载（首次启动下载）
      matcher.py                  #   Aho-Corasick 命中 + 简单词形归一
    translation/                  # 翻译
      port.py                     #   ChatModel 协议（complete(messages, options) → text, usage）
      openai_compat.py            #   OpenAI 兼容适配器（含 DeepSeek 思考开关方言）
      segmenter.py                #   IR → Segment（占位符 <mN/>、公式 LaTeX）
      prompt.py                   #   v2 提示词（RULES_V2）与批次消息组装
      batcher.py                  #   按 token 预算分批
      validate.py                 #   占位符集合校验、行号完整性
      service.py                  #   翻译用例：缓存 → 分批 → 并发 → 校验 → 重试 → 写回
      cost.py                     #   token 计价、费用预估
    rendering/
      background.py               #   去字背景页（redaction，S5 参数）
      crops.py                    #   行内公式原图裁剪（KaTeX 失败时兜底）
    pipeline/
      stages.py                   #   各阶段函数：输入/输出都是工件
      runner.py                   #   顺序执行、断点续跑、失败重试
      events.py                   #   进度事件总线（内存发布订阅）
      secrets.py                  #   任务级 Key 内存保管（任务结束清除）
    storage/
      port.py                     #   ArtifactStore、JobRepo、SegmentRepo、CacheRepo 协议
      files.py                    #   data/jobs/<id>/ 下的工件文件
      sqlite.py                   #   SQLModel 实现
    api/
      app.py                      #   FastAPI 应用、CSP 等中间件
      routes_jobs.py              #   任务：创建、查询、事件、文档、文件
      routes_segments.py          #   段落：编辑、重翻
      routes_meta.py              #   健康检查、模型列表、费用预估
      schemas.py                  #   请求/响应 DTO（与 IR 解耦）
      sse.py
    config.py                     # 环境变量配置（pydantic-settings）
    container.py                  # 组合根：创建适配器并注入
  tests/
    unit/  contract/  e2e/        # 见 §6
```

### 3.2 IR（domain/ir.py）

在 [01-tech-stack §4.1](01-tech-stack.md#41-ir中间表示) 的基础上，按实验结论补充字段。坐标统一为 **PDF point、左上角原点**。

```python
class Span(BaseModel):
    text: str
    bbox: Rect
    font: str | None
    size: float
    color: int
    bold: bool
    italic: bool

class InlineFormula(BaseModel):
    key: str                       # "m1"
    latex: str                     # 来自 MinerU inline_equation
    bbox: Rect                     # 用于原图裁剪兜底

class Block(BaseModel):
    id: str                        # "p3-b12"
    page: int
    type: BlockType                # text / title / caption / footnote / formula / table / figure / code / header / footer / reference
    bbox: Rect
    order: int
    spans: list[Span] = []
    formulas: list[InlineFormula] = []
    latex: str | None = None       # 独立公式
    continues_on: str | None = None  # 跨页段落：续行所在下一页空块的 id（S4）
    split_ratio: float | None = None # 本页部分占原文行数的比例
    style: BlockStyle
    translatable: bool

class Segment(BaseModel):
    id: str
    job_id: str
    block_ids: list[str]           # 跨页时为 [本页块, 下一页空块]
    source: str                    # 含 <mN/> 占位符
    formulas: dict[str, str]       # key → LaTeX（请求时展开成 <mN>LaTeX</mN>）
    section: str                   # 所在章节，作为批次上下文
    target: str | None = None
    status: SegmentStatus          # pending / done / failed / edited
    quality: Literal["default", "careful"] = "default"   # careful = 精翻（思考·low）

class Document(BaseModel):
    job_id: str
    pages: list[Page]              # 宽高、旋转、kind、blocks
    meta: DocMeta                  # 标题、摘要、领域、源语言、解析器、是否用了云端
```

IR 只描述“文档是什么”，不含任何服务调用细节；译文单独存 `segments` 表，IR 文件解析后不再改动。

### 3.3 端口（各模块对外的唯一依赖）

```python
# parsing/port.py
class LayoutParser(Protocol):
    name: str
    is_cloud: bool
    async def parse(self, pdf: Path, pages: list[int] | None) -> LayoutResult: ...

# translation/port.py
class ChatModel(Protocol):
    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult: ...
    # ChatOptions: model, temperature, max_tokens, thinking: off|low
    # ChatResult: text, usage(prompt/hit/miss/completion/reasoning)

# glossary/port.py
class TermSource(Protocol):
    def lookup(self, terms: Iterable[str]) -> dict[str, str]: ...

# storage/port.py
class ArtifactStore(Protocol):
    def path(self, job_id: str, name: str) -> Path: ...
class SegmentRepo(Protocol):
    def save_many(self, segs: list[Segment]) -> None: ...
    def pending(self, job_id: str) -> list[Segment]: ...
    def update_target(self, seg_id: str, target: str, status: SegmentStatus) -> None: ...
class CacheRepo(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, target: str) -> None: ...
```

新增解析服务（Doc2X、本地 MinerU）或翻译服务，只需新增适配器并在 `container.py` 注册，其他模块不改。

### 3.4 流水线（pipeline）

| 阶段 | 输入工件 | 输出工件 | 来自实验 |
|---|---|---|---|
| `classify` | original.pdf | page_kinds.json | §4.2 规则；P1 遇到 scanned/garbled 页给出提示并跳过 |
| `parse` | original.pdf | parser/raw.zip、layout.json | S2：`model_version=vlm`、公式/表格开、>200 页分段 |
| `extract` | original.pdf | chars.json | S4 |
| `fuse` | layout.json、chars.json | ir.json | S4 对齐、跨页拆分、文字纠正 |
| `segment` | ir.json | segments 表 | S7 切分规则：参考文献默认不翻 |
| `glossary` | ir.json | glossary.json | S10：TERMS_V3 + 程序校正 + GIST 命中 |
| `translate` | segments、glossary | segments 表（译文） | S7/S9：v2 提示词、约 1500 token 一批、温度 0.3、默认不思考 |
| `render` | original.pdf、ir.json | background.pdf、crops/ | S5：`REMOVE_IF_TOUCHED` |

- 每个阶段是**纯粹的“读工件 → 写工件”函数**，彼此不直接调用；runner 记录 `job.stage`，失败时从失败阶段重跑（MinerU 结果和译文都已落盘，重跑不重复花钱）。
- 每个阶段向事件总线发布 `JobEvent(stage, progress, message)`，api 的 SSE 只订阅总线。
- 执行模型：单进程 asyncio worker；`translate` 内部按服务商配置的并发数和 RPM 限流（S7 用 4 并发）。

### 3.5 翻译用例（translation/service.py）

```
pending 段落
  → 查缓存（key = sha256(source + formulas + model + prompt_version + glossary_hash + quality)）
  → 未命中的按 token 预算分批（同一批注明章节）
  → 组装消息：system = RULES_V2 + 标题/摘要/领域 + 本批命中的术语；user = 「编号|带 LaTeX 的原文」
  → 并发调用 ChatModel（quality=careful 的批次开思考·low）
  → 解析「编号|译文」，校验行号与占位符集合
  → 通过：写回 + 写缓存；不通过：该段单独重试 2 次，仍失败标 failed（阅读器显示原文 + 重试按钮）
  → 汇总 usage 计算费用
```

“标准档”（含 ≥2 个行内公式的段落自动用精翻）的规则放在 `segmenter`，用 `Segment.quality` 表达，翻译用例不关心规则本身。

### 3.6 API（P1）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/jobs` | multipart：`file`、`options`（JSON：解析器、模型、档位、页码范围、是否翻参考文献、`cloud_consent`）；请求头 `X-LLM-Base-URL`、`X-LLM-Key`、`X-LLM-Model`、`X-Parser-Key`。返回 `202 {job_id}` |
| GET | `/api/jobs/{id}` | 状态、当前阶段、页数、费用、错误 |
| GET | `/api/jobs/{id}/events` | SSE：`{stage, progress, message}` |
| GET | `/api/jobs/{id}/document` | 阅读器数据：页面尺寸、块（bbox、类型、样式、跨页信息）、段落（原文、译文、状态、公式 LaTeX） |
| GET | `/api/jobs/{id}/files/{name}` | `original.pdf`、`background.pdf`、`crops/{key}.png` |
| POST | `/api/jobs/{id}/retry` | 从失败阶段重跑（需重新带 Key） |
| PATCH | `/api/segments/{id}` | 手动修改译文（status=edited，不会被后续重翻覆盖） |
| POST | `/api/segments/{id}/retranslate` | 单段重翻，可选 `quality=careful`；需带 Key |
| POST | `/api/estimate` | 页数/字数 → 预估 token 与费用 |
| GET | `/api/health` | 健康检查 |

DTO 定义在 `api/schemas.py`，由 IR 转换而来；前端类型由 OpenAPI 生成（`openapi-typescript`），前后端只通过这份契约耦合。

### 3.7 存储

```
data/
  app.db                              # SQLite：jobs、segments、translation_cache、glossary_entries
  jobs/<job_id>/
    original.pdf
    page_kinds.json  chars.json  layout.json  ir.json  glossary.json
    parser/raw.zip                    # MinerU 原始结果，便于重放
    background.pdf
    crops/m*.png
  glossaries/vendor/gist_zh.csv       # 首次启动下载（Apache-2.0，保留署名）
```

### 3.8 安全与隐私

- Key 只在请求头中出现，后端放在 `pipeline/secrets.py` 的内存字典里（按 job_id），任务结束或失败即清除；日志过滤 `X-*-Key` 请求头。
- `cloud_consent` 为假时拒绝使用云端解析器（422）。
- 上传限制：200 MB、200 页、仅 `application/pdf`；文件名不入路径（用 job_id）。
- 严格 CSP，前端不加载第三方脚本（KaTeX、pdf.js 打包进前端资源）。

---

## 4. 前端设计

### 4.1 目录结构（按功能切片）

```
frontend/
  src/
    app/                    # 路由、全局 Provider（QueryClient）、布局
    pages/
      HomePage.tsx          #   上传 + 最近任务
      JobPage.tsx           #   进度 → 完成后切到阅读器
    features/
      upload/               # 上传区、选项面板、云端上传确认、费用预估
      job-progress/         # SSE 订阅、阶段进度条、失败重试
      reader/               # 阅读器（见 4.2）
      segment-edit/         # 段落弹出层：原文/译文、编辑、重翻
      settings/             # 服务配置：Base URL、模型、Key（localStorage）、默认档位
    entities/
      document/             # 文档/段落类型（由 OpenAPI 生成）与选择器
      job/
    shared/
      api/                  # fetch 客户端（自动附加 Key 请求头）、SSE 封装
      pdf/                  # pdfjs-dist 封装：加载、按页渲染、视口换算
      math/                 # KaTeX 渲染 <mN/>，失败回退裁剪图
      fit/                  # 文本拟合：先调行距、再二分字号（S5 的 FIT_JS 移植）
      storage/              # localStorage 安全读写（try/catch）
      ui/                   # 按钮、对话框等基础组件
```

**依赖规则**：`pages → features → entities → shared`，只能向下依赖；features 之间不互相导入（需要协作时通过 pages 组合或共享 store）；用 ESLint `import/no-restricted-paths` 检查。

### 4.2 阅读器（features/reader）

```
ReaderView
 ├ Toolbar          缩放、单栏/双栏/只看译文、跳页
 ├ SyncScroller     左右两栏按页对齐的同步滚动
 │ ├ OriginalPane   pdf.js 渲染 original.pdf（虚拟列表，只渲染可视区 ±2 页）
 │ └ TranslatedPane
 │    ├ BackgroundLayer   pdf.js 渲染 background.pdf
 │    └ OverlayLayer      绝对定位的译文块（pt 坐标 × 缩放），KaTeX 行内公式，拟合字号
 └ HoverLink        悬停任一侧的块 → 两侧对应块高亮；点击译文块打开 segment-edit
```

- **坐标**：容器内部始终用 pt 排版，外层整体 `transform: scale(zoom)`，保证不同缩放下排版一致（也为 P2 的导出复用同一套 HTML）。
- **跨页段落**：按 `split_ratio` 在标点处拆分译文，分别放进本页块和下一页空块（S5b 已验证）。
- **拟合**：每块渲染后在 `requestAnimationFrame` 中计算一次，结果缓存在内存，缩放时不重算。
- **状态**：`failed` 段显示原文（灰色）+ 重试按钮；`edited` 段右上角小标记。

### 4.3 设置与 Key

- 设置页保存：翻译服务（预设 DeepSeek / OpenAI 兼容自定义）、Base URL、模型、Key、默认档位、MinerU Token。
- 全部存在 localStorage（`shared/storage` 封装，读写失败时退回内存），提供“清除已保存的 Key”。
- `shared/api` 在每个请求上附加对应请求头；没有配置时，上传页引导去设置。

### 4.4 上传页的云端提醒

- 常驻提示条：“文件将上传至 MinerU 云端进行版面解析，请勿上传涉密或未公开文档”。
- 提交前必须勾选确认（`cloud_consent=true`）。
- 显示预估：页数、预计费用（调用 `/api/estimate`）、预计耗时。

---

## 5. 部署

```yaml
# deploy/docker-compose.yml
services:
  app:
    build: ../backend          # Python 3.12 + PyMuPDF；P1 不含 Chromium
    volumes: ["../data:/data"]
    environment: [DATA_DIR=/data]
  web:
    build: ../frontend         # 构建静态文件，由 Caddy 提供
    ports: ["8080:80"]         # Caddy：静态资源 + /api 反向代理 + 可选 Basic Auth
```

---

## 6. 测试策略

| 层 | 内容 | 依赖 |
|---|---|---|
| 单元 | geometry 换算、segmenter 占位符、validate、batcher、prompt 组装、术语校正与匹配、fit 算法（前端 vitest） | 无外部依赖 |
| 契约 | `mineru_normalize` 用 S2 录制的 layout.json 作夹具；`openai_compat` 用 `spikes/mock_llm.py` 作假服务 | 本地夹具 |
| 融合回归 | 5 篇论文的对齐覆盖率 ≥99.5%、跨页段落数、块类型统计与 S4 一致 | 夹具（论文 PDF 由脚本下载，不入库） |
| 端到端 | 上传 → 假 LLM 翻译 → document 接口返回完整段落；前端 Playwright 打开阅读器截图比对 | Docker |
| 翻译质量 | 改提示词/模型时手动运行 S10/S11 回归脚本 | 真实 Key，不进 CI |

CI：GitHub Actions 跑 ruff、pyright、import-linter、pytest（单元 + 契约 + 融合回归）、前端 lint/typecheck/vitest/build。

---

## 7. 开发顺序（第一阶段内部里程碑）

| 步骤 | 内容 | 完成标志 |
|---|---|---|
| M1 骨架 | 仓库目录、uv/Vite 工程、CI、domain 模型、container、健康检查、Docker Compose | `docker compose up` 后访问首页和 `/api/health` |
| M2 解析与融合 | parsing（MinerU 云端）、extraction、fusion、classify；把 spike 代码整理成模块 | 5 篇论文生成 ir.json，融合回归测试通过 |
| M3 翻译 | segmenter、prompt v2、batcher、validate、openai_compat、缓存、费用；glossary | 假 LLM 端到端通过；真实 DeepSeek 跑一篇费用 ≤ ¥0.1 |
| M4 背景页与 API | rendering、pipeline runner、事件、全部 P1 接口 | 用 curl 走通一次完整任务 |
| M5 前端 | 上传、设置、进度、阅读器、段落编辑 | 浏览器中完成验收标准 1–4 |
| M6 收尾 | README 部署说明、错误提示、性能检查（2 核 2 GB） | 验收标准 5，打 `v0.1.0` 标签 |

每个里程碑完成后提交并推送；spike 代码保留在 `spikes/` 作为参考，正式代码不导入 spike。

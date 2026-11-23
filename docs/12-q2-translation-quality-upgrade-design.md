# Q2 翻译质量提升设计

针对真实学术论文端到端实测暴露的翻译与成品质量问题，本轮在同一次 LLM 术语抽取调用中决定公共词库开关，并改进版面融合切段、伪公式与裁图、提示词风格。`PROMPT_VERSION` 为 `v3`，`EXPORT_CACHE_VERSION` 升至 `7` 以刷新公式裁图。

## 1. 公共词库去噪、领域门控与核心对象词锁定

### 问题
- 自动抽取源 `gist`（6400+ 条）及部分人工词库中的多义单实词（如 `Pool=汇聚`、`Operator=运算符`、`Loop=环`、`Instance=示例`、`Range=值域`、`Setting=情景`、`Actor=演员`、`Fluctuation=振荡`）被无差别注入非 AI 主领域论文，造成明显跨领域错译与前后自相矛盾。
- 全大写缩写词条（如 `CLIP`）匹配时不区分大小写，导致小写 `clip rate` 误命中多模态模型 `CLIP`。
- 论文术语抽取未锁定高频多译领域对象词（如运筹调度中的 `job` 在「作业 / 工件」间反复漂移）。
- 批次注入公共术语时仅做了大小写敏感去重，未抑制与论文术语译法冲突的子词（如论文术语为 `local search operator=局部搜索算子`，公共词库仍注入 `Operator=运算符`）。

### 设计与不变量
1. **默认来源净化（`glossary/sources.py`、`config.py`）**
   - `DEFAULT_ORDER` 与 `Settings.glossary_sources` 默认值移除 `gist`，保留 `["aitd", "d2l", "ml-technical-term", "ais-vocab", "computerese"]`（`gist` 解析器继续保留，部署者仍可显式开启）。
   - 公共词库默认仅保留**多词专业短语（词数 ≥ 2）**与明确维护的**单义 AI 专词白名单**（如 `softmax`、`backpropagation`、`hyperparameter`、`overfitting`、`underfitting`、`regularization`、`autoencoder`、`perplexity`、`tokenization`、`tokenizer`、`quantization`、`distillation` 等无跨学科歧义词条），过滤全部高歧义单实词。
   - 按源词和译法组合屏蔽与通行学术中文冲突的词条（如 Transformer 译为 `变换器`、actor 译为 `演员`、pooling 译为 `汇聚`，或 `discount factor→衰减系数`、`actor-critic framework→行动者-评论家框架` 等已知冲突译法）。保留直流变换器、网络汇聚等其他语境下成立的译法。
2. **匹配器大小写保护与三级优先级（`glossary/matcher.py`）**
   - 全大写缩写词条（`term == term.upper()` 且含至少 2 个字母，如 `CLIP`、`BERT`、`GAN`）仅匹配原文中同样全大写的词形，禁止匹配小写普通词。
   - 匹配器默认收录至少 4 字符的词条和至少 3 字符的全大写缩写。用户术语与论文术语各自显式允许 2 字符词条。
   - 严格执行 `用户术语 > 论文自抽术语 > 公共词库`：
     - 按大小写折叠和归一化词序列去重，屏蔽与高优先级术语只有单复数差异的公共词条。
     - **子词冲突抑制**：若公共词库词条（归一化词序列）是某条用户术语或论文自抽术语的词序列子串，且该公共词条的目标中文未出现在对应高层术语译文中，则屏蔽该公共词条。
3. **零成本领域门控与四入口统一（`glossary/extract.py`、`pipeline/runner.py`、`pipeline/usecases.py`、`cli.py`）**
   - `extract_terms` 提示词扩展两项（同一轮小调用，零新增请求）：
     - 模型根据标题、摘要、引言开头和章节标题给出 `AI公共词库：启用` 或 `AI公共词库：停用`。仅当论文主体属于 AI/ML/DL/NLP/CV/RL 算法理论或 CS 核心方向时启用。开关解析写入 `Extraction.use_library: bool` 并持久化到 `glossary.json`。
     - 移除领域关键词正则。模型漏输出开关、历史 `glossary.json` 缺字段、或文件缺失时，统一默认 `use_library=False`。任务仍使用已有的论文标题、摘要、领域背景、论文自抽术语与用户术语。
     - 明确要求同时收入论文中高频出现、存在多种中文译法需全文统一的核心对象词（如 `job`、`instance`、`operation`、`agent`、`node` 等），并要求模型/架构名（如 `Transformer`、`BERT`、`ResNet`）保留英文。
   - 首次翻译（`runner.py:_translate`）、单段重翻（`usecases.py:retranslate`）、术语面板（`usecases.py:job_terms`）与 `cli.py` 共享同一门控与冲突抑制逻辑：当 `use_library=False` 时关闭公共 AI 词库，用户术语库始终生效。

## 2. 首页 `HIGHLIGHTS` 放行、拉宽标题还原与跨栏/跨页补链

### 问题
- `segmenter.py:front_matter_ids` 把首页主标题与 `Abstract` 之间的全部 `TEXT` 块当作作者/单位跳过，误杀 Elsevier 模板位于 `Abstract` 上方的 `H I G H L I G H T S` 全部正文条目。
- 字母拉宽标题（`H I G H L I G H T S`、`A B S T R A C T`、`A R T I C L E I N F O`）因全为单字母，被 `MIN_WORDS` 的 `[A-Za-z]{2,}` 过滤掉，且 `fusion/build.py:_meta` 无法直接匹配拉宽的 `A B S T R A C T`。
- MinerU 偶发将图内坐标刻度（纯全大写/数字/连字符代号，如 `PPO-HHD1 PPO-HHD3 ...`）误标为 `CAPTION` 送去翻译并覆写原图。
- MinerU 未生成占位块时，跨栏（如 `consider both the` + `above sub-schedules.`）、跨页隔图（`under nonidentical` + `TOU tariffs.`）以及同页通栏算法框导致的跨栏阅读顺序倒置（左栏底 `as quickly` 与右栏顶 `as possible...`）均未建立 `continues_from`，导致前段脑补 + 后段重复翻译。

### 设计与不变量
1. **`front_matter_ids` 截断到首页首个结构子标题（`translation/segmenter.py`）**
   - 在首页主标题之后，选取匹配 `{"highlights", "articleinfo", "abstract", "keywords", "introduction"}`（去非字母转小写）且 `order` 最小的 `TITLE` 块。只跳过主标题到该子标题之间的作者/单位 `TEXT` 块。
2. **拉宽单字母标题归一化（`translation/segmenter.py`、`fusion/build.py`）**
   - 对 `TITLE` 块中由纯单字母+空格构成的拉宽英文标题（≥ 4 个单字母 token），折叠相邻单字母间单空格（保留多空格词界并正确识别常见双词 `ARTICLE INFO`）后再参与 `MIN_WORDS` 过滤、翻译和 `_meta()` 摘要定位。
3. **纯代号假 `CAPTION` 过滤（`translation/segmenter.py`）**
   - 对 `BlockType.CAPTION`，去掉 `<mN/>` 与富文本标签后，要求至少含 1 个正常英文单词（长度 ≥ 3 且含小写字母）或以 `Fig.` / `Figure` / `Table` / `Algorithm` / `Scheme` 开头；纯大写/数字/连字符代号序列跳过翻译。
4. **保守型跨栏/跨页补链（`fusion/build.py`）**
   - 各栏首个正文块须位于页面顶部 35% 范围内。首行缩进超过 0.8 个字号时按新段处理。页首左栏使用本栏正文作为缩进基准。
   - 在现有 `relocate` 之后对仍无 `continues_from` 的 `BlockType.TEXT` 块追加补链并检查成环。图表、图注、公式、代码、页眉和页脚允许跨越，标题、脚注和参考文献阻断续接。
     - **正向跨栏/跨页**。前块无句末终止标点，后块位于同页右栏或下一页首栏顶部，字号差 ≤ 1.5pt 且无新段缩进。后块以小写字母或接续标点开头，或前块以未完结虚词或未闭合括号结尾且后块最多六词，也允许以全大写缩写开头、接小写词且以句号结尾的非标题残句，最多四词。
     - **同页倒序残句补救**。右栏顶部以小写字母开头的正文块可接到唯一符合条件的左栏正文块。左栏候选须无句末标点和其他后继，字号差 ≤ 1.5pt，且其下方无左栏 `TITLE`、`FOOTNOTE` 或 `REFERENCE`。下游 `segmenter.py` 在链头合并翻译并按原块坐标回填。

## 3. 伪公式回收、公式边界清洗与裁图去黑边

### 问题
- MinerU 将 `Fig. 6,`（`^{6,}`）、`Section 3,`（`^{3,}`）、`Eq. (24)`（`\operatorname{Eq.}(24)`）、`1800 s,`（`\mathbf{s},`）误识为行内公式，破坏中文译文流畅度并引入冗余小裁图。
- MinerU 将紧邻公式的正文单词/字母或末尾标点吞进 `$...$`（如 `ReL $\mathrm{U}:500\times 300\times 100,$`、`$\mathrm{If} r_t(\theta)$`、`$\pi_\theta\colon$`），造成导出叠字 `ReLUU`、中英重词 `若 If` 与双冒号 `π_θ: ：`。
- 行内公式裁图使用上下重叠的 PyMuPDF 字体度量框，且导出与网页端分别再外扩 `0.5pt`/`1.0pt`，频繁切入上一行 `g/p/q/y` 下伸笔画黑点，同时抬高公式图高度导致个别块缩字。

### 设计与不变量
1. **公式边界吞字与标点剥离（`fusion/formula_align.py`）**
   - 块级内容切分 `$...$` 后清洗公式边界。扫描页和已有坐标的 MinerU 行共用这套规则。MinerU 原生行用 1pt 容差重新定位，全部公式定位成功才更新。正文 span 含字面美元符号时沿用原行。
     - 剥离公式末尾孤立的句间标点（包括带空格的 `mathrm { : }`、`	ext { : }`、`{ : }` 和 `colon`）归还后文，保留上下标内部标点、小数点和数学省略号。
     - 剥离公式开头紧贴空格或独立于数学符号的英文引导连词（保守集合：`If`、`When`、`Where`、`Then`、`And`、`Or`、`For`、`With`、`Let`，含 `\mathrm{...}`/`\text{...}` 包裹形式）归还前文；
     - 剥离断词残片：当前文以未完结字母片段结尾（如 `ReL `）且公式以单字母+冒号开头（如 `\mathrm{{U}}{:}500...`）时，前缀须含小写转大写的词形且续字为大写，满足条件才将首字母及冒号还原至前文恢复 `ReLU:`，仅保留纯数学部分作公式。
2. **扩展 `textual_formula` 回收伪公式（`translation/segmenter.py`）**
   - 在序数词基础上，额外将以下高置信伪公式还原为纯文本：
     - 纯数字编号加标点，包括字间插空格的误标上标 `^ { 1 2 , }`，回收为 `12,`。
     - 交叉引用，兼容伸缩括号和字间空格，例如将 `operatorname { E q . } left( 2 4 ight)` 回收为 `Eq. (24)`。
     - 紧跟数值的正体或粗体单位，兼容外围冗余花括号，例如将 `1800` 后的 `{ mathbf { s } } ,` 回收为 `s,`。普通变量与带上下标的表达式保留公式形态。
3. **公式裁图去黑边并统一双端入口（`fusion/formula_align.py`、`rendering/background.py`、`rendering/export.py`）**
   - `_formula_box` 用相邻视觉行中线约束上下界。正常字框高度上限为字号的 2.0 倍，覆盖 Elsevier 约 1.64 倍的正文和下标字框，同时排除超高数学符号的度量框。
   - 导出 PDF（`export.py:_formula_images`）与网页端（`crop_png`）共用裁图。水平留 0.3pt，垂直按公式框裁切。在最多 2pt 且不超过图像四分之一高度的边带内，用至少两行、约 0.5pt 的连续空白区分残影与主体。顶边残影行直接裁掉，底边残影行的 RGBA 像素置零并保留高度，使依图片底边对齐的基线保持原位。

## 4. Prompt v3 风格统一与中英空格确定性规范化

### 设计与不变量
1. **`translation/prompt.py`（`PROMPT_VERSION = "v3"`）**
   - 系统提示词区分文档级「本文术语表（全文统一使用）」与批次级「参考词库（仅当语境与论文领域吻合时参考，冲突时以自然学术译法为准）」。
   - 新增三条精简写作规范：
     - 除专有名词、模型/方法/数据集/软件名、指标缩写、变量/代号及原样作者引用外，所有普通英文词必须译成中文，禁止夹杂未译英文单词；
     - 汉字与英文、数字、公式占位符 `<mN/>` 之间不插入多余空格；
     - 中文文中引用统一用「等」（括号内作者年份引用保持原文），常见学术标题/元数据遵循中文惯例（如 `Problem statement` →「问题描述/问题陈述」，`Available online` →「在线发表」）。
2. **中英混排空格确定性规范化（`translation/batching.py`、`translation/service.py`、`rendering/typeset.py`、`rendering/export.py`）**
   - 译文验收后、落盘和写缓存前规范化去掉汉字与紧邻西文/数字/`<mN/>` 之间的单个水平空格（不破坏英文词间空格、标签及链接匹配）；导出排版在沿用原有分块后、分词前同步做同构清洗（遵守 `lint-imports` 能力模块互不导入契约），确保新旧译文在导出 PDF 中使用 W3C 四分之一汉字宽的视觉间距，PDF 文字流不写入多余的空格字符。前端阅读器继续通过 `spaceCjkLatin` 渲染一致视觉间距。
   - `pipeline/usecases.py` 使用 `EXPORT_CACHE_VERSION = 7`。

## 5. 回归验证矩阵

| 模块 | 离线确定性回归覆盖点 |
| --- | --- |
| 词库与门控 | 验证公共词库单实词过滤、缩写大小写、短词门槛、单复数去重与冲突子词抑制。假模型覆盖启用、停用和漏输出，旧任务覆盖字段缺失和文件缺失。首次翻译、重翻、术语面板与 CLI 共用门控。 |
| 首页与跨栏补链 | 验证首页标题乱序时保留亮点正文、跳过作者单位，识别拉宽标题和纯代号图注。合成 PDF 覆盖跨页缩写残句、普通独立段和标题前缀、倒序左栏结构阻断、防成环及通栏块对缩进基准的影响。 |
| 伪公式与裁图 | 覆盖六个带空格的 MinerU 样式、正常数学变量与标点、含 `$50` 的正文、1pt 行框容差与 Elsevier 字高。合成 PDF 验证顶边 5–6px 残影被裁掉、底边 RGBA 清零、主公式完整且两端共用裁图。 |
| 风格与空格 | 校验 `PROMPT_VERSION == "v3"`、`EXPORT_CACHE_VERSION == 7`、提示词分层及中英混排空格处理。合成导出验证文字流、富文本和链接定位。 |

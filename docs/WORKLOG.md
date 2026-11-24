# PDF Translator 项目交接记录

> 这份文件用于跨对话继续开发。每完成一个完整功能，更新“当前状态”“最近完成”和“下一步”，再单独提交 Git。
>
> 最后更新：2026-11-24（Asia/Shanghai）

### 部署与结项：Q4 翻译质量与词库进阶加固及六轮跨学科双盲评测闭环（2026-11-24）

- **方案与主包合入（`d72fba1`）**：经主控初稿 → Codex（`apice/gpt-6.2-sol xhigh`）红队审改 → 独立 Neon（`gemini-4-neon xhigh`）二轮审改 → 主控定稿（见 [Q4 设计方案](14-q4-translation-quality-and-glossary-tier2-design.md)），落地 Q4a–Q4d 全部能力并部署 HW-SGP（`app` + `web` 双镜像）。
- **六轮跨学科公开论文线上实测与双盲迭代收口（`edbccbb` → `03a5c2a` → `2957528` → `a7fe439` → `5f97183` → 收尾补丁）**：
  - 选取 6 篇覆盖 AI/CV 双栏（ResNet）、材料电化学单栏、Linux 系统双栏、金融数学单栏（带 `user_terms` 覆盖）、医学信息学全文及 200 DPI 纯图像扫描件的公开论文，在 `https://pdf.667786.xyz/` 完成 6 轮端到端翻译与单/双语导出实测（每轮测后均 `DELETE 204` 清理线上测试任务），每轮均由 **Codex** 与 **AGY / Neon** 独立双盲打分并交叉收敛共性工程缺陷：
  - **R2（`edbccbb`）**：修复引文校验三重假阳性与上标范围/混合定位引文、`</translations>` 行内截断、`\operatorname{If}`/尾 `=` 回收、Prompt `v6` 与高公式思考升档、上标无括号链接与 1pt 扁框自适应重叠过滤、原生 `×`/空格/上标恢复、绕图跨栏续接、术语上限 45/去泛词/用户词复合词消解、主标题居中、`CAPTION` 防孤字、扫描首页 `ABSTRACT` 纠序与 `Keywords.` 拆块。
  - **R3（`03a5c2a`）**：修复上标顿号/占位符假阳性、`relocate.py` 跨页坐标重合假命中与 `pi+2` 跨图归位、`INLINE_ABSTRACT` 补句点、`align.py` 双栏右溢出回收、`formula_align.py` 行中线约束与原生 `if:` 拆分、`background.py` 垂直墨迹连通带清跨行公式残影、移除 `_CHAIN_BARRIERS` 中的 `FOOTNOTE`、高公式批 60s 超时与 `<mN />` 规范化、前后端同构 `split_by_parts` 容量感知半径与短 ASCII 词界防腰斩、`links.py` 放行 `文献39`、`extract.py` 限域 `root` 与防复数自映射。
  - **R4（`2957528`）**：收紧 `_trim_formula_edges` 最小缝隙消除 `≤`/`≥`/`\tilde` 数学笔画误切；解耦单行溢出擦除与整块 `Block.bbox`、领回右栏误吸句末 `.` 并加 8pt 双栏右缘钳位；调优高公式批参数（`THRESHOLD=5, MAX_SEGMENTS=4, MAX_FORMULAS=18`）将 `04` 同口径费用压降 `48.2%`；`typesetter.py` 修复跨标签/`caption_no` 吞空格并补公式→中文标点微间距；`formula_align.py` 跳过裸字体开关 `\sf`；`service.py` 增设 `<mN/>=` 重复等号守卫。
  - **R5（`a7fe439`）**：回收公式右侧脱框黑板粗体（`→ ℝ`）、补全原 PDF 有 `→` 而 LaTeX 遗漏的 `\to`、恢复公式左缘擦断的相邻英文短词（如 `i` + `f` → `if`）并剥离虚假 `\dot`；升级 `PROMPT_VERSION = "v7"`；`links.py` 在 `<sup>` 内安全恢复 `Hfootnote/footnote` 跳转；`typesetter.py` + `export.py` 按几何相对坐标将公式裁图内引文热点精准映射到译文公式图（`02` 链接 `205→205`）；`align.py` 按垂直中心判行尾标点碰撞清零双栏栏缝残余 `]`；`extract.py`/`runner.py`/`cli.py` 提取文内显式缩写定义防先验幻觉并拦截 `sockets=socket` 同规范键单复数留英。
  - **R6（`5f97183`）及收尾补丁**：修复全仓导出 PDF 命名内部跳转（`LINK_NAMED`）纵坐标上下翻转 P0 Bug 与 `outline.py` 坐标系统一（`01`–`04` 共 1,639 条单/双语直接 `GoTo` 纵坐标翻转归零，误差 `< 0.0001pt`）；新增百分数事实锚点守卫、公式旁括号字面数值守卫（剥离相邻占位符编号防误判）、比较符结尾公式衔接守卫与译文 LaTeX 命令泄漏守卫；过滤 `缩写=纯英文多词全称` 误抽词条；`>= 10` 公式超高密度段独立成批防满载反转。
- **六轮质量演进实测**：
  - 交付成功率：`50% (3/6) → 100% (6/6)`（`342/342` 完成，R6 首试 `341/342 = 99.71%`，译文区坏字/PUA 全 `0/0`，溢出页全 `[]`）；
  - 双盲独立评分（6 篇等权均分）：**Codex `78.3 → 88.0 → 90.1 → 90.8 → 91.0 → 91.9`**（`+13.6`）；**AGY / Neon `69.0 → 85.3 → 89.0 → 91.2 → 91.7 → 93.0`**（`+24.0`，全员 90+）；
  - 线上最终状态（HW-SGP）：`PROMPT_VERSION="v7"`、`VALIDATION_VERSION=6`、`links.VERSION=6`、`EXPORT_CACHE_VERSION=15`，后端全量门禁 `1,170+ passed, 6 skipped`、`ruff`/`pyright`/`lint-imports` 0 错，前端 `40 files / 279 passed` + `vite build` 绿灯，公网 `/api/health` 200，镜像严格保留 2 代。
- **已明确归档的上游与轻模型边界（供 Q5 可选评估）**：原 PDF 矢量下划线/非标准数学字体致 MinerU LaTeX 错认、200 DPI 无文字层扫描 OCR 极限、原稿表文自身数值矛盾，以及轻模型 `deepseek-flash` 单次极偶发漏词/繁体字波动（长难数学或临床论文建议引导用户选择 `careful` 档）。

### 本轮完成：Q4 翻译质量与词库进阶加固初版（2026-11-24，已合入 `main` `d72fba1`）

- **方案与评审**：经主控初稿（v1）→ Codex（`apice/gpt-6.2-sol xhigh`）红队审改（v2）→ 独立 Neon（`gemini-4-neon xhigh`）二轮审改（v3）→ 主控拍板定稿（Final），完整设计见 [Q4 翻译质量与词库进阶加固设计方案](14-q4-translation-quality-and-glossary-tier2-design.md)。
- **核心落地（4 路零重叠 Codex 并发 + 阶段 2 端到端接线 + 主控自审收口）**：
  - **Q4a（校验、上下文与缓存）**：`PROMPT_VERSION = "v5"`、`VALIDATION_VERSION = 2`；新增确定性引文与数值上下标事实锚点校验（黑名单排除数组/容器索引、数学主语闭区间、含 `0` 区间与 4 位年份，同时严密保护 `as shown in [12]` 等高频句式）；局部 `<b>`/`<i>` 放宽合法拆合并增设整段误包守卫；支持 `</translations>` 缺省兼容结束标记与 4 条件竖线续行；章节切批以标题段为新批起点，支持同章相邻正文回指前文（上限 240 码点）；保留本文术语在 `system_prompt` 固定前缀，额外用户术语与公共参考词库按段号作用域注入；单段确定性 `cache_key` 扩展上下文与方言，缓存命中跳过不重发，单段补救通过后回写，语义降级不写共享缓存。
  - **Q4b（术语仲裁、抽取与前端面板）**：`domain/text.py` 与前端 `shared/text/termKey.ts` 共用 `term-key.fixture.json` 规范键（`acr:`/`lit:`/`word:`）；`TermSelection` 取消全文档 `_phrase_targets`，改为段内 `user → paper → library` 原文 span 占用仲裁；`normalize_terms` 逐段统计；`frequent_candidates` 支持技术词（`off-policy`、`k-means`、`TCP/IP`、`H2O`、`AP@0.5`、`GNNs`）与二/三元词组、`extract_terms` 截断丢弃残缺末行并兼容门控括号附注；前端 `terms.ts` 修复 ASCII 词界、`TCP/IP` 斜杠保护与同段更长译法实例级遮蔽。
  - **Q4c（跨块原子切分、导出截断提示与全链路接线）**：前后端同构 $O(N)$ 线性扫描器共用 `split.fixture.json`（扩充至 22 条夹具），保护公式、URL、数字引文与小数/版本号；导出仅统计真实丢行/空裁框译文页码，`EXPORT_CACHE_VERSION = 10` + 精简 `.meta.json` 原子提交、`POST /export/prepare` 返回 `overflow_pages` 与带 16 位 `snapshot` 的 `download_url`、`GET /export` 支持快照绑定与 Range/206、前端 `ExportDownloadLink.tsx` 同步覆盖三端溢出提示；管线、重翻与 CLI 统一接入按段术语、全文原序前文、并发策略与协议选择，`job_terms` 按规范键聚合并保持单行 `source` 唯一，OpenAPI 与 `schema.d.ts` 已同步再生。
  - **Q4d（双协议降级、断词与撕裂公式）**：OpenAI / Anthropic 仅对明确指向本次已发送参数的 HTTP 400 做单次受控降级与实例级单调记忆，区分参数名替换（`use_max_completion_tokens` 正常复用缓存）与思考/采样语义降级（标 `compatibility_fallback = True` 禁写共享缓存）；同步更新设置页与段落编辑器精翻文案；跨行复合词采用全文行内证据优先 + 9 个超高置信非音节前缀兜底，序数回收补齐合法英文序数与算符守卫；撕裂公式合并扩展 4 对互补边界、局部字号间距、散文硬屏蔽、粗体单字母向量转 `\mathbf{x}` 与符号归一化。
- **全量串行门禁实测（全绿）**：
  - 后端：`ruff format --check`（158 文件）、`ruff check`（0 错误）、`pyright`（0 错误 0 警告）、`lint-imports`（3/3 kept，90 文件 257 依赖）、`pytest -q`（**1,042 passed, 6 skipped**，较 Q3c 净增 336 项，含 `test_openapi_sync` 与 Q3c 原样回归）。
  - 前端（Node `v22.23.5`）：`npm run lint`、`npm run typecheck`、`npm test`（**40 files / 278 passed**，较基线净增 66 项）、`npm run build`（450ms）。
  - `git diff --check` 通过。
- **待确认**：本地分支 `feat/q4-translation-quality-tier2` 待用户确认后合入 `main`、推送远端，并按需重建 `app` + `web` 双镜像部署至 HW-SGP 与开展公开论文盲评。

### 部署：Q3 翻译质量与源文保真全包（Q3a + Q3b + Q3c）合入与上线（2026-11-24）

- **范围与提交**：在 `main` 线性合入三批后端/全栈升级并全部部署至 HW-SGP：
  - **Q3a**（`fa3099d`）：源文假上下标识别、严格子序列补字、补字后重算粗斜体、私用区安全裁图、漏检独立公式屏障与撕裂公式合并、行内摘要与算法标题保留、历史坏源付费前拦截、跨端可见 Unicode 切点对齐、`PROMPT_VERSION = v3`、`EXPORT_CACHE_VERSION = 8`。
  - **Q3b**（`ea3865d`）：Prompt 升至 `v4`（公式占位符随概念移动与中文定语倒装示例）、全文高频候选词入词库抽取（上限 45）、双标题/窄侧栏/悬挂列表拆块、评审稿等距行号排除、PDF 标题末两行均衡与网页 `text-wrap: balance`、`list_item` 首行缩进控制与 OpenAPI 同步、`EXPORT_CACHE_VERSION = 9`。
  - **Q3c**（`4a930c1`）：孤立 `Cc`/`Cf` 控制符 span 跳过且不连坐整块弃译；`recover_dropped_chars` 允许词尾逗号/句号并拒绝词内插标点；`choose`/`_restore_tags` 修复脚本标签内坏字；`merge_torn_formulas` 放行中间斜体变量；词库 `clean_zh` 先剥括号再切分隔符、`normalize_terms` 保留正文高频非结构标题词且兼容连字符/空白与全大写缩写、`TermMatcher` 补 `-es` 复数并禁跨句匹配且兼容复数缩写与连字符统一归并、`TermSelection` 按规范键用用户术语覆盖本文术语；`bad_source()` 排除化学式误杀；`front_matter_ids()` 识别结构化摘要与长正文句段；`EditIn`/`edit_segment` 拒绝纯空格提交；翻译缓存键补齐 `domain`/`section`/`provider_id` 且 `use_cache=False` 不覆写共享缓存；单段重试注入具体校验错误诊断并按尝试次数升序调度。
- **门禁验证**：后端在 `4a930c1` 串行通过 Ruff format/check（149 文件）、Pyright（0 错误）、import-linter（3/3 kept）、Pytest **706 passed / 6 skipped**（含 OpenAPI 同步与 Q3c 合成回归单测）及 `git diff --check`。Q3a+Q3b 合入前前端 Vitest/lint/typecheck/build 全绿，Q3c 无前端改动。
- **HW-SGP 上线结果**（源码 `4a930c1`）：
  - Q3a+Q3b 上线重建双镜像，Q3c 仅改后端故仅重建 `app`（沿用 `backend/Dockerfile` 运行阶段并复用已校验 `/app/fonts`，避免 1.8 GB 小机重跑字体精简 OOM）；
  - 构建前后进行中任务均为空（`进行中: []`）；数据库备份 `/data/backups/pre-q3c-4a930c1-20261124.db`（`PRAGMA integrity_check: ok`）；回滚镜像保留 `pdf-translator-app:before-q3c-20261124`（`02e5fb1b7f4a`）及 `before-q3-20261124` 双镜像；新 `app` 镜像 `d08237a60eab`（healthy），`web` 保持 `0f8e22ab8de3`。
- **公网与端到端验收**：
  - `/api/health` 返回 200（`data_dir_writable: true`）；`PATCH /api/segments/{id}` 提交纯空格返回 422（`译文不能为空`）；
  - 线上实测复杂英文学术 PDF 样例均 100% 首试通过且零失败段、单双语 PDF 导出正常，测试任务用完均已 `DELETE -> 204` 清理：
    - 35 页长篇样例：218/218 段（较 Q3a+Q3b 找回此前被孤立控制符连坐弃译的 1 段正文，全篇非译文本块仅余预期的 1 处伪代码块），导出单语 PDF 对应页已含完整中文译文；
    - 13 页密集公式样例：166/166 段（相较 Q2 大面积失败已彻底清零），此前残余的坏字（`\ufffd`）与词尾丢逗号均已清零，导出单语 PDF 全文 `\ufffd` 为 0。
  - 注：本次 Q3c 线上复验于工作日 11:03–11:07（DeepSeek 2x 高峰价 + 新缓存键冷启动）执行，单篇费用分别为 ¥0.1647 与 ¥0.1334（折合 1x 常规时段约 ¥0.0823 与 ¥0.0667）。
- **下一步**：后续可按第二档质量加固计划，择机评估批量失败快速止损、术语跨段一致性巡检与极少数跨样式撕裂公式合并的进一步增强。

### 本轮完成：Q2 翻译质量提升（2026-11-23）

- 本地分支 `feat/q2-translation-quality-upgrade` 的 `bae460b` 完成公共词库过滤与领域门控、首页结构和跨栏续接、公式清洗和共享裁图、Prompt v3 与中英混排规范化。导出缓存版本为 6。[Q2 设计与回归矩阵](12-q2-translation-quality-upgrade-design.md)
- 九项门禁串行通过。后端 Pytest 574 项通过、6 项跳过，三个导入契约通过。前端 Vitest 40 个文件、212 项通过，生产构建成功。翻译与解析回归使用假模型、合成 PDF 与本地夹具。
- 下一步由 Neon 复核混合领域回退、密集双栏版面和公式边缘的独立小字。导出文字流已去除多余的混排空格，PDF 阅读器可能依据视觉字距补出复制空格。

### 本轮完成：Q1 独立评审修复（2026-11-23）

- 拒绝或过滤的批次拆成单段重试。正文校验补齐中文引导语照抄、英文夹汉字例字、整段复读和普通斜杠词的覆盖。切段只在有资格的块之间建立续接。
- 导出恢复单行原字号，极矮框保留首行，回退公式仍可提取文本，引用链接按正文定位。乱码页沿用乱码状态，无字符块只统计生成的非空公式。导出缓存版本为 5。[Q1 设计文档](11-q1-translation-quality-guardrails-design.md)
- 前后端 9 项门禁串行通过。后端 Pytest 539 项通过、6 项跳过，前端 Vitest 40 个文件、212 项通过。

### 本轮完成：Q1 翻译质量护栏（2026-11-23）

- 在本地分支 `fix/q1-translation-quality-guardrails` 完成选页续接、局部斜体、模型结束状态与编号解析、富文本与正文复验、扫描公式、跨页链接、解析漏页和导出溢出修复。具体行为见 [Q1 设计文档](11-q1-translation-quality-guardrails-design.md)。
- 串行门禁全部通过。后端 Pytest 522 项通过、6 项跳过，含 OpenAPI 同步。前端 Vitest 40 个文件、212 项通过。导出缓存版本为 4。

### 部署：阶段 A 前后端确定性护栏与内存预算上线（2026-11-23）

- 设计文档：`docs/09-stage-a-backend-guardrails-design.md`（后端）、`docs/10-stage-a-frontend-guardrails-design.md`（前端）。
- 双 worktree 独立并行开发并经独立代码评审收尾后，线性合入 `main`（`5f617a9..25ee3c8`，12 条提交）：
  - **后端**（`cf07107..bb676b8`）：`POST /api/jobs` 流式写盘（前置磁盘空间与 `%PDF` 魔数检查、同盘 staging、`to_thread` 移出事件循环）、MinerU 流式 PUT + 屏蔽二次取消回收 + ZIP 流式限额与工作线程解压、`fusion` 按真实 PDF `page_idx` 对齐校验、导出改 `doc.save()` 流式写盘并与流水线共享容量为 1 的 PyMuPDF 重任务预算（含同快照去重、停机等待与过期清理互斥）、SQLite 统一显式 `write_tx`（`BEGIN IMMEDIATE` + 失败回滚 + 15s `busy_timeout` + 锁冲突重试，覆盖 `sqlite`/`cache`/`admin`）、阶段 JSON 原子落盘与 LLM SDK 连接池关闭。
  - **前端**（`782ef66..25ee3c8`）：`PdfCanvas` 单画布像素（桌面 600 万 / 移动端 400 万）与 4096 单边上限、卸载后显存归零及与 `PdfTextLayer` 协调的异步 `page.cleanup()`；`UploadPanel` 多选计页并发上限 2 与重选/卸载中止；`Caddyfile` 拆出 `/assets/*`、`/pdfjs/*` 独立静态块与 `Cache-Control` 分层（缺失旧分包返回真实 404，SPA 入口 `no-cache`）。
- 主仓全量门禁实测通过：后端 Ruff format/check、Pyright 0 错误、import-linter 3/3 kept、Pytest **487 passed / 6 skipped**（含 OpenAPI 同步）；前端 lint / typecheck / Vitest **40 files / 205 passed** / build（575ms）。
- HW-SGP 上线结果（源码 `25ee3c8`，双镜像均已更新）：
  - 构建前后进行中任务均为空（`进行中: []`）；数据库备份 `/data/backups/pre-stage-a-25ee3c8-20261123.db`（`PRAGMA integrity_check: ok`）；双回滚镜像保留为 `pdf-translator-app:before-stage-a-20261123`（`931bd5509c92`）与 `pdf-translator-web:before-stage-a-20261123`（`0af970a91068`）。
  - `app` 构建沿用仓库 `backend/Dockerfile` 运行阶段并复用已校验的 `before-stage-a-20261123` `/app/fonts`（19 文件 / 17 MB，避免 1.8 GB 小机重跑字体精简 OOM），新镜像 `b27d373cd237`（healthy）；`web` 新镜像 `128c667cbd0c`。
  - 公网验收：`/api/health` 200（`data_dir_writable: true`），`/` 与 `/guide` 200（`Cache-Control: no-cache`），首页引用新 `/assets/index-Bmt0kiY2.js`（`Cache-Control: public, max-age=31536000, immutable`），`/pdfjs/jbig2_nowasm_fallback.js` 200（`public, max-age=86400`），缺失资源 `/assets/nonexistent-old-chunk-test.js` 返回真实 404 且无长缓存。

### 部署：长论文阅读器虚拟化与原文 pdf.js 文字层上线（2026-11-23）

- 已将 `feat/reader-virtualization-text-layer` 快进合入 `main`（`ea435d7..4af35cc`）并推送至 `origin/main`，前端在 `main` 上全量复测 40 文件 / 199 项通过，`lint` / `typecheck` / `build` 全绿。
- 已按规范部署至 HW-SGP（源码与 web 镜像 `4af35cc`，只重建 `pdf-translator-web`，本轮无后端改动，`app` 保持 `d3491b5` healthy）：
  - 构建前后进行中任务均为空（`进行中: []`）；
  - 数据库备份 `/data/backups/pre-reader-virt-textlayer-4af35cc-20261123.db`（`PRAGMA integrity_check: ok`）；
  - 旧 web 镜像保留为 `pdf-translator-web:before-reader-virt-textlayer-20261123`（`588322689d97`），新镜像 `0af970a91068`。
- 公网验收：`/api/health` 200（`data_dir_writable: true`），`/` 200 且引用新资源 `/assets/index-DZcGV46h.js` 与 `/assets/index-Cdnwtaa9.css`（已验证含 `.pdf-text-layer` 与 `data-main-rotation` 规则），`/guide` 200。

### 本轮完成：长论文阅读器虚拟化、搜索防抖与原文 pdf.js 文字层（2026-11-23）

- 设计文档：`docs/08-reader-virtualization-and-text-layer-design.md`（方案 A1 + B1 + C1，选项 A，分两阶段实施）。
- 阶段 1（`virtual.ts`、`search.ts`、`ReaderView.tsx`、`styles.css`）：基于每页已知 PDF 尺寸的前缀和窗口虚拟化，移除每页独立的 `IntersectionObserver` 与远端页常驻 `.hit` 节点，对 `PageRow` 做 `React.memo` 与按页属性收敛，跳转定位改用精确几何计算（支持未挂载远端页直接跳转并保留实测页宽缩放校准）；预构建搜索归一化索引并加入 150ms 输入防抖（按 Enter 立即触发）。
- 阶段 2（`pdf.tsx`、`ReaderView.tsx`、`styles.css`）：新增 `PdfTextLayer`（基于 `pdfjs-dist@6.4.299` `TextLayer`，缩放时走 `update()` 不重提取文本流，切换/卸载时 `cancel()` 并处理加载中途缩放竞态、旋转页 `data-main-rotation` 对齐、跨 span 词级术语高亮与非取消异常自动降级）；原文页按画布 / 视觉底纹 `.hit-layer` / 文字层 / 链接层四层解耦，采用坐标命中与拖拽阈值保护驱动悬停联动与点击编辑；扫描页（`page.kind !== "native"`）继续保留块级 `.source-text-layer` 降级层。
- 双模型评审与修复闭环：经 Codex `gpt-6.1-sol` (high) 与 Grok `grok-4.7` (high) 独立评审，补齐缩放/切显示模式阅读锚点保持（`windowAnchor` + `useLayoutEffect`）、统一 `scrollTop - paddingTop` 布局坐标系、短距离平滑跳转 `pinnedPage` 防闪白与远距离 `auto` 直达、`data-main-rotation` 90°/180°/270° 旋转样式、原生页跨 span 词级术语高亮、`PdfTextLayer` 失败清理与块级降级、中文 IME `isComposing` 过滤、非连续 `page.index` 双向映射及 `pagehide` 同步落盘。
- 验证结果：Node `v22.23.5` 下前端全量 Vitest 40 文件 / 199 项通过（较基线 175 项新增 24 项测试），`npm run lint`、`npm run typecheck`、`npm run build` 均通过，已合入 `main` 并上线。

### 部署：统一任务卡片与逐篇编辑入口上线（2026-11-23）

- 首页统一任务卡片、逐篇重试与逐篇“编辑译文”入口已部署到 HW-SGP（源码与 web 镜像 `d6e5198`）。
- 部署前进行中任务为空；数据库备份 `pre-task-cards-d6e5198-20261123.db` 完整性 ok；旧 web 镜像保留为 `before-task-cards-20261123`。
- 只重建 web（本轮无后端改动，app 保持 `d3491b5`）。公网 `/api/health` 200，首页引用新 `/assets/index-C0eRKL2B.js` 且包含新入口文案，`/guide` 200。

### 首页逐篇编辑入口（2026-11-19）

- 在已完成论文的下载按钮左侧增加“编辑译文”，单篇和同次上传的论文共用一个入口。验收覆盖编辑其中一篇、保存并重新下载，以及同组另一篇的内容检查。
- 同一页面会话内从阅读器返回时，各卡片保留已选下载格式。刷新页面后默认左原文、右译文。
- 桌面 1440×900 和手机 390×844 均经浏览器连接本地 FastAPI 实测。修改甲文一段并保存后，单语 PDF 和双语 PDF 的译文侧都含新文本，乙文的段落和导出文件仍为原译。手机上编辑和下载同排，无横向溢出。前端测试、Lint、类型检查和构建通过。

### 首页任务卡片统一（2026-11-19）

- 修正单篇和多篇的背景、列宽和格式选择位置不同的问题。验收时比较两类卡片的文件名、状态及操作按钮在桌面和手机上的实际位置。
- 两类上传共用同一张卡片模板和文件行。页数、费用与保留期统一放在文件名下，下载格式放入卡头。
- Playwright 在 1440×900 和 390×844 视口核对两类卡片的文件名、状态、下载和删除按钮，四处横坐标差均为 0 像素。每个视口验证 4 次 PDF 下载，覆盖独立选格式及同格式再次下载。前端 174 项测试、Lint、类型检查和构建通过。

### 首页任务列表调整（2026-11-19）

- 根据使用反馈，把单篇和同次上传的多篇合并到“我的任务”，按上传时间倒序显示。多篇以实际上传时间标识，显示可下载篇数与总篇数。分开显示后，每篇仍在列表中。
- 格式选择在有译文可下载时出现，默认左原文、右译文。查询故障只提示在对应文件旁。失败可直接重试，取消后可重新开始，服务端返回的新状态立即更新。
- 用于预览的文件名均为虚构示例。桌面 1440×900 和手机 390×844 验证了文件排序、两种格式、同格式重复下载、重试、重新开始和取消分组。前端 174 项测试通过，Lint、类型检查和构建通过。后端 435 项通过、6 项跳过。

### 第一版批次界面目标（2026-11-19）

- 将“我的批次”移到上传区下方，合并批次内成员与个人任务列表的重复条目。
- 每批次选一次 PDF 格式，默认左原文、右译文。支持逐篇下载和同格式再次下载。
- 验收桌面与 390×844 手机布局，覆盖混合任务状态、查询中、临时失败和服务器确认不可用。

### 第一版批次界面完成记录（2026-11-19，`fe6d3ee`）

- “我的批次”紧接上传区，每篇批次论文只在所属批次展示。移除分组后仍可在“我的任务”找到论文，包括已退出最近 30 个任务列表的批次成员。
- 批次共用一个下载格式，默认双语（左原文、右译文），可切换为仅译文。准备期间锁定格式并防止重复发起。下载启动或准备失败后可再次选择同一格式下载，反馈独立于按钮。
- 批次进度按任务分别查询，暂时失败每 3 秒重试。查询中和暂时失败不会计入已处理进度。收到 404 或 410 时才显示任务已过期或已删除。
- 前端 173 项测试通过，Lint、类型检查与构建通过。Playwright 使用合成任务通过 1440×900 桌面与 390×844 手机验收。两种视口均无横向溢出，核对了两种格式参数、同格式连下两次、按钮尺寸、混合状态和移除分组。
- 后端 435 项测试通过、6 项跳过。Ruff、Pyright 和 import-linter 检查通过。
- 本轮完成逐篇下载，整批 ZIP 下载列入后续事项。

### 本轮补充（2026-11-18）

- 首页“我的任务”和“我的批次”的已完成任务均提供下载类型选择：默认双语（左原文、右译文），也可仅下载译文。
- 准备 PDF 时锁定类型与重复点击；切换类型后重置下载状态，避免上一种格式的状态阻塞另一种格式。
- 前端 Node 22 全量测试 167 passed，lint、typecheck、build 通过；桌面及 390×844 手机浏览器验证下载参数和无横向溢出。
- 已重新部署到 HW-SGP，包括此前已提交但未上线的版式、超时、任务并发保护和自定义地址安全修复。
- 功能提交 `d3491b5`，GitHub CI `44334005571` 前后端均成功；后端全量 435 passed、6 skipped。
- 服务器字体构建遇到内存压力，已停止并验证终止遗留字体进程；本地复用同版本中文精简字体并生成新增 Tinos 斜体/粗斜体，与前端产物一并封装上传（SHA256 校验通过），运行镜像沿用仓库运行阶段。未修改仓库 Dockerfile。
- 发布后两个容器版本均为 `d3491b5`，健康检查 200；构建和重启前均检查 active jobs=0，数据库备份完整性 ok，旧镜像保留。
- 线上现有已完成任务实测两种导出成功：双语 PDF 页数与原文一致、首屏宽度为双倍且左半文字与原文一致；仅译文 PDF 页数和页面宽度正常。没有调用付费翻译或新增测试提交。

### 本轮补充（2026-11-15）

- 下载 PDF：顶部下载入口现在共享显示准备中、已开始和失败重试状态，菜单项不会各自隐藏进度。
- 段首排版：普通正文第一块统一按两个汉字宽度缩进；列表项、标题、图注和跨栏后续块继续不缩进，网页与 PDF 规则保持一致。
- 相关提交：`86e5df2`、`7f3bf1b`。

### 本轮目标（2026-11-15）

- 按整体审计顺序修复：任务修改并发覆盖、MinerU 全流程超时、运行配置范围、用户自定义 LLM 地址 SSRF、导出与任务列表异常状态、公式/链接/斜体边界。
- 验收标准：每项独立 Git 提交；后端测试、lint、pyright、import-linter 和前端 lint、typecheck、build 通过；不部署、不重启服务器。

### 本轮目标（2026-11-14）

- 完整修复版式样式边界：粗体字体回退保持字重、斜体端到端保留、章节标题识别扩展、混合字号与扫描页标题稳健处理，并补充颜色/目录/孤立文字的回归保护。
- 验收标准：原文局部粗体与斜体在翻译输入、网页阅读器和导出 PDF 中保持；常见编号标题不因解析器类型差异降级为正文；缺字回退不改变字重；后端测试、lint、typecheck 与前端 lint、typecheck、test、build 通过。

### 本轮目标（2026-11-12）

- 修复普通 `Medium` 字体被误判为粗体，避免译文无故切换到黑体粗体。
- 调整整块粗体正文的译文排版：译文使用普通正文宋体/Times；标题、图表编号和局部 `<b>` 仍按结构或强调使用对应粗体。
- 验收标准：网页阅读器与导出 PDF 的角色映射一致；未翻译的粗体正文回退文本保留原有字重；相关单元测试、lint、typecheck、build 通过。

## 当前状态

- 仓库：`D:\\01\\pdf-translator`
- 分支：`main`
- 本地功能代码以 `git log -1 -- docs/WORKLOG.md` 为准，本轮记录统一任务卡片上线与部署。
- 最新交接文档版本以 `git log -1 -- docs/WORKLOG.md` 为准。
- GitHub：<https://github.com/weandy/pdf-translator>
- 线上地址：<https://pdf.667786.xyz/>
- 线上部署：Docker，宿主机 `127.0.0.1:18765` 反向代理到 web 容器
- 线上源码版本：`d6e5198`
- 线上 app 镜像：`d3491b5`（本轮未重建）
- 线上 web 镜像：`d6e5198`
- 数据库：Docker 命名卷中的 SQLite `/data/app.db`
- 任务文件：`/data/jobs/`
- 管理入口：`/weandy/login`（实际路径由 `PDFTR_ADMIN_PATH` 控制）
- 管理 API：仍为 `/api/admin/...`
- 旧入口：`/admin/login` 已由 Caddy 返回 HTTP 404

## 最近完成

### 阅读器与翻译

- 文字选择、复制、全文搜索。
- 手机工具栏和编辑面板优化。
- 完成后的阅读、下载中文版、下载双语版入口。
- 多文件上传批次管理。
- 重翻后费用和状态即时刷新。
- 统一错误卡片、失败重试、设置未保存保护。
- 翻译失败段落最多 10 次尝试（含首次），单次模型请求最多 30 秒，并有总时限。
- 失败任务不会带着未翻译正文标记为完成。
- 已有可用译文重翻失败时保留旧译文。
- 原文局部粗体完整保留到网页和导出 PDF。
- 修复 `Medium` 字体被误判为粗体；整块粗体正文的译文使用普通宋体/Times，标题、图表编号和局部 `<b>` 继续按结构或强调使用粗体；网页与导出 PDF 角色一致。
- 翻译校验拒绝原文没有 `<b>` 时模型新增粗体，避免整块粗体正文的译文被重新包成粗体。
- MinerU 云端解析单次等待上限改为 5 分钟，超时自动重新提交，最多两次重试；排队、解析、整理结果状态分别展示。
- PDF 下载先准备或命中服务端缓存，再启动浏览器下载；下载按钮显示准备、已开始和失败状态，防止慢网络下重复点击。
- 字体试验：正文和局部 `<b>` 禁止浏览器合成粗体，强制继承当前字体族；增加 Source Han Serif/Nimbus Roman 作为系统回退，保持内嵌字体 Regular/Bold 配对。
- 修复 MinerU 对 small-caps 罗马数字章节标题的漏识别：`I. INTRODUCTION` 这类普通 `text` 块会提升为章节标题，参与正确的章节上下文和标题排版。
- 修复 small-caps 章节标题字号取中位数导致译文标题偏小：标题块改用字符最大字号，正文字号规则不变。
- 完善版式样式边界：粗体缺字时保持同字重回退；斜体从字符融合、翻译标签校验到网页与 PDF 排版完整贯通；标题识别支持多 span、两行、数字/字母/中文编号；标题字号对异常大字稳健；扫描页标题使用行框估算；解析器标题层级进入目录；孤立文字过滤竖排字符。
- 新增 Tinos 斜体与粗斜体字体资源，并同步更新字体接口和前端类型。
- 章节、内部链接和搜索跳转会短暂高亮并淡出。
- 修改记录支持差异突出显示。

### 术语功能

- 术语面板支持本文术语、用户术语库、公共词库和批量重翻。
- 点击术语现在按具体词所在块定位，不再默认跳到段落首块。
- 原文和译文中命中的术语会高亮。
- “可能不一致”已改为“可能未采用推荐译法”。
- 该提示是推荐译法的字面匹配，不是语义一致性判断。
- 匹配会忽略 `<b>`、`<i>`、`<sup>`、`<sub>` 和公式占位符，避免富文本造成误报。

### 统计与管理员

- `/stats` 只公开最近 7 天聚合统计，首页不展示统计卡片。
- 公开统计包含 7 天汇总、今日数据、更新时间、日期数值图表和跨日访问浏览器去重。
- 管理提交明细支持标题、IP、时间、页数、状态搜索、分页和日期筛选。
- 管理会话通过环境变量配置，当前线上会话有效期为 30 天。
- 提交账本新增当前阶段、失败类型、脱敏失败原因、可重试状态和失败更新时间。
- 旧数据库中仍存在的失败任务会在启动时补齐安全失败摘要。
- 普通页面只使用有限性数据告知，不直接列出具体长期保存字段。

## 重要设计约束

- 后端依赖方向：`api → pipeline → 能力模块 → domain`；能力模块之间不能互相导入。
- 前端依赖方向：`app → pages → features → entities → shared`；features 之间不能互相导入。
- API 类型必须从 OpenAPI 生成，不能手写 `schema.d.ts`。
- MinerU 默认 Token 放部署机 `.env` 的 `PDFTR_MINERU_API_TOKEN`；用户 Token 可覆盖服务器默认值。
- PaddleOCR-VL 适配器保留在后端，但界面不开放。
- 论文正文、PDF 和译文按任务保留期清理；公开统计只返回最近 7 天聚合数据。
- 不把真实论文内容、真实文件名、API Key、Token、Cookie、密码写入代码、测试或交接文档。
- 每次服务器构建或重启前，必须确认 SQLite 中 `running/queued` 任务为 0。
- 服务器远程操作必须使用 Netcatty MCP，不使用本地 SSH 绕过。
- 前端格式化行宽约 120，只格式化实际修改过的文件。

## 验证结果

最近一轮术语修改：

- 前端全量测试：162 passed。
- 术语、阅读器定向测试通过。
- `npm run lint` 通过。
- `npm run typecheck` 通过。
- `npm run build` 通过。
- GitHub CI run `43023389189` 成功。
- 线上 web 资源已切换到 `9f405e0`，容器健康，部署时 active jobs 为 0。

本轮字体修复（本地提交 `363ac78`）：

- 后端全量测试：405 passed，6 skipped。
- 后端 `ruff check`、`ruff format --check`、`pyright`、`lint-imports` 通过。
- 前端 `npm run lint`、`npm run typecheck`、`npm run build` 通过。
- 前端纯逻辑 Vitest（`--environment node`）：6 passed。
- 默认 jsdom Vitest 在当前 Node `v20.19.2` 启动 worker 时被 `undici` 使用 Node 22 才有的 `zstd` 解压 API 拦截，浏览器环境测试仍待升级 Node 后补跑。

本轮 MinerU 与下载体验优化（本地提交 `a29eef9`）：

- 后端全量测试：406 passed，6 skipped。
- 后端 `ruff check`、`ruff format --check`、`pyright`、`lint-imports` 通过。
- MinerU 契约测试：10 passed；状态展示测试：1 passed。
- 前端 `npm run lint`、`npm run typecheck`、`npm run build` 通过。
- 默认 `npm test` 仍受当前 Node `v20.19.2` 与 `undici` 的 Node 22 `zstd` API 兼容问题影响，未能启动完整 jsdom worker。

本轮版式边界修复（本地提交 `c820231`、`853e0ad`、`dcb9af5` 及后续集成提交）：

- 后端全量测试：418 passed，6 skipped。
- 后端 `ruff format --check`、`ruff check`、`pyright`、`lint-imports` 通过。
- 前端 `npm run lint`、`npm run typecheck`、`npm run build` 通过。
- 前端 Node 环境定向测试：3 个文件、16 passed；完整 jsdom 测试仍受当前 Node 20 与 `undici` 的 Node 22 `zstd` API 兼容问题影响。

本轮整体审计修复（`2d44151`、`54f212f`、`75a0bb5`、`9923554`、`28372bd`、`30be071`）：

- 任务编辑、重翻、撤回和重试增加完成状态与任务级互斥，避免后台翻译覆盖用户修改。
- MinerU 上传、轮询、下载统一使用一次解析总时限，上传取消任务会等待清理。
- 并发、队列、上传、页数和保留期配置增加范围校验。
- 用户自定义 LLM URL 默认拒绝内网、本机、元数据和带凭据地址；本地模型需显式开启私网地址配置。
- 导出准备增加服务端 5 分钟超时和客户端超时提示；任务列表与批次对瞬时失败、过期任务容错。
- 公式跨行裁剪、跨块链接归属和小写字体名斜体识别增加修复与回归测试。
- 后端全量测试：435 passed，6 skipped；ruff、pyright、import-linter 通过。
- 前端 lint、typecheck、build 通过；完整 jsdom 测试仍受 Node 20 与 `undici` 的 Node 22 `zstd` API 兼容问题影响。

## 当前已知限制

- 术语“未采用推荐译法”仍然是字面匹配，不能判断同义词或真正的语义错误。
- 术语面板会标记相关段落以便浏览，但点击定位已经落到具体词。
- 公开统计不提供论文名称、IP 或任务明细。
- 管理页面只保护管理 API 和管理明细，不等同于全站账号系统。
- 项目是单副本内存调度；多副本部署需要 Redis、持久化队列和共享事件总线。
- 本地 Windows 的部分 MuPDF 临时文件 e2e 受文件句柄限制，Linux CI 是完整验证环境。

## 下一步候选事项

开始新功能前，先在本文件追加目标和验收标准，再编码：

1. 整批 ZIP 下载、批次信息跨设备恢复和批量重试。
2. 并发编辑版本锁，避免后台翻译与手动编辑互相覆盖。
3. 上传幂等键、接口限流和自定义 LLM Base URL 的 SSRF 防护。
4. 浏览器级 smoke test，覆盖上传、任务完成、阅读器和导出。

## 新对话继续开发模板

```text
继续维护 PDF Translator 项目。

项目路径：D:\\01\\pdf-translator

请先读取：
1. AGENTS.md
2. docs/WORKLOG.md
3. 最近 5 条 git log
4. git status

当前目标：
（填写这次要修改的功能）

要求：
- 不重复实现已完成内容
- 先分析现状和影响范围，再直接实施
- 每个完整优化独立 Git commit
- 补充必要测试并完成 lint、typecheck、build
- 修改接口时重新生成 OpenAPI 和前端类型
- 涉及服务器时使用 Netcatty，并在构建、重启前确认没有 running/queued 任务
- 不读取或输出 .env、Key、Token、Cookie、密码和真实用户论文
```

## 收尾更新规则

完成任务时更新本文件：

1. 修改“最新提交”和“线上部署版本”。
2. 在“最近完成”增加功能和验证结果。
3. 在“已知限制”记录未解决的边界。
4. 在“下一步候选事项”移除已完成项目或补充新的事项。
5. 和代码一起提交并推送，提交说明使用中文前缀。

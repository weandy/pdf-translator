# PDF 翻译数学保真、段落防重与保版排版修复设计方案（批次 A / B / C）

> 状态：已定稿待实施（基于 `main` 提交 `1adc08c`，在独立 git worktree `D:\01\pdf-translator-tr-fix`、分支 `fix/translation-fidelity-layout` 实施）
>
> 协同模式：3 路零重叠并行实现（Codex `apice/gpt-6.2-astra xhigh`） → 主 Agent（Neon）胶水接线与深度评审 → Codex 定向修复评审缺陷 → 全量 9 项串行门禁验收

---

## 1. 目标与边界

本设计覆盖双盲分析与源码交叉核验后确认的全部确定性工程缺陷（批次 A + B + C），共 9 项：

| 编号 | 优先级 | 缺陷摘要 | 归属泳道 |
|---|---|---|---|
| `TR-002` | P1 | 行内公式多字母数学变量前缀丢失/撕裂（如 `machine_{j,m}` 截成 `mach\|ine`）与紧行距下标 `j`/`n` 底部被中线裁切 | 泳道 1（数学保真） |
| `TR-003` | P1 | 公式底部下标字形（如 `RPT_{jm}` 的 `jm`）逃逸到正文层，产生游离斜体 `<i>jm</i>` | 泳道 1（数学保真） + 主 Agent `build.py` 接线 |
| `TR-004` | P1 | 纯文本块中黑板粗体/向量空间记号（如 `Score ∈ ℝ^o`）未被识别为行内公式，退化为普通文字 `Score∈Ro` | 泳道 1（数学保真） + 主 Agent `build.py` 接线 |
| `NEW-03` (`TR-014`) | P2 | 行内公式 bbox 右缘在几何上多包入紧随的正文非数学字体尾逗号，造成中文侧双重逗号 `,，` 或括号内多余逗号 | 泳道 1（数学保真） |
| `TR-001` | P1 | 跨栏/跨页续段与同页物理重叠重复块/孤儿块导致同页同一段译文重复出现；另加固 `prev_context` 防吸纳上文 | 泳道 2（去重与引文） + 主 Agent `build.py` 接线 |
| `TR-010` | P2 | 方括号数字引文丢逗号（`[29,30]` 变 `[29 30]`）与译后同组引文乱序（`[30,29]`）未拦截 | 泳道 2（去重与引文） |
| `TR-008` | P2 | 栏内左对齐短小节标题框被锁死在英文原文窄框宽（如 86pt），导致中文长标题被迫缩字至 5.5–6.6pt | 泳道 3（保版排版与双端同步） |
| `TR-009` | P2 | 同栏相邻无障碍续接 `SegmentPart` 分开排导致 30pt+ 视觉空白断层；跨栏拆块需优先断在强句末标点并前后端一致 | 泳道 3（保版排版与双端同步） |
| `TR-013` | P2 | 跨样式独立半角标点（如 `<i>,</i>`）绕过行首禁则掉到下一行行首；短括号参数组（如 `U(1, 3)`）被劈断 | 泳道 3（保版排版与双端同步） |

**明确不在本轮范围（不可改动）**：
- `TR-005`（表体文字）、`TR-007`（光栅图内标签）：按 `AGENTS.md` 硬性设计取舍维持不变。
- `TR-006`（Algorithm 1 算法块与 `CODE` 保护）：保持现状，保留既有回归测试。
- 批次 D（`TR-011` 术语抽取消歧、`TR-012` 翻译 Prompt 调优）：触发 `PROMPT_VERSION` 递增与盲评门禁，待本分支合入后独立 PR 推进。

---

## 2. Git Worktree 隔离与零重叠泳道分工

### 2.1 分支与工作树隔离
- 主仓库 `D:\01\pdf-translator` 保持在 `main` 不动，避免干扰并行开展的其他前台/管理后台优化任务。
- 本轮全部开发与测试均在独立 worktree `D:\01\pdf-translator-tr-fix`（分支 `fix/translation-fidelity-layout`，起点 `1adc08c`）中完成。
- `backend/.venv` 由 `uv sync --frozen --offline` 在新 worktree 独立生成（editable 指向新 worktree 源码）；`frontend/node_modules` 通过目录联接复用主仓依赖（不修改主仓任何跟踪文件）。

### 2.2 严格文件边界与并发纪律
- **架构红线**：严格遵守 `backend/pyproject.toml` 的 `lint-imports` 规则——`fusion`、`translation`、`rendering` 之间**绝不互相导入**，仅通过 `domain` 交换数据；前端遵守 `features -> entities -> shared` 单向依赖与 `@/` 路径别名。
- **内存红线**：本机 30 GB 内存常态占用 85%+，3 路并行子 Agent **只编写各自独占文件与对应单测**，只允许极轻量语法检查（如 `python -m py_compile`），**严禁并发运行 `pyright` / `ruff` / 全量 `pytest` / `npm run build` / `git add` / `git commit`**。
- **公共文件留给主 Agent 收口**：`fusion/build.py`、`pipeline/usecases.py`（`EXPORT_CACHE_VERSION`）、`translation/batching.py` 中的 `VALIDATION_VERSION` 常量值与 `tests/unit/test_fuse_synthetic.py` 均由主 Agent 在 3 路返回后统一接线、统一递增版本号并串行执行全量门禁。

| 执行者 | 独占修改文件（严格零重叠） |
|---|---|
| **泳道 1：数学保真** | `backend/src/pdftr/fusion/formula_align.py`<br>`backend/src/pdftr/rendering/background.py`<br>`backend/tests/unit/test_formula_align.py`<br>`backend/tests/unit/test_q2_pdf_regressions.py` |
| **泳道 2：去重与引文** | `backend/src/pdftr/fusion/relocate.py`<br>`backend/src/pdftr/fusion/text_fix.py`<br>`backend/src/pdftr/translation/batching.py`（**暂保持 `VALIDATION_VERSION = 6` 不改，由主 Agent 统一升至 7**）<br>`backend/src/pdftr/translation/segmenter.py`<br>`backend/src/pdftr/translation/service.py`<br>`backend/tests/unit/test_relocate.py`<br>`backend/tests/unit/test_text_rules.py`<br>`backend/tests/unit/test_segmenter.py`<br>`backend/tests/unit/test_translation_service.py` |
| **泳道 3：保版排版与双端同步** | `backend/src/pdftr/rendering/export.py`<br>`backend/src/pdftr/rendering/typesetter.py`<br>`backend/tests/unit/test_export_split.py`<br>`backend/tests/unit/test_typesetter.py`<br>`frontend/src/shared/text/split.ts`<br>`frontend/src/shared/text/split.fixture.json`<br>`frontend/src/entities/document/model.ts`<br>`frontend/src/features/reader/ReaderView.tsx`<br>`frontend/src/shared/fit/fit.test.ts` |
| **主 Agent 串行收口** | `backend/src/pdftr/fusion/build.py`<br>`backend/src/pdftr/pipeline/usecases.py`（`EXPORT_CACHE_VERSION` 15 → 16）<br>`backend/src/pdftr/translation/batching.py`（`VALIDATION_VERSION` 6 → 7）<br>`backend/tests/unit/test_fuse_synthetic.py` 及版本号断言同步 |

---

## 3. 泳道 1 详细设计：数学保真闭环（`TR-002` + `TR-003` + `TR-004` + `NEW-03`）

### 3.1 公共判定辅助与常量扩展（`fusion/formula_align.py`）
1. 扩展黑板体字体正则：
   - `_BLACKBOARD_FONT = re.compile(r"MSBM|BBOLD|DSROM|BBO|DoubleStruck|Blackboard", re.I)`
2. 新增 Unicode 数学字母与黑板体识别辅助：
   - `_EXTRA_MATH_LETTERS = frozenset("\u210e\u210f\u2113\u2118\u212f\u2134")`（含 `ℎ` `U+210E` 等常见 Letterlike 数学字母）
   - `_BLACKBOARD_UNICODE_MAP`：覆盖 `ℝ/ℕ/ℤ/ℚ/ℂ/ℙ/ℍ`（`U+2102, U+210D, U+2115, U+2119, U+211A, U+211D, U+2124`）及 `U+1D538–U+1D550` 数学双线大写字母，映射至对应 ASCII 大写字母（或通过 `unicodedata.normalize("NFKC", ch)` 校验映射结果在 `A-Z`）。
   - `_is_math_glyph(ch: Char) -> bool`：命中 `MATH_FONT.search(ch.font)`、或 `0x1D400 <= ord(ch.c) <= 0x1D7FF`、或 `ch.c in _EXTRA_MATH_LETTERS`、或 `_blackboard_letter(ch) is not None`。
   - `_blackboard_letter(ch: Char) -> str | None`：若 `ch.c` 属于黑板 Unicode 集合，返回其 NFKC 单大写字母；或若 `_BLACKBOARD_FONT.search(ch.font)` 且 `ch.c in "RNZQCKEPH"`，返回 `ch.c`；否则返回 `None`。
   - `_is_strict_superscript(ch: Char, body_size: float, baseline: float | None, row_cy: float) -> bool`：要求 `ch.size <= 0.85 * body_size` 且（若 `baseline is not None and ch.origin_y is not None` 则 `baseline - ch.origin_y >= 0.15 * body_size`，否则 `row_cy - ch.bbox.center[1] >= 0.12 * body_size`）。

### 3.2 原生数学词全量吸附与下标所有权守恒（`TR-002` + `TR-003`）
在 `clean_formula_lines(block, chars)` 中，位于 `merge_torn_formulas` / `_split_native_if_formulas` 之后、`_clamp_line_formulas` 之前新增两步原生几何修复：

1. **`_expand_math_token_bounds(line: LayoutLine, chars: Sequence[Char], rows: Sequence[Sequence[Char]]) -> LayoutLine`**：
   - 将 `line` 匹配到对应的视觉行 `row`（按 `text` 字符中位 `y` 或 `line.bbox.center[1]` 匹配最近视觉行；计算 `line_size, line_baseline = main_metrics(row)`）。
   - 对行内每个 `located=True` 的 `inline_formula`：
     - 先收集公式当前命中的原生非空字符 `f_chars = [c for c in row if span.bbox.contains_point(*c.bbox.center, 0.5)]`；同时收集水平中心落入 `[span.bbox.x0 - 0.15 * line_size, span.bbox.x1 + 0.15 * line_size]`、属于同一视觉行 `row` 且满足 `script_glyph(c, line_size, line_baseline)` 的附属上下标字符（如 `RPT_{jm}` 贴在底缘的 `j, m`），但不得越过前一个/后一个公式 span 边界，且严禁吸收正文正常大小英文词。
     - **向左吸附被截断的连续数学词**：若 `f_chars` 最左非空字符 `first_c` 属于 `_is_math_glyph(first_c)`（或为斜体拉丁字母且公式含下标/数学字体），在同视觉行 `row` 中向左扫描紧邻字符 `prev_c`：只要 `first_c.bbox.x0 - prev_c.bbox.x1 <= 0.22 * max(first_c.size, prev_c.size)`、`_is_math_glyph(prev_c)`、且 `prev_c` 不落入前一个 `inline_formula` 的 bbox，就持续并入该数学词前缀（如 `mach` + `ine_{j,m}`、`machin` + `e_{j,m}`），直至遇空格、正文非数学字体字符、左括号/标点或前一个公式边界。
     - **向右吸附被截断的连续数学词及附属下标**：对称处理右缘——若 `f_chars` 最右字符属于数学词，向右继续吸附紧邻间距 `<= 0.22 * size` 的同数学字体字母与紧随其后（间距 `<= 0.25 * size`）的 `script_glyph` 上下标字符（例如把遗留在右缘外的 `jm` 一并纳入公式归属集）。
     - **相邻两公式间数学碎片清理**：若 `prev_span` 与 `cur_span` 之间存在被 MinerU 切碎的纯文本 span，且其全部或末尾字符已被 `cur_span` 向左吸附（或开头字符已被 `prev_span` 向右吸附，如 `machin` 残片或游离 `jm`），同步裁剪相邻 `text` span 的 `content` 与 `bbox`（裁剪后若 `content.strip()` 为空则移除该冗余 `text` span）。
     - **扩展公式 bbox**：将公式 `bbox` 更新为覆盖所有归属字符（`owned_chars`）真实外接框与原 `span.bbox` 合理融合后的边界（注意左右取真实归属字符覆盖 `min/max`，上下若原框偏矮导致未包住归属的正常高度字形/下标 `j`/`jm`，先扩展到归属字符真实 `[y0, y1]`，后续由安全垂向限位函数约束防侵入邻行）。
     - 若向左吸附了纯字母前缀（NFKC 归一化为 `[A-Za-z]+`），且公式原 LaTeX 开头恰好匹配吸附前公式首词剩余部分（如原 LaTeX 以 `ine_` 开头而完整词是 `machine`），可安全补回前缀；否则保留原 LaTeX 仅修正 `bbox` 与相邻文本（避免破坏复杂 LaTeX）。

2. **下标防逃逸出口契约（供主 Agent 在 `build.py` 调用）**：
   - 在 `formula_align.py` 导出公开函数：
     ```python
     def exclude_formula_chars(
         chars: Sequence[Char],
         formula_boxes: Sequence[Rect],
         body_size: float,
         line_baseline: float | None = None,
     ) -> list[Char]:
     ```
     语义：过滤掉应由行内公式独占的字符——除 `any(e.contains_point(*ch.bbox.center, 0.5) for e in formula_boxes)` 之外，若某字符水平中心落入任一公式 `[e.x0 - 0.15 * body_size, e.x1 + 0.15 * body_size]`、纵向中心落在公式主基线 ± `0.85 * body_size`（或 `[e.y0 - 0.35 * body_size, e.y1 + 0.45 * body_size]`）内，且 `script_glyph(ch, body_size, line_baseline)` 为真，也一并排除出正文 `span_chars`，从根源彻底阻断 `TR-003`（图里有 `jm`、正文又漏一份 `<i>jm</i>`）。

### 3.3 几何尾标点剥离（`NEW-03` / `TR-014`）
在 `clean_formula_lines()` 末尾（垂向限位前）新增 `_trim_trailing_prose_punct(line: LayoutLine, chars: Sequence[Char], rows: Sequence[Sequence[Char]]) -> LayoutLine`：
- 仅针对 `located=True` 的 `inline_formula`：
  - 取出落在 `span.bbox` 内同视觉行非空字符按 `x0` 排序 `owned`。要求 `len(owned) >= 2`。
  - 考察最右字符 `tail = owned[-1]`：
    - 必须满足 `tail.c in {",", ".", ";"}`；
    - 必须属于**正文非数学字体**：`not MATH_FONT.search(tail.font)` 且 `not tail.italic`（排除数学斜体标点）且非 `script_glyph`；
    - 公式本身 LaTeX 的括号必须配平（`not _delimiters(span.content)[1]`），且 `owned[:-1]` 的可见字符 `(` / `[` 括号计数已闭合（不剥离未闭合括号内部逗号，例如 `f(x, y)` 尾字符是 `)` 本就不命中，`(x,` 未闭合也不命中）；
    - 若 `tail.c == "."`，额外要求 `owned[-2].c` 不是数字且不是缩写字母后点（仅防句末逗号/分号/句号误吞；对 `,` 与 `;` 最常见场景重点放行，`.` 仅当紧随 `)` / `]` / `}` 或非单字母结尾且其右侧已无公式字符）；
    - 确认 `tail` 确实是句法尾标点（右侧相邻无 `text` span，或右侧相邻 `text` span 为正文——若右侧相邻 `text` span 开头**已存在**同标点如 `,`，直接把 `tail` 从公式 `bbox.x1` 缩出不重复追加，彻底消除 `,，`；若右侧 `text` span 开头**没有**该标点且公式 LaTeX 末尾也不含该标点，则收紧公式 `bbox.x1 = max(c.bbox.x1 for c in owned[:-1]) + 0.2`，并把 `tail.c` 归还到右侧 `text` span 开头或新建紧邻 `text` span）。

### 3.4 邻行异属字形安全垂向限位与裁图护栏（`TR-002` 下标切钩）
1. **升级 `_formula_box()` 与 `_clamp_line_formulas()`（`fusion/formula_align.py`）**：
   - `_clamp_line_formulas(line, chars, block_centers, block_rows=None)`（`block_rows` 可选，默认从 `visual_lines(chars)` 推导，保持调用向后兼容）。
   - 对每个 `located=True` 的 `inline_formula`：
     - 找出本公式在当前视觉行 `row_idx` 的归属字形 `owned`（非空且不落在上下邻行）。
     - 若有 `owned`，计算真实归属字形高度边界：`normal_owned = [c for c in owned if c.bbox.height <= _NORMAL_GLYPH_HEIGHT * c.size] or owned`，`f_y0 = min(c.bbox.y0 for c in normal_owned)`，`f_y1 = max(c.bbox.y1 for c in normal_owned)`。
     - **区分“超高主行符号框”与“真实下标/正常字形”**：
       - 判定上/下端是否存在“超高异常字框”：`is_tall_top = any(c.bbox.height > 1.4 * c.size and not script_glyph(c, line_size, line_baseline) and c.bbox.y0 <= f_y0 + 0.5 for c in owned)`；`is_tall_bottom` 对称定义。
     - **上方安全边界**（当 `row_idx > 0`）：
       - 传统中线 `mid_lo = (block_centers[row_idx - 1] + block_centers[row_idx]) / 2`。
       - 在 `block_rows[row_idx - 1]` 中筛选水平投影与 `[span.bbox.x0 - 0.5, span.bbox.x1 + 0.5]` 重叠的非空字符 `upper_ov`。
       - 若 `is_tall_top` 且 `upper_ov` 中有字符 `c.bbox.y1 > span.bbox.y0`：沿用传统中线 `safe_lo = mid_lo`（**严格保住 `test_clean_formula_lines_clamps_tall_cmsy_formula_to_block_visual_row_midpoints` 断言 `y0 >= 111.0`**！）。
       - 否则：若 `upper_ov` 非空，令 `upper_y1 = max(c.bbox.y1 for c in upper_ov)`；若 `f_y0 >= upper_y1 + 0.2`，则 `safe_lo = f_y0`（完整保留真实上标/升部！）；若真实轻微字框相交（`f_y0 < upper_y1 + 0.2`），取 `safe_lo = max(min(mid_lo, upper_y1 + 0.2), (f_y0 + upper_y1) / 2)`，且绝不越过 `block_centers[row_idx - 1]`；若 `upper_ov` 为空，完整保留 `f_y0`（不高于 `block_centers[row_idx - 1]`）。
     - **下方安全边界**（当 `row_idx + 1 < len(block_centers)`）：
       - 传统中线 `mid_hi = (block_centers[row_idx] + block_centers[row_idx + 1]) / 2`。
       - 在 `block_rows[row_idx + 1]` 中筛选水平投影与 `[span.bbox.x0 - 0.5, span.bbox.x1 + 0.5]` 重叠的非空字符 `lower_ov`。
       - 若 `is_tall_bottom` 且 `lower_ov` 中有字符 `c.bbox.y0 < span.bbox.y1`：沿用传统中线 `safe_hi = mid_hi`（**严格保住既有 `CMSY10` 测试断言 `y1 <= 123.0`**！）。
       - 否则：若 `lower_ov` 非空，令 `lower_y0 = min(c.bbox.y0 for c in lower_ov)`；若 `f_y1 <= lower_y0 - 0.2`，则直接把底边扩展/保持到 `f_y1`（完整保住紧行距下标 `j`/`n` 下钩！）；若字框轻微重合（`f_y1 > lower_y0 - 0.2`），取 `safe_hi = min(max(mid_hi, lower_y0 - 0.2), (f_y1 + lower_y0) / 2)` 且不低于 `block_centers[row_idx + 1]`；若 `lower_ov` 为空，完整保留 `f_y1`（不低于 `block_centers[row_idx + 1]`）。
     - 同步让 `_formula_box()` 支持可选参数 `rows: Sequence[Sequence[Char]] | None = None`，使用同样的邻行异属字形安全边界。
2. **裁图微调（`rendering/background.py`）**：
   - `crop_formula(page, bbox, zoom=4.0)`：水平维持 `0.3 pt`，垂直增加 `0.25 pt` 极小抗锯齿余量：`clip = pymupdf.Rect(bbox.x0 - 0.3, bbox.y0 - 0.25, bbox.x1 + 0.3, bbox.y1 + 0.25) & page.rect`。
   - `_trim_formula_edges(pix, zoom)`：保持全部既有分式、`≤`/`≥`/`\tilde`/`\bar` 与 `>= 6px` 残影回归测试全部通过，不把下标下伸部（与主体连通或缝隙 `< min_gap`）误判为贴边残影。

### 3.5 黑板体补齐与纯文本块向量空间晋升（`TR-004`）
1. **升级 `_recover_right_blackboard_codomain(line, chars)`**：
   - 使用 `_blackboard_letter(g)` 同时匹配 `STIXMathBlackboard` 字体与 Unicode 黑板码点（如 `ℝ` `U+211D`），并支持 `head["ch"]` 为 Unicode 黑板字符或 ASCII `RNZQCKEP`。
   - 命中黑板字母后，继续向右扫描紧随其后的连续上标字符（满足 `_is_strict_superscript` 且横向间距 `<= 0.35 * size`，如 `^o`、`^{o×v}`），一并收入公式 `bbox`、生成 `\\mathbb{R}^{o}` 形式 LaTeX，并从右侧 `text` span 开头扣除已吸收字符。
2. **新增公开函数 `promote_native_math_spans(block: LayoutBlock, chars: Sequence[Char]) -> LayoutBlock`**（供主 Agent 在 `build.py` 对所有原生页 `BlockType.TEXT` 块调用）：
   - 若 `not chars` 或 `block.type is not BlockType.TEXT` 或 `not block.lines`，原样返回。
   - 预先构建 `block_rows = visual_lines(chars)` 与 `block_centers`。
   - 快速短路：若 `chars` 中**既无**任何 `_blackboard_letter(c) is not None` 的字符，直接零开销返回 `block`（避免对普通纯文本块做多余计算）。
   - 逐行扫描 `text` span：
     - 取出该 span 落在同一视觉行内的非空原生字符 `row_chars`（按 `x0` 排序），算 `body_size, baseline = main_metrics(row_chars)` 及 `row_cy`。
     - 找到满足 `_blackboard_letter(c) is not None` 的黑板字符下标 `bb_idx`。
     - **强数学上下文校验（防普通 `Ro`/单独 `R` 误晋升）**：至少满足其一：
       1. `bb_idx` 左侧紧邻（允许少量间距）存在数学集合/映射关系符 `∈ ∉ ⊂ ⊆ ⊃ ⊇ → ↦`；
       2. `bb_idx` 右侧紧邻（横向间距 `<= 0.35 * body_size`）存在**实测几何上标**（`_is_strict_superscript` 为真，如 `^o`、`^{o×v}`）。
     - 向左锚定起始下标 `lo`：
       - 若存在集合/映射关系符 `op_idx`，纳入该关系符；并可再向左吸收紧邻该关系符（间距 `<= 0.6 * body_size`）的**单个**数学变量/标识符词（如 `Score`、斜体字母或带下标变量；遇开括号 `(` `[` `{`、标点、或再往前一个空格即停，**绝不吞入外层括号或散文词**）。
     - 向右锚定结束下标 `hi`：
       - 连续吸收紧随黑板体右侧的实测上标/下标及上标内部的维度算符（如 `×`, `·`, `+`, `-`, `,`；要求仍处于上标高度且遇闭括号 `)` `]` `}`、正文正常字号或明显空格即停）。
     - 将该 `text` span 在 `[lo, hi]` 位置精确拆分为 `[左正文(若非空), LayoutSpan(kind="inline_formula", content=latex, bbox=安全公式框, located=True), 右正文(若非空)]`（其中 `latex` 将黑板字符写成 `\mathbb{R}`、关系符写成 `\in`/`\to`、上标写成 `^{...}`）。
     - 反例保证：普通正体字体 `Ro`、单独基线大写 `R`（无黑板体字体/码点），100% 跳过不晋升！

### 3.6 泳道 1 离线回归测试（`RT-01` / `RT-02` / `RT-03`）
全部在 `test_formula_align.py` 和 `test_q2_pdf_regressions.py` 使用合成字符/合成 `Pixmap` 编写（严禁出现真实用户论文文本）：
- **`RT-01`**：① 合成紧行距（10.5pt）含 `STIXMath-Italic`（含 `U+210E` `ℎ`）多字母变量与下标 `j`/`n`，初始公式框故意截在 `mach|ine` 且底边高于 `j` 下钩；断言左缘回吸至完整变量起点、底边保留 `j` 真实下钩且不侵入下一行重叠字符顶边；② 合成相邻两公式间残留被切碎数学前缀，断言吸附归位且不留残片文本；③ 确认既有 `CMSY10` 超高符号与 `_trim_formula_edges` 全部保持绿灯。
- **`RT-02`**：① 合成下标 `jm` 紧贴公式底边，断言 `clean_formula_lines` + `exclude_formula_chars` 后正文不再残留独立 `jm`；② 合成公式 `bbox` 右缘多包入正文非数学字体尾逗号 `,`（含右侧正文已有逗号导致 `,，` 与右侧无逗号两种情形），断言尾逗号被干净剥离且 `f(x,y)` 括号内逗号不被误剥。
- **`RT-03`**：构造纯 `text` 块含 `STIXMathBlackboard` / Unicode `ℝ`（`U+211D`）+ `∈` + 几何上标 `o`（包裹在括号内如 `(Var ∈ ℝ^o)`），断言 `promote_native_math_spans` 将核心数学短语晋升为单个 `inline_formula` 且外层括号留在正文；构造普通基线 `Ro` / 单独 `R` 反例断言不误晋升。

---

## 4. 泳道 2 详细设计：段落唯一归属与引文守恒（`TR-001` + `TR-010`）

### 4.1 跨栏续段物理字形唯一归属防重（`TR-001`）
1. **`fusion/relocate.py`**：
   - 保持 `RelocationReport` 向后兼容，新增字段 `deduped_lines: int = 0`、`deduped_blocks: int = 0`。
   - 新增同页物理行重叠检测与去重逻辑，并拆出两个清晰函数：
     1. **`_dedupe_relocated_pages(pages, links, char_index) -> tuple[int, int]`**（在 `relocate()` 末尾、清除已填充 `placeholder` 标记后自动执行）：
        - 仅在**同一页内**比较不同非空、非占位、非丢弃的文本类块（`BlockType.TEXT`）的源行 `LayoutLine`。
        - 判定两行 `la`, `lb` 是否指向同一片物理版面区域：
          - 空间重叠：计算 `inter = la.bbox.intersection(lb.bbox)`；要求 `inter is not None` 且满足：
            - `inter.area / min(la.bbox.area, lb.bbox.area) >= 0.70`，或
            - 水平覆盖 `inter.width / min(la.bbox.width, lb.bbox.width) >= 0.70` 且垂直覆盖 `inter.height / min(la.bbox.height, lb.bbox.height) >= 0.70`。
          - 原生字形集合验证（当 `char_index.get(page.index)` 非空）：
            - 分别取落入 `la.bbox` 和 `lb.bbox`（容差 `TOL`）的同页原生非空字符对象 ID 集合 `ca`, `cb`；
            - 若两者均非空，要求 `len(ca & cb) / min(len(ca), len(cb)) >= 0.80`；
            - 扫描页/无原生字符时，额外要求两行规范化文本键 `_line_text_key` 相似度（`SequenceMatcher.ratio()`）`>= 0.85` 且 `inter.area / min(la.bbox.area, lb.bbox.area) >= 0.85`。
        - **保留优先级裁决（当块 A 与块 B 存在物理重叠行时）**：
          1. 挂接在跨栏/跨页 `links` 中（作为 `links` 的 key 或 value）的块优先保留；
          2. 包含更多有效行/更多规范化字符数的块优先保留；
          3. `order` 更早的块优先保留。
        - 对落败块，仅剔除与优胜块物理重叠的重复行；若落败块所有行均被剔除，则从 `page.blocks` 移除该空块，并安全清理 `links` 中指向或源自该块的条目（若 A -> B -> C 中 B 被删，不留悬空引用）。
        - **硬性红线**：严禁跨页比较、严禁仅凭文本相似度删除不同物理坐标的合法重复句！
     2. **导出供主 Agent 在 `build.py` 兜底调用的函数**：
        ```python
        def dedupe_page_blocks(blocks: list[Block], page_chars: Sequence[Char] | None = None) -> list[Block]:
        ```
        - 仅在同页内检查 `orphan=True` 的补漏 `BlockType.TEXT` 块（或其他重复 `TEXT` 块）是否与已有非 `orphan`（或已挂 `continues_from` 链）的 `TEXT` 块在同页物理坐标重叠 `>= 0.70` 且原生字符集重叠 `>= 0.80`（无原生字符时文本重合 `>= 0.85` 且面积重叠 `>= 0.85`）；若重叠则剔除重复的 `orphan`/落败块，确保 `_orphan_blocks()` 也不会制造同页重复译文。
2. **`translation/segmenter.py`**：
   - 在 `segment()` 中维护 `emitted_blocks: set[str]`：遍历链 `chain` 前跳过已被先前段收录的 `block.id`，构造每个 `Segment` 时过滤掉已在 `emitted_blocks` 中的块（防止异常多入度 `continues_from` 导致同一 `block_id` 进入两个 `Segment.parts`），随后将该段所有 `block_id` 记入 `emitted_blocks`。
3. **`translation/service.py`（`prev_context` 防吸纳上文兜底）**：
   - 在 `_target_problems(..., prev_context: str = "")` 增加可选关键字参数 `prev_context: str = ""`（保持调用方完全向后兼容，并在 `service.py` 缓存命中与译文验收两处传入 `prev_context=prev_context` / `prev_context=u.prev_context`）。
   - 新增保守检测函数 `_context_leakage_problems(source: str, target: str, prev_context: str) -> list[str]`：
     - 仅在 `prev_context.strip()` 非空且去标签/公式占位后长度 `>= 30` 时启用；
     - 从 `prev_context` 提取**不出现在 `source` 中**的显著唯一锚点：独立大写专名/缩写（如 `ResNet`, `Algorithm 2`, `Table 5`, `Section 3`）以及非 trivial 独立数值（如小数、4 位年份、带单位数字、>= 2 位整数且非孤立 `10`/`100`）；
     - 仅当同时满足以下两个条件才触发重试提示（防假阳性）：
       1. 译文纯文本显著膨胀：`tgt_cjk_len >= max(50, int(src_words * 3.2) + 30)` 且 `tgt_cjk_len >= int(src_chars * 1.45)`；
       2. 在 `target` 前半段或整段中命中 **至少 2 个** 仅属于 `prev_context` 而不属于 `source` 的唯一锚点（或 `prev_context` 自身较长英文短语被原样抄入 `target` 开头）。
     - 触发时返回 `["译文疑似吸纳了参考上文内容，请仅翻译本段原文，不要重复翻译上文"]`，进入既有有界单段重试循环。

### 4.2 引文逗号源串修复与译后保序校验（`TR-010`）
1. **`fusion/text_fix.py`**：
   - 在 `recover_dropped_chars(parser_text: str, pdf_text: str) -> str` 中增加极窄、严格限定的数字方括号引文分隔符恢复例外：
     - 新增辅助 `_is_bracket_citation_sep(word: str, idx: int, plain: str) -> bool`：
       1. 将 `word` 去掉尾部常见外置标点（`.,;:!?)`）得到 `core`；
       2. `core` 必须严格全匹配 `^\[\d+(?:[,;\-–—]\d+)+\]$`（注意：开头必须是 `[`，因此 `A[2,3]` 直接返回 `False`！）；
       3. `0 < idx < len(core) - 1` 且 `core[idx - 1].isdigit()` 且 `core[idx + 1].isdigit()`；
       4. 将 `core` 中各数字串提取出来 `nums = re.findall(r"\d+", core)`，校验 `plain`（保留原始空格！）中确实存在以**空白分隔**的同序方括号数字模式（如 `re.search(r"(?<![A-Za-z0-9_])\[\s*" + r"\s+".join(nums) + r"\s*\]", plain)`，允许部分逗号已存在、缺失处为空格，即 `r"(?<![A-Za-z0-9_])\[\s*" + r"(?:\s*[,;\-–—]\s*|\s+)".join(nums) + r"\s*\]"` 且在 `plain` 中匹配到的原始子串里，对应被插入分隔符的那一对相邻数字之间**确实存在至少一个空白字符**，从而拒绝把连写的 `[2930]` 凭空劈成 `[29,30]`！）。
     - 在 `SequenceMatcher` 的 `insert` 字符白名单处，除原有 `_LETTER / ",." / _SAFE_INSERT_SYMBOLS / digit` 外，允许 `ch in "-–—"` 仅在后续词级检查通过 `_is_bracket_citation_sep` 时放行（其他位置一律拒绝！）；
     - 在词级循环 `if ch in ",.":` 分支，当 `ch == ","` 且 `tail` 含字母数字时，仅在 `_is_bracket_citation_sep(word, idx, plain)` 为真时放行；当 `ch in "-–—"` 时同样仅在 `_is_bracket_citation_sep(word, idx, plain)` 为真时放行。
     - 保持 `strip_fake_scripts`、`_restore_tags`、扫描页（`pdf_text == ""`）及 `A[2,3]` 反例 100% 不受影响。
2. **`translation/batching.py`**：
   - 在 `citation_problems(source: str, target: str) -> list[str]` 中，保持既有锚点计数校验（包括 `[29,30]` 译成 `[29 30]` 时直接检出缺少 `[29]`/`[30]`，同时若源文存在合法多编号方括号引文而译文出现同数字序列的空格退化形式如 `[29 30]`，给出明确诊断）；
   - 新增**同组引文保序校验**：
     - 抽取出源文与译文中所有有效确认（非 `excluded`）的方括号多编号引文组，将每组展开为有序编号元组（如 `[29, 30]` -> `(29, 30)`，`[1–3]` -> `(1, 2, 3)`，仅检查展开长度 `2 <= len <= 15` 且无重复元素的组）；
     - 将源文多编号组按其无序多重集键 `tuple(sorted(seq))` 分桶，译文多编号组同样按 `tuple(sorted(seq))` 分桶；
     - 若源文和译文在同一个 `sorted_key` 下存在匹配的多编号组，但译文出现源文该桶中**不存在的顺序**（例如源文仅 `(29, 30)`，译文写出 `(30, 29)`），报错：
       `f"引文编号顺序与原文不一致：[{', '.join(map(str, got_seq))}] 应保持原文顺序"`。
     - 注意：若译文把 `[1, 2]` 合法拆分为 `[1]` 和 `[2]`，单个引文长度 `< 2` 不进入多编号组比较，零误报；暂不修改 `VALIDATION_VERSION = 6`（留给主 Agent 收口时统一升至 `7` 并同步断言测试）。

### 4.3 泳道 2 离线回归测试（`RT-04` / `RT-05`）
- **`RT-04`**（`test_relocate.py` + `test_segmenter.py` + `test_translation_service.py`）：
  1. 合成双栏跨栏续段 + 同页右栏物理坐标重叠的重复块（带原生 `CharPage` 字符坐标），断言 `relocate()` 去重后仅保留挂在跨栏链中的唯一一份；
  2. 合成**异页**（或同页不同坐标）内容完全相同的两段合法文字，断言绝不被误删；
  3. 合成 `dedupe_page_blocks` 面对重叠 `orphan` 块场景，断言剔除重复 `orphan`；
  4. 合成异常重复引用同一 `block_id` 场景，断言 `segment()` 产出的 `Segment.parts` 无重复 `block_id`；
  5. 合成 `prev_context` 吸纳上文（译文异常膨胀并带入前段独有专名与数值）被 `_target_problems` 拦截重译，且正常含指代词翻译零误报。
- **`RT-05`**（`test_text_rules.py` + `test_translation_service.py`）：
  1. `recover_dropped_chars("See prior work [29 30].", "See prior work [29,30].")` 恢复为 `"See prior work [29,30]."`（含带 `<i>` 标签情形与范围号 `[29–30]`）；
  2. 反例：`A[2,3]`、连写 `[2930]` 对 `[29,30]`、普通词内插逗号、扫描页空 `pdf_text`，均严格保持原样不改；
  3. `citation_problems` 拦截 `[29,30]` 退化为 `[29 30]` 及同组乱序 `[30,29]`，放行合法顺序 `[29, 30]` 与 `[29]、[30]`。

---

## 5. 泳道 3 详细设计：保版排版与前后端一致（`TR-008` + `TR-009` + `TR-013`）

### 5.1 短标题安全栏宽扩展（`TR-008`，前后端完全同构）
1. **后端 `rendering/export.py`**：
   - 新增 `_expand_title_box(block: Block, box: pymupdf.Rect, page_width: float, neighbors: Sequence[Block]) -> pymupdf.Rect`：
     - 仅在 `block.type is BlockType.TITLE`、`not box.is_empty`、`page_width > 0` 且 `_heading_align(block, page_width) == "left"` 时生效；否则原样返回 `box`。
     - 推断当前标题所在栏的右边界 `col_right`：
       - 判别该页是否存在双栏布局：设 `mid = page_width / 2`；判断标题处于左栏（`block.bbox.x1 <= mid + block.style.font_size`）还是右栏（`block.bbox.x0 >= mid - block.style.font_size`），否则为跨栏/单栏。
       - 收集同栏 `BlockType.TEXT` 候选块 `peers`：
         - 左栏标题：仅收集左栏 `TEXT`（`b.bbox.x1 <= mid + b.style.font_size` 且 `b.bbox.x0 <= block.bbox.x0 + max(24.0, 3 * block.style.font_size)`）；
         - 右栏标题：仅收集右栏 `TEXT`（`b.bbox.x0 >= mid - b.style.font_size` 且 `b.bbox.x0 <= block.bbox.x0 + max(24.0, 3 * block.style.font_size)`）；
         - 其他单栏标题：收集 `b.bbox.x0 <= block.bbox.x0 + max(24.0, 3 * block.style.font_size)` 且 `b.bbox.x1 >= block.bbox.x1 - 2.0` 的 `TEXT` 块。
       - 若无 `peers`，不扩宽，直接返回 `box`。
       - 取 `col_right = min(page_width - BOTTOM_MARGIN, max(b.bbox.x1 for b in peers))`。
     - 右侧障碍物碰撞截断：
       - 令 `safe_x1 = col_right`。
       - 遍历同页所有 `other in neighbors`（`other.id != block.id`）：若 `other.bbox` 与标题当前纵向带 `[box.y0, box.y1]` 相交（`ob.y0 < box.y1 and ob.y1 > box.y0`）且位于标题原框右侧（`ob.x0 >= block.bbox.x1 - 2.0`），收紧 `safe_x1 = min(safe_x1, ob.x0 - _COLUMN_MIN_GAP)`。
     - 若 `safe_x1 > box.x1`，返回 `pymupdf.Rect(box.x0, box.y0, safe_x1, box.y1)`；否则返回 `box`。
   - 在 `_place()` 中：
     - 将初始 `box` 改为 `box = _clamp_column_right(block, _expand_title_box(block, _box(block) & page.rect, page.rect.width, neighbors), neighbors)`。
     - **字号保底优先**：若 `block.type is BlockType.TITLE` 且按 `scale = 1.0` 遍历 `LINE_HEIGHTS` 在原框高内放不下时，在降到 `MIN_SCALE` 缩字之前，先以 `scale = 1.0, pitch = LINE_HEIGHT` 检查 `len(lines) <= 2` 且 `max(height, drawn) <= safe_height(lines, 1.0) - 2.0`（确认下方有足够安全空白且零碰撞），通过则优先原字号折为 2 行，否则再进入原有二分缩字逻辑。
2. **前端同步（`frontend/src/entities/document/model.ts` + `frontend/src/features/reader/ReaderView.tsx`）**：
   - 在 `entities/document/model.ts` 中导出 `headingAlign(block, pageWidth)` 与扩展后的 `layoutBox(block: BlockData, pageBlocks?: BlockData[], pageWidth?: number)`（可选参数完全向后兼容既有调用）：
     - 实现与后端 `_heading_align` + `_expand_title_box` 完全一致的几何扩宽与碰撞截断规则（常量 `COLUMN_MIN_GAP = 8`、`BOTTOM_MARGIN = 4`）。
   - 在 `ReaderView.tsx` 中将 `page.blocks` 与 `page.width` 传入 `OverlayBlock` 的 `layoutBox(block, pageBlocks, pageWidth)`，并将 `rect.x1 - rect.x0`、`rect.y1 - rect.y0` 加入 `useLayoutEffect` 依赖数组，确保扩宽后 DOM 立即重新按宽容器拟合字号。

### 5.2 同栏无障碍续接块合并排版 + 强句末标点优先拆块（`TR-009`）
> 重要约束：既有测试 `test_q4_eval_round1_fusion_export.py:403` 严格校验 `fusion` 层保留同栏续接块的独立 `Block` 与 `continues_from` 链，因此同栏无障碍合并放在**渲染层（后端 `export.py` 与前端 `entities/document/model.ts` / `ReaderView.tsx`）**实现，既零破坏既有 IR 与单测，又天然兼容存量已落盘 `ir.json` 任务。

1. **同栏连续无障碍 `SegmentPart` 逻辑合区算法（前后端双端同构）**：
   - 判定同一个 `Segment` 内相邻两个连续块 `prev`、`nxt` 可合并为同一逻辑排版区的严格条件（必须全部满足）：
     1. 同页（`prev.page == nxt.page`）且均为 `BlockType.TEXT`；
     2. 水平重叠 `h_ov = min(prev.x1, nxt.x1) - max(prev.x0, nxt.x0)` 满足 `min_w > 0` 且 `h_ov >= 0.70 * min(prev.width, nxt.width)`；
     3. 左右对齐合理：`abs(prev.x0 - nxt.x0) <= 1.2 * max_size` 且 `nxt.x1 <= prev.x1 + 1.5 * max_size`（兼容末块单行不满行较短，同时防误合缩进或跨栏块）；
     4. 垂直紧邻：`-0.5 * max_size <= nxt.y0 - prev.y1 <= 1.5 * max_size`（`max_size = max(prev.font_size, nxt.font_size)`）；
     5. 两块之间及合并外接框内无任何第三方块阻隔：设 `lo_x = min(prev.x0, nxt.x0)`, `hi_x = max(prev.x1, nxt.x1)`, `lo_y = min(prev.y1, nxt.y0) - 1.0`, `hi_y = max(prev.y1, nxt.y0) + 1.0`；同页不存在任何不属于本合并组的其他块 `other`（任意类型，含图/表/公式/标题/文本）与 `[lo_x, hi_x] × [prev.y0, nxt.y1]` 的中部或间隙带发生碰撞。
   - **合并结果（后端 `export.py`）**：
     - 在 `_typeset_all()` 与 `block_pieces()` 中共用同一分组函数 `_merge_render_parts(seg, blocks, by_page)`：
       - 将连续可合并的 `SegmentPart` 聚成一个渲染组 `(anchor_block, member_blocks, merged_bbox, total_chars)`，跨栏/跨页/绕图段仍保持独立分组；
       - 按各渲染组的 `total_chars` 调 `split_by_parts()`（若全段合成 1 组则零切分！彻底消除 `TR-009` p11 空白断层与劈词）；
       - 用临时合成的渲染块 `anchor_block.model_copy(update={"bbox": merged_bbox})` 调用 `_place()` 排版（注意传入的 `neighbors` 排除 `member_blocks` 中其他同组成员，防自己拦自己！）；
       - 落位记录：`placed[member.id]` 与 `placed_images[member.id]` 让同组所有成员块共享指向该合区的落位列表（且 `block_pieces()` 中仅主块 `anchor_block.id` 持完整合区文本，或副块置 `""` 且 `place_links` 通过已有 `siblings` 自动把原本落在副块坐标里的链接定位到主块文字上！**验证**：看 `export.py:677` `siblings = {bid: seg.block_ids for seg in segments for bid in seg.block_ids}`，`locate()` 会在 `siblings` 内搜文字并把 `link.block_id` 改指向实际含该文字的 `anchor_block.id`，所以把主块设合区全文、同组副块在 `block_pieces` 设 `""` 时，所有链接 100% 精准命中无重复！）。
   - **合并结果（前端 `entities/document/model.ts` + `ReaderView.tsx`）**：
     - 在 `blockTexts(doc)` 中实现完全同构的同栏无障碍 `SegmentPart` 分组：
       - `BlockText` 新增可选字段 `renderBox?: { x0: number; y0: number; x1: number; y1: number }`（合区时取 `union` 后的外接框）；
       - 仅为主块（每组第一块）写入 `out.set(anchor.id, { ..., text: groupPiece, renderBox: mergedBbox })`，同组副块不写入译文叠加层（避免重复渲染与 30pt 断层），而在 `ReaderView.tsx` 的 `OverlayBlock` 中优先使用 `item.renderBox` 作为基础框传入 `layoutBox`（并对扫描页 `source-text-layer` 仍可正常展示，或同步取合区 `sourceText`）。
2. **强句末标点优先拆块（`TR-009` 跨栏/跨页场景，双端锁死）**：
   - 后端 `rendering/export.py` 与前端 `frontend/src/shared/text/split.ts` 同步更新 `_cut_near` / `cutNear`：
     - 定义强句末标点集合：`_STRONG_BREAK_AFTER = set("。！？；.!?;")`（注意小数与 URL 已被 `_split_atoms` 合并成 `char == ""` 复合原子，不会误命中）；
     - 次级标点集合保持：`_BREAK_AFTER = set("，。；：、）！？,;:.)")`；
     - 在 `[target - radius, target + radius]` 窗口内：**第一趟**按距离 `d = 0..radius` 搜索 `atoms[pos - 1].char in _STRONG_BREAK_AFTER`；若未命中，**第二趟**按距离 `d = 0..radius` 搜索 `atoms[pos - 1].char in _BREAK_AFTER`；仍未命中则回退 `_avoid_mid_word`。
     - 保持 `frontend/src/shared/text/split.fixture.json` 原有 24 条夹具全部通过，并在末尾追加强句末标点优先（同窗口内有 `，` 与 `。` 时优先在 `。` 处切分）的新共享测试向量！

### 5.3 跨样式行首禁则与短括号参数组防拆（`TR-013`）
修改 `backend/src/pdftr/rendering/typesetter.py`：
1. **跨样式 ASCII 行首/行尾禁则根因修复**：
   - 新增辅助方法 `_token_text(tok: Token) -> str`（拼接 `r.text for r in tok.runs`，若为空则回退 `tok.char`）；
   - 新增 `_is_no_start(tok: Token) -> bool`：
     - `tok.kind == "cjk" and tok.char in _NO_START`，或
     - `tok.kind == "word"` 且 `txt := _token_text(tok)` 非空且 `all(ch in _NO_START for ch in txt)`（拦截跨 `<i>,</i>` 产生的独立逗号 `,`、独立闭括号 `)` `]` `}`、分号 `;`、冒号 `:`、百分号 `%` 等）。
   - 新增 `_is_no_end(tok: Token) -> bool`：
     - `tok.kind == "cjk" and tok.char in _NO_END`，或
     - `tok.kind == "word"` 且 `txt := _token_text(tok)` 非空且 `all(ch in _NO_END for ch in txt)`（拦截跨样式独立开括号 `(` `[` `{`）。
   - 在 `break_lines()` 中将原仅限 `tok.kind == "cjk"` 的行首悬挂（L293）与行尾搬移（L303）统一替换为 `_is_no_start(tok)` 与 `_is_no_end(cur[-1])`。
2. **短函数/分布括号参数组整体断行（兼容跨样式）**：
   - 在 `Token` 上新增可选布尔字段 `joined_prev: bool = False`（默认 `False`，在 `tokenize()` 中当两个相邻非空片段被 `<i>`/`<b>` 等富文本标签分隔、原文之间无任何空白字符、且前后两个 token 均为 `"word"` 时，将后一个 token 标记 `joined_prev = True`，表示它与前一个 token 在视觉上无缝相连）。
   - 在 `break_lines(tokens, width, size, indent)` 开始前，运行轻量扫描 `_group_short_parens(tokens, width, size) -> list[list[Token]]`：
     - 将跨样式无缝相连的 ASCII 词/标点先视作连续链，识别配对的半角圆括号组 `(...)`（若开括号前无缝紧贴着 `joined_prev` 链路中的简短函数/分布名如 `U`、`N`、`Exp`、`Beta`，将该前置函数名一并归入组头）；
     - 满足以下全部条件才判定为**短括号参数组**：
       1. 组内不含 `"cjk"`、不含 `"image"`、不含换行；
       2. 括号配对闭合且无嵌套超深（深度 <= 2）；
       3. 组纯文本长度 `<= 16` 字符；
       4. 整组排版宽度 `group_width <= 0.75 * width`（确保 47pt 窄宽下 `U(1, 3)` 约 23pt 完整命中，而常规栏宽下的长解释性括号仍可在逗号/空格后正常换行）。
     - 命中短括号参数组时，在 `break_lines()` 中把整组作为不可拆分整体推进：若当前行放得下则放入当前行；若当前行放不下且 `cur` 非空，则先换行（仍尊重行尾禁则），把完整短括号组（如 `U(1, 3)`）整体放至下一行行首！
     - 若整组本身放不下空行（极端窄框），降级回逐 token 放置且仍严格执行 `_is_no_start` 禁则，绝不死循环。

### 5.4 泳道 3 离线回归测试（`RT-06` / `RT-07`）
- **`RT-06`**（`test_typesetter.py` + `frontend/src/shared/fit/fit.test.ts`）：
  1. 后端：构造双栏页中 86pt 宽左对齐小节标题框 + 250pt 宽同栏正文 + 17 字中文译文（字号 7.97pt），断言扩宽后导出实际渲染字号 `≈ 7.97pt`（误差 `<= 0.1pt`，不再缩至 5.5pt）；构造右侧紧邻 `FIGURE` 障碍物反例，断言扩宽严格止于 `obstacle.x0 - 8.0` 且零重叠；构造居中主标题反例断言不被左对齐扩宽逻辑干扰；
  2. 前端：在 `fit.test.ts` 补测 `layoutBox(titleBlock, pageBlocks, pageWidth)` 与 `searchFit` 联动，验证 86pt 标题扩至同栏宽后保持原字号、右侧有图阻挡时安全截断。
- **`RT-07`**（`split.fixture.json` + `test_export_split.py` + `test_typesetter.py`）：
  1. `split.fixture.json` 追加强句末标点优先夹具，由后端 `test_export_split.py` 与前端 `split.test.ts` 双端共同验证；
  2. `test_export_split.py` 补测同栏连续无障碍 `SegmentPart` 合并渲染（断言合并后不拆出多余空隙/断词、跨栏或中间有图时保持按块拆分、且原有链接仍正确定位）；
  3. `test_typesetter.py` 补测 `均匀分布<i>U</i>(1<i>,</i> 3)生成` 在 47pt 窄宽（size=8pt）下 `U(1, 3)` 完整留在同一行、不出现任何行以 `,` 开头；并补测长括号组内独立 `<i>,</i>` 不会掉到行首且仍可在长括号内部正常换行。

---

## 6. 主 Agent 串行收口与全量验收清单

3 路泳道全部完成后，主 Agent 按序执行：
1. **边界审计**：`git status` 与 `git diff --stat` 确认 3 路未越界修改任何非独占文件。
2. **胶水接线（`fusion/build.py`）**：
   - 在 L170 区域：对所有原生页 `BlockType.TEXT` 块，先按块内是否已有 `inline_formula` 走 `clean_formula_lines`，若为纯 `text` 块（或两者结合）调用 `promote_native_math_spans(lb, chars)`；若晋升出了新 `inline_formula`，再走一遍 `clean_formula_lines(lb, chars)` 统一执行安全垂向限位与清洗。
   - 在 L630 区域：`span_chars` 使用泳道 1 导出的 `exclude_formula_chars(chars, formula_boxes, ...)` 替代单纯矩形排除，阻断下标 `jm` 逃逸进正文。
   - 在 L197 区域（`_orphan_blocks` 追加之后）：调用泳道 2 导出的 `dedupe_page_blocks(blocks, cp.chars if cp.kind is PageKind.NATIVE else None)` 兜底防同页物理重复。
   - 在 `tests/unit/test_fuse_synthetic.py` 补齐端到端融合集成测试（纯文本 `ℝ^o` 晋升、`jm` 下标防逃逸、同页重叠重复块消重）。
3. **公共版本号递增**：
   - `backend/src/pdftr/pipeline/usecases.py`：`EXPORT_CACHE_VERSION` `15 → 16`；
   - `backend/src/pdftr/translation/batching.py`：`VALIDATION_VERSION` `6 → 7`（并同步核查测试中对 `VALIDATION_VERSION` 的断言）。
4. **Neon 深度代码评审**：通读全量 `git diff`，重点审查边界条件、数值稳定性、内存开销、架构分层、前后端一致性及潜在 bug；如有实质缺陷，开启新 `delegate_task` 派发 Codex（`apice/gpt-6.2-astra xhigh`）定点修复，再由 Neon 二次复审。
5. **串行全量 9 项门禁**（在 `D:\01\pdf-translator-tr-fix` 串行执行）：
   - 后端：`uv run --frozen ruff format --check .` → `uv run --frozen ruff check .` → `uv run --frozen pyright` → `uv run --frozen lint-imports` → `uv run --frozen pytest -q`
   - 前端：`npm run lint` → `npm run typecheck` → `npm test` → `npm run build`
   - 卫生检查：`git diff --check`、密钥扫描、确认主目录 `D:\01\pdf-translator` 保持干净未受影响。

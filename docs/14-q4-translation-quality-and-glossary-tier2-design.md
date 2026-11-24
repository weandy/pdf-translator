# Q4 翻译质量与词库进阶加固设计方案（审查报告第二档及余项）

> **目标基线**　`main`（`49dadeb`，含 Q3a / Q3b / Q3c）
> **状态**　Final（主控已综合 Codex v2 与 Neon v3 评审汇总定稿，可进入实施）
> **关联文档**　[Q3 深度红队审查报告](13-q3-translation-quality-audit.md)、[项目交接记录](WORKLOG.md)

---

## 一、背景、约束与总体目标

Q3c 已处理第一档问题，包括失败诊断反馈、优先补救未重试段落、坏源误杀与结构化摘要遗漏、基础术语匹配及缓存读写隔离。Q4 交付 Q3 第二档的事实锚点、术语按段注入、粗斜体合法拆合、跨块原子切分和导出截断提示，并补齐余项中的上下文、兼容接口降级、术语抽取质量和源文保真。

- 后端沿用 `api → pipeline → 能力模块 → domain`，能力模块互不导入；跨能力模块的纯函数与数据契约下沉到 `domain`，翻译与术语能力在 `pipeline` 和 `cli` 组合。前端沿用 `app → pages → features → entities → shared`，跨目录一律使用 `@/`，不同 feature 由页面组合。
- 复用既有依赖、SQLite JSON 段落存档与导出文件目录。新增 IR 字段提供向后兼容默认值；`SegmentOut.of` 按显式 DTO 输出，`role` 仅服务于翻译上下文，不改变对外 `SegmentOut` 结构。旧任务重试从已完成阶段继续，既有 `DONE` / `EDITED` 段落与旧 `glossary.json` 保留，新规则对新进入对应阶段的请求生效。
- Q4 同时升级 `PROMPT_VERSION = "v5"`、`VALIDATION_VERSION = 2` 和 `EXPORT_CACHE_VERSION = 10`；前两者隔离翻译缓存，后者隔离导出 PDF 产物，各自独立维护。未改动原文链接提取规则，不升级 `rendering/links.py:VERSION`。
- 新规则坚持 KISS 与低误报优先：只对可机械确认的事实加硬校验，用确定算法和合成正反例锁定边界；裸数字、作者年份、公式角色、否定主语和条件附着列入公开论文盲评，不凭空引入脆弱的 NLP 启发式。

---

## 二、关键架构权衡与决策记录

| 议题 | v3 裁决与事实依据 |
| --- | --- |
| 缓存粒度与重发策略 | **采用单段级确定性缓存键 + 按段号作用域注入（推翻 v2 首批请求级缓存与部分命中重发整批）**。v2 将整批 `user_prompt` 摘要与段号纳入缓存键、部分命中时按原编号重发整批且禁止单段补救写缓存，会导致长文断点续跑、增量选页、单段补救重跑时缓存命中率骤降并重复支付 4 倍价输出 token，且直接破坏 `test_translation_service.py` 中 4 个既有缓存契约断言（含 L88、L515、L593、L635）。通过按段号隔离术语/前文/角色消除批次串味后，单段确定性指纹已能覆盖全部语义上下文，命中缓存的段落直接跳过不重发，单段补救通过同一校验后照常回写单段缓存。 |
| 本文术语表位置 | **保留 `DocContext.terms`（经 `TermSelection.paper` 按规范键完成用户覆写仲裁）在 `system_prompt` 固定前缀中（推翻 v2 移出至 `user_prompt`）**。本文术语最多 45 条（约 200–350 tokens），留在固定前缀中可稳定命中 DeepSeek 98% 折扣缓存并帮助短摘要论文跨过 OpenAI/Anthropic 1024 token 前缀缓存门槛；移至 `user_prompt` 会在几十个批次中反复以全价重发且削弱全文派生形态一致性，同时破坏 `test_api_flow.py:655` 与 `test_translation_parts.py:70-73` 契约。仅将未进 `selection.paper` 的额外用户术语与经三层 span 仲裁后的公共参考词库按段号注入 `user_prompt`。 |
| 事实锚点 | 采用**黑名单排他 + 确定性形态规则**替代模糊书目语境推断：引文排除数组索引、含 `0` 区间、数学算符/区间/向量语境、独立 4 位年份、URL 与公式内方括号后，锁定正整数引文（`1..999`、跨度 `<= 30` 展开）；上下标在结构通过后锁定负/显式符号上标、数字/单位基底正数上标及纯数字下标（仅对 1–2 字母元素/变量基底锁基底）。 |
| 公共词库 | 延续二态门控与软提示。解析放宽配对括号附注（仅认显式“启用”/“停用”），截断时仅消费完整前缀行，门控行不完整默认停用。 |
| 导出一致性与缓存 | 保留 `POST /export/prepare` 返回 `overflow_pages` 与带 16 位十六进制 `snapshot` 的 `download_url`，`GET /export?snapshot=...` 绑定已发布快照。**瘦身 `.meta.json` 状态机**：仅存 `{"v": 10, "overflow_pages": [...]}`，沿用 `pdf_destination` 先原子发布 PDF、再原子发布 `.meta.json` 作为提交标记，取消多余的 `digest/size_bytes` 二次比对与重型状态机。 |
| 兼容性降级 | 仅 HTTP 400 且错误明确指向本次已发送的可选参数（或要求 `max_tokens → max_completion_tokens`）时，单次 `complete` 最多降级重发 1 次；降级能力记忆限于当前适配器实例且单调累积（事件循环无锁合并）。**区分参数名适配与语义降级**：仅当触发思考/采样语义降级（`drop_thinking`/`drop_reasoning_effort`/`drop_output_config`/`drop_temperature`）时设 `compatibility_fallback = True` 禁写共享缓存；纯参数名等价替换（`use_max_completion_tokens`）未改变模型推理强度，允许正常复用缓存。 |

---

## 三、分模块详细设计

### 共享契约基线

主控先锁定下列接口、规范键算法与共用夹具并单次提交，再从该基线创建四个零文件重叠的开发 worktree。

1. `backend/src/pdftr/domain/ir.py`：`Segment` 新增 `role: BlockType | None = None`（由分段阶段填首块类型，旧 JSON 存档自动回退 `None`）。`SegmentPart.chars` 保持 `visible_length` 计数不变。
2. `backend/src/pdftr/domain/text.py`：新增纯函数 `normalize_term_key(term: str) -> str`，返回带显式类别前缀的规范键（空串/纯空白返回 `""`，不入索引）：
   - **缩写类别 `acr:`**：单 token 且不含技术分隔符，匹配 `^[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)*s?$` 且至少含 2 个大写字母；末尾为小写 `s` 且去 `s` 后仍至少含 2 个大写字母时去 `s`（`SVMs → "acr:SVM"`、`GNNs → "acr:GNN"`、`ADAM → "acr:ADAM"`，保留大小写，与 `Adam → "word:adam"`、`svms → "word:svm"` 严格分离）。
   - **字面技术词类别 `lit:`**：含 `/`, `@`, `.`, `:`, `+`, `#` 等技术分隔符（`TCP/IP → "lit:tcp/ip"`、`AP@0.5 → "lit:ap@0.5"`，与 `TCP IP → "word:tcp ip"` 分离），或含非 ASCII 字母等无可靠英语词形规则的词条（压缩空白并 `casefold` 后加 `lit:` 前缀）。
   - **普通词/短语类别 `word:`**：纯 ASCII 字母/数字/空格/连字符/撇号，按 `[\s-]+` 切分，对每个词片小写化并做确定性简单复数归并（`len > 3` 时 `ies→y`、`(s|x|z|ch|sh)es→去es`、非 `ss/us/is` 结尾 `s→去s`），拼为 `"word:" + " ".join(parts)`（`boxes/box → "word:box"`、`policy-gradient/policy gradients → "word:policy gradient"`）。
3. `frontend/src/shared/text/term-key.fixture.json`：随基线一并落盘前后端共用的 `normalize_term_key` 对拍向量（含 `boxes/box`、`policy-gradient/policy gradients`、`SVMs/SVM/svms`、`GNNs/GNN`、`ADAM/Adam`、`TCP/IP/TCP IP`、`AP@0.5`、空串、含非 ASCII 词条），由后端 `backend/tests/unit/test_text_rules.py` 与 Q4b 前端共同验证。
4. `backend/src/pdftr/domain/llm.py`：`ChatResult` 新增 `compatibility_fallback: bool = False`，兼容所有既有假模型构造方式。

---

### Q4a　翻译校验、编号协议、段内上下文与缓存

**文件归属**　`backend/src/pdftr/translation/{batching,prompt,service}.py`；测试 `backend/tests/unit/{test_translation_parts,test_translation_service,test_fuse_synthetic,test_q4a_translation_guardrails}.py`。
Q4a 从 `test_translation_parts.py` 移除 `EXPORT_CACHE_VERSION` 导入与断言（移交 Q4c 独立断言），同步更新 `test_translation_parts.py` 与 `test_fuse_synthetic.py` 中的 `PROMPT_VERSION == "v5"` 断言。

#### 1. 事实锚点（确定性窄规则）

`VALIDATION_VERSION = 2`。`_target_problems(source, target)` 按「公式占位符 → 富文本结构与粗斜体守卫 → 事实锚点（引文 + 数值上下标）→ 正文质量」顺序校验，首次模型输出、单段补救输出和缓存读命中均走同一入口；失败原因直接进入 `retry_feedback`。人工编辑仅沿用公式占位符与 `rich_text_problems`，保留用户纠错权。

- **引文锚点（`citation_problems(source, target) -> list[str]`）**：
  - 扫描视图：剥离 `</?(?:b|i|sup|sub)>`，将 `<m\d+/>` 和 URL（`https?://\S+`、`www.\S+`）替换为不可匹配的分隔符，按配对半角 `[...]`（译文额外接受全角 `［...］`）提取候选方括号组，总内容长度上限 60 字符。
  - 语法约束：内容仅允许 ASCII/全角数字（归一后 `1 <= n <= 999`；若前紧邻 `Ref.`/`Refs.`/`Citation`/`文献`/`参考文献` 则允许到 `9999`）、空白及分隔符 `, ; - – —`（译文另允 `，、；`）。含 `0` 的组（如 `[0, 1]`）自动排除。
  - 显式非引文黑名单（原译文均生效，无歧义）：
    1. 紧邻数组/索引前缀：左邻标识符为单字母、含 `_` 或含数字的索引变量名（如 `A[12]`、`A [12]`、`x_i[1]`、`v2[1]`），或常见代码容器名（`arr/vec/mat/seq/idx/dim/buf/mem/ptr/reg/tab/row/col/node/edge/tensor/layer`）无空格紧贴 `[`，或紧贴 `)`（`f(x)[1]`），或右邻 `=` / `[` / `(`（`[1] = 2`、`M[1][2]`）；
    2. 紧接 Markdown 链接目标 `(https?://...)`；
    3. 左邻明确数学/数据结构语境词或算符：`=`, `+`, `-`, `*`, `/`, `∈`, `⊂`, `⊆`, `<mN/>` 紧邻算符，或前词为 `interval`, `range`, `vector`, `list`, `array`, `matrix`, `tuple`, `shape`, `size`, `dims`, `kernel`, `stride`, `in`（译文另排 `区间`, `范围`, `数组`, `向量`, `矩阵`）；
    4. 无 `Ref.`/`文献` 前缀的 4 位数年份 `[1900]`–`[2099]`（自然被 `<= 999` 拦截）。
  - 展开与多重集比对：仅在原文存在至少 1 个已确认引文时比对。范围 `a-b` 满足 `1 <= a < b` 且 `b - a <= 30` 时展开为 `a..b`，超宽范围只记录 `(a, b)` 区间元组不展开。`[1, 2]`、`[1][2]`、`［1，2］` 均归并到同一编号 `Counter`；原文若同时存在明确非引文方括号与已确认引文含同一编号，从本次硬校验中剔除该歧义编号。拦截改号（`[12] → [21]`）、漏掉重复引文或凭空新增已确认引文。
- **数值上下标锚点（`script_value_problems(source, target) -> list[str]`）**：
  - 仅在 `script_problems` 通过后运行。提取每对 `<sup>...</sup>` 与 `<sub>...</sub>` 的内部文本（剥离嵌套 `b/i` 和空白），归一化全角数字 `０-９ → 0-9`、Unicode 减号 `−–—－ → -`、`+N → N`；只针对纯数值载荷 `^[+-]?\d+(?:\.\d+)?$` 建锚点，非纯数值（如 `<sup>∗</sup>`、`<sub>steady_state</sub>`、`<sub>i,j</sub>`）只走结构检查。
  - **上标硬锁条件**（排除作者/年份脚注零误报）：仅锁定以下两类 `<sup>`：① 载荷带负号或原文本带显式 `+` 号（如 `<sup>−6</sup>`、`<sup>+2</sup>`）；② 载荷为正数且左邻（忽略样式标签）紧贴数字（排除四位年份 `(?<!(?:19|20)\d{2})`）或单位/数学基底 `(?<![A-Za-z])(?:m|cm|mm|km|s|ms|ns|Hz|kHz|MHz|GHz|kg|g|mg|K|Pa|R)`（如 `10<sup>6</sup>`、`m<sup>2</sup>`、`R<sup>2</sup>`）。记为 `("sup", None, norm_val)`。
  - **下标硬锁条件**：所有纯数字 `<sub>` 均锁定其归一数值；若左邻（忽略样式标签与空白外的紧贴字符）恰好为 1–2 位独立 ASCII 字母标识符 `(?<![A-Za-z])[A-Za-z]{1,2}$`（覆盖化学元素 `H`, `O`, `Fe`, `Li`, `CO` 及数学变量 `x`, `y`, `L`, `F`；跳过 3 字母以上可翻译英文单词如 `Layer<sub>1</sub>`），则一并锁定左侧基底，记为 `("sub", base, norm_val)`，否则记为 `("sub", None, norm_val)`。
  - 按 `Counter` 多重集比较原译文锚点：拦截 `10<sup>−6</sup> → 10<sup>6</sup>`、`H<sub>2</sub>O → H<sub>3</sub>O / HO<sub>2</sub>`、`x<sub>1</sub>, y<sub>2</sub> → x<sub>2</sub>, y<sub>1</sub>`，同时允许整组随语序重排（`y<sub>2</sub>和x<sub>1</sub>`）以及 `n<sup>2</sup>` 以外的普通正文数字改写（`two → 2`、`0.05 → 5%`）。

#### 2. 局部粗斜体合法拆合与整段误包守卫

- 对 `b`、`i` 取消严格对数相等（`sup`、`sub` 维持严格对数相等）：
  1. 原文无该标签时，译文严禁出现（保留含 `"新增粗体"` / `"新增斜体"` / `"未完整配对"` 的错误文案）。
  2. 原文有该标签时，译文必须通过 `rich_text_problems(target, tag=tag)`（配对、无交叉嵌套、无空标签），禁止同类标签自我嵌套（如 `<b>...<b>...</b>...</b>`），且译文至少保留 1 个有效 `<tag>` 区间（缺失时报 `"丢失"`）。
  3. **确定性整段误包守卫**：从原文去除所有 `<tag>...</tag>` 区间及富文本标签后，统计剩余外部文本中的实词数（排除冠词/短介连词 `{a, an, the, in, on, of, to, for, and, or, is, are, was, were, be, by, at, as}` 的 `[A-Za-z]{2,}` 词，加上 `<m\d+/>` 个数）。当原文外部实词数 `>= 2` 时，计算译文去除全部 `<tag>...</tag>` 覆盖区间、富文本标签、空白及中英文标点（`，。、；：？！（）()[]「」『』“”"'—–-,.;:?!`）后的剩余字符数；若剩余为 `0`（含 `<b>甲</b>，<b>乙</b>。` 这种多对合盖全段的情形），拒绝并返回 `"{label}覆盖了整段正文"`。
- 更新 `test_translation_parts.py:48`（改测合法 `1 → 2` 拆分放行），补充外部 `>= 2` 实词时单对/多对整段误包拦截、重复嵌套拦截与仅剩冠词放行的单测。

#### 3. 编号协议、结束标记、续行与分批预算

- **Prompt v5 与 `</translations>` 结束标记**：
  - `PROMPT_VERSION = "v5"`。提示词沿用紧凑 `N|译文`，新增规则：① 明确优先级「用户术语 > 本文术语表 > 参考词库」；② 要求 `<sup>`/`<sub>` 沿用原标签表达数值与符号；③ 全部段落译完后另起一行输出 `</translations>`；④ 修正示例 7 为 `7|自注意力只需<m1/>次顺序操作，而循环层需要<m2/>次，因此自注意力可以大幅提高并行度。`，保留示例 8 的公式随概念移动绑定。
  - `parse_numbered(text: str, *, batch_size: int | None = None, sources: Sequence[str] | None = None) -> dict[int, str]`：
    - 遇到剥离空白后等于 `</translations>` 的行：若已收集条目已覆盖 `1..batch_size`（或 `batch_size is None` 且已有条目），立即 `break` 忽略其后的模型解释尾注；若未收齐即提前出现 `</translations>`，同样停止解析（外层因缺行自动触发补救）。无 `</translations>` 时完整解析至末尾，**不报错**，100% 兼容旧响应与假模型。
    - 保留重复、乱序、越界、超长编号（`> 12` 位）拒绝及 `finish_reason in ("length", "max_tokens")` 丢弃最后一条的截断保守策略。
    - **竖线续行判定**：当行匹配 `^(\d+)\s+[|｜]\s+(.*)$`（竖线两侧**均有空白**）且同时满足 4 个条件——`last is not None`、`number` 非合法后继编号（`number <= last` 或 `batch_size is not None and number > batch_size`）、`sources` 已传入且 `sources[last - 1]` 含 `|` 或 `｜`——则作为当前段的续行拼接，否则照常按编号解析/报错。`2|3`、`1|甲\n1|乙`、`3 | 第三段` 均维持原行为。
- **章节感知分批与预算估算**：
  - `make_batches` 增加可选 `sections` / `roles`（或独立 `make_unit_batches` 保持旧 `make_batches` 签名兼容 `test_translation_parts.py:27`）。
  - **切批规则**：贪心累加每段预估 token（含展开公式、段号、本段额外术语、角色标注和前文句，单段累加不重复拼接整批 prompt）；`BATCH_TOKENS = 1500` 为批内用户内容上限，单段超限独立成批。切章边界规则为：① 新段 `role is BlockType.TITLE` 且当前批非空时，以标题作为新批起点切批（避免标题独自落单成 1 段小批，标题与后续同章正文自然同批）；② 当前批已有非空章节 `cur_sec` 且新段章节 `next_sec` 非空且 `next_sec != cur_sec` 时切批。同步记录含 `system_prompt` 的估算输入总 token 便于观测。

#### 4. 段内上下文、按段作用域注入与单段确定性缓存

1. **接口向后兼容**：
   - `translate(...)` 新增两个可选关键字参数：
     - `lookup_term_hits: Callable[[str], tuple[dict[str, str], dict[str, str], dict[str, str]]] | None = None`（返回经三层 span 仲裁后的 `(user_hits, paper_hits, library_hits)`；未传时回退旧 `lookup_user_terms` / `lookup_terms`）；
     - `context_segments: Sequence[Segment] | None = None`（文档完整原序段落，用于回指前文提取；未传时回退 `segments` 自身顺序）。
   - `user_prompt` 保留原 4 个位置参数与 `retry_feedback`，新增可选关键字参数 `scoped_meta: Sequence[SegmentPromptMeta] | None = None`。仅传旧参数时输出逐行完全不变（兼容 `test_translation_parts.py:75-80`）。
   - 传入 `scoped_meta` 时按段号作用域渲染：`本批所在章节：...`（同章合写，跨章按 `第N段章节`）、仅非正文角色写 `段落类型：第N段=标题/图注/脚注/参考文献/图中标签`、仅命中回指段写 `第N段前文（仅供理解指代，勿翻译）：...`、按段号聚合写 `第1、3段用户术语（统一使用）：...` 与 `第2段参考词库（仅在语境与论文领域吻合时参考）：...`。
   - **本文术语留在 `system_prompt`**：`user_scoped` 仅包含 `user_hits` 中未以相同译法出现在 `ctx.terms` 的词条；`ref_scoped` 为仲裁后的 `library_hits`；`paper_hits` 已由 `system_prompt` 的 `本文术语表` 承载，不在 `user_prompt` 重复注入。
2. **回指前文预提取（有界确定规则）**：
   - 在跳过 `DONE`/`EDITED`、查缓存、去重和分批之前，基于 `context_segments` 建立原序相邻前段映射（允许读取已翻完或未选中的前段原文）。
   - 仅当本段与紧邻前段**同时满足**：① 两者 `role is BlockType.TEXT`；② 两者 `section` 均非空且相等；③ 前段末块页号与本段首块页号（解析 `block_id` 的 `p<page>-b<idx>`，无法解析时跳过）满足 `0 <= cur_page - prev_page <= 1`；④ 本段去标签后首 6 词内存在明确回指开头（如 `^(?:This|These|That|Those|It|Its|They|Their|Such|The\s+(?:former|latter|above|aforementioned|same|following))\b` 或句首 `However, (?:this|it|they)\b`）——才提取前段末句作为 `prev_context`。
   - 末句提取：先将 `<m\d+/>` 替换为 `[公式]`、剥离富文本标签、压平换行空白；保护常见缩写（`e.g.`, `i.e.`, `et al.`, `Fig.`, `Eq.`, `Sec.`, `No.`, `vs.`, `Dr.`）与小数点后取最后一句，超长从末尾保留至多 240 Unicode 码点（带省略前缀）。同批、跨批和单段补救使用同一份 `prev_context`。
3. **单段确定性缓存与去重（高命中、零冗余重发）**：
   - 保留单段 `cache_key` 并扩展带默认值的关键字参数，零破坏现有单测：
     ```python
     def cache_key(
         seg: Segment,
         model: str,
         terms: dict[str, str],
         quality: Quality,
         *,
         reference_terms: dict[str, str] | None = None,
         domain: str = "",
         section: str = "",
         provider: str = "",
         validation_version: int = VALIDATION_VERSION,
         title: str = "",
         abstract: str = "",
         role: str = "",
         prev_context: str = "",
     ) -> str
     ```
     当 `ctx` 的 `title`/`abstract` 为空且 `role`/`prev_context` 为空时，与直接调用 `cache_key(seg, model, terms, quality)` 完全一致；同时同 `domain` 不同 `title`/`abstract`、同 `base_url` 不同 `dialect`（由 `provider_id` 携带）、同原文不同 `prev_context`/`section`/`role`/命中术语的段落全部严格隔离。
   - 查缓存先于分批：对每个待译段先计算本段 `user_scoped`、`ref_scoped`、`prev_context` 与 `key`，若 `o.use_cache` 且命中且通过 `_target_problems`，直接采用并计入 `report.cached`，**不进入 `pending`、绝不重发给模型**。
   - `_Unit` 以单段 `key` 为去重键（自然涵盖 `source, formulas, section, role, prev_context, 有效术语`）：同文同上下文同术语的重复段合并翻译，同文异前文或异章节的重复段自动分成独立单元。
   - **缓存回写**：首批与单段补救通过 `_target_problems` 的结果，在 `o.use_cache and not result.compatibility_fallback` 时写入单段缓存；`use_cache=False`（手动重翻/精修）或 `result.compatibility_fallback=True` 时本次正常使用但不写缓存。

---

### Q4b　术语区间仲裁、抽取质量与面板提示

**文件归属**　`backend/src/pdftr/glossary/{matcher,extract,normalize}.py`，`backend/tests/unit/{test_glossary,test_user_glossary,test_q4b_glossary}.py`，`frontend/src/shared/text/termKey.ts`，`frontend/src/entities/glossary/{model,model.test}.ts`（新增规范键查询辅助，不改存储与导入导出语义），`frontend/src/features/job-terms/{terms,terms.test}.ts`。`sources.py:clean_zh` 已在 Q3c 修复，补测归 `test_q4b_glossary.py`。

#### 1. 三层区间仲裁与逐段校正

- `TermMatcher.match_spans(text: str) -> list[TermMatch]`：返回按原文升序排列的非重叠命中列表（每项含 `start: int, end: int, key: str, term: str, target: str`）。保留 `match(text, accept=None) -> dict[str, str]` 兼容入口。
  - 扫描在剥离 `</?(?:b|i|sup|sub)>` 保留原串偏移的可见视图上进行，`<m\d+/>` 与 URL（`https?://\S+`、`www.\S+`）作为不可跨越的阻断边界；多词间隔仅允许横向空白或连字符（明确拒绝跨句号、逗号、分号、斜杠与换行分隔的伪相邻词），含 `/`、`@` 等技术词作为完整 token 保留。
  - 索引按 `normalize_term_key` 分桶保存候选列表（替代 `setdefault` 覆盖），匹配时优先精确拼写命中，再按 `acr:`/`word:`/`lit:` 规则校验大小写与缩写形态；同起点按最长 span 优先，其次精确拼写优先。
- `TermSelection`：
  - `__init__` 使用 `normalize_term_key` 对齐 `user_terms` 与 `found.terms`：同规范键时用用户译法覆写 `self.paper`（与 `test_q3c_fixes.py:166` 和 `test_api_flow.py:655` 保持一致）。删除全局 `_phrase_targets` 与全词典 `_higher_keys` 屏蔽。
  - 新增 `match_segment(text: str) -> tuple[dict[str, str], dict[str, str], dict[str, str]]`：按 `user → paper → library` 顺序执行**原文区间占用仲裁**——`user` 先匹配并占用其命中 `[start, end)`；`paper` 仅接受与已占用区间无交集的命中并追加占用；`library` 仅接受与已占用区间无交集、且不与同规范键更高层冲突的命中。
  - `match_user`/`match_paper`/`match_library` 保留为兼容入口并统一委托给 `match_segment` 的对应层输出。更新 `test_user_glossary.py:88`：同一 span 内已被用户 `graph neural network` 占用的 `neural network` 不再重复返回，同时补充原文另一处独立出现 `neural network` 时正常放行、以及 `fair value` 与独立 `value` 同段共存的用例。
- `normalize_terms(terms: dict[str, str], body: str | Sequence[str], headings: Iterable[str] = ()) -> dict[str, str]`：
  - 接受段落序列（兼容单个字符串供旧单测直接调用），逐段统计出现次数与字面写法，彻底消除跨段交界假词；章节标题单独计数，仅凭正文（不含标题）出现次数判断与标题同名的术语（保留正文出现 `>= 2` 次的 `Recall`，剔除纯结构词 `_SECTION_ONLY`）；使用 `normalize_term_key` 区分缩写与普通词大小写，避免 `ADAM` 与 `Adam` 互相覆盖。

#### 2. 高频候选词与抽取截断保护

- `frequent_candidates(texts: Sequence[str]) -> tuple[str, ...]`：
  - 逐段剥离富文本标签、将公式占位符与 URL 置为边界；支持识别普通词与技术词 token（含连字符复合词 `off-policy`、`k-means`、含数字/符号技术词 `H2O`、`TCP/IP`、`AP@0.5`、`LoRA`、全大写及复数缩写 `GNNs`、`SVMs`），排除纯数字、4 位年份、常见计量串（如 `10ms`、`5kg`）和停用词。
  - 仅在同一段且相邻词间为纯空白时统计二元/三元词组（公式与标点切断），使用统一单复数/缩写归并（避免 `GNN GNNs` 产生伪二元词组），展示形式保留正文最常见原始大小写与技术分隔符。
  - 阈值与配额：普通词 `>= 5` 次、技术词（含连字符/斜杠/`@`/大小写混合/全大写缩写）`>= 2` 次、二元/三元词组 `>= 3` 次；另为含 `@` 或连字符/混合大小写的强特征指标词（如 `AP@0.5`）留至多 5 个低频配额（出现 `>= 1` 次即可入选，覆盖只在末章出现的指标）；候选总数上限 60 条（普通词 + 技术词 + 词组分别设上限防止短语挤掉高频词，总字符上限 1200），按频次降序、首次出现位置升序稳定排序。模型提示词仍限定最多抽取 45 条。
- `extract_terms` 截断与门控解析：
  - `parse(text: str, *, truncated: bool = False)`：`truncated=True`（即 `finish_reason in ("length", "max_tokens")`）时丢弃最后一条非空行（即使含 `=`，防止半截词条入库），仅解析可信完整前缀；若门控行本身被截断或未完整匹配则默认 `use_library=False`。
  - 门控行放宽为匹配 `^AI\s*公共词库\s*[:：]\s*(启用|停用)(?:\s*[（(][^）)\r\n]*[）)])?$`（允许成对括号附注，仍严格要求显式“启用”或“停用”）。
  - 拒答或无有效文本走既有空词表路径；任何情况下均保留 `found.usage = result.usage` 计费，日志只记截断标志与保留条目数。沿用 `max_tokens=1500` 与单次调用，不追加二次请求。

#### 3. 前端术语面板识别与遮蔽

- `frontend/src/shared/text/termKey.ts` 实现 `normalizeTermKey`，与 `term-key.fixture.json` 逐条对拍。`entities/glossary/model.ts` 新增 `findTermMatching(source, terms)`（先精确 `same` 匹配，再按 `normalizeTermKey` 匹配）；原有 `findTerm`、`upsertTerms`、`planImport`、存储键保持不变。
- `frontend/src/features/job-terms/terms.ts`：
  - `termRows` 用 `findTermMatching` 将浏览器最新用户术语覆盖到行译法上。
  - **替代项解析**：先去配对括号注释 `[（(][^）)]*[）)]`，再按 `[；;|、，,]` 以及**非两翼 ASCII 字母数字的斜杠** `(?<![A-Za-z0-9])/(?![A-Za-z0-9])`（支持浏览器兼容实现：扫描或等价拆分，保护 `TCP/IP`、`I/O` 完整不拆，同时拆开 `随机失活/丢弃法`）拆出替代项。
  - **标准化与边界匹配**：将 `</?(?:b|i|sup|sub)>` 删空（跨样式连词），将 `<m\d+/>` 替换为 `\0`（公式切断），折叠空白并仅移除至少一侧邻接 CJK 字符（`[\u3400-\u4dbf\u4e00-\u9fff]`）的空格，保留 ASCII 词间单空格。对每个替代项枚举标准化译文中的全部合法出现区间 `[start, end)`，若替代项首/尾为 ASCII 词字符 `[A-Za-z0-9_]`，要求命中前/后字符不为 `[A-Za-z0-9_]`（拦截 `GAN` 命中 `organic`、`RL` 命中 `world`，放行 `使用 RL 模型`、`使用RL模型`）。
  - **按段长译法实例级遮蔽**：对同一段 `seg`（仅 `status` 为 `done` 或 `edited`），预先收集本段所有关联术语的全部合法命中区间；判定术语 `T` 时，只有当 `T` 的**某次具体命中 `[s, e)`** 被同段**另一不同术语 `U`** 的某次严格更长命中 `[s2, e2)` 完整包含（`s2 <= s && e <= e2 && (e2 - s2) > (e - s)`）时，才遮蔽该次命中（同译法不互相遮蔽、部分重叠拼接不遮蔽）。只要 `T` 至少存在 1 个未被遮蔽的命中实例，即视为已采用；`renders(target, translation)` 导出函数保持签名兼容并用同一边界规则。

---

### Q4c　双端跨块切分、导出截断通报与调用方接线

为实现与 A/B/D 零重叠并发，Q4c 明确分为两段执行：

- **Q4c 第一段（纯渲染/导出快照/前端提示，可与 A/B/D 并行）**
  - **文件归属**：`backend/src/pdftr/rendering/export.py`，`backend/src/pdftr/pipeline/runner.py`（仅 `ExportResult` 结构扩展），`backend/src/pdftr/pipeline/usecases.py`（仅导出缓存/快照/版本 10 部分），`backend/src/pdftr/api/{routes_jobs,schemas}.py`；测试 `backend/tests/unit/{test_export_split,test_document_name,test_q4c_export_and_pipeline}.py`、`backend/tests/e2e/test_api_flow.py`（仅新增导出快照用例）；前端 `frontend/src/shared/text/split{.ts,.test.ts,.fixture.json}`、`frontend/src/features/export/{ExportDownloadLink.tsx,DownloadChoice.test.tsx}`；生成物 `frontend/openapi.json`、`frontend/src/shared/api/schema.d.ts`。
- **Q4c 第二段（端到端接线，待 A/B/D 合并后单分支完成）**
  - **文件归属**：`backend/src/pdftr/pipeline/{runner,usecases}.py`，`backend/src/pdftr/cli.py`，`backend/tests/unit/{test_q3b_translation,test_q4c_export_and_pipeline}.py`，`backend/tests/e2e/test_api_flow.py`。

#### 1. 双端有界原子切分与链接回归

- **统一线性扫描器（Python 与 TypeScript 同构，$O(N)$ 无回溯）**：
  - 先单趟扫描原串，提取白名单标签 `</?(?:b|i|sup|sub)>` 之外的可见单元序列（Python 用字符下标、JS 用 UTF-16 偏移，每个普通单元为 1 个完整 Unicode 码点、权重 1；每个 `<m\d+/>` 为独立公式单元、权重 1 且是扫描阻断边界）。
  - 在可见单元序列上按优先级顺序一次前向合并复合原子（记录原串起止区间及可见码点权重）：
    1. **公式占位符**：`<m\d+/>`（权重 1）。
    2. **URL**：起点匹配 `https?://` 或 `www.`（大小写不敏感，左侧非 ASCII 字母数字），向右消费 `[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]`，遇空白、公式占位符或非 ASCII 字符即停；从尾部剥离外部句末标点 `.,;:!?` 和未在 URL 内配对的多余右括号 `)` / 右方括号 `]`，以及非 IPv6 主机且非查询串末尾的独立数字方括号组（如 `example.com[12]` 中剥离 `[12]`）。权重 = 所含可见码点数。
    3. **数字方括号组**：`[`/`［` 开头，至配对 `]`/`］` 结束（不嵌套、不跨公式、长度 `<= 40` 码点），内部仅含 ASCII 数字、空格、`, - – — ; ， 、 ；` 且至少含 1 个数字。权重 = 所含可见码点数（内部夹有样式标签如 `[<b>12</b>]` 仍归为同一原子）。
    4. **小数/版本号连续数字**：可选单个前导 `v`/`V` + `\d+(?:\.\d+)+`（如 `3.14`、`v2.1.0`）。权重 = 所含可见码点数。
    5. **单 Unicode 码点**：其余单个可见码点（权重 1）。
- **权重前缀和与确定性落刀**：
  - 保留 `SegmentPart.chars` 权重（全零退化为总权重 1）。预计算原子码点权重前缀和 `prefix`，令 `total_vis = prefix[-1]`。
  - 第 `i` 处目标可见码点 `target_vis = (2 * total_vis * cumulative + total) // (2 * total)`（JS 在乘积可能超出 `Number.MAX_SAFE_INTEGER` 时转 `BigInt` 精确计算），在合法原子索引 `[start + 1, limit]` 中选使 `abs(prefix[k] - target_vis)` 最小的索引（同距选较小索引）。
  - 在目标原子索引 `± _SEARCH`（40 个原子步长）内寻找最近的单码点标点原子（`_BREAK_AFTER`），落刀后先吸收紧接闭合样式标签，再吸收紧接的行首禁则单码点原子（`_NO_START`），最后执行 `_balance_rich_parts`。对于不含复合原子的既有 9 条夹具，切分结果与旧算法逐字符完全一致。
- **共用夹具与链接回归**：扩充 `frontend/src/shared/text/split.fixture.json`，新增补充平面字符、组合符、NBSP/BOM、嵌套样式内的 `[<b>12</b>]`、带查询串/端口/尾标点 URL、`[1, 3–5]`、`v2.1.0`、原子数少于块数等向量，由 `test_export_split.py` 与 `split.test.ts` 双端逐片断言，并用 `place_links/locate` 确认跨块引文与 URL 链接落位与双语跳转目标正确。

#### 2. 导出截断页码与快照绑定（精简可靠版）

- **真实丢行统计**：
  - `_place(...) -> bool`：仅在 `translated=True` 且发生真实丢行（`count < len(lines)`，或裁框为空 `box.is_empty` 但存在非空译文 token）时返回 `True`；所有行保留（含极矮框缩小后保住全部单行）或未翻译回退原文时返回 `False`。
  - `_typeset_all` 返回 `(placed, sorted_overflow_pages)`，页码为原 PDF 页索引加一（1 起、去重升序），`mono` 与 `dual` 共用同一编号。`export_pdf` 保持返回 `bytes` 不变；`export_pdf_to_path(...) -> list[int]` 返回截断页码列表（同步更新 `test_document_name.py` 中的 6 个 monkeypatch fake `render` 返回 `[]`）。
- **精简 `.meta.json` 缓存与提交顺序**：
  - `EXPORT_CACHE_VERSION = 10`。`ExportResult` 保持 `NamedTuple` 并新增默认值字段：`filename: str`, `path: Path`, `size_bytes: int`, `digest: str = ""`, `overflow_pages: tuple[int, ...] = ()`。`routes_jobs.py` 改用字段访问消除三元组解包。
  - 缓存文件对：`exports/{mode}-{side}-{digest}.pdf` + 同名旁路 `exports/{mode}-{side}-{digest}.meta.json`（内容仅 `{"v": 10, "overflow_pages": [3, 7]}`）。
  - 发布顺序：先由 `pdf_destination(out)` 原子发布 PDF，再由同目录 `.part` 临时文件原子 `replace` 发布 `.meta.json`。
  - 命中判定：`pdf.is_file()` 且 `.meta.json` 存在、为合法 JSON、`v == EXPORT_CACHE_VERSION` 且 `overflow_pages` 为升序正整数列表；任意缺失或损坏视作未命中，直接重新渲染并以原子替换覆盖。
  - 并发与清理：async 路径沿用 `export_once` 请求合并与 `export_access` 下载保护；同步兼容入口 `export_job` 维持二元组 `(filename, path)` 返回，用 16 槽固定路径锁（同 `_LINK_CACHE_LOCKS` 模式）防极少数同步竞争即可。旧缓存清理在既有 `export_cleanup_access` 内进行：对超过 60s 宽限期的非当前旧快照，先删除 `.meta.json` 再删除 `.pdf`，同时清理超过 60s 的孤立 `.meta.json` 和 `.part` 临时文件。
- **API 契约与前端提示**：
  - `ExportPrepareOut` 新增 `overflow_pages: list[int] = Field(default_factory=list)`（OpenAPI 生成为可选属性，兼容 `DownloadChoice.test.tsx` 既有 mock）。
  - `POST /api/jobs/{job_id}/export/prepare` 返回 `overflow_pages` 与 `download_url = f"/api/jobs/{job_id}/export?mode={mode}&side={side}&snapshot={result.digest}"`。
  - `GET /api/jobs/{job_id}/export` 新增可选参数 `snapshot: str | None = Query(default=None, pattern=r"^[0-9a-f]{16}$")`：
    - 带 `snapshot` 时，在 `export_access` 内直接校验该任务下 `exports/{mode}-{side}-{snapshot}.pdf` 与对应 `.meta.json` 是否为有效 pair，有效即返回 `FileResponse`（保留 `Content-Disposition` 与 HTTP Range/206），已清理或不存在返回 404（提示重新下载）；
    - 省略 `snapshot` 时维持原行为（准备并返回最新 PDF），兼容 `HomePage.test.tsx`、`JobPage.test.tsx`、`test_api_flow.py` 的直接下载链接与测试。
  - **前端最小改动覆盖三端**：仅修改 `frontend/src/features/export/ExportDownloadLink.tsx`——`ExportDownloadLink` 在 `prepareExport` 返回非空 `overflow_pages` 时拼装提示文案（如「已开始下载，第 3、7 页部分译文未写入 PDF」；超过 5 页时显示「已开始下载，第 1、2、3、4、5 页等共 N 页部分译文未写入 PDF」），通过既有 `report("started", message)` 透传给 `ExportDownloadStatus`（`started` 态改为渲染 `message || "已开始下载"`），自动同时覆盖首页卡片（`HomeJobRow`）、任务等待页（`JobPage`）和阅读器菜单（`DownloadMenu` 复用 `onStateChange`），零改其他页面组件。重新准备、切换格式或失败时自动清空上次告警。
  - 同步用 `cd backend && uv run python scripts/export_openapi.py ../frontend/openapi.json` 与 `cd frontend && npm run gen:api` 更新两个生成文件。

#### 3. 管线、重翻、CLI 与术语面板接线（Q4c 第二段）

- `runner.py:_glossary` 与 `cli.py`：正文候选词与 `normalize_terms` 传入排除 `BlockType.TITLE` 与 `BlockType.REFERENCE` 的段落原文序列，标题列表单独传给 `extract_terms` 和 `normalize_terms`。
- `runner.py:_translate`、`usecases.py:retranslate_segments`、`cli.py`：保持 `DocContext(title=..., abstract=..., domain=..., terms=selection.paper)` 将本文术语表留在 `system_prompt` 固定前缀，同时传入 `lookup_term_hits=selection.match_segment` 和文档完整原序 `context_segments`。
- `usecases.py:job_terms`：改用 `selection.match_segment(seg.source)` 遍历各段三层命中，按 `normalize_term_key` 聚合 `segment_ids`，冲突时按 `user > paper > library` 选定单行来源与译法，保持每行 `source` 唯一（直接兼容 `TermsPanel.tsx` 的 `key={row.source}`）。
- `usecases.py:retranslate_segments` 复用 `pipeline._concurrency(secrets)`（不再写死 4 路并发），45s `total_timeout_s` 覆盖排队与 400 兼容重发。`cli.py` 根据 `settings.llm_protocol` 选择 `AnthropicChat` 或 `OpenAICompatChat`，复用 `settings.llm_concurrency or default_concurrency(settings.llm_base_url)`，并用 `chat_session` 正确关闭客户端。
- 同步更新 `test_q3b_translation.py`（`PROMPT_VERSION == "v5"`、Self-attention 示例与候选词顺序断言）和 `test_api_flow.py:673`（按段作用域用户术语格式断言）。

---

### Q4d　适配器兼容、跨行连字符与跨样式公式

**文件归属**　`backend/src/pdftr/translation/{openai_compat,anthropic_compat,segmenter}.py`，`backend/src/pdftr/fusion/formula_align.py`，`backend/tests/contract/{test_openai_compat,test_anthropic_compat}.py`，`backend/tests/unit/{test_segmenter,test_formula_align,test_q4d_compat_segment_formula}.py`，`frontend/src/features/settings/{SettingsPanel.tsx,SettingsPanel.test.tsx}`，`frontend/src/features/segment-edit/SegmentEditor.tsx`（仅 L226 精修按钮 `title` 文案）。

#### 1. 适配器能力边界与 HTTP 400 单次降级

- **显式能力边界与指纹**：
  - `OpenAICompatChat.provider_id` 改为 `f"openai:{self.dialect}:{base_url.rstrip('/')}"`，将实际方言纳入缓存隔离；`generic` 方言维持默认请求形态，不按模型名猜测第三方中转站能力。
  - `AnthropicChat` 收紧 `_EFFORT_MODELS` 为精确前缀与版本边界正则：`^claude-(?:opus|sonnet|haiku)-(?:4[.-][6-9]|[5-9](?:[.-]\d+)?)(?:$|-)`（兼容 `claude-sonnet-4-6`、`claude-opus-5-5` 及日期后缀，拦截 `claude-haiku-4-5`、`claude-opus-50` 及未知前缀）。
  - 同步更新 `SettingsPanel.tsx`（L301/L308「默认」「精翻」说明）与 `SegmentEditor.tsx`（L226「精修」tooltip），明确思考强度依服务商与模型支持而定、费用按实际用量计，移除“固定不开启思考”与“约 4 倍费用”的绝对承诺。
- **HTTP 400 精准识别与单次降级**：
  - 仅在 `status_code == 400` 时判定；先排除上下文超长（`context_length_exceeded` / `maximum context length` / `too many tokens` / `prompt is too long`）等非参数不兼容 400，原样抛 `ChatError(retryable=False)`。
  - 解析 `e.body` 中的 `error` 对象（读取 `param`、`code`、`type`、`message`），严格对比本次实际发出的请求参数快照 `sent`，只接受命中以下已发送字段的明确不兼容信号（`param` 精确相等，或 `message` 同时含准确字段名与不兼容关键词 `unsupported|unknown|unrecognized|not supported|not allowed|extra|unexpected|invalid`）：
    - **OpenAI 兼容**：`max_tokens`（仅当 `param == "max_tokens"` 或消息同时提及 `max_tokens` 与 `max_completion_tokens` / 明确不支持 `max_tokens`）→ 记 `use_max_completion_tokens`；`reasoning_effort` → 记 `drop_reasoning_effort`；`thinking`（在 `extra_body` 中）→ 记 `drop_thinking` 且联动 `drop_reasoning_effort`；`temperature` → 记 `drop_temperature`。
    - **Anthropic**：`output_config`（或消息含 `output_config`/`effort` 不支持）→ 记 `drop_output_config`；`thinking` → 记 `drop_thinking`。
  - 降级标记存入实例级单调集合 `self._disabled: set[str]`（事件循环内 `self._disabled |= delta` 原子合并，绝不为探测持锁等待网络）。重算 `next_kw = self.request_kwargs(opts)`，仅当 `next_kw != sent` 时执行最多 1 次重发（降级重发固定 `max_retries=0` 避免叠乘 SDK 重试），仍失败直接抛原 `ChatError`。
  - 仅当本次请求受语义降级标记集合 `_SEMANTIC_FALLBACKS = frozenset({"drop_thinking", "drop_reasoning_effort", "drop_output_config", "drop_temperature"})` 影响（即本实例 `self._disabled & _SEMANTIC_FALLBACKS` 非空且本次调用原本会发送对应思考/采样参数）时，返回 `ChatResult(..., compatibility_fallback=True)` 禁写共享缓存；仅 `use_max_completion_tokens` 参数名替换不降级推理语义，保持 `compatibility_fallback=False` 正常复用缓存。日志仅记录协议、降级标记与状态码，不原样打印远端错误体。外层超时、取消与 `report.requests` 逻辑计数保持不变。

#### 2. 跨行复合词断词与首块角色

- `segment(doc, ...)` 填充 `Segment.role = block.type`（链头块类型）。
- **单层全文行内词形证据 + 确定性形态规则**（取代三级作用域索引）：
  - 在 `segment(doc, ...)` 入口一次性扫描所有候选块内**同一视觉行中不跨行的完整词形**，构建两个小写集合：`inline_hyphenated`（如 `off-policy`）与 `inline_joined`（如 `representation`），禁止把跨行断词自身计为证据。
  - `_join(prev, nxt, *, hyphenated=(), joined=())`（默认空集合，完全向后兼容 `test_segmenter.py` 直接调用），同时作用于块内视觉行拼接与 `continues_from` 跨块拼接：
    1. 仅当 `plain_prev` 末尾为软断字符 `U+00AD` 或单个 ASCII `-`（排除 `--`、数学减号 `−–—` 与 URL 尾段）且 `plain_next` 首字符为小写 `[a-z]` 时触发断词判断；
    2. `U+00AD` 直接删除并合并；
    3. 提取左词段 `left` 与右词段 `right`：若 `left` 自身已含 `-`（`left-to-` + `right`），或 `left` 为单字母/含数字（`k-means`、`n-gram`、`Q-learning`、`2-stage`、`3D-printed`、`L1-norm`），保留连字符；
    4. 查全文行内证据：`cand_h = f"{left}-{right}".lower()` 在 `hyphenated` 中（含两者冲突保守情形）保留连字符；仅 `cand_j = f"{left}{right}".lower()` 在 `joined` 中则删连字符合并；
    5. 无行内证据时，仅当 `left.lower()` 属于超高置信非音节前缀白名单 `{"self", "off", "cross", "zero", "few", "well", "half", "quasi", "pseudo"}` 且 `len(right) >= 3` 且 `right.lower()` 不在常见屈折/派生后缀集 `{"ing", "ed", "er", "est", "es", "ly", "ish", "al", "ize", "ism", "ness", "ment", "tion", "sion", "ity", "ers", "ings", "ies", "ful", "less"}` 时保留连字符（防 `cross-ing`、`well-ness`、`self-ish` 误留；`multi`/`meta`/`real`/`end`/`fine` 因易误伤 `multiple`/`realize`/`endure`/`metabolic` 不入无证据白名单，连同 `non-linear`/`semi-conductor` 统一交全文行内证据判定）；
    6. 其余沿用原规则删连字符并执行 `_merge_emphasis`，保留闭合样式标签与上下标/公式边界。
- **序数回收语境守卫**：
  - `textual_formula(latex: str, before: str = "", after: str = "")` 新增可选参数 `after=""`（兼容既有直接调用），`_textual_formulas` 同时传入紧邻后文。
  - 回收序数要求同时满足：① 数字与英文后缀合法配对（`n % 100 in (11, 12, 13) → th`，否则末位 `1→st, 2→nd, 3→rd`，其余 `th`，拦截 `2^{st}`、`1^{rd}` 等真数学幂）；② `before` 末尾与 `after` 开头均不为数学算符 `= + - * / < > ≤ ≥ ≈ × ·`。

#### 3. 跨样式撕裂公式合并

- `merge_torn_formulas(line, chars)` 保留相邻「已定位公式、文本、已定位公式」、唯一字形归属、桥接原字一致与单视觉行守卫，按以下确定条件扩展：
  1. **互补外层边界**：复用 `_delimiters`，要求左端未匹配符恰好 1 个开符 `o`、右端恰好 1 个闭符 `c`，且 `(o, c) ∈ {("(", ")"), ("[", "]"), (r"\{", r"\}"), (r"\langle", r"\rangle")}`，合并后 `_delimiters(merged)[1] == []`。
  2. **局部字号间距守卫**：设 `body_size` 为该行字形中位字号，要求 `middle.bbox.x0 - left.bbox.x1 <= 1.5 * body_size`、`right.bbox.x0 - middle.bbox.x1 <= 1.5 * body_size`，且无严重交叠（双向重叠均 `<= 0.3 * body_size`），拦截跨栏与远间距误并。
  3. **确定性短数学桥接与散文硬屏蔽**：桥接去首尾空白后长度 `<= 16`，严禁含 `\`, `_`, `^`, `{`, `}`, `$`；由至多 3 个简单项（数字 `\d+(?:\.\d+)?` 或 1–8 位字母标识符）与分隔/运算符 `, ; : = + - − * / < > ≤ ≥ · × |` 组成。凡任一字母词（lowercase）落入英文连接/介词停用词表 `{and, or, if, in, of, to, as, is, are, be, by, for, with, from, where, when, then, than, the, that, this, not, vs}`，无论斜体或粗体一律拒绝合并。
  4. **字母字形独立证据**：桥接中每个字母 token 须满足以下之一：① 数学字体（`MATH_FONT`）或斜体（`c.italic`）；② 数学函数白名单（`MATH_WORDS` 排除易作单位的单字母 `m/g/s`）且邻接运算符/分隔符；③ **粗体单字母向量**（`len(token) == 1` 且 `c.bold` 且邻接逗号或运算符，拼接时包裹为 `\mathbf{x}`）。纯正体非数学字体字母一律拒绝（保留 `test_formula_align.py:113-114` 负例）。
  5. **白名单符号归一化**：仅将 `− → -`、`× → \times`、`· → \cdot`、`≤ → \le`、`≥ → \ge` 转为 LaTeX 后拼接。

---

### 文件边界与执行顺序

| 阶段 | 可并行内容 | 写文件归属（严格零重叠） |
| --- | --- | --- |
| **基线** | 锁定共享契约与共用规范键夹具 | `domain/{ir,text,llm}.py`、`frontend/src/shared/text/term-key.fixture.json`、`tests/unit/test_text_rules.py` |
| **阶段 1：Q4a** | 事实锚点、粗斜体拆合、编号协议、前文上下文与单段确定性缓存 | `translation/{batching,prompt,service}.py`、`tests/unit/{test_translation_parts,test_translation_service,test_fuse_synthetic,test_q4a_translation_guardrails}.py` |
| **阶段 1：Q4b** | 三层 span 仲裁、候选词与截断保护、前端术语规范键与面板遮蔽 | `glossary/{matcher,extract,normalize}.py`、`tests/unit/{test_glossary,test_user_glossary,test_q4b_glossary}.py`、`frontend/src/shared/text/termKey.ts`、`frontend/src/entities/glossary/{model,model.test}.ts`、`frontend/src/features/job-terms/{terms,terms.test}.ts` |
| **阶段 1：Q4c-1** | 双端原子切分、导出截断页码与快照绑定、前端提示与 OpenAPI 生成 | `rendering/export.py`、`pipeline/runner.py`（仅 `ExportResult`）、`pipeline/usecases.py`（仅导出快照段）、`api/{routes_jobs,schemas}.py`、`tests/unit/{test_export_split,test_document_name,test_q4c_export_and_pipeline}.py`、`tests/e2e/test_api_flow.py`（仅导出快照段）、`frontend/src/shared/text/split{.ts,.test.ts,.fixture.json}`、`frontend/src/features/export/{ExportDownloadLink.tsx,DownloadChoice.test.tsx}`、`frontend/openapi.json`、`frontend/src/shared/api/schema.d.ts` |
| **阶段 1：Q4d** | 双协议 400 单次降级、跨行复合词断词、跨样式撕裂公式与精翻文案 | `translation/{openai_compat,anthropic_compat,segmenter}.py`、`fusion/formula_align.py`、`tests/contract/{test_openai_compat,test_anthropic_compat}.py`、`tests/unit/{test_segmenter,test_formula_align,test_q4d_compat_segment_formula}.py`、`frontend/src/features/settings/{SettingsPanel.tsx,SettingsPanel.test.tsx}`、`frontend/src/features/segment-edit/SegmentEditor.tsx` |
| **阶段 2：Q4c-2** | 待 A/B/C-1/D 合并后单分支接线：管线/重翻/CLI 接入按段术语、全文前文、统一并发与协议 | `pipeline/{runner,usecases}.py`、`cli.py`、`tests/unit/{test_q3b_translation,test_q4c_export_and_pipeline}.py`、`tests/e2e/test_api_flow.py` |
| **阶段 3：主控收口** | 回归验收、只读复合测试复核、串行全量门禁与盲评 | 复核 `test_q3c_fixes.py`（按 v3 预期 100% 原样通过）与 `test_openapi_sync.py`、文档与盲评报告 |

---

## 四、测试策略与验收门禁

1. **合成回归**：每个包先跑各自定向测试，阶段 2 合并后跑全部 Q3/Q4 单元、契约及 e2e 测试。事实锚点分别统计误报与漏报，覆盖句中引文（`We use ResNet [12]`）、`A[12]` / `A [12]`、`[2020]`、`[0,1]`、`interval [1,3]`、引文重复与重排、作者/年份脚注、`10<sup>−6</sup> → 10<sup>6</sup>`、化学式与变量下标交换、全角数字、裸数值改写及 `Layer<sub>1</sub>`。缓存覆盖单段命中跳过重发、单段补救回写缓存、同文异前文隔离、同领域异标题/摘要隔离、同地址异方言隔离及降级产物不入缓存。并发测试使用可控事件同步验证 400 单次降级、导出快照编辑隔离、损坏 `.meta.json` 自动重建、Range/206 与孤儿回收。
2. **串行后端门禁**（控制本机内存，严禁并发拉起）：`uv run --frozen ruff format --check .`、`uv run --frozen ruff check .`、`uv run --frozen pyright`、`uv run --frozen lint-imports`、`uv run --frozen pytest -q`。接口变更后从 `backend/` 运行 `uv run python scripts/export_openapi.py ../frontend/openapi.json`，再从 `frontend/` 运行 `npm run gen:api`。
3. **串行前端门禁**：Node 22 下串行执行 `npm run lint`、`npm run typecheck`、`npm test`、`npm run build`。按首页卡片、任务等待页、阅读器下载菜单三条路径，在桌面（1440×900）与移动（390×844）视口验收正常下载、部分页未写入提示、准备失败与再次下载。
4. **公开论文盲评**：对照 Q3c，在公式密集、含化学式/脚注、学科交叉、长文跨栏与双语导出公开样本上抽样评审，记录首试通过率、事实锚点误报/漏报率、逻辑请求数 vs 真实 HTTP 次数、Prompt Cache 命中率、token 消耗与费用，同时评估裸数字、公式角色、否定主语、条件附着与导出截断准确度。
5. **变更验收**：核对 `git diff --check`、无真实用户数据或密钥混入、生成文件一致且各包边界清洁。

---

## 五、评审修订记录

- **v1（主控 Neon 初稿，2026-11-24）**　定义 Q4 决策、共享契约与四包初版。
- **v2（Codex 审核修订）**
  1. 引文增加源语境证据和歧义降级，上下标绑定基底/元素与规范符号，将裸数值及脚注留在软检查与盲评。
  2. 粗斜体改用整栈合法性、原文有无强调和覆盖区间并集守卫；编号解析加入 `</translations>` 终止标记与竖线续行判定。
  3. 提出按全文原序预取前文、对每段显式注入及首批精确请求级缓存键。
  4. 升级三层按实际原文区间仲裁、本文术语改按段注入、前端精确优先与实例级长译法遮蔽。
  5. 确认 `sources.py` 已在 Q3c 修复，补齐逐段正文校正、技术词候选、末章低频配额、截断安全前缀及门控括号附注。
  6. 跨块切分改用双端同口径有界扫描、Unicode 码点权重与共用 `split.fixture.json`。
  7. 导出改按实际丢行统计，加入 `.meta.json` 旁路与 `snapshot` 下载绑定，复用 `ExportDownloadLink` 覆盖三端。
  8. 适配器 400 按实际已发送参数做单次降级与实例级单调记忆，同步更新设置页精翻文案。
  9. 跨行断词改用行内证据与受限形态，公式合并补间距、互补边界与数学桥接守卫。
  10. 拆开 `test_translation_parts.py` 版本断言归属，补齐 `cli.py`、测试与 OpenAPI 生成文件边界，分两段集成 Q4c。
- **v3（Neon 二轮审核修订）**
  1. **【核心推翻】取消首批请求级缓存与部分命中重发整批，回归「单段级确定性缓存键 + 按段号作用域注入」**：实查 `service.py`、`test_translation_service.py`（L70-88、L515、L588-593、L634-637）与 `test_q3c_fixes.py`（L262-267）及 DeepSeek 计价（输出 4 倍于输入未命中、200 倍于缓存命中）。v2 方案在断点续跑、选页试翻、单段补救重跑时会令缓存命中率骤降并重复付费生成已缓存段，且直接打红 4 个核心单测。改为单段确定性指纹（补 `validation_version`、`title`、`abstract`、`role`、`prev_context` 与含 `dialect` 的 `provider`，空默认值保持与直接调用 `cache_key` 比特级兼容），命中缓存段直接跳过不重发，单段补救通过校验后照常回写缓存；仅 `use_cache=False` 与 `compatibility_fallback=True` 不写缓存。
  2. **【核心推翻】保留本文术语表（`DocContext.terms`）在 `system_prompt` 固定前缀中**：实查 `prompt.py:45-57`、`test_translation_parts.py:69-73`、`test_api_flow.py:655` 及供应商前缀缓存机制（DeepSeek 98% 折扣、OpenAI/Anthropic 1024 token 门槛）。本文术语表仅 ~200–350 tokens，留在 `system_prompt` 成本可忽略且保护前缀缓存与全文派生词一致性，且 `TermSelection.__init__` 已用 `normalize_term_key` 以用户译法覆写冲突项；仅将未进 `selection.paper` 的额外用户术语与经 span 仲裁的公共参考词库按段号注入 `user_prompt`。
  3. **【规则收紧】收敛事实锚点与粗斜体守卫为确定低误报算法**：引文从模糊书目语境白名单改为「黑名单排除数组索引/含 `0` 区间/数学数据结构语境/4 位年份 + 锁定 `1..999` 正整数引文」，大幅提升句中引文（如 `We use ResNet [12]`）召回率且保持零误报；数值上下标去掉复杂化学式/变量分类器，收敛为「负/显式符号上标 + 数字/单位基底正数上标 + 纯数字下标（仅 1–2 字母基底锁基底，避开 `Layer<sub>1</sub>` 误杀）」；局部粗斜体整段误包守卫定死为「原文外部实词 `>= 2` 时，译文去样式/标点/空白后外部字符数为 `0` 即拦截」；补齐 `</translations>` 缺省兼容与竖线续行 4 条件确定规则。
  4. **【KISS 瘦身】化简导出 `.meta.json` 状态机、跨行断词与撕裂公式合并**：保留 `snapshot` 绑定与真实丢行统计，将 `.meta.json` 瘦身为 `{"v": 10, "overflow_pages": [...]}`，取消多余 `digest/size_bytes` 二次校验与同步重型锁，并明确更新 `test_document_name.py` 6 处 fake `render` 返回值；跨行断词砍掉三级作用域索引改为单层全文行内证据 + 明确前缀/屈折后缀白黑名单，序数回收加入合法英文序数配对 + 两侧算符排除；撕裂公式合并给出确定性桥接词法、英文散文停用词硬屏蔽、粗体单字母向量转 `\mathbf{x}` 与字号间距阈值。
  5. **【细节固化】锁定共享契约接口签名与双端算法**：在基线锁定 `normalize_term_key` 的 `acr:/lit:/word:` 完整确定算法与 `term-key.fixture.json`；明确章节切批以标题作为新批起点（避免 `segmenter.py:332` 下标题 `section=""` 被切成 1 段小批）；收敛 `split.ts`/`export.py` 线性扫描器与 `terms.ts` 保护 `TCP/IP` 的替代项拆分规则；明确 Anthropic `_EFFORT_MODELS` 精确版本边界正则与双协议 400 字段映射表。
  6. **【查全纠偏】补齐 4 处 v2 漏列的联动文件，实现真正零重叠并行**：① `test_fuse_synthetic.py:114` 含 `PROMPT_VERSION == "v4"`，划归 Q4a；② `test_user_glossary.py:88` 断言旧子短语放行，划归 Q4b 更新；③ `SegmentEditor.tsx:226` 同样含「费用约为普通的 4 倍」tooltip，与 `SettingsPanel.tsx` 一并划归 Q4d；④ `test_q3b_translation.py` 同时含 Prompt v4 与 runner/cli 断言，移至 Q4c 第二段统一更新，并确认 `job_terms` 维持 `source` 唯一直接兼容 `TermsPanel.tsx`、`ExportDownloadLink.tsx` 单文件修改即覆盖三端。
- **Final（主控汇总定稿，2026-11-24）**
  1. **采纳 Neon v3 全部核心纠偏与查全补漏**：确认回归「单段级确定性缓存键 + 按段号作用域注入」、保留 `DocContext.terms` 在 `system_prompt` 固定前缀、收敛 `.meta.json` 与事实锚点/断词/公式合并为确定算法、补齐 `test_fuse_synthetic.py` / `test_user_glossary.py` / `SegmentEditor.tsx` / `test_q3b_translation.py` 4 处文件归属。
  2. **拍板 3 项最终分歧**：
     - **400 降级缓存隔离**：区分参数名替换（`use_max_completion_tokens`，未改变推理语义，保持 `compatibility_fallback=False` 允许正常复用缓存）与语义降级（`drop_thinking`/`drop_reasoning_effort`/`drop_output_config`/`drop_temperature`，标 `compatibility_fallback=True` 禁写共享缓存）。
     - **引文锚点策略**：定案采用 v3 黑名单排他路线（保障句中裸引文如 `We use ResNet [12]` 零漏检），并补齐常见代码容器名（`arr/vec/mat/tensor/layer/...`）与含数字标识符无空格紧贴 `[` 的索引排除。
     - **跨行复合词无证据前缀白名单**：剔除易误伤 `multiple`/`metabolic`/`realize`/`endure`/`finest` 的 5 个前缀，最终锁定 9 个超高置信前缀 `{"self", "off", "cross", "zero", "few", "well", "half", "quasi", "pseudo"}` 并扩充屈折/派生后缀排除集；其余复合词交全文行内证据准确裁决。

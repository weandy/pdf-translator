# 批次 D 实施规格计划：TR-011 术语消歧 + TR-012 Prompt v8 调优 + A/B/C 三组消融

> **For Antigravity:** 本规格由 Neon 与 Codex 双盲独立出案，经 Codex (`apice/gpt-6.2-astra xhigh`) 深度评审，由主 Agent（Neon）综合代码事实与勘误后定稿。实施严格遵循独立 `git worktree` + 多 Agent 零重叠并行 + 主 Agent 串行门禁。**达标才升 `v8`**：在真实 LLM 双盲门禁全部通过前，生产默认严格锁定 `PROMPT_VERSION = "v7"` 且不污染生产 `backend/src/` 业务代码。

**Goal:** 在生产零缓存雪崩、零既有回归的前提下，交付一套完整、可离线 100% 确定性自检、接入真实凭据后可一键执行 `A / B / C` 三组消融与异构双盲 MQM 评审的实验与准入体系，分别验证 **TR-011（跨学科近义专业术语消歧）** 与 **TR-012（翻译 Prompt `v8` 两处微创规则）** 的独立净收益，并依据硬性门禁分别决定生产晋级与独立回滚提交。

**Architecture:**
- **生产零侵入隔离**：所有实验工具、候选补丁、合成语料与离线/在线运行器统一落在仓库顶层 `spikes/batch_d/`；真实双盲达标前**不修改** `backend/src/pdftr/` 任何生产代码（不加过渡 profile 开关、不接无消费点的常驻诊断日志、不提前加 `v7` 缓存回读兼容层）。
- **固定 Git 对象三树同源执行**：运行器用 `git -c core.autocrlf=false archive --format=tar <BASE_SHA> backend/src/pdftr` 导出 LF 临时源码树，从提交树读取 `tr011.patch` / `tr012.patch`，在仓库外隔离目录先 `git apply --check --whitespace=error` 再构造 `A`（基线）、`B`（`A + tr011.patch`）、`C`（`A + tr011.patch + tr012.patch`）三棵完整源码树，并启动独立子进程优先导入对应临时树（强制校验 `pdftr`、`extract`、`prompt`、`service` 的 `__file__` 均位于目标临时树内），彻底消除 Prompt 漂移、父进程按名导入串组与 Windows CRLF 补丁失败问题。
- **B/C 严格单因素归因**：每篇每轮仅做 A、B 各一次术语抽取，**C 逐字节复用 B 的归一化术语快照**（`domain`、`terms`、`use_library` 及有序项摘要），确保 `C - B` 纯净衡量 TR-012 两条 Prompt 修订的增量效应。
- **四层依赖与前端边界**：严守后端 `api → pipeline → 能力层 → domain` 四层架构；前端零改动；隐私红线 100% 强制守卫（全部语料与示例为新撰通用合成学术文本，零真实用户论文原文、零真实文件名、零 emoji）。

**Tech Stack:** Python 3.12（仓库已有 `backend/.venv`）、Git、标准库、Pydantic v2、既有 `pdftr.glossary` / `pdftr.translation` / `pdftr.pipeline` 模块与 LLM 适配器（零新增第三方依赖）。

**Frozen Baseline:** `main` 提交 `c15018594d1078d943086221a38baa5f18d4466c`（`PROMPT_VERSION = "v7"`、`VALIDATION_VERSION = 7`、`links.VERSION = 7`、`EXPORT_CACHE_VERSION = 17`）。

---

## 1. 双盲方案共识、分歧裁定与勘误收口

| 议题 | 最终裁定 | 代码与数据依据 |
|---|---|---|
| **候选隔离机制** | 采纳 `spikes/batch_d/` 单文件补丁 + `git -c core.autocrlf=false archive` 临时源码树 | `service.py:45–52` 按名导入常量；Windows 下裸 `git archive` 因 `core.autocrlf=true` 致 LF 补丁检查失败，关闭该次归档 `autocrlf` 后退出 0；且避免未达标前向 `backend/src/` 注入临时开关。 |
| **碰撞诊断归属** | 归入 `spikes/batch_d/term_diagnostics.py` | 复用生产 `normalize_term_key`（不自造小写规则），识别恶性撞名、良性缩写/别称、用户有意同译、漏抽与良恶混合组；避免无明确运维消费点时把用户术语写进常驻生产日志。 |
| **B/C 术语快照** | 强制每篇每轮 B/C 共享同一份 B 的完整归一化快照 | `runner.py:737–772` 与 `service.py:619–644` 将 `domain`、`terms`、`use_library` 全部注入翻译上下文；不共享快照会让 `C - B` 混入抽取随机性。 |
| **v7 缓存回读兼容层** | 不在本批写进生产 `service.py`，仅作为 C 晋级阶段可选策略保留 | 遵循 KISS 与“无依据不加兼容层”；待 C 真实达标时，若冷调用预算可承受则直接换 `v8`，否则再以独立可回滚提交启用单代 v7 回读并单列 `legacy_cached`。 |
| **合成语料规模与路径** | 采用 **8 篇开发 + 24 篇留出**（4 学科 × 每科 6 篇 × 每篇 4 段 = 128 段），存放于 `spikes/batch_d/fixtures/` | `.gitignore:13` 的 `data/` 会忽略 `spikes/batch_d/data/`，改用 `fixtures/`；排除独立 `accuracy` 作为入表考核项（实测被 `extract.py:52–55` `_GENERIC_UNIGRAM_WORDS` 过滤）。 |
| **Prompt 长度控制** | TR-012 两条精确追加共 **91 Unicode 码点**（硬上限 `<= 120`） | 吸收 `docs/02-spikes.md` S10 `v3` 留出掉分教训（98.4 → 97.6），保持 `_target_problems` 与 `VALIDATION_VERSION = 7` 不变。 |

---

## 2. 冻结的候选 Prompt 与基线指纹

### 2.1 基线固定指纹（用于离线回归金样）
- 基线 `BASE_SHA`：`c15018594d1078d943086221a38baa5f18d4466c`
- 基线 `extract.PROMPT` UTF-8 SHA-256：`ee4f67a94676b2eab3fc34e9bfdea5df34c7142ece9e9d9289c6bed1f3821550`
- 基线 `prompt.RULES` UTF-8 SHA-256：`eb9a1afc2aa1f9c4c2011b9be366270538b656d7e14e22a3a30e28d5c9609556`
- 基线 `cache_key` 金样（源文 `Inspect the sample.`、空公式、模型 `synthetic-model`、空术语、`quality="default"`、其余默认）：`14c5408c8783c2b4e7d0910dc43749963374a40cedb5e155981e9f496cda6f3d`

### 2.2 TR-011 候选补丁（`spikes/batch_d/tr011.patch`）
- **唯一改动目标**：`backend/src/pdftr/glossary/extract.py`（L75「使用中文学界通行译法。」之后新增 1 条通用约束）：
  ```text
     - 对需入表且本文明确区分的相近概念，选用能体现差别的通行译名。确指同一概念的别称、缩写或全称可同译。保留用户指定译法。
  ```
- **设计边界**：不硬编码单一学科词典，不在提示中塞入领域词对示例，不重复声明已有 `extract.py:361–364` 用户复合词约束。

### 2.3 TR-012 候选补丁（`spikes/batch_d/tr012.patch`）
- **唯一改动目标**：`backend/src/pdftr/translation/prompt.py`（更新文档头、实验树内 `PROMPT_VERSION = "v8"` 及两处精确句末追加，**净增严格为 91 Unicode 码点**）：
  1. 在 L26 现有绑定条款句末追加（44 码点）：
     `即使式内已有相应记号，也须译出式外明示的分布或关系及其谓语，保留其对象和原文的确定程度。`
  2. 在 L32 现有意译条款句末追加（47 码点）：
     `同一编号内，让步或条件从句被误断且主句归属明确时，可调整标点接回原主句。归属不明时保留原句界。`
- **不改项**：不增标题、不增示例、不改 `_target_problems`、不改 `VALIDATION_VERSION`（维持 7）。

---

## 3. A / B / C 消融设计、合成语料与双盲评测协议

### 3.1 三组严格对照
| 组别 | 临时源码树 | 术语抽取 | 翻译规则与版本 | 归因对象 |
|---|---|---|---|---|
| **A（基线）** | `BASE_SHA` 原样导出 | 独立旧抽取（`extract_A`） | 基线 `RULES` + `v7` | 现网默认行为 |
| **B（仅 TR-011）** | `BASE_SHA + tr011.patch` | 独立新抽取（`extract_B`） | 基线 `RULES` + `v7` | `B - A` = TR-011 净效应 |
| **C（TR-011 + TR-012）** | `BASE_SHA + tr011.patch + tr012.patch` | **逐字节复用 `extract_B` 快照** | 候选 `RULES` + 实验树 `v8` | `C - B` = TR-012 净效应；`C - A` = 联合效应 |

- 每组均执行真实生产调用链：`glossary_texts → frequent_candidates → extract_acronym_definitions → extract_terms → normalize_terms → TermSelection → DocContext → translate`，使用 `TranslateOptions(use_cache=False, concurrency=1, quality="default")` 与**一旦读写即抛错的隔离缓存替身**。
- 轮换三组发译顺序，正式评测跑 **2 轮独立生成**（轮内 B/C 共用快照，轮间重新抽取）。

### 3.2 分层合成语料（`spikes/batch_d/fixtures/`，100% 合成）
- **开发集 `dev.jsonl`**：8 篇合成短文（4 学科 × 2 篇 × 每篇 4 个计分段 = 32 段）。
- **留出集 `holdout.jsonl`**：24 篇独立合成短文（`operations_scheduling`、`measurement_engineering`、`probability_statistics`、`computer_systems` 4 学科 × 每科 6 篇 × 每篇 4 个计分段 = **96 个留出段**），按文档与概念/句法模板家族与开发集严格互斥：
  - 每篇含 4 类各 1 段：① `concept`（概念边界）、② `formula_relation`（公式外分布/关系修饰）、③ `clause_boundary`（误断从句边界）、④ `control`（自然正常与越界负对照，覆盖常规 `priority`、合法缩写/别称、用户有意同译、完整条件句、跨编号断片、归属不明句与常规学术句）；
  - 每学科 3 篇提供参考译文（仅给评审，严禁进入生成输入）、3 篇无参考译文，分层统计；
  - 语料单测强制校验：所有要求入表的词对均能通过生产 `extract.parse()` + `normalize_terms()`、`<mN/>` 与 `formulas` 双向完整、开发/留出模板族零重叠、零真实论文痕迹/零 emoji。

### 3.3 异构双盲 MQM 与自动门禁
- **双盲包生成（`judge.py`）**：每名评审（`codex`、`neon`，独立模型家族）、每轮独立私有随机种子，96 题覆盖 A/B/C 的 6 种位置排列各 16 次；同题同上下文逐字相同译文折叠送评并私有还原；盲包 `extra="forbid"` 白名单字段；MQM 评分 `max(0, 100 - 15 * major - 4 * minor)`。
- **确定性裁决（`gate.py`）**：终态 `OFFLINE_VERIFIED`、`INCOMPLETE`、`HOLD`、`RETAIN_A`、`ELIGIBLE_B_ONLY`、`ELIGIBLE_B_AND_C`。模拟数据永久不得产出 `ELIGIBLE_*`。
  - **B 门（`B - A`）**：48 个 `concept` 段次中每位评审净增 `>= 4` 次（跨 ≥2 学科、各轮不退、文档级单侧符号检验 `p <= 0.05`），普通词/别称/用户约定零新增误纠；
  - **C 门（`C - B` & `C - A`，以 B 通过为前提）**：`formula_relation` 与 `clause_boundary` 各 48 段次中，每类每位评审净增 `>= 4` 次（跨 ≥2 学科、各轮不退、单侧符号检验 `p <= 0.05`），术语不退于 B；
  - **防回归底线**：两位评审各自的留出总体与正常段文档配对均分差 `>= 0`、2000 次学科分层整篇自助采样 95% CI 下界 `>= -1.0`、各学科及有/无参考分层均差 `>= -1.0`；逐 `(doc_id, seg_id, round, anchor_id)` 新增 `protected` 错误为 `0`，含 Major 段数与 `<= 85` 分段数不增；
  - **成本复核**：生成成本比 `<= 1.10`、请求比 `<= 1.10`、p95 耗时比 `<= 1.25`。

---

## 4. 多 Agent 并行实施 Wave 与零文件重叠矩阵

所有工作在 `D:\01\pdf-translator-batch-d`（分支 `feat/batch-d-glossary-prompt-v8`）推进；Wave 1 / Wave 2 并行泳道各占独立文件集合（零重叠），主 Agent 统一收口并串行执行所有测试与静态检查。

| Wave | 泳道 / 所有者 | 独占文件 | 验收要点 |
|---|---|---|---|
| **Wave 0** | 主 Agent（Neon） | `docs/plans/2026-11-25-batch-d-glossary-prompt-v8-ablation.md`、`spikes/batch_d/__init__.py`、`contract.py`、`policy.json`、`tests/__init__.py`、`tests/test_contract.py` | 冻结全部 Pydantic v2 契约、基线 SHA-256 金样与策略门禁。 |
| **Wave 1** | Lane G（Codex xhigh） | `spikes/batch_d/tr011.patch`、`term_diagnostics.py`、`tests/test_terms.py` | LF 补丁验证、复用 `normalize_term_key` 诊断碰撞/别称/用户同译/漏抽/混合组。 |
| **Wave 1** | Lane P（Codex xhigh） | `spikes/batch_d/tr012.patch`、`tests/test_prompt.py` | LF 补丁验证、净增严格 91 ≤ 120 码点、基线双指纹与缓存金样。 |
| **Wave 1** | Lane D（Codex xhigh） | `spikes/batch_d/fixtures/dev.jsonl`、`fixtures/holdout.jsonl`、`tests/test_corpus.py` | 8 篇 dev + 24 篇 holdout（128 段），过生产入表、公式完整性、互斥与隐私守卫。 |
| **Wave 2** | Lane E（Codex xhigh） | `spikes/batch_d/generate.py`、`tests/test_generation.py` | `git -c core.autocrlf=false archive` 三树构建、子进程隔离验证、B/C 共享快照、禁缓存替身与断点续跑。 |
| **Wave 2** | Lane J（Codex xhigh） | `spikes/batch_d/judge.py`、`tests/test_judge.py` | 6 种排列平衡双盲包、同文折叠还原、OpenAI/Anthropic 协议传输与模型家族独立性校验、断点锁票。 |
| **Wave 2** | Lane Q（Codex xhigh） | `spikes/batch_d/gate.py`、`tests/test_gate.py` | 确定性 MQM 聚合、2000 次整篇文档分层自助采样 95% CI、精确符号检验、逐锚点保护错与 6 终态全覆盖。 |
| **Wave 3** | 主 Agent + Codex | `spikes/batch_d/run.py`、`README.md`、`tests/test_run.py`、`backend/tests/unit/test_batch_d_entrypoints.py` | 一键 `selftest`/`live`/`report`、二次空目录确定性 `evidence_hash`、生产 runner/CLI `v7` 默认回归验证。 |
| **Wave 4（条件晋级）** | 主 Agent（仅真 LLM 达标后） | 达标时分次独立提交应用 `tr011.patch` 与（若 C 达标）`tr012.patch` + 同步 6 处 `PROMPT_VERSION` 断言 + 归档 `docs/02-spikes.md` S13 | 严格「达标才升 `v8`」，每次切换独立可一键 `revert`。 |

---

## 5. 验收门禁命令

```powershell
$repo = (Get-Location).Path
$py = Join-Path $repo 'backend/.venv/Scripts/python.exe'
$runRoot = Join-Path ([System.IO.Path]::GetTempPath()) 'pdftr-batch-d-verify'

& $py -B -m unittest discover -s spikes/batch_d/tests -t . -v
& $py -B -m spikes.batch_d.run selftest --out (Join-Path $runRoot 'offline-1')
& $py -B -m spikes.batch_d.run selftest --out (Join-Path $runRoot 'offline-2')

Push-Location (Join-Path $repo 'backend')
uv run --offline --frozen --no-sync ruff format --check . ../spikes/batch_d
uv run --offline --frozen --no-sync ruff check . ../spikes/batch_d
uv run --offline --frozen --no-sync pyright
uv run --offline --frozen --no-sync pyright --project pyproject.toml ../spikes/batch_d
uv run --offline --frozen --no-sync lint-imports
uv run --offline --frozen --no-sync pytest -q
Pop-Location

Push-Location (Join-Path $repo 'frontend')
npm run lint
npm run typecheck
npm test
npm run build
Pop-Location
```

后续接入真实 LLM 凭据后执行：
```powershell
& $py -B -m spikes.batch_d.run live --config (Join-Path $runRoot 'live-config.json') --out (Join-Path $runRoot 'live')
& $py -B -m spikes.batch_d.run report --run (Join-Path $runRoot 'live')
```

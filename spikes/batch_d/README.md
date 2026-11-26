# 批次 D（TR-011 / TR-012 / A-B-C 消融）离线自检与真实双盲指南

本目录存放批次 D 的离线验证与真实 LLM 消融评测套件。双模型实盘均保留 A 组。生产提示词维持 `PROMPT_VERSION = "v7"`，译文校验升级至 `VALIDATION_VERSION = 8`。

## 1. 目录结构

- `contract.py` / `policy.json`：冻结数据契约、基线 SHA-256 金样与量化门禁阈值
- `tr011.patch` / `term_diagnostics.py`：TR-011 术语抽取单文件补丁（纯 LF）与复用 `normalize_term_key` 的碰撞/覆盖诊断
- `tr012.patch`：已冻结的 TR-012 翻译提示词候选，供 A/B/C 复现与离线回归使用（纯 LF，净增 91 Unicode 码点）
- `fixtures/dev.jsonl` / `fixtures/holdout.jsonl`：8 篇开发 + 24 篇留出原创合成语料（128 个计分段，四学科与有/无参考均衡）
- `generate.py`：使用 `git -c core.autocrlf=false archive` 构造 A/B/C 三棵隔离临时源码树，B/C 共享同一次术语抽取快照
- `judge.py`：6 种位置排列均衡双盲包、同文折叠还原、OpenAI 兼容 / Anthropic 双协议受限传输与逐题锁票
- `gate.py`：纯确定性 MQM 聚合、2000 次学科分层整篇文档自助采样 95% CI、精确单侧符号检验与逐锚点保护错门禁
- `run.py`：`selftest` / `live` / `report` 统一入口

## 2. 离线确定性自检（零外部 API Key，零外网调用）

在仓库根目录执行（输出目录必须位于仓库外）：

```powershell
$py = Join-Path $PWD 'backend/.venv/Scripts/python.exe'
$runRoot = Join-Path ([System.IO.Path]::GetTempPath()) 'pdftr-batch-d'

& $py -B -m unittest discover -s spikes/batch_d/tests -v
& $py -B -m spikes.batch_d.run selftest --out (Join-Path $runRoot 'offline-1')
& $py -B -m spikes.batch_d.run selftest --out (Join-Path $runRoot 'offline-2')
```

确认两次运行的 `decision.json` 均为 `"state": "OFFLINE_VERIFIED"`、`"eligible_b": false`、`"eligible_c": false`，且 `evidence_hash` 逐字一致、`manifest.json` 中 `network_calls == 0`。

## 3. 接入真实 LLM 凭据运行在线 A/B/C 与异构双盲

在仓库外准备 `live-config.json`（只写环境变量名，绝不写明文密钥）：

```json
{
  "generator": {
    "protocol": "openai_compat",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "model_family": "deepseek",
    "key_env": "DEEPSEEK_API_KEY"
  },
  "codex": {
    "protocol": "openai_compat",
    "base_url": "https://your-openai-endpoint.example/v1",
    "model": "apice/gpt-6.2-astra",
    "model_family": "openai",
    "key_env": "CODEX_JUDGE_API_KEY",
    "allowed_model_ids": ["apice/gpt-6.2-astra"]
  },
  "neon": {
    "protocol": "anthropic",
    "base_url": "https://your-neon-endpoint.example",
    "model": "gemini-4-neon",
    "model_family": "gemini",
    "key_env": "NEON_JUDGE_API_KEY",
    "allowed_model_ids": ["gemini-4-neon"]
  },
  "budget": {
    "max_requests": 1600,
    "max_cost_cny": 50.0,
    "max_seconds_per_stage": 3600.0
  }
}
```

设置对应环境变量后一键运行：

```powershell
& $py -B -m spikes.batch_d.run live --config (Join-Path $runRoot 'live-config.json') --out (Join-Path $runRoot 'live')
& $py -B -m spikes.batch_d.run report --run (Join-Path $runRoot 'live')
```

仅有翻译模型凭据时，可加 `--generate-only` 落盘三组生成结果（终态保持 `INCOMPLETE`，退出码 `2`）。

`generator.allow_private` 默认为 `false`。连接本机或私网翻译端点时可显式设为 `true`。离线自检使用模拟传输。

## 4. 退出码与分步晋级规程

| 退出码 | 状态 | 后续动作 |
|---:|---|---|
| `0` | `OFFLINE_VERIFIED` / `ELIGIBLE_B_AND_C` | 前者表示离线自检通过，后者表示两项候选均达到量化门禁 |
| `3` | `ELIGIBLE_B_ONLY` | 仅独立提交应用 `tr011.patch`，生产保持 `PROMPT_VERSION = "v7"` 不变 |
| `4` | `RETAIN_A` | 保持基线不变，将评测结论归档至 `docs/02-spikes.md` |
| `2` | `INCOMPLETE` / `HOLD` | 补齐凭据/断点续跑，或对 `HOLD` 争议段完成盲态仲裁后重跑 `report` |
| `1` | 异常 | 协议、配置或文件完整性错误 |

## 5. 双模型实盘归档

两次实盘各覆盖 24 篇留出文档、2 轮、A/B/C 3 组和 1,152 票。下列数据来自精修 TR-011 之前的候选。

| 生成模型与运行标识 | 原生终态与分歧 | 剥离争议项后的终态 | 格式泄露 |
|---|---|---|---:|
| Nemotron 550B，`live-nemotron-550b-v1` | `HOLD`，157 项 | `RETAIN_A` | 2 |
| DeepSeek `deepseek-flash`，`live-deepseek-chat-v2` | `HOLD`，124 项（84 `protected`、40 `score_gap`） | `RETAIN_A` | 0 |

- Nemotron A/B/C 均分依次为 Codex 93.61 / 93.49 / 93.71，Neon 96.62 / 95.91 / 96.27。泄露形式为残缺 `</translations` 与原文未含的自闭合标签。
- DeepSeek 证据摘要 `evidence_hash=a463ec6db3ea1dc8e1ccf1c110999f3c0669cd02dd5f60695876da61773a58c7`。
- DeepSeek 的 B 对 A（TR-011）。Codex 95.31 → 95.70（+0.40，`concept` 净增 6/48，`p=0.1445`），Neon 97.48 → 97.86（+0.39，`concept` 净增 5/48，`p=0.2266`）。费用 0.942×（¥0.0975 对 ¥0.1035），请求 0.968×（241 对 249），p95 0.869×（6.23 秒对 7.17 秒）。未通过的门禁为 24 篇的符号检验与 Codex 评出的 5 处新增保护错，同期修复 8 处保护错。
- DeepSeek 的 C 对 B（TR-012）。Codex −0.06，Neon −0.08，`formula_relation` 双评净增均为 0/48，`clause_boundary` 均为 +1/48。提示词加长使翻译前缀缓存命中率从 83.5% 降至 76.3%，总费用 1.222×（¥0.1191），超过 1.10× 上限。

TR-012 冻结，生产提示词维持 `v7`。TR-011 已精修，要求完整抽取核心复合术语并让普通限定词按语境翻译，留待复测。生产优先交付标签校验和残缺协议标签守卫，译文缓存校验版本升至 8。

"""验证实验 S11：用带人工参考译文的公开数据集对照打分，比较思考 / 不思考。

数据（均可公开获取，原文不入库，脚本按固定规则抽样）：
- ACL 60/60（ymoslem/acl-6060，CC-BY-4.0）：ACL 2022 学术报告转写及专业人工中文译文。
  取 dev 集 4 个连续片段 × 40 句 = 160 句（保留上下文）。
- WMT24++ en-zh_CN（google/wmt24pp，Apache-2.0）：新闻/文学/社交/口语四个领域，参考译文经人工审校。
  每个领域取 10 段（排除 canary、坏样本和含换行的段落）= 40 段。

系统：DeepSeek Flash 不思考 / 思考·low / 思考·high；gpt-6-luna（代理默认设置，作参照）。
提示词：ACL 用 v2（学术）；WMT 用由 v2 去掉学术专属条目得到的通用版，同一数据集内所有系统用同一提示词。

指标：
- 自动指标：corpus BLEU（sacrebleu，tokenize=zh）与 chrF++，对“与不思考的差值”做 1000 次配对自助采样求 95% 置信区间
- 参照评审：gpt-6.1-sol（xhigh）给出人工参考译文作对照、MQM 风格盲评（ACL 40 句 + WMT 20 段）
- Claude 人工盲评（附参考译文）

用法：DATA_DIR=... python s11_benchmark.py translate | metrics | judge | report | blind
"""

import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import sacrebleu
from openai import OpenAI

from s8_quality import parse_lines
from s9_prompt import RULES_V2

OUT = Path(__file__).parent / "out" / "bench"
DATA = Path(os.environ.get("DATA_DIR", "."))
PRICE = (0.02, 1.0, 4.0)  # deepseek-flash 空闲价：命中 / 未命中 / 输出，元/百万 token

RULES_GENERAL = """你是一名专业翻译，把用户给出的英文段落译成简体中文。

【格式】
- 每行一段，格式「编号|原文」；输出「编号|译文」，一行对应一行，编号不变，不要输出解释。

【要求】
- 准确第一：不漏译、不增译，语气和确定程度（如 apparently、may、likely）与原文一致。
- 意译优先：按中文表达习惯调整语序，避免逐词直译和欧化句式；文学、口语内容保留原文的风格和语气。
- 人名、品牌、作品名等专有名词使用通行译法，没有通行译法的保留原文。
- 使用中文标点。

【示例】
输入：
7|The new policy, which critics say was rushed, apparently took effect on Monday.
输出：
7|批评者称这项新政策出台仓促，它似乎已于周一生效。"""

SYSTEMS = {
    "ds-nothink": dict(api="deepseek", model="deepseek-flash", thinking=None),
    "ds-think-low": dict(api="deepseek", model="deepseek-flash", thinking="low"),
    "ds-think-high": dict(api="deepseek", model="deepseek-flash", thinking="high"),
    "luna": dict(api="judge", model="gpt-6-luna"),
}


def clients():
    return {
        "deepseek": OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com"),
        "judge": OpenAI(api_key=os.environ["JUDGE_API_KEY"], base_url=os.environ["JUDGE_BASE_URL"],
                        timeout=1500, max_retries=0),
    }


def complete(cli, cfg, system, user, stream=False):
    kw: dict[str, Any] = dict(model=cfg["model"],
                              messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if cfg["api"] == "deepseek":
        kw["max_tokens"] = 32000
        if cfg.get("thinking"):
            kw["extra_body"] = {"thinking": {"type": "enabled"}}
            kw["reasoning_effort"] = cfg["thinking"]
        else:
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
            kw["temperature"] = 0.3
    if cfg.get("effort"):
        kw["reasoning_effort"] = cfg["effort"]
    t0 = time.time()
    for attempt in range(int(os.environ.get("MAX_RETRY", "6"))):
        try:
            if stream:  # 长时间推理用流式，避免代理网关空闲超时
                text, usage = [], {}
                for ev in cli.chat.completions.create(**kw, stream=True, stream_options={"include_usage": True}):
                    if ev.choices and ev.choices[0].delta.content:
                        text.append(ev.choices[0].delta.content)
                    if ev.usage:
                        usage = ev.usage.model_dump()
                return "".join(text), usage, time.time() - t0
            r = cli.chat.completions.create(**kw)
            return r.choices[0].message.content or "", (r.usage.model_dump() if r.usage else {}), time.time() - t0
        except Exception as e:
            wait = 30 if "429" in str(e) else 10
            print(f"  retry {attempt} ({str(e)[:80]}), wait {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError("request failed")


def build_sample():
    acl = json.loads((DATA / "acl6060.json").read_text(encoding="utf-8"))
    dev = [r for r in acl if r["split"] == "dev"]
    items = []
    for start in (0, 120, 240, 360):
        for r in dev[start:start + 40]:
            items.append({"set": "acl", "group": f"acl-{start}", "src": r["en"], "ref": r["zh"]})
    rng = random.Random(7)
    wmt = [json.loads(line) for line in (DATA / "wmt24pp_en-zh_CN.jsonl").read_text(encoding="utf-8").splitlines()]
    for dom in ("news", "literary", "social", "speech"):
        pool = [w for w in wmt if w["domain"] == dom and not w["is_bad_source"] and "\n" not in w["source"]
                and "\n" not in w["target"] and len(w["source"].split()) >= 8]
        for w in rng.sample(pool, 10):
            items.append({"set": "wmt", "group": f"wmt-{dom}", "src": w["source"], "ref": w["target"]})
    for i, it in enumerate(items):
        it["id"] = i
    return items


def translate():
    OUT.mkdir(parents=True, exist_ok=True)
    sample = build_sample()
    (OUT / "sample.json").write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")
    cl = clients()
    batches = []
    for g in dict.fromkeys(s["group"] for s in sample):
        items = [s for s in sample if s["group"] == g]
        size = 20 if items[0]["set"] == "acl" else 10
        batches += [items[i:i + size] for i in range(0, len(items), size)]

    def system_for(it):
        if it["set"] == "acl":
            return RULES_V2 + "\n\n文档信息：ACL 2022 学术报告的英文转写（口语），请译成规范、通顺的中文。"
        return RULES_GENERAL

    def run(job):
        name, batch = job
        cfg = SYSTEMS[name]
        user = "\n".join(f"{s['id']}|{s['src']}" for s in batch)
        text, u, t = complete(cl[cfg["api"]], cfg, system_for(batch[0]), user)
        got = parse_lines(text)
        return name, {str(s["id"]): got.get(s["id"], "") for s in batch}, u, t

    results = {n: {"out": {}, "usage": [], "seconds": 0.0} for n in SYSTEMS}
    jobs = [(n, b) for n in SYSTEMS for b in batches]
    for name, out, u, t in ThreadPoolExecutor(6).map(run, jobs):
        results[name]["out"].update(out)
        results[name]["usage"].append(u)
        results[name]["seconds"] += t
    for n, r in results.items():
        print(n, "missing", sum(1 for s in sample if not r["out"].get(str(s["id"]))), "secs", round(r["seconds"], 1))
    (OUT / "translations.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def corpus_scores(hyps, refs):
    bleu = sacrebleu.corpus_bleu(hyps, [refs], tokenize="zh").score
    chrf = sacrebleu.corpus_chrf(hyps, [refs], word_order=2).score
    return bleu, chrf


def metrics():
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    res = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    rng = random.Random(0)
    report = {}
    for subset in ("acl", "wmt"):
        items = [s for s in sample if s["set"] == subset]
        refs = [s["ref"] for s in items]
        hyp = {n: [res[n]["out"][str(s["id"])] for s in items] for n in SYSTEMS}
        base = hyp["ds-nothink"]
        # 配对自助采样：同一组下标重采样，比较各系统与不思考的差值
        boots = [[rng.randrange(len(items)) for _ in items] for _ in range(1000)]
        rows = {}
        for n in SYSTEMS:
            bleu, chrf = corpus_scores(hyp[n], refs)
            row = {"BLEU": round(bleu, 2), "chrF++": round(chrf, 2)}
            if n != "ds-nothink":
                diffs = []
                for idx in boots:
                    r_ = [refs[i] for i in idx]
                    diffs.append(sacrebleu.corpus_chrf([hyp[n][i] for i in idx], [r_], word_order=2).score
                                 - sacrebleu.corpus_chrf([base[i] for i in idx], [r_], word_order=2).score)
                diffs.sort()
                row["chrF++_diff_vs_nothink_95CI"] = [round(diffs[25], 2), round(diffs[974], 2)]
            rows[n] = row
        report[subset] = rows
    (OUT / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


JUDGE_REF_PROMPT = """你是严格的英译中翻译评审。每个原文段落附有一份人工参考译文，以及若干候选译文（标签随机）。
参考译文只用来帮助你理解原意和判断术语，候选不必与它逐字一致，措辞不同但意思准确、表达自然的不应扣分。
评分标准（MQM 风格）：从 100 分起扣，重大错误（误译、漏译、增译、语气或确定程度改变、关键术语错误）每处 -15，
次要错误（措辞生硬、不够地道、标点问题、小的术语不一致）每处 -4。
只输出 JSON：{"段落编号": {"候选标签": {"score": 整数, "errors": ["简短说明", ...]}, ...}, ...}"""


def judge_subset(sample):
    acl = [s for s in sample if s["set"] == "acl"]
    wmt = [s for s in sample if s["set"] == "wmt"]
    rng = random.Random(5)
    return sorted(rng.sample(acl, 40), key=lambda s: s["id"]) + sorted(rng.sample(wmt, 20), key=lambda s: s["id"])


def judge():
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    res = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    sub = judge_subset(sample)
    chunks = [sub[i:i + 5] for i in range(0, len(sub), 5)]
    parts = OUT / "judge_parts"
    parts.mkdir(exist_ok=True)
    cli = clients()["judge"]
    names = list(SYSTEMS)

    def run(idx):
        f = parts / f"{idx}.json"
        if f.exists():
            return
        rng = random.Random(100 + idx)
        blocks, mapping = [], {}
        for s in chunks[idx]:
            order = names[:]
            rng.shuffle(order)
            mapping[str(s["id"])] = dict(zip("ABCD", order))
            cands = "\n".join(f"  [{lab}] {res[n]['out'][str(s['id'])]}" for lab, n in zip("ABCD", order))
            blocks.append(f"段落 {s['id']}：\n原文：{s['src']}\n参考译文：{s['ref']}\n候选：\n{cands}")
        jcfg = dict(api="judge", model="gpt-6.1-sol", effort="xhigh")

        def ask(bs):
            text, u, t = complete(cli, jcfg, JUDGE_REF_PROMPT, "\n\n".join(bs), stream=True)
            got = json.loads(text[text.index("{"): text.rindex("}") + 1])
            return {re.sub(r"\D", "", k): v for k, v in got.items()}, u, t

        try:
            got, u, t = ask(blocks)
        except RuntimeError:  # 整批推理太久被代理断开时，拆成单段逐个评审
            got, u, t = {}, {}, 0.0
            for b in blocks:
                g, _, tt = ask([b])
                got.update(g)
                t += tt
        f.write_text(json.dumps({"mapping": mapping, "scores": got, "usage": u, "seconds": round(t, 1)},
                                ensure_ascii=False), encoding="utf-8")
        print(f"chunk {idx} done {t:.0f}s", flush=True)

    list(ThreadPoolExecutor(3).map(run, range(len(chunks))))
    done = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(parts.glob("*.json"))]
    merged = {"mapping": {}, "scores": {}, "usage": [d["usage"] for d in done], "seconds": [d["seconds"] for d in done]}
    for d in done:
        merged["mapping"].update(d["mapping"])
        merged["scores"].update(d["scores"])
    (OUT / "judge.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print("judge done", len(done), "/", len(chunks))


def report():
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    res = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    m = json.loads((OUT / "metrics.json").read_text(encoding="utf-8"))
    j = json.loads((OUT / "judge.json").read_text(encoding="utf-8"))
    sub = judge_subset(sample)
    src_tokens = {"acl": 0, "wmt": 0}
    rows = []
    for n, cfg in SYSTEMS.items():
        sc = {"acl": [], "wmt": []}
        for s in sub:
            sid = str(s["id"])
            lab = next(k for k, v in j["mapping"][sid].items() if v == n)
            sc[s["set"]].append(j["scores"][sid][lab]["score"])
        usage = res[n]["usage"]
        reasoning = sum((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0 for u in usage)
        row = {
            "system": n,
            "acl_BLEU": m["acl"][n]["BLEU"], "acl_chrF++": m["acl"][n]["chrF++"],
            "acl_judge": round(sum(sc["acl"]) / len(sc["acl"]), 1), "acl_judge_le85": sum(x <= 85 for x in sc["acl"]),
            "wmt_BLEU": m["wmt"][n]["BLEU"], "wmt_chrF++": m["wmt"][n]["chrF++"],
            "wmt_judge": round(sum(sc["wmt"]) / len(sc["wmt"]), 1), "wmt_judge_le85": sum(x <= 85 for x in sc["wmt"]),
            "acl_chrF_CI": m["acl"][n].get("chrF++_diff_vs_nothink_95CI"),
            "wmt_chrF_CI": m["wmt"][n].get("chrF++_diff_vs_nothink_95CI"),
            "output_tokens": sum(u.get("completion_tokens", 0) for u in usage),
            "reasoning_tokens": reasoning,
            "seconds": round(res[n]["seconds"], 1),
        }
        if cfg["api"] == "deepseek":
            row["cny_total_idle"] = round(sum(u.get("prompt_cache_hit_tokens", 0) * PRICE[0]
                                              + u.get("prompt_cache_miss_tokens", u["prompt_tokens"]) * PRICE[1]
                                              + u["completion_tokens"] * PRICE[2] for u in usage) / 1e6, 4)
        rows.append(row)
    print(json.dumps({"n_acl": sum(s["set"] == "acl" for s in sample), "n_wmt": sum(s["set"] == "wmt" for s in sample),
                      "judged": len(sub), "judge_seconds": j["seconds"], "rows": rows}, ensure_ascii=False, indent=1))


def blind():
    sample = {str(s["id"]): s for s in json.loads((OUT / "sample.json").read_text(encoding="utf-8"))}
    res = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    rng = random.Random(2718)
    acl = [k for k, s in sample.items() if s["set"] == "acl" and len(s["src"].split()) >= 12]
    wmt = [k for k, s in sample.items() if s["set"] == "wmt"]
    ids = rng.sample(acl, 5) + rng.sample(wmt, 5)
    mapping = {}
    for sid in ids:
        s = sample[sid]
        order = list(SYSTEMS)
        rng.shuffle(order)
        mapping[sid] = dict(zip("ABCD", order))
        print(f"\n### 段落 {sid}（{s['group']}）\n原文：{s['src']}\n参考：{s['ref']}")
        for lab, n in zip("ABCD", order):
            print(f"[{lab}] {res[n]['out'][sid]}")
    (OUT / "blind_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    {"translate": translate, "metrics": metrics, "judge": judge, "report": report, "blind": blind}[sys.argv[1]]()

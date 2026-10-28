"""验证实验 S12：翻译模型横向对比（智谱免费模型 vs DeepSeek vs GPT）。

系统（均用同一套提示词、不开思考或使用接口默认）：
- glm-4.7-flash（智谱免费，关闭思考）
- glm-4-flash-250414（智谱免费，不思考）
- deepseek-flash（不思考，本项目当前默认）
- gpt-6-luna、gpt-6-sol（经代理，默认设置）

样本：
- bench：S11 的公开数据集样本（ACL 60/60 160 句 + WMT24++ 40 段，含人工参考译文）
- paper：S10 的论文样本 30 段（含公式占位符，其中 BERT 10 段为留出集）

评审：gpt-6.1-sol（xhigh）盲评——bench 对照参考译文（60 段），paper 不给参考（30 段）；
注意评审与 gpt-6-* 同家族，可能偏向自家译文，因此另做 Claude 盲评与自动指标作对照。

用法：DATA_DIR=... python s12_models.py translate | metrics | judge | report | blind
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

import s11_benchmark as b11
from s8_quality import JUDGE_PROMPT
from s9_prompt import RULES_V2, with_latex
from s10_prompt_v3 import paper_info

OUT = Path(__file__).parent / "out" / "models"
QUALITY = Path(__file__).parent / "out" / "quality"

ALL_SYSTEMS = {
    "glm-4.7-flash": dict(api="zhipu", model="glm-4.7-flash", thinking=False),
    "glm-4-flash": dict(api="zhipu", model="glm-4-flash-250414"),
    "deepseek": dict(api="deepseek", model="deepseek-flash", thinking=False),
    "gpt-6-luna": dict(api="proxy", model="gpt-6-luna"),
    "gpt-6-sol": dict(api="proxy", model="gpt-6-sol"),
}
# 参与指标/评审/报告的系统，可用 USE=名1,名2 指定（默认全部）
SYSTEMS = {n: c for n, c in ALL_SYSTEMS.items() if n in (os.environ.get("USE") or ",".join(ALL_SYSTEMS)).split(",")}
LABELS = "ABCDE"


def clients():
    return {
        "zhipu": OpenAI(api_key=os.environ["ZHIPU_API_KEY"], base_url="https://open.bigmodel.cn/api/paas/v4/",
                        timeout=600, max_retries=0),
        "deepseek": OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com"),
        "proxy": OpenAI(api_key=os.environ["JUDGE_API_KEY"], base_url=os.environ["JUDGE_BASE_URL"],
                        timeout=1500, max_retries=0),
    }


def complete(cli, cfg, system, user, stream=False):
    kw: dict[str, Any] = dict(model=cfg["model"],
                              messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if cfg["api"] in ("zhipu", "deepseek"):
        kw["temperature"] = 0.3
        kw["max_tokens"] = 16000
        if cfg.get("thinking") is False:
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
    if cfg.get("effort"):
        kw["reasoning_effort"] = cfg["effort"]
    t0 = time.time()
    for attempt in range(8):
        try:
            if stream:
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
            print(f"  [{cfg['model']}] retry {attempt} ({str(e)[:70]}), wait {wait}s", flush=True)
            time.sleep(wait)
    raise RuntimeError("request failed")


def paper_jobs():
    """论文样本：按论文分批，系统提示用 v2 + 论文标题摘要 + v2 术语表（S10 已抽好）；请求里段落编号为 1000+id。"""
    sample = json.loads((QUALITY / "sample_v3.json").read_text(encoding="utf-8"))
    v3 = json.loads((QUALITY / "translations_v3.json").read_text(encoding="utf-8"))
    jobs = []
    for p in dict.fromkeys(s["paper"] for s in sample):
        info = paper_info(p)
        terms = v3["_terms"][f"{p}/v2"]["terms"]
        system = (f"{RULES_V2}\n\n论文标题：{info['title']}\n摘要：{info['abstract']}\n\n术语表（推荐译法）：\n"
                  + "\n".join(f"{k}={v}" for k, v in terms.items()))
        items = [s for s in sample if s["paper"] == p]
        sections = list(dict.fromkeys(info["segs"][s["text"]]["section"] for s in items))
        user = "本批段落所在章节：" + "；".join(sections) + "\n" + "\n".join(
            f"{1000 + s['id']}|{with_latex(s['text'], info['segs'][s['text']]['formulas'])}" for s in items)
        jobs.append((system, user, [f"p{s['id']}" for s in items]))
    return sample, jobs


def bench_jobs():
    sample = json.loads((b11.OUT / "sample.json").read_text(encoding="utf-8"))
    jobs = []
    for g in dict.fromkeys(s["group"] for s in sample):
        items = [s for s in sample if s["group"] == g]
        size = 20 if items[0]["set"] == "acl" else 10
        for i in range(0, len(items), size):
            batch = items[i:i + size]
            system = (RULES_V2 + "\n\n文档信息：ACL 2022 学术报告的英文转写（口语），请译成规范、通顺的中文。"
                      if batch[0]["set"] == "acl" else b11.RULES_GENERAL)
            jobs.append((system, "\n".join(f"{s['id']}|{s['src']}" for s in batch), [str(s["id"]) for s in batch]))
    return sample, jobs


def parse_ids(text):
    got = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\|(.*)", line)
        if m:
            got[m.group(1)] = m.group(2).strip()
    return got


def translate():
    OUT.mkdir(parents=True, exist_ok=True)
    _, bj = bench_jobs()
    _, pj = paper_jobs()
    jobs = bj + pj
    cl = clients()
    prev = json.loads((OUT / "translations.json").read_text(encoding="utf-8")) if (OUT / "translations.json").exists() else {}

    def run_system(name):
        cfg = ALL_SYSTEMS[name]
        res = prev.get(name) or {"out": {}, "usage": [], "seconds": 0.0}
        for system, user, ids in jobs:
            if all(res["out"].get(i) for i in ids):
                continue  # 断点续跑：已有结果的批次跳过
            text, u, t = complete(cl[cfg["api"]], cfg, system, user)
            got = parse_ids(text)
            for i in ids:
                # 论文段在请求里用 1000+编号，避免模型丢掉字母前缀
                res["out"][i] = got.get(str(1000 + int(i[1:])) if i.startswith("p") else i, "")
            res["usage"].append(u)
            res["seconds"] += t
        print(name, "done", round(res["seconds"], 1), "s, missing", sum(1 for _, _, ids in jobs for i in ids
                                                                        if not res["out"].get(i)), flush=True)
        return name, res

    import threading
    lock = threading.Lock()
    results = dict(prev)

    def run_and_save(name):
        name, res = run_system(name)
        with lock:  # 每个系统完成即存盘，中途停止不会丢掉已完成的系统
            results[name] = res
            (OUT / "translations.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    only = [n for n in os.environ.get("ONLY", "").split(",") if n] or list(ALL_SYSTEMS)
    list(ThreadPoolExecutor(len(only)).map(run_and_save, only))


def metrics():
    sample = json.loads((b11.OUT / "sample.json").read_text(encoding="utf-8"))
    res = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    paper = json.loads((QUALITY / "sample_v3.json").read_text(encoding="utf-8"))
    rep = {}
    for subset in ("acl", "wmt"):
        items = [s for s in sample if s["set"] == subset]
        refs = [s["ref"] for s in items]
        rep[subset] = {}
        for n in SYSTEMS:
            hyp = [res[n]["out"].get(str(s["id"]), "") for s in items]
            rep[subset][n] = {"BLEU": round(sacrebleu.corpus_bleu(hyp, [refs], tokenize="zh").score, 2),
                              "chrF++": round(sacrebleu.corpus_chrf(hyp, [refs], word_order=2).score, 2)}
    rep["paper_placeholder_ok"] = {
        n: sum(sorted(re.findall(r"<m\d+/>", res[n]["out"].get(f"p{s['id']}", ""))) ==
               sorted(re.findall(r"<m\d+/>", s["text"])) for s in paper) for n in SYSTEMS}
    rep["missing"] = {n: sum(1 for v in res[n]["out"].values() if not v) for n in SYSTEMS}
    (OUT / "metrics.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1))


def judge_tasks():
    sample = json.loads((b11.OUT / "sample.json").read_text(encoding="utf-8"))
    sub = b11.judge_subset(sample)
    paper = json.loads((QUALITY / "sample_v3.json").read_text(encoding="utf-8"))
    infos = {p: paper_info(p) for p in dict.fromkeys(s["paper"] for s in paper)}
    tasks = []
    for k in range(0, len(sub), 5):
        tasks.append(("bench", [(str(s["id"]), s["src"], s["ref"]) for s in sub[k:k + 5]]))
    for k in range(0, len(paper), 3):
        tasks.append(("paper", [(f"p{s['id']}", with_latex(s["text"], infos[s["paper"]]["segs"][s["text"]]["formulas"]),
                                 None) for s in paper[k:k + 3]]))
    return tasks


def judge():
    res = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    tasks = judge_tasks()
    parts = OUT / "judge_parts"
    parts.mkdir(exist_ok=True)
    cli = clients()["proxy"]
    jcfg = dict(api="proxy", model="gpt-6.1-sol", effort="xhigh")
    names = list(SYSTEMS)

    def run(idx):
        f = parts / f"{idx}.json"
        if f.exists():
            return
        kind, segs = tasks[idx]
        rng = random.Random(500 + idx)
        blocks, mapping = [], {}
        for sid, src, ref in segs:
            order = names[:]
            rng.shuffle(order)
            mapping[sid] = dict(zip(LABELS, order))
            cands = "\n".join(f"  [{lab}] {res[n]['out'].get(sid, '')}" for lab, n in zip(LABELS, order))
            num = sid.lstrip("p")
            if kind == "bench":
                blocks.append(f"段落 {num}：\n原文：{src}\n参考译文：{ref}\n候选：\n{cands}")
            else:
                blocks.append(f"段落 {num}：\n原文（公式以 <mN>LaTeX</mN> 标出，译文中写作 <mN/>）：{src}\n候选：\n{cands}")
        prompt = b11.JUDGE_REF_PROMPT if kind == "bench" else JUDGE_PROMPT

        def ask(bs):
            text, u, t = complete(cli, jcfg, prompt, "\n\n".join(bs), stream=True)
            got = json.loads(text[text.index("{"): text.rindex("}") + 1])
            return {re.sub(r"\D", "", k): v for k, v in got.items()}, u, t

        try:
            got, u, t = ask(blocks)
        except Exception:
            got, u, t = {}, {}, 0.0
            for blk in blocks:
                g, _, tt = ask([blk])
                got.update(g)
                t += tt
        prefix = "p" if kind == "paper" else ""
        got = {prefix + k: v for k, v in got.items()}
        f.write_text(json.dumps({"kind": kind, "mapping": mapping, "scores": got, "usage": u, "seconds": round(t, 1)},
                                ensure_ascii=False), encoding="utf-8")
        print(f"chunk {idx} ({kind}) done {t:.0f}s", flush=True)

    list(ThreadPoolExecutor(3).map(run, range(len(tasks))))
    done = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(parts.glob("*.json"))]
    merged = {"mapping": {}, "scores": {}, "kind": {}}
    for d in done:
        merged["mapping"].update(d["mapping"])
        merged["scores"].update(d["scores"])
        for sid in d["mapping"]:
            merged["kind"][sid] = d["kind"]
    (OUT / "judge.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print("judge done", len(done), "/", len(tasks))


def report():
    res = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    m = json.loads((OUT / "metrics.json").read_text(encoding="utf-8"))
    j = json.loads((OUT / "judge.json").read_text(encoding="utf-8"))
    sample = {str(s["id"]): s for s in json.loads((b11.OUT / "sample.json").read_text(encoding="utf-8"))}
    rows = []
    for n in SYSTEMS:
        sc = {"acl": [], "wmt": [], "paper": []}
        for sid, labs in j["mapping"].items():
            lab = next(k for k, v in labs.items() if v == n)
            score = j["scores"][sid][lab]["score"]
            key = "paper" if sid.startswith("p") else sample[sid]["set"]
            sc[key].append(score)
        rows.append({
            "system": n,
            **{f"{k}_judge": round(sum(v) / len(v), 1) for k, v in sc.items()},
            **{f"{k}_le85": sum(x <= 85 for x in v) for k, v in sc.items()},
            "acl_BLEU": m["acl"][n]["BLEU"], "acl_chrF++": m["acl"][n]["chrF++"],
            "wmt_BLEU": m["wmt"][n]["BLEU"], "wmt_chrF++": m["wmt"][n]["chrF++"],
            "placeholder_ok": f"{m['paper_placeholder_ok'][n]}/30",
            "missing": m["missing"][n],
            "seconds": round(res[n]["seconds"], 1),
            "output_tokens": sum(u.get("completion_tokens", 0) for u in res[n]["usage"]),
        })
    print(json.dumps(rows, ensure_ascii=False, indent=1))


def blind():
    """Claude 盲评：bench 6 段（附参考）+ paper 6 段，选与 S11 盲评不重复的段落。"""
    res = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    bench = {str(s["id"]): s for s in json.loads((b11.OUT / "sample.json").read_text(encoding="utf-8"))}
    paper = {f"p{s['id']}": s for s in json.loads((QUALITY / "sample_v3.json").read_text(encoding="utf-8"))}
    used = set(json.loads((b11.OUT / "blind_mapping.json").read_text(encoding="utf-8")))
    infos = {p: paper_info(p) for p in dict.fromkeys(s["paper"] for s in paper.values())}
    rng = random.Random(1618)
    acl = [k for k, s in bench.items() if s["set"] == "acl" and k not in used and len(s["src"].split()) >= 15]
    wmt = [k for k, s in bench.items() if s["set"] == "wmt" and k not in used and len(s["src"].split()) <= 80]
    pp = [k for k, s in paper.items() if len(s["text"].split()) <= 120]
    ids = rng.sample(acl, 3) + rng.sample(wmt, 3) + rng.sample(pp, 6)
    mapping = {}
    for sid in ids:
        order = list(SYSTEMS)
        rng.shuffle(order)
        mapping[sid] = dict(zip(LABELS, order))
        if sid.startswith("p"):
            s = paper[sid]
            print(f"\n### {sid}（论文 {s['paper']}，{s['kind']}）\n原文：{with_latex(s['text'], infos[s['paper']]['segs'][s['text']]['formulas'])}")
        else:
            s = bench[sid]
            print(f"\n### {sid}（{s['group']}）\n原文：{s['src']}\n参考：{s['ref']}")
        for lab, n in zip(LABELS, order):
            print(f"[{lab}] {res[n]['out'].get(sid, '')}")
    (OUT / "blind_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    {"translate": translate, "metrics": metrics, "judge": judge, "report": report, "blind": blind}[sys.argv[1]]()

"""验证实验 S8：翻译质量 × 成本对比。

从两篇论文中分层抽取段落（普通段落、含多个行内公式的段落、图注），
用同一套提示词和批次格式，在多种配置下翻译，然后：
- 自动检查：占位符是否完整、残留未译英文比例、长度比
- 盲评：DeepSeek Pro（开思考）对每段的匿名候选打分（MQM 风格，0–100 + 错误列表），候选顺序随机
- 成本：按实测 usage 和官方价格换算为"每篇论文"的费用

用法：
  python s8_quality.py translate   # 各配置翻译样本
  python s8_quality.py judge       # 盲评
  python s8_quality.py report      # 汇总
  python s8_quality.py blind N     # 打印 N 段匿名候选供人工评审（不显示配置名）
"""

import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

from s7_cost import ntok, segments, system_prompt

OUT = Path(__file__).parent / "out" / "quality"
PAPERS = ["1412.6980", "1706.03762"]
# 元/百万 token：(命中, 未命中, 输出)，空闲时段价格；高峰 ×2
PRICE = {"deepseek-flash": (0.02, 1.0, 4.0), "deepseek-v4-pro": (0.15, 4.5, 13.5)}

CONFIGS = {
    "flash-nothink": dict(model="deepseek-flash", thinking=False),
    "flash-think-low": dict(model="deepseek-flash", thinking=True, effort="low"),
    "flash-think-high": dict(model="deepseek-flash", thinking=True, effort="high"),
    "pro-nothink": dict(model="deepseek-v4-pro", thinking=False),
    "pro-think-low": dict(model="deepseek-v4-pro", thinking=True, effort="low"),
    # 两步：先用 flash-nothink 的译文，再让 flash 开思考审校，只输出需要修改的行
    "flash-2pass": dict(model="deepseek-flash", thinking=True, effort="low", post_edit_of="flash-nothink"),
}

REVIEW_RULES = """你是资深学术翻译审校。下面每行给出「编号|英文原文」和对应的「编号|中文初译」。
逐行检查初译是否有误译、漏译、术语错误、不通顺或占位符（如 <m1/>）丢失。
只输出需要修改的行，格式为「编号|修改后的完整译文」；全部无需修改时只输出 OK。"""

JUDGE_PROMPT = """你是严格的英译中学术翻译评审。对每个原文段落，给出的若干候选译文（标签随机）逐一评分。
评分标准（MQM 风格）：从 100 分起扣，重大错误（误译、漏译、关键术语错误、占位符丢失）每处 -15，
次要错误（措辞生硬、不够学术、标点问题、小的术语不一致）每处 -4。占位符 <m1/> 等代表公式，应原样保留。
只输出 JSON：{"段落编号": {"候选标签": {"score": 整数, "errors": ["简短说明", ...]}, ...}, ...}"""


def client():
    return OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")


def load_sample(per_paper: int = 10):
    """分层抽样：每篇 3 段含 ≥2 个行内公式、2 段图注、其余普通段落（含摘要）。"""
    rng = random.Random(42)
    sample, contexts = [], {}
    for paper in PAPERS:
        md = Path(__file__).parent / "out" / "mineru" / paper / "vlm"
        layout = json.loads((md / "layout.json").read_text(encoding="utf-8"))
        cost = json.loads((md / "_cost.json").read_text(encoding="utf-8"))
        segs, _ = segments(layout, with_refs=False)
        title = next(s["text"] for s in segs if s["type"] == "title")
        abstract = next((segs[i + 1]["text"] for i, s in enumerate(segs) if s["text"].upper() == "ABSTRACT"), "")
        contexts[paper] = system_prompt(title, abstract, cost["terms_map"])
        pool = [s for s in segs if len(s["text"].split()) >= 15]
        formula = [s for s in pool if len(re.findall(r"<m\d+/>", s["text"])) >= 2]
        caption = [s for s in pool if s["type"].endswith("caption") and s not in formula]
        normal = [s for s in pool if s not in formula and s not in caption]
        picked = rng.sample(formula, min(3, len(formula))) + rng.sample(caption, min(2, len(caption)))
        abs_seg = [s for s in normal if s["text"] == abstract]
        rest = [s for s in normal if s["text"] != abstract]
        picked += abs_seg + rng.sample(rest, per_paper - len(picked) - len(abs_seg))
        for s in picked:
            kind = "formula" if s in formula else "caption" if s in caption else "normal"
            sample.append({"id": len(sample), "paper": paper, "kind": kind, "text": s["text"]})
    return sample, contexts


def chat(cli, cfg, system, user, max_tokens):
    kw = dict(model=cfg["model"], max_tokens=max_tokens,
              messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if cfg["thinking"]:
        kw["extra_body"] = {"thinking": {"type": "enabled"}}
        kw["reasoning_effort"] = cfg["effort"]
    else:
        kw["extra_body"] = {"thinking": {"type": "disabled"}}
        kw["temperature"] = 0.3
    t0 = time.time()
    r = cli.chat.completions.create(**kw)
    return r.choices[0].message.content or "", r.usage.model_dump(), time.time() - t0


def parse_lines(text):
    got = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\|(.*)", line)
        if m:
            got[int(m.group(1))] = m.group(2).strip()
    return got


def translate():
    OUT.mkdir(parents=True, exist_ok=True)
    sample, contexts = load_sample()
    (OUT / "sample.json").write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")
    cli = client()
    results = {}
    if (OUT / "translations.json").exists():
        results = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))

    def run_config(name):
        cfg = CONFIGS[name]
        out, usages, secs = {}, [], 0.0
        for paper in PAPERS:
            items = [s for s in sample if s["paper"] == paper]
            if "post_edit_of" in cfg:
                draft = results[cfg["post_edit_of"]]["out"]
                user = "\n".join(f"{s['id']}|{s['text']}\n{s['id']}|{draft[str(s['id'])]}" for s in items)
                text, u, t = chat(cli, cfg, REVIEW_RULES + "\n\n" + contexts[paper].split("\n\n", 1)[1], user, 8000)
                fixed = parse_lines(text)
                for s in items:
                    out[str(s["id"])] = fixed.get(s["id"], draft[str(s["id"])])
                # 两步方案的成本 = 初译成本 + 审校成本
            else:
                user = "\n".join(f"{s['id']}|{s['text']}" for s in items)
                text, u, t = chat(cli, cfg, contexts[paper], user, 16000)
                got = parse_lines(text)
                for s in items:
                    out[str(s["id"])] = got.get(s["id"], "")
            usages.append(u)
            secs += t
        return name, {"out": out, "usage": usages, "seconds": round(secs, 1)}

    order = [n for n in CONFIGS if "post_edit_of" not in CONFIGS[n] and n not in results]
    for name, res in ThreadPoolExecutor(3).map(run_config, order):
        results[name] = res
        print(name, "done", res["seconds"], "s")
    for name in CONFIGS:
        if "post_edit_of" in CONFIGS[name] and name not in results:
            results[name] = run_config(name)[1]
            print(name, "done")
    (OUT / "translations.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def judge():
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    results = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    names = list(CONFIGS)
    rng = random.Random(7)
    labels = "ABCDEF"
    mapping = {}  # 段落 id -> {标签: 配置名}
    chunks = [sample[i:i + 4] for i in range(0, len(sample), 4)]
    cli = client()
    judge_cfg = dict(model="deepseek-v4-pro", thinking=True, effort="high")

    def run(chunk):
        parts = []
        for s in chunk:
            order = names[:]
            rng.shuffle(order)
            mapping[s["id"]] = dict(zip(labels, order))
            cands = "\n".join(f"  [{lab}] {results[n]['out'][str(s['id'])]}" for lab, n in zip(labels, order))
            parts.append(f"段落 {s['id']}：\n原文：{s['text']}\n候选：\n{cands}")
        text, u, _ = chat(cli, judge_cfg, JUDGE_PROMPT, "\n\n".join(parts), 20000)
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M)
        return json.loads(text[text.index("{"): text.rindex("}") + 1]), u

    scores, usages = {}, []
    for got, u in ThreadPoolExecutor(3).map(run, chunks):
        # 评审偶尔把键写成"段落4"，统一成纯数字
        scores.update({re.sub(r"\D", "", k): v for k, v in got.items()})
        usages.append(u)
    (OUT / "judge.json").write_text(json.dumps({"mapping": mapping, "scores": scores, "usage": usages},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    print("judge done")


def paper_cost(cfg_name, results, sample_src_tokens, paper_src_tokens=6200):
    """把样本上的实测 usage 按原文 token 比例放大到一篇论文（约 6200 原文 token）。"""
    def one(name):
        cfg = CONFIGS[name]
        hit, miss, out = PRICE[cfg["model"]]
        c = sum(u.get("prompt_cache_hit_tokens", 0) * hit + u.get("prompt_cache_miss_tokens", u["prompt_tokens"]) * miss
                + u["completion_tokens"] * out for u in results[name]["usage"]) / 1e6
        return c * paper_src_tokens / sample_src_tokens
    c = one(cfg_name)
    if "post_edit_of" in CONFIGS[cfg_name]:
        c += one(CONFIGS[cfg_name]["post_edit_of"])
    return c


def report():
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    results = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    j = json.loads((OUT / "judge.json").read_text(encoding="utf-8"))
    src_tokens = sum(ntok(s["text"]) for s in sample)
    rows = []
    for name in CONFIGS:
        sc, by_kind = [], {}
        ph_ok, residue = 0, []
        for s in sample:
            sid = str(s["id"])
            lab = next(k for k, v in j["mapping"][sid].items() if v == name)
            r = j["scores"][sid][lab]
            sc.append(r["score"])
            by_kind.setdefault(s["kind"], []).append(r["score"])
            out = results[name]["out"][sid]
            ph_ok += sorted(re.findall(r"<m\d+/>", out)) == sorted(re.findall(r"<m\d+/>", s["text"]))
            words = re.findall(r"[A-Za-z]{4,}", re.sub(r"<[^>]+>", "", out))
            residue.append(len(words))
        reasoning = sum((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
                        for u in results[name]["usage"])
        rows.append({
            "config": name,
            "score_mean": round(sum(sc) / len(sc), 1),
            "score_min": min(sc),
            **{f"score_{k}": round(sum(v) / len(v), 1) for k, v in sorted(by_kind.items())},
            "placeholder_ok": f"{ph_ok}/{len(sample)}",
            "en_words_left": sum(residue),
            "output_tokens": sum(u["completion_tokens"] for u in results[name]["usage"]),
            "reasoning_tokens": reasoning,
            "seconds": results[name]["seconds"],
            "cny_per_paper_idle": round(paper_cost(name, results, src_tokens), 3),
        })
    judge_cost = sum(u.get("prompt_cache_miss_tokens", u["prompt_tokens"]) * 4.5 + u["completion_tokens"] * 13.5
                     for u in j["usage"]) / 1e6
    print(json.dumps({"sample_segments": len(sample), "sample_src_tokens": src_tokens,
                      "judge_cost_idle_cny": round(judge_cost, 3), "rows": rows}, ensure_ascii=False, indent=1))


def blind(n: str = "8"):
    """打印匿名候选，供人工评审；映射另存，评完再看。"""
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    results = json.loads((OUT / "translations.json").read_text(encoding="utf-8"))
    rng = random.Random(2026)
    picked = rng.sample(sample, int(n))
    mapping = {}
    for s in picked:
        order = list(CONFIGS)
        rng.shuffle(order)
        mapping[s["id"]] = dict(zip("ABCDEF", order))
        print(f"\n### 段落 {s['id']}（{s['kind']}）\n原文：{s['text']}")
        for lab, name in zip("ABCDEF", order):
            print(f"[{lab}] {results[name]['out'][str(s['id'])]}")
    (OUT / "blind_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    {"translate": translate, "judge": judge, "report": report, "blind": blind}[sys.argv[1]](*sys.argv[2:])

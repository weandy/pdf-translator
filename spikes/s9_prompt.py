"""验证实验 S9：提示词调优。

在 S8 的同一批 20 段上，对比旧提示词（v1，S8 已有结果）与新提示词（v2）：
- v2 改动：公式以 <mN>LaTeX</mN> 给出内容（输出仍写 <mN/>）、补充风格要求和示例、
  模型名规则、术语为"推荐译法"、按章节提供上下文、术语抽取不再受全大写标题污染
- 温度：0.3 与 DeepSeek 官方建议的翻译温度 1.3
- 思考：v2 不思考 / 思考·low / 思考·high；思考配置重复 3 次，统计推理 token 的波动

用法：
  python s9_prompt.py translate
  python s9_prompt.py judge
  python s9_prompt.py report
  python s9_prompt.py blind      # 打印匿名候选供人工盲评
"""

import json
import random
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from s7_cost import ntok, segments
from s8_quality import JUDGE_PROMPT, PAPERS, PRICE, chat, client, parse_lines

OUT = Path(__file__).parent / "out" / "quality"
S8 = OUT / "translations.json"

RULES_V2 = r"""你是资深学术论文译者，把用户给出的英文段落译成简体中文学术文本。

【格式】
- 每行一段，格式「编号|原文」；输出「编号|译文」，一行对应一行，编号不变，不要输出解释。
- <mN>…</mN> 是公式，里面的 LaTeX 只供你理解，不要翻译或改写；译文中只写 <mN/>，每个编号恰好出现一次。
- 公式在数学陈述中的对应关系必须与原文一致（例如“A 的上界是 B”不能写成“B 的上界是 A”）。
- <b>…</b> 等格式标签原样保留。

【要求】
- 准确第一：不漏译、不增译；数字、单位、引用编号 [12]、作者年份引用 (Smith et al., 2020) 原样保留。
- 意译优先：按中文学术写作习惯调整语序，拆分过长的定语，避免“允许……更多的……”“被……”等生硬直译。
- 模型名、方法名、数据集名、软件名及其配置后缀（如 Transformer (big)、ResNet-50）和评价指标缩写（如 BLEU）保留原文，大小写与原文一致。
- 术语表是推荐译法，原文出现该术语时使用。
- 使用中文标点。

【示例】
输入：
7|Self-attention requires <m1>O(1)</m1> sequential operations, whereas a recurrent layer requires <m2>O(n)</m2>, which allows for significantly more parallelization.
输出：
7|自注意力只需<m1/>次顺序操作，而循环层需要<m2/>次，因此可以大幅提高并行度。"""

TERMS_V2 = """从下面的论文内容中抽取需要全文统一译法的专业术语，最多 40 个。
要求：
- 只收专业术语（概念、方法、指标等），不收章节名和普通词汇。
- 术语按它在摘要或正文中的大小写书写，不要使用标题里的全大写形式。
- 模型名、方法名、数据集名等应保留英文的，输出「英文=英文」。
- 使用中文学界通行译法。
每行输出「英文=中文」，不要输出其他内容。

标题：{title}
摘要：{abstract}
正文开头：{intro}
章节标题（仅供判断领域）：{headings}"""

V2_CONFIGS = {
    "v2-nothink-t0.3": dict(model="deepseek-flash", thinking=False, temperature=0.3),
    "v2-nothink-t1.3": dict(model="deepseek-flash", thinking=False, temperature=1.3),
    "v2-think-low": dict(model="deepseek-flash", thinking=True, effort="low", repeat=3),
    "v2-think-high": dict(model="deepseek-flash", thinking=True, effort="high", repeat=3),
}
JUDGED = ["flash-nothink", "flash-think-low", "v2-nothink-t0.3", "v2-nothink-t1.3", "v2-think-low", "v2-think-high"]
NAMES = {"flash-nothink": "v1-nothink", "flash-think-low": "v1-think-low"}


def with_latex(text: str, formulas: list[str]) -> str:
    def rep(m):
        k = int(m.group(1))
        tex = formulas[k - 1] if k <= len(formulas) else ""
        return f"<m{k}>{tex}</m{k}>"
    return re.sub(r"<m(\d+)/>", rep, text)


def paper_context():
    """每篇论文：分段（含公式 LaTeX 和所在章节）、标题、摘要、正文开头。"""
    ctx = {}
    for paper in PAPERS:
        layout = json.loads((Path(__file__).parent / "out" / "mineru" / paper / "vlm" / "layout.json").read_text(encoding="utf-8"))
        segs, _ = segments(layout, with_refs=False)
        section = ""
        for s in segs:
            if s["type"] == "title":
                section = s["text"]
            s["section"] = section
        title = next(s["text"] for s in segs if s["type"] == "title")
        ai = next((i for i, s in enumerate(segs) if s["text"].upper() == "ABSTRACT"), None)
        abstract = segs[ai + 1]["text"] if ai is not None else ""
        intro = next((s["text"] for s in segs[(ai or 0) + 2:] if s["type"] == "text"), "")
        headings = [s["text"] for s in segs if s["type"] == "title"]
        ctx[paper] = dict(segs={s["text"]: s for s in segs}, title=title, abstract=abstract, intro=intro, headings=headings)
    return ctx


def extract_terms_v2(cli, c):
    prompt = TERMS_V2.format(title=c["title"], abstract=c["abstract"], intro=c["intro"][:1500],
                             headings="; ".join(c["headings"]))
    text, u, _ = chat(cli, dict(model="deepseek-flash", thinking=False), "你是学术术语专家。", prompt, 1200)
    terms = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            terms[k.strip()] = v.strip()
    return terms, u


def system_v2(c, terms):
    gl = "\n".join(f"{k}={v}" for k, v in terms.items())
    return f"{RULES_V2}\n\n论文标题：{c['title']}\n摘要：{c['abstract']}\n\n术语表（推荐译法）：\n{gl}"


def chat_t(cli, cfg, system, user, max_tokens):
    """chat() 固定温度 0.3，这里允许覆盖温度。"""
    if cfg.get("thinking") or "temperature" not in cfg:
        return chat(cli, cfg, system, user, max_tokens)
    import time
    t0 = time.time()
    r = cli.chat.completions.create(
        model=cfg["model"], max_tokens=max_tokens, temperature=cfg["temperature"],
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        extra_body={"thinking": {"type": "disabled"}},
    )
    return r.choices[0].message.content or "", r.usage.model_dump(), time.time() - t0


def translate():
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    ctx = paper_context()
    cli = client()
    terms, term_usage = {}, []
    for paper in PAPERS:
        terms[paper], u = extract_terms_v2(cli, ctx[paper])
        term_usage.append(u)
    results = {"_terms": terms, "_term_usage": term_usage}

    def user_msg(paper):
        items = [s for s in sample if s["paper"] == paper]
        sections = list(dict.fromkeys(ctx[paper]["segs"][s["text"]]["section"] for s in items))
        lines = [f"{s['id']}|{with_latex(s['text'], ctx[paper]['segs'][s['text']]['formulas'])}" for s in items]
        return "本批段落所在章节：" + "；".join(sections) + "\n" + "\n".join(lines), items

    jobs = [(name, rep) for name, cfg in V2_CONFIGS.items() for rep in range(cfg.get("repeat", 1))]

    def run(job):
        name, rep = job
        cfg = V2_CONFIGS[name]
        out, usages, secs = {}, [], 0.0
        for paper in PAPERS:
            user, items = user_msg(paper)
            text, u, t = chat_t(cli, cfg, system_v2(ctx[paper], terms[paper]), user, 16000)
            got = parse_lines(text)
            for s in items:
                out[str(s["id"])] = got.get(s["id"], "")
            usages.append(u)
            secs += t
        return name, rep, {"out": out, "usage": usages, "seconds": round(secs, 1)}

    for name, rep, res in ThreadPoolExecutor(4).map(run, jobs):
        results.setdefault(name, []).append(res)
        print(name, rep, res["seconds"], "s")
    (OUT / "translations_v2.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def candidates():
    """各配置用于评审的译文：v1 取 S8 结果，v2 取第一次运行。"""
    s8 = json.loads(S8.read_text(encoding="utf-8"))
    v2 = json.loads((OUT / "translations_v2.json").read_text(encoding="utf-8"))
    return {n: (s8[n]["out"] if n in s8 else v2[n][0]["out"]) for n in JUDGED}


def judge():
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    ctx = paper_context()
    cands = candidates()
    rng = random.Random(11)
    mapping = {}
    chunks = [sample[i:i + 4] for i in range(0, len(sample), 4)]
    cli = client()

    def run(chunk):
        parts = []
        for s in chunk:
            order = JUDGED[:]
            rng.shuffle(order)
            mapping[str(s["id"])] = dict(zip("ABCDEF", order))
            src = with_latex(s["text"], ctx[s["paper"]]["segs"][s["text"]]["formulas"])
            lines = "\n".join(f"  [{lab}] {cands[n][str(s['id'])]}" for lab, n in zip("ABCDEF", order))
            parts.append(f"段落 {s['id']}：\n原文（公式以 <mN>LaTeX</mN> 标出，译文中写作 <mN/>）：{src}\n候选：\n{lines}")
        text, u, _ = chat(cli, dict(model="deepseek-v4-pro", thinking=True, effort="high"), JUDGE_PROMPT, "\n\n".join(parts), 20000)
        got = json.loads(text[text.index("{"): text.rindex("}") + 1])
        return {re.sub(r"\D", "", k): v for k, v in got.items()}, u

    scores, usages = {}, []
    for got, u in ThreadPoolExecutor(3).map(run, chunks):
        scores.update(got)
        usages.append(u)
    (OUT / "judge_v2.json").write_text(json.dumps({"mapping": mapping, "scores": scores, "usage": usages},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    print("judge done")


def cost_of(usages, model="deepseek-flash", scale=1.0):
    hit, miss, out = PRICE[model]
    return sum(u.get("prompt_cache_hit_tokens", 0) * hit + u.get("prompt_cache_miss_tokens", u["prompt_tokens"]) * miss
               + u["completion_tokens"] * out for u in usages) / 1e6 * scale


def report():
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    s8 = json.loads(S8.read_text(encoding="utf-8"))
    v2 = json.loads((OUT / "translations_v2.json").read_text(encoding="utf-8"))
    j = json.loads((OUT / "judge_v2.json").read_text(encoding="utf-8"))
    cands = candidates()
    scale = 6200 / sum(ntok(s["text"]) for s in sample)  # 样本 → 一篇论文（约 6200 原文 token）
    rows = []
    for n in JUDGED:
        sc, kinds = [], {}
        for s in sample:
            sid = str(s["id"])
            lab = next(k for k, v in j["mapping"][sid].items() if v == n)
            r = j["scores"][sid][lab]
            sc.append(r["score"])
            kinds.setdefault(s["kind"], []).append(r["score"])
        ph = sum(sorted(re.findall(r"<m\d+/>", cands[n][str(s["id"])])) == sorted(re.findall(r"<m\d+/>", s["text"]))
                 for s in sample)
        runs = [s8[n]] if n in s8 else v2[n]
        reasoning = [sum((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0 for u in r["usage"]) for r in runs]
        costs = [cost_of(r["usage"], scale=scale) for r in runs]
        rows.append({
            "config": NAMES.get(n, n),
            "score_mean": round(sum(sc) / len(sc), 1), "score_min": min(sc),
            **{f"score_{k}": round(sum(v) / len(v), 1) for k, v in sorted(kinds.items())},
            "placeholder_ok": f"{ph}/{len(sample)}",
            "runs": len(runs),
            "reasoning_tokens": reasoning,
            "input_tokens": sum(u["prompt_tokens"] for u in runs[0]["usage"]),
            "cny_per_paper_idle": [round(c, 3) for c in costs],
            "cny_per_paper_idle_mean": round(statistics.mean(costs), 3),
        })
    judge_cost = cost_of(j["usage"], model="deepseek-v4-pro")
    print(json.dumps({"judge_cost_idle": round(judge_cost, 3), "terms_v2": v2["_terms"], "rows": rows},
                     ensure_ascii=False, indent=1))


def blind():
    """固定挑 S8 里失分最多的 5 段 + 随机 3 段，打印匿名候选。"""
    sample = {str(s["id"]): s for s in json.loads((OUT / "sample.json").read_text(encoding="utf-8"))}
    cands = candidates()
    rng = random.Random(99)
    ids = ["1", "7", "9", "11", "16"] + rng.sample([k for k in sample if k not in {"1", "7", "9", "11", "16"}], 3)
    ctx = paper_context()
    mapping = {}
    for sid in ids:
        s = sample[sid]
        order = JUDGED[:]
        rng.shuffle(order)
        mapping[sid] = dict(zip("ABCDEF", order))
        print(f"\n### 段落 {sid}（{s['kind']}）\n原文：{with_latex(s['text'], ctx[s['paper']]['segs'][s['text']]['formulas'])}")
        for lab, n in zip("ABCDEF", order):
            print(f"[{lab}] {cands[n][sid]}")
    (OUT / "blind_v2_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    {"translate": translate, "judge": judge, "report": report, "blind": blind}[sys.argv[1]]()

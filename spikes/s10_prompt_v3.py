"""验证实验 S10：通用提示词 v3 + 跨模型验证 + 留出集。

- 提示词 v3：在 v2 基础上吸收开源项目与论文的做法（见 docs/02-spikes.md S10），
  写法不依赖任何特定模型，可用于任意 OpenAI 兼容的对话模型
- 术语抽取 v3：同时给出论文领域（参考 MAPS）；抽取后做程序校正
  （按正文中实际出现的大小写改写、正文中没出现或等于章节名的丢弃）
- 样本：S8 的 20 段 + 留出论文 BERT（1810.04805）10 段，后者在设计提示词时未看过
- 翻译模型：DeepSeek Flash（不思考 / 思考·low）与 gpt-6-luna（验证换模型是否依然有效）
- 评审：gpt-6.1-sol（reasoning_effort=xhigh）盲评；另由 Claude 人工盲评

用法：python s10_prompt_v3.py translate | judge | report | blind
"""

import json
import os
import random
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from openai import OpenAI

from s7_cost import ntok, segments
from s8_quality import JUDGE_PROMPT, PRICE, load_sample, parse_lines
from s9_prompt import RULES_V2, TERMS_V2, with_latex

OUT = Path(__file__).parent / "out" / "quality"
MINERU = Path(__file__).parent / "out" / "mineru"
HELD_OUT = "1810.04805"

RULES_V3 = r"""你是一名专业的学术翻译，负责把论文段落从源语言（多为英文，也可能是其他语言）译成简体中文。

## 输入
- 用户消息先给出本批段落所在的章节，随后每行一个段落，格式为「编号|原文」。
- 段落可能是被分栏、分页截断的片段：逐行独立翻译，不要合并、拆分或调换段落。
- <mN>…</mN> 是公式，里面的 LaTeX 只供你理解，不要翻译或改写；<b>…</b> 等是格式标签。

## 输出
- 每个输入行输出一行「编号|译文」，编号、行数、顺序与输入一致；不要输出解释、标题或代码块。
- 公式只写成 <mN/>，每个编号恰好出现一次；格式标签原样保留，只翻译标签内的文字。

## 翻译原则（冲突时按序号优先）
1. 忠实：意思、逻辑关系和限定条件与原文完全一致，不漏译、不增译、不臆测。特别注意：
   - 公式与文字的对应关系（谁是条件、谁是结论、谁是谁的界）；
   - 否定、比较、程度、范围、因果等限定词；
   - 长句中的每个从句都要译出。
2. 保留原文：数字、单位、引用编号 [12]、作者-年份引用 (Smith et al., 2020)、URL、代码与反引号内容；
   模型、方法、数据集、软件的名称及其配置后缀（如 Transformer (big)、ResNet-50）和评价指标缩写（如 BLEU），大小写与原文一致。
3. 术语：术语表是推荐译法，原文出现该术语时使用，不要在译文后附加英文原词；
   术语表没有的专业术语使用本领域通行译法，并在全文中保持一致；术语被换行或连字符断开时同样适用。
4. 通顺：符合中文学术写作习惯，按中文语序重组长句，拆分过长的定语，避免逐词直译和欧化句式。
5. 规范：使用中文标点；原文已是中文或本身无需翻译的行原样输出。

输出前逐行自检：编号齐全、公式编号各出现一次、条件与结论没有颠倒、没有遗漏从句。

## 示例
输入：
本批段落所在章节：3 Method
7|Self-attention requires <m1>O(1)</m1> sequential operations, whereas a recurrent layer requires <m2>O(n)</m2>, which allows for significantly more parallelization.
8|As shown by Kingma et al. (2015), the estimate is unbiased only if <m3>\beta < 1</m3>.
输出：
7|自注意力只需<m1/>次顺序操作，而循环层需要<m2/>次，因此可以大幅提高并行度。
8|Kingma et al. (2015) 指出，只有当<m3/>时该估计才是无偏的。"""

TERMS_V3 = """阅读下面的论文信息，完成两件事：
1. 第一行输出「领域：……」，用一句话概括论文所属学科与研究主题。
2. 之后抽取需要全文统一译法的专业术语，最多 40 个，每行「英文=中文」：
   - 只收专业术语（概念、方法、指标等），不收章节名和普通词汇；
   - 按术语在摘要或正文中的写法（大小写）输出，不要使用标题里的全大写或首字母大写形式；
   - 模型名、方法名、数据集名等应保留英文的，输出「英文=英文」；
   - 使用中文学界通行译法。
不要输出其他内容。

标题：{title}
摘要：{abstract}
正文开头：{intro}
章节标题（仅供判断领域）：{headings}"""

CANDIDATES = {
    "ds-v2-nothink": dict(api="deepseek", model="deepseek-flash", thinking=False, prompt="v2"),
    "ds-v3-nothink": dict(api="deepseek", model="deepseek-flash", thinking=False, prompt="v3"),
    "ds-v3-think-low": dict(api="deepseek", model="deepseek-flash", thinking=True, prompt="v3"),
    "luna-v2": dict(api="judge", model="gpt-6-luna", prompt="v2"),
    "luna-v3": dict(api="judge", model="gpt-6-luna", prompt="v3"),
}


def clients():
    return {
        "deepseek": OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com"),
        "judge": OpenAI(api_key=os.environ["JUDGE_API_KEY"], base_url=os.environ["JUDGE_BASE_URL"], timeout=1500, max_retries=0),
    }


def complete(cli, cfg, system, user, max_tokens=16000):
    kw: dict[str, Any] = dict(model=cfg["model"], messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if cfg["api"] == "deepseek":
        kw["max_tokens"] = max_tokens
        if cfg.get("thinking"):
            kw["extra_body"] = {"thinking": {"type": "enabled"}}
            kw["reasoning_effort"] = "low"
        else:
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
            kw["temperature"] = 0.3
    elif cfg.get("effort"):
        kw["reasoning_effort"] = cfg["effort"]
        # 长时间推理时用流式返回，避免代理网关因连接空闲超时（524）
        t0 = time.time()
        text, usage = [], {}
        for ev in cli.chat.completions.create(**kw, stream=True, stream_options={"include_usage": True}):
            if ev.choices and ev.choices[0].delta.content:
                text.append(ev.choices[0].delta.content)
            if ev.usage:
                usage = ev.usage.model_dump()
        return "".join(text), usage, time.time() - t0
    t0 = time.time()
    r = cli.chat.completions.create(**kw)
    return r.choices[0].message.content or "", r.usage.model_dump() if r.usage else {}, time.time() - t0


def paper_info(paper):
    layout = json.loads((MINERU / paper / "vlm" / "layout.json").read_text(encoding="utf-8"))
    segs, _ = segments(layout, with_refs=False)
    section = ""
    for s in segs:
        if s["type"] == "title":
            section = s["text"]
        s["section"] = section
    title = next(s["text"] for s in segs if s["type"] == "title")
    ai = next((i for i, s in enumerate(segs) if s["text"].upper() == "ABSTRACT"), None)
    if ai is None:  # 没有"Abstract"标题时，取第一段足够长的正文作为摘要
        ai = next(i for i, s in enumerate(segs) if s["type"] == "text" and len(s["text"].split()) >= 60) - 1
    abstract = segs[ai + 1]["text"]
    intro = next((s["text"] for s in segs[ai + 2:] if s["type"] == "text" and len(s["text"].split()) >= 30), "")
    headings = [s["text"] for s in segs if s["type"] == "title"]
    body = " ".join(s["text"] for s in segs if s["type"] != "title")
    return dict(segs={s["text"]: s for s in segs}, title=title, abstract=abstract, intro=intro,
                headings=headings, body=body)


def build_sample():
    """S8 的 20 段 + 留出论文 10 段（同样的分层抽样规则）。"""
    base = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    import s8_quality
    old = s8_quality.PAPERS
    s8_quality.PAPERS = [HELD_OUT]
    extra, _ = load_sample()
    s8_quality.PAPERS = old
    for k, s in enumerate(extra):
        s["id"] = len(base) + k
        s["held_out"] = True
    return base + extra


def normalize_terms(terms: dict[str, str], info) -> dict[str, str]:
    """程序校正：按正文中最常见的写法改写术语，正文没出现或等于章节名的丢弃。"""
    out = {}
    headings = {h.lower() for h in info["headings"]}
    for k, v in terms.items():
        found = re.findall(re.escape(k), info["body"], flags=re.I)
        if not found or k.lower() in headings:
            continue
        best = Counter(found).most_common(1)[0][0]
        out[best] = best if v.strip().lower() == k.strip().lower() else v
    return out


def extract_terms(cli, info, version):
    tmpl = TERMS_V3 if version == "v3" else TERMS_V2
    prompt = tmpl.format(title=info["title"], abstract=info["abstract"], intro=info["intro"][:1500],
                         headings="; ".join(info["headings"]))
    text, u, _ = complete(cli, dict(api="deepseek", model="deepseek-flash", thinking=False), "你是学术术语专家。", prompt, 1500)
    domain, terms = "", {}
    for line in text.splitlines():
        if line.startswith("领域"):
            domain = line.split("：", 1)[-1].strip()
        elif "=" in line:
            k, v = line.split("=", 1)
            terms[k.strip()] = v.strip()
    if version == "v3":
        terms = normalize_terms(terms, info)
    return domain, terms, u


def system_prompt(version, info, domain, terms):
    gl = "\n".join(f"{k}={v}" for k, v in terms.items())
    if version == "v2":
        return f"{RULES_V2}\n\n论文标题：{info['title']}\n摘要：{info['abstract']}\n\n术语表（推荐译法）：\n{gl}"
    return (f"{RULES_V3}\n\n## 文档信息\n标题：{info['title']}\n领域：{domain}\n摘要：{info['abstract']}\n\n"
            f"## 术语表（推荐译法）\n{gl}")


def translate():
    sample = build_sample()
    (OUT / "sample_v3.json").write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")
    papers = list(dict.fromkeys(s["paper"] for s in sample))
    infos = {p: paper_info(p) for p in papers}
    cl = clients()
    ctx = {}
    for p in papers:
        for v in ("v2", "v3"):
            ctx[(p, v)] = extract_terms(cl["deepseek"], infos[p], v)
    results = {"_terms": {f"{p}/{v}": {"domain": d, "terms": t} for (p, v), (d, t, _) in ctx.items()}}

    def run(name):
        cfg = CANDIDATES[name]
        out, usages, secs = {}, [], 0.0
        for p in papers:
            items = [s for s in sample if s["paper"] == p]
            sections = list(dict.fromkeys(infos[p]["segs"][s["text"]]["section"] for s in items))
            lines = [f"{s['id']}|{with_latex(s['text'], infos[p]['segs'][s['text']]['formulas'])}" for s in items]
            user = "本批段落所在章节：" + "；".join(sections) + "\n" + "\n".join(lines)
            domain, terms, _ = ctx[(p, cfg["prompt"])]
            text, u, t = complete(cl[cfg["api"]], cfg, system_prompt(cfg["prompt"], infos[p], domain, terms), user)
            got = parse_lines(text)
            for s in items:
                out[str(s["id"])] = got.get(s["id"], "")
            usages.append(u)
            secs += t
        return name, {"out": out, "usage": usages, "seconds": round(secs, 1)}

    for name, res in ThreadPoolExecutor(5).map(run, CANDIDATES):
        results[name] = res
        print(name, res["seconds"], "s", "missing:", sum(1 for v in res["out"].values() if not v))
    (OUT / "translations_v3.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def judge():
    """每批 3 段，全部并发；每批完成即存盘（含该批的标签映射），重跑时跳过已完成的批。"""
    sample = json.loads((OUT / "sample_v3.json").read_text(encoding="utf-8"))
    res = json.loads((OUT / "translations_v3.json").read_text(encoding="utf-8"))
    infos = {p: paper_info(p) for p in dict.fromkeys(s["paper"] for s in sample)}
    names = list(CANDIDATES)
    parts_dir = OUT / "judge_v3_parts"
    parts_dir.mkdir(exist_ok=True)
    chunks = [sample[i:i + 3] for i in range(0, len(sample), 3)]
    cli = clients()["judge"]
    jcfg = dict(api="judge", model="gpt-6.1-sol", effort="xhigh")

    def run(idx):
        part = parts_dir / f"{idx}.json"
        if part.exists():
            return
        rng = random.Random(1000 + idx)
        parts, mapping = [], {}
        for s in chunks[idx]:
            order = names[:]
            rng.shuffle(order)
            mapping[str(s["id"])] = dict(zip("ABCDE", order))
            src = with_latex(s["text"], infos[s["paper"]]["segs"][s["text"]]["formulas"])
            lines = "\n".join(f"  [{lab}] {res[n]['out'][str(s['id'])]}" for lab, n in zip("ABCDE", order))
            parts.append(f"段落 {s['id']}：\n原文（公式以 <mN>LaTeX</mN> 标出，译文中写作 <mN/>）：{src}\n候选：\n{lines}")
        attempt = 0
        while attempt < 3:
            try:
                text, u, t = complete(cli, jcfg, JUDGE_PROMPT, "\n\n".join(parts))
                got = json.loads(text[text.index("{"): text.rindex("}") + 1])
                got = {re.sub(r"\D", "", k): v for k, v in got.items()}
                part.write_text(json.dumps({"mapping": mapping, "scores": got, "usage": u, "seconds": round(t, 1)},
                                           ensure_ascii=False), encoding="utf-8")
                print(f"chunk {idx} done {t:.0f}s", flush=True)
                return
            except Exception as e:
                if "429" in str(e):  # 代理并发上限：等待后重试，不计入失败次数
                    time.sleep(30)
                    continue
                attempt += 1  # 超时或输出不完整
                print(f"chunk {idx} retry {attempt}: {str(e)[:120]}", flush=True)

    workers = int(os.environ.get("JUDGE_CONCURRENCY", "3"))
    list(ThreadPoolExecutor(workers).map(run, range(len(chunks))))
    done = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(parts_dir.glob("*.json"))]
    if len(done) < len(chunks):
        print(f"only {len(done)}/{len(chunks)} chunks done, rerun to resume")
        return
    merged = {"judge": "gpt-6.1-sol/xhigh", "mapping": {}, "scores": {}, "usage": [], "seconds": []}
    for d in done:
        merged["mapping"].update(d["mapping"])
        merged["scores"].update(d["scores"])
        merged["usage"].append(d["usage"])
        merged["seconds"].append(d["seconds"])
    (OUT / "judge_v3.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print("judge done", merged["seconds"])


def report():
    sample = json.loads((OUT / "sample_v3.json").read_text(encoding="utf-8"))
    res = json.loads((OUT / "translations_v3.json").read_text(encoding="utf-8"))
    j = json.loads((OUT / "judge_v3.json").read_text(encoding="utf-8"))
    scale = 6200 / sum(ntok(s["text"]) for s in sample)
    rows = []
    for n, cfg in CANDIDATES.items():
        groups = {"all": [], "held_out": [], "formula": [], "major_errors": 0}
        for s in sample:
            sid = str(s["id"])
            lab = next(k for k, v in j["mapping"][sid].items() if v == n)
            r = j["scores"][sid][lab]
            groups["all"].append(r["score"])
            if s.get("held_out"):
                groups["held_out"].append(r["score"])
            if s["kind"] == "formula":
                groups["formula"].append(r["score"])
            groups["major_errors"] += r["score"] <= 85
        ph = sum(sorted(re.findall(r"<m\d+/>", res[n]["out"][str(s["id"])])) == sorted(re.findall(r"<m\d+/>", s["text"]))
                 for s in sample)
        row = {
            "config": n,
            "score_mean": round(sum(groups["all"]) / len(groups["all"]), 1),
            "score_min": min(groups["all"]),
            "held_out_mean": round(sum(groups["held_out"]) / len(groups["held_out"]), 1),
            "formula_mean": round(sum(groups["formula"]) / len(groups["formula"]), 1),
            "segments_le_85": groups["major_errors"],
            "placeholder_ok": f"{ph}/{len(sample)}",
            "input_tokens": sum(u.get("prompt_tokens", 0) for u in res[n]["usage"]),
            "output_tokens": sum(u.get("completion_tokens", 0) for u in res[n]["usage"]),
            "seconds": res[n]["seconds"],
        }
        if cfg["api"] == "deepseek":
            hit, miss, outp = PRICE["deepseek-flash"]
            row["cny_per_paper_idle"] = round(sum(
                u.get("prompt_cache_hit_tokens", 0) * hit + u.get("prompt_cache_miss_tokens", u["prompt_tokens"]) * miss
                + u["completion_tokens"] * outp for u in res[n]["usage"]) / 1e6 * scale, 3)
        rows.append(row)
    print(json.dumps({"segments": len(sample), "held_out": sum(1 for s in sample if s.get("held_out")),
                      "judge": j["judge"], "judge_seconds": j["seconds"],
                      "judge_tokens": sum(u.get("completion_tokens", 0) for u in j["usage"]),
                      "terms": res["_terms"], "rows": rows}, ensure_ascii=False, indent=1))


def blind():
    """挑 8 段：留出集 4 段 + 公式段 2 段 + 其他 2 段。"""
    sample = {str(s["id"]): s for s in json.loads((OUT / "sample_v3.json").read_text(encoding="utf-8"))}
    res = json.loads((OUT / "translations_v3.json").read_text(encoding="utf-8"))
    infos = {p: paper_info(p) for p in dict.fromkeys(s["paper"] for s in sample.values())}
    rng = random.Random(314)
    held = [k for k, s in sample.items() if s.get("held_out")]
    formula = [k for k, s in sample.items() if s["kind"] == "formula" and not s.get("held_out")]
    other = [k for k, s in sample.items() if s["kind"] != "formula" and not s.get("held_out")]
    ids = rng.sample(held, 4) + rng.sample(formula, 2) + rng.sample(other, 2)
    mapping = {}
    for sid in ids:
        s = sample[sid]
        order = list(CANDIDATES)
        rng.shuffle(order)
        mapping[sid] = dict(zip("ABCDE", order))
        src = with_latex(s["text"], infos[s["paper"]]["segs"][s["text"]]["formulas"])
        print(f"\n### 段落 {sid}（{s['kind']}{'，留出集' if s.get('held_out') else ''}）\n原文：{src}")
        for lab, n in zip("ABCDE", order):
            print(f"[{lab}] {res[n]['out'][sid]}")
    (OUT / "blind_v3_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    {"translate": translate, "judge": judge, "report": report, "blind": blind}[sys.argv[1]]()

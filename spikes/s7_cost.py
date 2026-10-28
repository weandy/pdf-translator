"""验证实验：一篇论文的真实翻译开销（DeepSeek）与省 token 策略对比。

输入：S2 产出的 MinerU layout.json。
1. 按块类型切出翻译单元：正文/标题/图注/脚注；公式、算法、图表、页眉页码不翻；
   行内公式换成 <m1/> 占位符；参考文献按开关决定是否翻译。
2. 估算三种请求组织方式的 token 量：
   A 逐段请求（每段都带完整说明）
   B BabelDOC 式（≤200 token 或 ≤5 段一批，JSON 缩进，说明放在用户消息）
   C 本项目方案（固定前缀可命中缓存、约 1500 token 一批、紧凑行格式、跳过参考文献、去重）
3. 用方案 C 真实调用 DeepSeek，记录 usage（含缓存命中），按官方价格算钱。

用法：DEEPSEEK_API_KEY=... python s7_cost.py out/mineru/1412.6980/vlm/layout.json [--dry] [--with-refs]
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tiktoken
from openai import OpenAI

MODEL = "deepseek-flash"
# 元/百万 token：(缓存命中, 缓存未命中, 输出)，2026-10-28 官网价格
PRICE = {"idle": (0.02, 1.0, 4.0), "peak": (0.04, 2.0, 8.0)}
BATCH_TOKENS = 1500
enc = tiktoken.get_encoding("cl100k_base")


def ntok(s: str) -> int:
    return len(enc.encode(s, disallowed_special=()))


# ---------- 1. 切分翻译单元 ----------

def block_text(block) -> tuple[str, list[str]]:
    parts, formulas = [], []
    for line in block.get("lines", []):
        for s in line.get("spans", []):
            if s["type"] == "inline_equation":
                formulas.append(s["content"])
                parts.append(f"<m{len(formulas)}/>")
            elif s["type"] == "text":
                parts.append(s["content"])
    text = " ".join(parts)
    text = re.sub(r"(\w)- (\w)", r"\1\2", text)  # 行尾连字符断词
    return re.sub(r"\s+", " ", text).strip(), formulas


def segments(layout: dict, with_refs: bool):
    segs, skipped = [], {"ref_text": 0, "equation": 0, "code": 0, "header/page": 0}
    for page in layout["pdf_info"]:
        blocks = list(page["para_blocks"])
        blocks += [b for b in page["discarded_blocks"] if b["type"] == "page_footnote"]
        for b in blocks:
            t = b["type"]
            subs = b.get("blocks", [b])
            for x in subs:
                st = x["type"]
                if st in ("chart_body", "image_body", "table_body", "code_body"):
                    continue
                if t == "interline_equation":
                    skipped["equation"] += 1
                    continue
                if t == "ref_text" and not with_refs:
                    skipped["ref_text"] += ntok(block_text(x)[0])
                    continue
                text, _ = block_text(x)
                if len(re.findall(r"[A-Za-z]{2,}", text)) < 2:
                    continue
                if segs and x.get("merge_prev"):
                    segs[-1]["text"] += " " + text  # 跨栏/跨页段落合并
                else:
                    segs.append({"type": st, "text": text})
        skipped["header/page"] += sum(1 for b in page["discarded_blocks"] if b["type"] != "page_footnote")
    return segs, skipped


# ---------- 2. 提示词 ----------

RULES = """你是学术论文翻译，将用户给出的各行英文译为简体中文。
规则：
1. 每行格式为「编号|原文」，输出同样的「编号|译文」，行数、编号不变，不要输出其他内容。
2. <m1/>、<b>…</b> 等标签原样保留，位置可随语序调整。
3. 学术书面语，中文标点；模型名、数据集名、算法缩写保留英文。
4. 术语表中的词必须使用给定译法。"""

BABELDOC_STYLE_RULES_TOKENS = 520  # BabelDOC PROMPT_TEMPLATE 固定部分约 520 token（tiktoken 实测）


def system_prompt(title: str, abstract: str, glossary: dict[str, str]) -> str:
    gl = "\n".join(f"{k}={v}" for k, v in glossary.items())
    return f"{RULES}\n\n论文标题：{title}\n摘要：{abstract}\n\n术语表：\n{gl}"


def batches(items: list[tuple[int, str]], limit: int):
    cur, n = [], 0
    for i, t in items:
        k = ntok(t)
        if cur and n + k > limit:
            yield cur
            cur, n = [], 0
        cur.append((i, t))
        n += k
    if cur:
        yield cur


# ---------- 3. 估算 ----------

def estimate(segs, sys_tokens: int, out_ratio: float):
    src = [ntok(s["text"]) for s in segs]
    total_src = sum(src)
    out = total_src * out_ratio
    # A：逐段，每段带说明（说明不变但放在每个请求里，按无缓存算）
    a_in = sum(k + sys_tokens for k in src)
    # B：BabelDOC 式，≤200 token 或 ≤5 段一批，JSON 缩进约 +40% 包装开销
    b_in, cur_n, cur_k = 0, 0, 0
    for k in src:
        cur_n += 1
        cur_k += k
        if cur_k > 200 or cur_n > 5:
            b_in += BABELDOC_STYLE_RULES_TOKENS + cur_k * 1.4
            cur_n, cur_k = 0, 0
    if cur_k:
        b_in += BABELDOC_STYLE_RULES_TOKENS + cur_k * 1.4
    return {"source_tokens": total_src, "A_input": round(a_in), "B_input": round(b_in), "est_output": round(out)}


def cost(hit: int, miss: int, out: int, period: str) -> float:
    p = PRICE[period]
    return (hit * p[0] + miss * p[1] + out * p[2]) / 1e6


# ---------- 4. 真实调用 ----------

def call(client: OpenAI, system: str, user: str, max_tokens: int):
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.3,
        max_tokens=max_tokens,
        extra_body={"thinking": {"type": "disabled"}},  # 关闭思考，思考 token 按输出计费
    )
    u = r.usage.model_dump()
    return r.choices[0].message.content, u


def extract_terms(client: OpenAI, title: str, abstract: str, headings: list[str]):
    prompt = (
        "从下面的论文标题、摘要和章节标题中抽取需要统一译法的专业术语（最多 40 个），"
        "每行输出「英文=中文」，模型名/数据集名等不译的词输出「英文=英文」，不要输出其他内容。\n\n"
        f"标题：{title}\n摘要：{abstract}\n章节：{'; '.join(headings)}"
    )
    r = client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": prompt}], temperature=0,
        max_tokens=1200, extra_body={"thinking": {"type": "disabled"}},
    )
    terms = {}
    for line in r.choices[0].message.content.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            terms[k.strip()] = v.strip()
    return terms, r.usage.model_dump()


def main(layout_path: str, *flags):
    layout = json.loads(Path(layout_path).read_text(encoding="utf-8"))
    with_refs = "--with-refs" in flags
    segs, skipped = segments(layout, with_refs)
    title = next(s["text"] for s in segs if s["type"] == "title")
    abstract = next((segs[i + 1]["text"] for i, s in enumerate(segs) if s["text"].upper() == "ABSTRACT"), "")
    headings = [s["text"] for s in segs if s["type"] == "title"]

    # 去重：完全相同的原文只翻一次
    uniq = {}
    for s in segs:
        uniq.setdefault(s["text"], None)
    items = list(enumerate(uniq))

    print(f"翻译单元 {len(segs)} 个（去重后 {len(items)}），跳过：{skipped}")
    if "--dry" in flags:
        sys_tok = ntok(system_prompt(title, abstract, {}))
        print(json.dumps(estimate(segs, sys_tok, 0.9), ensure_ascii=False))
        return

    import os
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    usages = []
    t0 = time.time()
    terms, u = extract_terms(client, title, abstract, headings)
    usages.append(("terms", u))
    system = system_prompt(title, abstract, terms)

    groups = list(batches(items, BATCH_TOKENS))
    results, failed = {}, 0

    def run(group):
        user = "\n".join(f"{i}|{t}" for i, t in group)
        text, u = call(client, system, user, max_tokens=ntok(user) * 2 + 200)
        got = {}
        for line in text.splitlines():
            m = re.match(r"\s*(\d+)\|(.*)", line)
            if m:
                got[int(m.group(1))] = m.group(2).strip()
        return group, got, u

    # 第一批单独跑，让固定前缀进入缓存；其余并发
    first = run(groups[0])
    rest = list(ThreadPoolExecutor(4).map(run, groups[1:]))
    for group, got, u in [first, *rest]:
        usages.append(("batch", u))
        for i, t in group:
            src_tags = sorted(re.findall(r"<m\d+/>", t))
            out = got.get(i)
            if out is None or sorted(re.findall(r"<m\d+/>", out)) != src_tags:
                failed += 1
            else:
                results[i] = out
    elapsed = time.time() - t0

    hit = sum(u.get("prompt_cache_hit_tokens", 0) for _, u in usages)
    miss = sum(u.get("prompt_cache_miss_tokens", u["prompt_tokens"]) for _, u in usages)
    out = sum(u["completion_tokens"] for _, u in usages)
    src_tik = sum(ntok(t) for _, t in items)
    report = {
        "model": MODEL,
        "segments": len(segs), "unique": len(items), "batches": len(groups),
        "terms": len(terms), "failed_segments": failed, "elapsed_s": round(elapsed, 1),
        "system_prompt_tiktoken": ntok(system),
        "source_tiktoken": src_tik,
        "usage": {"cache_hit": hit, "cache_miss": miss, "output": out,
                  "prompt_total": hit + miss},
        "cost_cny": {p: round(cost(hit, miss, out, p), 4) for p in PRICE},
        "skipped": skipped,
        "estimate_tiktoken": estimate(segs, ntok(RULES) + ntok(abstract) + 40, 0.9),
        "sample": [(items[i][1][:120], results.get(i, "")[:80]) for i in range(0, len(items), max(1, len(items) // 4))],
        "raw_usage_first": usages[1][1],
    }
    outp = Path(layout_path).parent / "_cost.json"
    outp.write_text(json.dumps({**report, "terms_map": terms, "translations": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:])

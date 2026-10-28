r"""翻译提示词 v2（S9 定稿，S10/S11/S12 在 DeepSeek、gpt-6-luna、公开数据集上验证）。

要点：公式以 <mN>LaTeX</mN> 给模型看、输出只写 <mN/>（S9 收益最大的一条）；
风格要求 + 一个示例；模型名及配置后缀保留原文；术语为推荐译法。
改动这里的任何文字都要重跑 S10/S11 回归评测，并递增 PROMPT_VERSION（它是缓存键的一部分）。
"""

import re

from pydantic import BaseModel, Field

PROMPT_VERSION = "v2"

RULES = r"""你是资深学术论文译者，把用户给出的英文段落译成简体中文学术文本。

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


class DocContext(BaseModel):
    title: str = ""
    abstract: str = ""
    domain: str = ""
    terms: dict[str, str] = Field(default_factory=dict)  # 文档级术语（固定前缀，可命中缓存）


def system_prompt(ctx: DocContext) -> str:
    parts = [RULES, ""]
    if ctx.title:
        parts.append(f"论文标题：{ctx.title}")
    if ctx.domain:
        parts.append(f"领域：{ctx.domain}")
    if ctx.abstract:
        parts.append(f"摘要：{ctx.abstract}")
    if ctx.terms:
        parts += ["", "术语表（推荐译法）："] + [f"{k}={v}" for k, v in ctx.terms.items()]
    return "\n".join(parts)


def expand_formulas(source: str, formulas: dict[str, str]) -> str:
    """<m1/> → <m1>LaTeX</m1>，让模型看到公式内容。"""
    return re.sub(r"<(m\d+)/>", lambda m: f"<{m.group(1)}>{formulas.get(m.group(1), '')}</{m.group(1)}>", source)


def user_prompt(items: list[tuple[int, str]], sections: list[str], extra_terms: dict[str, str]) -> str:
    """items：(批内编号, 已展开公式的原文)。extra_terms：本批命中的词库术语（不放进固定前缀）。"""
    lines = []
    if sections:
        lines.append("本批段落所在章节：" + "；".join(sections))
    if extra_terms:
        lines.append("本批术语（推荐译法）：" + "；".join(f"{k}={v}" for k, v in extra_terms.items()))
    lines += [f"{i}|{text}" for i, text in items]
    return "\n".join(lines)

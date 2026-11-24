r"""翻译提示词 v4（保留公式格式，明确公式与概念的绑定）。

要点：公式以 <mN>LaTeX</mN> 给模型看、输出只写 <mN/>（S9 收益最大的一条）；
风格要求与示例；模型名及配置后缀保留原文；区分全文术语和公共参考词库。
改动这里的任何文字都要重跑 S10/S11 回归评测，并递增 PROMPT_VERSION（它是缓存键的一部分）。
"""

import re

from pydantic import BaseModel, Field

PROMPT_VERSION = "v4"

RULES = r"""你是资深学术论文译者，把用户给出的英文段落译成简体中文学术文本。

【格式】
- 每行一段，格式「编号|原文」；输出「编号|译文」，一行对应一行，编号不变，不要输出解释。
- <mN>…</mN> 是公式，里面的 LaTeX 只供你理解，不要翻译或改写；译文中只写 <mN/>，每个编号恰好出现一次。
- 公式在数学陈述中的对应关系必须与原文一致（例如“A 的上界是 B”不能写成“B 的上界是 A”）。
- 调整语序时，尤其是中文定语前置时，<mN/> 必须跟随原本对应的概念或实体一起移动，编号不必递增。
- <b>…</b> 等格式标签原样保留。

【要求】
- 准确第一：不漏译、不增译；数字、单位、引用编号 [12]、作者年份引用 (Smith et al., 2020) 原样保留。
- 意译优先：按中文学术写作习惯调整语序，拆分过长的定语，避免“允许……更多的……”“被……”等生硬直译。
- 模型名、方法名、数据集名、软件名及其配置后缀（如 Transformer (big)、ResNet-50）和评价指标缩写（如 BLEU）保留原文，大小写与原文一致。
- 跨学科多义词按本文领域的通行学术译法统一翻译。普通英文词完整译成中文，禁止半中半英夹译。专名、指标缩写、变量、代号和原样引用按原文保留。
- 汉字与英文、数字、<mN/> 之间不插入空格，使用中文标点。
- 中文叙述中的作者引用用「等」，括号内的作者年份引用保留原文。
- 学术标题和元数据按中文惯例翻译，如 Problem statement 译为「问题描述」，Available online 译为「在线发表」。

【示例】
输入：
7|Self-attention requires <m1>O(1)</m1> sequential operations, whereas a recurrent layer requires <m2>O(n)</m2>, which allows for significantly more parallelization.
8|The weight <m1>w</m1> of the sample <m2>s</m2> is fixed.
输出：
7|自注意力只需<m1/>次顺序操作，而循环层需要<m2/>次，因此可以大幅提高并行度。
8|样本<m2/>的权重<m1/>固定。"""


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
        parts += ["", "本文术语表（全文统一使用）："] + [f"{k}={v}" for k, v in ctx.terms.items()]
    return "\n".join(parts)


def expand_formulas(source: str, formulas: dict[str, str]) -> str:
    """<m1/> → <m1>LaTeX</m1>，让模型看到公式内容。"""

    def expand(match: re.Match[str]) -> str:
        key = match[1]
        latex = (formulas[key] or "[symbol]") if key in formulas else ""
        return f"<{key}>{latex}</{key}>"

    return re.sub(r"<(m\d+)/>", expand, source)


def user_prompt(
    items: list[tuple[int, str]],
    sections: list[str],
    extra_terms: dict[str, str],
    user_terms: dict[str, str] | None = None,
    *,
    retry_feedback: str = "",
) -> str:
    """items：(批内编号, 已展开公式的原文)。extra_terms：本批命中的词库术语（不放进固定前缀）。"""
    lines = []
    if sections:
        lines.append("本批段落所在章节：" + "；".join(sections))
    if user_terms:
        lines.append("本批用户术语（统一使用）：" + "；".join(f"{k}={v}" for k, v in user_terms.items()))
    if extra_terms:
        lines.append(
            "参考词库（仅在语境与论文领域吻合时参考，冲突时以自然学术译法为准）："
            + "；".join(f"{k}={v}" for k, v in extra_terms.items())
        )
    if retry_feedback:
        lines.append(
            f"上次翻译未通过校验（{retry_feedback}），请修正该问题并重新输出完整「编号|译文」，严格保留所有公式占位符与格式标签。"
        )
    lines += [f"{i}|{text}" for i, text in items]
    return "\n".join(lines)

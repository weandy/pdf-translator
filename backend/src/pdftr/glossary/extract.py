"""用一次小的模型调用抽取文档术语与领域（S10 的 TERMS_V3；参考 MAPS 让模型先给出主题）。

只读标题、摘要、正文开头和章节标题（约 600 token），不扫全文——BabelDOC 的全文术语抽取约占其处理量的 30%。
"""

import re

from pydantic import BaseModel, Field

from pdftr.domain.llm import ChatModel, ChatOptions, ChatUsage

PROMPT = """阅读下面的论文信息，按顺序输出：
1. 第一行输出「领域：……」，用一句话概括论文所属学科与研究主题。
2. 第二行输出「AI公共词库：启用」或「AI公共词库：停用」。
   仅当论文主领域为 AI、机器学习、深度学习、自然语言处理、计算机视觉、强化学习的算法理论，
   或计算机科学核心方向时启用。工业工程、运筹调度、物理、生物医学、材料、金融、社科等应用或非 AI 学科停用，
   即使这些论文采用了 AI 方法。
3. 之后抽取需要全文统一译法的专业术语，最多 40 个，每行「英文=中文」：
   - 收入概念、方法、指标，以及本文高频出现、有多种译法的核心领域对象词（如 job、instance、operation、agent、node）。
     不收章节名和与本文领域无关的普通词汇。
   - 按术语在摘要或正文中的写法输出，避免沿用标题的大写格式。
   - 模型、架构（如 Transformer、BERT、ResNet）及应保留原文的方法和数据集名，输出「英文=英文」。
   - 使用中文学界通行译法。
不要输出其他内容。

标题：{title}
摘要：{abstract}
正文开头：{intro}
章节标题（仅供判断领域）：{headings}"""


class Extraction(BaseModel):
    domain: str = ""
    terms: dict[str, str] = Field(default_factory=dict)
    use_library: bool = False
    usage: ChatUsage = Field(default_factory=ChatUsage)


def parse(text: str) -> Extraction:
    domain, terms = "", {}
    use_library = False
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if m := re.match(r"^领域\s*[:：]\s*(.+)$", line):
            domain = m.group(1).strip()
        elif m := re.fullmatch(r"AI\s*公共词库\s*[:：]\s*(启用|停用)", line, re.I):
            use_library = m.group(1) == "启用"
        elif "=" in line:
            k, v = (p.strip() for p in line.split("=", 1))
            if k and v and len(k) <= 80:
                terms[k] = v
    return Extraction(domain=domain, terms=terms, use_library=use_library)


async def extract_terms(chat: ChatModel, *, title: str, abstract: str, intro: str, headings: list[str]) -> Extraction:
    prompt = PROMPT.format(title=title, abstract=abstract, intro=intro[:1500], headings="; ".join(headings))
    result = await chat.complete("你是学术术语专家。", prompt, ChatOptions(max_tokens=1500))
    found = parse(result.text)
    found.usage = result.usage
    return found

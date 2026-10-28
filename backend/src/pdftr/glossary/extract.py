"""用一次小的模型调用抽取文档术语与领域（S10 的 TERMS_V3；参考 MAPS 让模型先给出主题）。

只读标题、摘要、正文开头和章节标题（约 600 token），不扫全文——BabelDOC 的全文术语抽取约占其处理量的 30%。
"""

import re

from pydantic import BaseModel, Field

from pdftr.domain.llm import ChatModel, ChatOptions, ChatUsage

PROMPT = """阅读下面的论文信息，完成两件事：
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


class Extraction(BaseModel):
    domain: str = ""
    terms: dict[str, str] = Field(default_factory=dict)
    usage: ChatUsage = Field(default_factory=ChatUsage)


def parse(text: str) -> tuple[str, dict[str, str]]:
    domain, terms = "", {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if m := re.match(r"^领域\s*[:：]\s*(.+)$", line):
            domain = m.group(1).strip()
        elif "=" in line:
            k, v = (p.strip() for p in line.split("=", 1))
            if k and v and len(k) <= 80:
                terms[k] = v
    return domain, terms


async def extract_terms(chat: ChatModel, *, title: str, abstract: str, intro: str, headings: list[str]) -> Extraction:
    prompt = PROMPT.format(title=title, abstract=abstract, intro=intro[:1500], headings="; ".join(headings))
    result = await chat.complete("你是学术术语专家。", prompt, ChatOptions(max_tokens=1500))
    domain, terms = parse(result.text)
    return Extraction(domain=domain, terms=terms, usage=result.usage)

"""用户术语库：多种格式的规则识别、模型兜底、用户术语优先。"""

import asyncio
import json

from pdftr.domain.llm import ChatOptions, ChatResult, ChatUsage
from pdftr.glossary.matcher import TermMatcher, combine, override
from pdftr.glossary.user_import import parse_rules, parse_upload


def _pairs(text: str) -> list[tuple[str, str, str]]:
    return [(e.source, e.target, e.note) for e in parse_rules(text)]


def test_csv_with_header_any_column_order_and_notes():
    text = "中文,英文,说明\n注意力机制,attention mechanism,\n残差连接,residual connection,也叫跳跃连接\n"
    assert _pairs(text) == [
        ("attention mechanism", "注意力机制", ""),
        ("residual connection", "残差连接", "也叫跳跃连接"),
    ]


def test_tsv_markdown_pairs_and_json():
    assert _pairs("dropout\t随机失活\nbatch size\t批量大小") == [
        ("dropout", "随机失活", ""),
        ("batch size", "批量大小", ""),
    ]
    md = "| 术语 | 译法 |\n|---|---|\n| `encoder` | 编码器 |\n| decoder | 解码器 |\n"
    assert _pairs(md) == [("encoder", "编码器", ""), ("decoder", "解码器", "")]
    assert _pairs("embedding = 嵌入\nfine-tuning：微调\n") == [("embedding", "嵌入", ""), ("fine-tuning", "微调", "")]
    assert _pairs(json.dumps({"token": "词元"}, ensure_ascii=False)) == [("token", "词元", "")]
    rows = [{"en": "loss", "zh": "损失", "note": "目标函数的值"}]
    assert _pairs(json.dumps(rows, ensure_ascii=False)) == [("loss", "损失", "目标函数的值")]


def test_duplicates_keep_first_case_insensitive():
    assert _pairs("Token,词元\ntoken,令牌") == [("Token", "词元", "")]


class Chat:
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        self.calls += 1
        return ChatResult(text="gradient clipping\t梯度裁剪\t防止梯度爆炸\n", usage=ChatUsage(prompt_tokens=10))

    async def list_models(self) -> list[str]:
        return []


def test_free_text_needs_model_and_rules_skip_it():
    prose = "我们组约定：gradient clipping 统一译作梯度裁剪，用来防止梯度爆炸。\n其他术语见附录。".encode()
    # 规则读不出自由文本（不会自动调用模型花用户的额度）
    assert asyncio.run(parse_upload(prose, chat=None)).entries == []
    chat = Chat()
    got = asyncio.run(parse_upload(prose, chat=chat))
    assert got.method == "ai" and chat.calls == 1
    assert [(e.source, e.target, e.note) for e in got.entries] == [("gradient clipping", "梯度裁剪", "防止梯度爆炸")]


def test_gb18030_file():
    assert asyncio.run(parse_upload("卷积,convolution".encode("gb18030"), chat=None)).entries[0].target == "卷积"


def test_user_terms_win_over_library_and_extracted():
    library = TermMatcher({"attention mechanism": "注意机制", "feature map": "特征图"})
    lookup = combine({"Attention Mechanism": "注意力机制", "GA": "遗传算法"}, library)
    assert lookup is not None
    hits = lookup("The attention mechanism and GA use a feature map.")
    assert hits == {"feature map": "特征图", "Attention Mechanism": "注意力机制", "GA": "遗传算法"}
    assert combine({}, None) is None
    assert override({"attention mechanism": "注意机制", "loss": "损失"}, {"Attention Mechanism": "注意力机制"}) == {
        "attention mechanism": "注意力机制",
        "loss": "损失",
    }

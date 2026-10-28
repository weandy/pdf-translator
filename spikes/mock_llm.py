"""伪翻译服务：模拟 OpenAI 兼容的 /v1/chat/completions。

只用于版式实验：保留占位符和标签，其余文字按长度换成中文素材，
并把请求记录到 out/mock_requests.jsonl，方便查看调用方发来的提示词格式。

用法：python mock_llm.py [端口]
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(__file__).parent / "out" / "mock_requests.jsonl"
ZH = (
    "本文提出了一种新的网络结构，完全基于注意力机制，摒弃了循环和卷积。"
    "在两个机器翻译任务上的实验表明，该模型在质量上更优，同时更易于并行化，"
    "训练所需时间显著减少。我们进一步证明该方法可以很好地推广到其他任务。"
)
RATIO = 0.4
# 需要原样保留的片段：{v1}、<b>…</b>、<style id='1'>、XML 风格占位符等
KEEP = re.compile(r"\{v?\d+\}|</?[a-zA-Z_][\w\-]*(?:\s[^<>]*)?/?>|\$[^$]+\$")


def fake(text: str) -> str:
    out, pos, k = [], 0, 0
    for m in list(KEEP.finditer(text)) + [None]:
        seg = text[pos : m.start() if m else len(text)]
        if seg.strip():
            n = max(1, round(len(seg) * RATIO))
            out.append((ZH * (n // len(ZH) + 2))[k : k + n])
            k = (k + n) % len(ZH)
        if m:
            out.append(m.group())
            pos = m.end()
    return "".join(out)


def respond(messages: list[dict]) -> str:
    """尽量识别"待翻译原文"：取最后一条 user 消息里最后一段非指令文本。"""
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    if isinstance(user, list):
        user = " ".join(p.get("text", "") for p in user)
    # 调用方若要求 JSON（批量翻译），对 JSON 里的字符串值逐个伪翻译
    m = re.search(r"(\[\s*\{.*\}\s*\]|\{.*\})\s*$", user, re.S)
    if m:
        try:
            data = json.loads(m.group(1))

            def walk(x, key=None):
                if isinstance(x, dict):
                    return {k: walk(v, k) for k, v in x.items()}
                if isinstance(x, list):
                    return [walk(v) for v in x]
                if isinstance(x, str) and key not in ("id", "type", "layout_label"):
                    return fake(x)
                return x

            return json.dumps(walk(data), ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    tail = user.rsplit("\n\n", 1)[-1]
    return fake(tail)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = respond(body.get("messages", []))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"req": body, "resp": content}, ensure_ascii=False) + "\n")
        payload = {
            "id": "mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002  静默访问日志
        pass


if __name__ == "__main__":
    LOG.parent.mkdir(exist_ok=True)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

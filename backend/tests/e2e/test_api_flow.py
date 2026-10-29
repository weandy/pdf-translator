"""端到端：通过 HTTP 接口走完 上传 → 解析 → 融合 → 切分 → 术语 → 翻译 → 背景页，再编辑与重翻。

解析服务和大模型用假实现（不访问网络），PDF 按合成夹具的坐标现场生成。
"""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.layout import LayoutResult
from pdftr.domain.llm import ChatError, ChatOptions, ChatResult, ChatUsage
from pdftr.parsing.mineru_normalize import normalize
from pdftr.pipeline import usecases

FIXTURES = Path(__file__).parent.parent / "fixtures"
TEXTS = [
    (0, 52, 56, "A Synthetic Title"),
    (0, 52, 609, "Let"),
    (0, 124, 609, "f(x)"),
    (0, 154, 609, "be a function that"),
    (0, 312, 109, "continues in the next column"),
    (0, 52, 556, "Figure 1: A caption."),
    (0, 312, 712, "[1] Some reference."),
    (0, 52, 18, "Running header"),
    (0, 52, 755, "1 A footnote."),
    (1, 52, 69, "and ends on the next page."),
    (1, 52, 109, "A normal paragraph."),
]
HEADERS = {"X-LLM-Key": "test-key", "X-Parser-Key": "test-token", "X-LLM-Model": "deepseek-flash"}


def make_pdf() -> bytes:
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=600, height=800)
    for page, x, y, text in TEXTS:
        doc[page].insert_text((x, y), text, fontsize=9, fontname="tiro")
    # 译文块里的页内跳转（要在译文里重新定位）与译文块外的网址（原位置保留）
    first = doc[0]
    first.insert_link({"kind": pymupdf.LINK_GOTO, "from": first.search_for("function")[0], "page": 1,
                       "to": pymupdf.Point(52, 760)})  # fmt: skip
    first.insert_link({"kind": pymupdf.LINK_URI, "from": first.search_for("Running header")[0],
                       "uri": "https://example.org/"})  # fmt: skip
    return doc.tobytes()


class FakeParser:
    name = "fake"
    is_cloud = True

    async def parse(self, pdf: Path, pages=None, raw_dir=None, on_progress=None) -> LayoutResult:
        if on_progress:
            on_progress("MinerU 正在解析，第 1/2 页", 0.5)
        return normalize(json.loads((FIXTURES / "mineru_layout_min.json").read_text(encoding="utf-8")))


class FakeChat:
    model = "deepseek-flash"
    fail = False

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        if FakeChat.fail:
            raise ChatError("翻译服务的 API Key 无效")
        if "抽取需要全文统一译法的专业术语" in user:
            return ChatResult(
                text="领域：测试\nfunction=函数", usage=ChatUsage(prompt_tokens=100, completion_tokens=40)
            )
        out = []
        for line in user.splitlines():
            if m := re.match(r"^(\d+)\|(.*)$", line):
                text = re.sub(r"<(m\d+)>.*?</\1>", r"<\1/>", m.group(2))
                tag = "精" if opts.thinking == "low" else "译"
                out.append(f"{m.group(1)}|{tag}:{text}")
        return ChatResult(text="\n".join(out), usage=ChatUsage(prompt_tokens=100, completion_tokens=40))

    async def list_models(self) -> list[str]:
        if FakeChat.fail:
            raise ChatError("翻译服务的 API Key 无效")
        return ["deepseek-flash", "deepseek-v4-pro"]


def _client(data_dir: Path, font_dir: Path):
    FakeChat.fail = False
    settings = Settings(data_dir=data_dir, glossary_sources="", font_dir=font_dir)
    container = build_container(
        settings, {"parser_factory": lambda s, name: FakeParser(), "chat_factory": lambda s: FakeChat()}
    )
    return TestClient(create_app(container))


@pytest.fixture()
def client(tmp_path: Path):
    with _client(tmp_path, tmp_path / "fonts") as c:  # 不依赖本机是否下载了字体
        yield c


def _upload(client: TestClient, consent: bool = True, data: bytes | None = None, headers=HEADERS, options=None):
    options = json.dumps(options or {"cloud_consent": consent})
    files = {"file": ("paper.pdf", data if data is not None else make_pdf(), "application/pdf")}
    return client.post("/api/jobs", files=files, data={"options": options}, headers=headers)


def _wait(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("任务超时")


def test_full_flow(client: TestClient):
    r = _upload(client)
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()["id"])
    assert job["status"] == "done", job
    assert job["page_count"] == 2 and job["cost_cny"] is not None

    # 进度事件：已完成的任务直接推送最终状态后结束
    with client.stream("GET", f"/api/jobs/{job['id']}/events") as s:
        body = "".join(s.iter_text())
    assert '"status":"done"' in body

    doc = client.get(f"/api/jobs/{job['id']}/document").json()
    assert doc["title"] == "A Synthetic Title"
    assert [p["index"] for p in doc["pages"]] == [0, 1]
    segs = {s["source"]: s for s in doc["segments"]}
    chain = segs["Let <m1/> be a function that continues in the next column and ends on the next page."]
    assert chain["block_ids"] == ["p0-b1", "p0-b2", "p1-b0"]
    assert chain["target"].startswith("译:") and chain["status"] == "done"
    assert chain["formulas"]["m1"]["latex"] == "f(x)" and chain["formulas"]["m1"]["page"] == 0
    assert len(chain["parts"]) == 3
    assert "[1] Some reference." not in segs  # 参考文献默认不翻

    # 背景页已生成，且译文块的文字被删掉、未翻译的保留
    pdf = client.get(f"/api/jobs/{job['id']}/files/background.pdf")
    assert pdf.status_code == 200
    with pymupdf.open(stream=pdf.content, filetype="pdf") as bg:
        text = bg[0].get_text()
    assert "be a function" not in text and "Some reference" in text and "Running header" in text
    assert client.get(f"/api/jobs/{job['id']}/files/original.pdf").status_code == 200
    assert client.get(f"/api/jobs/{job['id']}/files/secret.txt").status_code == 422

    # 编辑、重翻、公式裁剪图
    sid = chain["id"]
    edited = client.patch(f"/api/segments/{sid}", json={"target": "用户修改的译文"}).json()
    assert edited["status"] == "edited" and edited["target"] == "用户修改的译文"
    again = client.post(f"/api/segments/{sid}/retranslate", json={"quality": "careful"}, headers=HEADERS).json()
    assert again["target"].startswith("精:") and again["quality"] == "careful"
    png = client.get(f"/api/segments/{sid}/formulas/m1.png")
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")
    assert client.get(f"/api/segments/{sid}/formulas/m9.png").status_code == 404

    # 导出：纯译文 PDF 里是译文（含用户刚才的修改），双语对照页宽是原来的两倍
    mono = client.get(f"/api/jobs/{job['id']}/export", params={"mode": "mono"})
    assert (
        mono.status_code == 200
        and "filename*=UTF-8''paper-%E8%AF%91%E6%96%87.pdf" in mono.headers["content-disposition"]
    )
    with pymupdf.open(stream=mono.content, filetype="pdf") as pdf:
        text = "".join(str(p.get_text()) for p in pdf)
    assert "精:" in text and "译:A Synthetic Title" in text  # 假模型把原文加前缀当译文
    dual = client.get(f"/api/jobs/{job['id']}/export", params={"mode": "dual"})
    with pymupdf.open(stream=dual.content, filetype="pdf") as pdf:
        assert pdf[0].rect.width == 1200 and "be a function" in pdf[0].get_text()
        left = str(pdf[0].get_text(clip=pymupdf.Rect(0, 0, 600, 800)))
    assert "Let" in left and "精:" not in left  # 默认原文在左、译文在右
    dual_left = client.get(f"/api/jobs/{job['id']}/export", params={"mode": "dual", "side": "left"})
    with pymupdf.open(stream=dual_left.content, filetype="pdf") as pdf:
        assert "精:" in str(pdf[0].get_text(clip=pymupdf.Rect(0, 0, 600, 800)))

    # 不提供任务列表（防止公开部署时看到别人的任务）；可以删除自己的任务
    assert client.get("/api/jobs").status_code == 405
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 404


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"consent": False}, "确认文件会上传"),
        ({"data": b"hello"}, "只支持 PDF"),
        ({"data": b"%PDF-1.4 broken"}, "无法打开"),
        ({"headers": {"X-Parser-Key": "t"}}, "API Key"),
    ],
)
def test_upload_rejected(client: TestClient, kwargs: dict, message: str):
    r = _upload(client, **kwargs)
    assert r.status_code == 400
    assert message in r.json()["detail"]


def test_chat_error_fails_job_and_retry_resumes(client: TestClient):
    FakeChat.fail = True
    job = _wait(client, _upload(client).json()["id"])
    assert job["status"] == "failed" and "API Key 无效" in job["error"]
    assert job["stage"] == "glossary"  # 解析、融合、切分已完成

    FakeChat.fail = False
    r = client.post(f"/api/jobs/{job['id']}/retry", headers=HEADERS)
    assert r.status_code == 202
    assert _wait(client, job["id"])["status"] == "done"


def test_expired_jobs_and_cache_are_purged(client: TestClient):
    pipeline = client.app.state.container.pipeline  # type: ignore[attr-defined]
    job = _wait(client, _upload(client).json()["id"])
    job_dir = pipeline.deps.artifacts.job_dir(job["id"])
    assert usecases.purge_expired(pipeline, 7) == 0  # 未过期

    later = datetime.now(UTC) + timedelta(days=8)
    assert usecases.purge_expired(pipeline, 7, now=later) == 1
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert not job_dir.exists()


def test_busy_server_rejects_uploads(client: TestClient):
    settings = client.app.state.container.settings  # type: ignore[attr-defined]
    settings.max_concurrent_jobs, settings.max_queued_jobs = 0, 0
    r = _upload(client)
    assert r.status_code == 503 and "正忙" in r.json()["detail"]

    settings.max_queued_jobs, settings.min_free_disk_mb = 10, 10**12
    r = _upload(client)
    assert r.status_code == 503 and "存储空间不足" in r.json()["detail"]


def test_meta(client: TestClient):
    cfg = client.get("/api/config").json()
    assert cfg["server_has_llm_key"] is False and cfg["max_pages"] == 200 and cfg["retention_days"] == 7
    est = client.get("/api/estimate", params={"pages": 15}).json()
    assert est["pages"] == 15 and 0 < est["cny_low"] <= est["cny_high"] < 0.2
    assert client.get("/api/jobs/0123456789abcdef").status_code == 404
    # 阅读器字体：未安装时 404（前端退回系统字体）；不在白名单里的文件名不接受
    assert client.get("/api/fonts/NotoSerifSC-Regular.woff2").status_code == 404
    assert client.get("/api/fonts/..%2Fapp.db").status_code in (404, 422)


FONT_DIR = Path(__file__).parents[2] / "fonts"


@pytest.mark.skipif(not (FONT_DIR / "NotoSerifSC-Regular.ttf").exists(), reason="需要先运行 scripts/fetch_fonts.py")
def test_export_uses_gbt_fonts(tmp_path: Path):
    """装了字体时用 Tinos 排西文（假模型的译文是英文加前缀），复制出来的空格是普通空格，字体接口可用。"""
    with _client(tmp_path, FONT_DIR) as client:
        job = _wait(client, _upload(client).json()["id"])
        data = client.get(f"/api/jobs/{job['id']}/export", params={"mode": "mono"}).content
        assert client.get("/api/fonts/NotoSerifSC-Regular.woff2").status_code == 200
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        fonts = {str(f[3]) for p in pdf for f in p.get_fonts()}
        text = "".join(str(p.get_text()) for p in pdf)
    assert any("Tinos" in f for f in fonts)
    assert "\xa0" not in text and "A normal paragraph." in text


def test_paddle_parser_requires_its_own_token(client: TestClient):
    options = json.dumps({"cloud_consent": True, "parser": "paddleocr-vl"})
    files = {"file": ("paper.pdf", make_pdf(), "application/pdf")}
    r = client.post("/api/jobs", files=files, data={"options": options}, headers=HEADERS)
    assert r.status_code == 400 and "PaddleOCR Token" in r.json()["detail"]
    r = client.post("/api/jobs", files=files, data={"options": options}, headers={**HEADERS, "X-Paddle-Key": "pt"})
    assert r.status_code == 202 and r.json()["options"]["parser"] == "paddleocr-vl"


def test_revisions_record_manual_changes_and_revert_per_step(client: TestClient):
    job = _wait(client, _upload(client).json()["id"])
    segs = client.get(f"/api/jobs/{job['id']}/document").json()["segments"]
    a, b = segs[0], segs[1]
    client.patch(f"/api/segments/{a['id']}", json={"target": "第一次修改"})
    client.patch(f"/api/segments/{b['id']}", json={"target": "另一段的修改"})
    client.post(f"/api/segments/{a['id']}/retranslate", json={"quality": "careful"}, headers=HEADERS)
    client.patch(f"/api/segments/{a['id']}", json={"target": "第二次修改"})
    revs = client.get(f"/api/jobs/{job['id']}/revisions").json()
    assert [r["action"] for r in revs] == ["edit", "edit", "careful", "edit"]
    assert revs[0]["before"] == a["target"] and revs[0]["after"] == "第一次修改"

    # 撤回 a 的第一步：a 回到机器译文，a 之后的两步一并作废；b 不受影响
    seg = client.post(f"/api/segments/revisions/{revs[0]['id']}/revert").json()
    assert seg["target"] == a["target"] and seg["status"] == "done"
    revs = client.get(f"/api/jobs/{job['id']}/revisions").json()
    undo = revs[-1]
    assert undo["action"] == "revert" and undo["reverts"] == revs[0]["id"] and undo["before"] == "第二次修改"
    assert [r["reverted_by"] for r in revs[:4]] == [undo["id"], None, undo["id"], undo["id"]]
    doc = client.get(f"/api/jobs/{job['id']}/document").json()
    assert {s["id"]: s["target"] for s in doc["segments"]}[b["id"]] == "另一段的修改"

    # 已撤回的步骤不能再撤回；撤回 b 的那一步后 b 也回到机器译文
    assert client.post(f"/api/segments/revisions/{revs[0]['id']}/revert").status_code == 400
    assert client.post(f"/api/segments/revisions/{revs[1]['id']}/revert").json()["target"] == b["target"]
    assert client.post("/api/segments/revisions/99999/revert").status_code == 404
    client.delete(f"/api/jobs/{job['id']}")
    assert client.get(f"/api/jobs/{job['id']}/revisions").status_code == 404


def test_llm_models_and_check(client: TestClient):
    assert client.get("/api/llm/models", headers=HEADERS).json() == {"models": ["deepseek-flash", "deepseek-v4-pro"]}
    ok = client.post("/api/llm/check", headers=HEADERS).json()
    assert ok["ok"] and ok["model"] == "deepseek-flash" and ok["latency_ms"] >= 0
    FakeChat.fail = True
    bad = client.post("/api/llm/check", headers=HEADERS).json()
    assert not bad["ok"] and "Key 无效" in bad["detail"]
    assert client.get("/api/llm/models", headers=HEADERS).status_code == 502


def test_shared_llm_key_only_goes_to_configured_service(tmp_path: Path):
    from pdftr.container import Container

    c = Container(settings=Settings(data_dir=tmp_path, llm_api_key="shared"), pipeline=None)  # type: ignore[arg-type]
    kw = {"api_key": None, "model": None, "parser_token": None}
    assert c.secrets(base_url=None, **kw).llm_api_key == "shared"
    assert c.secrets(base_url="https://api.deepseek.com/", **kw).llm_api_key == "shared"
    # 改了接口地址或协议就不再带上部署者的 Key，防止被转发到别人的服务器
    assert c.secrets(base_url="https://evil.example/v1", **kw).llm_api_key == ""
    assert c.secrets(base_url=None, protocol="anthropic", **kw).llm_api_key == ""
    assert (
        c.secrets(base_url="https://evil.example/v1", api_key="mine", model=None, parser_token=None).llm_api_key
        == "mine"
    )


def test_user_terms_reach_the_prompt_and_glossary_upload(client: TestClient):
    seen: list[str] = []
    original = FakeChat.complete

    async def spy(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        seen.append(system + "\n" + user)
        return await original(self, system, user, opts)

    FakeChat.complete = spy  # type: ignore[method-assign]
    try:
        r = _upload(client, options={"cloud_consent": True, "user_terms": {"function": "映射", "next page": "下一页"}})
        job = _wait(client, r.json()["id"])
        assert job["options"]["user_terms"]["function"] == "映射"
        prompts = "\n".join(seen)
        # 自动抽取出的 function=函数 被用户的译法覆盖；用户术语按命中放进本批
        assert "function=映射" in prompts and "function=函数" not in prompts and "next page=下一页" in prompts

        # 重翻时用浏览器里最新的术语库
        seg = client.get(f"/api/jobs/{job['id']}/document").json()["segments"][1]
        seen.clear()
        client.post(f"/api/segments/{seg['id']}/retranslate", headers=HEADERS,
                    json={"quality": "default", "user_terms": {"function": "功能"}})  # fmt: skip
        assert "function=功能" in "\n".join(seen)
    finally:
        FakeChat.complete = original  # type: ignore[method-assign]

    files = {"file": ("terms.csv", "英文,中文\ndropout,随机失活\n".encode(), "text/csv")}
    got = client.post("/api/glossary/parse", files=files, headers=HEADERS).json()
    assert got["method"] == "rule" and got["entries"] == [{"source": "dropout", "target": "随机失活", "note": ""}]
    big = {"file": ("big.txt", b"x" * (2 * 1024 * 1024 + 10), "text/plain")}
    assert client.post("/api/glossary/parse", files=big, headers=HEADERS).status_code == 400


def test_cost_includes_term_extraction_and_retranslation(client: TestClient):
    job = _wait(client, _upload(client).json()["id"])
    segs = client.get(f"/api/jobs/{job['id']}/document").json()["segments"]
    # 假模型每次请求 100 个输入 token、40 个输出 token；术语抽取那一次也计入
    per_request = (100 * 1.0 + 40 * 4.0) / 1e6  # 空闲时段单价；高峰时段翻倍
    assert job["cost_cny"] >= 2 * per_request - 1e-12
    client.post(f"/api/segments/{segs[0]['id']}/retranslate", json={"quality": "default"}, headers=HEADERS)
    after = client.get(f"/api/jobs/{job['id']}").json()["cost_cny"]
    assert after > job["cost_cny"]  # 重翻的费用也计入


def test_links_survive_translation(client: TestClient):
    job = _wait(client, _upload(client).json()["id"])
    links = client.get(f"/api/jobs/{job['id']}/document").json()["links"]
    inner = next(x for x in links if x["block_id"])
    outer = next(x for x in links if x["uri"])
    # 正文里的跳转：在译文里找到 function（段落跨三块，按译文长度拆开后它落在相邻一块）
    # 页眉的网址在译文块外，原位置不变
    assert inner["block_id"] in ("p0-b1", "p0-b2") and inner["text"] == "function" and inner["dest_page"] == 1
    assert abs(inner["dest_top"] - 40) < 1  # 目标点 y=760（自下而上）→ 离页顶 40
    assert outer["block_id"] is None and outer["uri"] == "https://example.org/"

    mono = client.get(f"/api/jobs/{job['id']}/export", params={"mode": "mono"})
    with pymupdf.open(stream=mono.content, filetype="pdf") as pdf:
        got = pdf[0].get_links()
        jump = next(x for x in got if x["kind"] == pymupdf.LINK_GOTO)
        assert "function" in pdf[0].get_textbox(jump["from"]) and jump["page"] == 1
        assert any(x.get("uri") == "https://example.org/" for x in got)
    dual = client.get(f"/api/jobs/{job['id']}/export", params={"mode": "dual"})
    with pymupdf.open(stream=dual.content, filetype="pdf") as pdf:
        jumps = [x for x in pdf[0].get_links() if x["kind"] == pymupdf.LINK_GOTO]
        # 两半各一个，跳转目标落在同一半
        assert sorted(x["from"].x0 >= 600 for x in jumps) == [False, True]
        assert all((x["from"].x0 >= 600) == (x["to"].x >= 600) for x in jumps)

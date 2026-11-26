"""从冻结的 Git 对象构建三组，在各自的解释器内生成消融证据。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx2

from .contract import (
    ALL_GROUPS,
    BASELINE_EXTRACT_PROMPT_SHA256,
    BASELINE_PROMPT_VERSION,
    BASELINE_RULES_SHA256,
    BASELINE_SHA,
    CaseDoc,
    EndpointConfig,
    ExtractionSnapshot,
    GenerationRecord,
    GenerationStatus,
    GroupId,
    PolicyConfig,
    PriceEntry,
    RunMode,
    canonical_json,
    sha256_canonical,
    sha256_text,
)

if TYPE_CHECKING:
    from pdftr.domain.ir import Segment
    from pdftr.domain.llm import ChatOptions, ChatResult
    from pdftr.translation.cost import Price
    from pdftr.translation.openai_compat import OpenAICompatChat

_TARGETS = {
    "tr011.patch": "backend/src/pdftr/glossary/extract.py",
    "tr012.patch": "backend/src/pdftr/translation/prompt.py",
}
_MODULE_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PreparedTrees:
    base_sha: str
    root: Path
    trees: dict[GroupId, Path]
    patch_hashes: dict[str, str]
    prompt_hashes: dict[GroupId, dict[str, str]]


def _git(cwd: Path, args: list[str], data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, input=data, stdout=subprocess.PIPE, timeout=60).stdout


def _tree_digest(tree: Path) -> dict[str, str]:
    files = {}
    for path in tree.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise ValueError(f"隔离源码树包含链接：{path}")
        if path.is_file():
            files[path.relative_to(tree).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _fingerprints(tree: Path) -> dict[str, str]:
    import pdftr
    from pdftr.glossary import extract
    from pdftr.translation import prompt, service

    src = (tree / "backend/src").resolve(strict=True)
    for module in (pdftr, extract, prompt, service):
        if not module.__file__ or not Path(module.__file__).resolve(strict=True).is_relative_to(src):
            raise RuntimeError(f"隔离导入失败：{module.__name__} 来自 {module.__file__}")
    if service.PROMPT_VERSION != prompt.PROMPT_VERSION:
        raise ValueError("翻译服务绑定的提示词版本不一致")
    return {
        "prompt_version": prompt.PROMPT_VERSION,
        "extract_prompt_sha256": sha256_text(extract.PROMPT),
        "rules_sha256": sha256_text(prompt.RULES),
    }


def _check_matrix(hashes: dict[GroupId, dict[str, str]]) -> None:
    baseline = {
        "prompt_version": BASELINE_PROMPT_VERSION,
        "extract_prompt_sha256": BASELINE_EXTRACT_PROMPT_SHA256,
        "rules_sha256": BASELINE_RULES_SHA256,
    }
    a, b, c = (hashes[g] for g in ALL_GROUPS)
    if a != baseline or a["extract_prompt_sha256"] == b["extract_prompt_sha256"]:
        raise ValueError("基线或 TR-011 抽取指纹不符")
    if b != (a | {"extract_prompt_sha256": c["extract_prompt_sha256"]}):
        raise ValueError("B/C 抽取指纹或 B 翻译指纹不符")
    if c["prompt_version"] != "v8" or c["rules_sha256"] == b["rules_sha256"]:
        raise ValueError("TR-012 翻译指纹不符")


@contextmanager
def _worker(tree: Path, mode: RunMode) -> Iterator[subprocess.Popen[bytes]]:
    env = os.environ | {"PYTHONPATH": os.pathsep.join(map(str, [tree / "backend/src", _MODULE_ROOT]))}
    with subprocess.Popen(
        [sys.executable, "-B", "-X", "utf8", "-m", "spikes.batch_d.generate", "--worker", tree, mode],
        cwd=tree,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as proc:
        try:
            yield proc
        finally:
            if proc.stdin:
                proc.stdin.close()
            if proc.poll() is None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def _receive(proc: subprocess.Popen[bytes]) -> Any:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(f"隔离子进程提前退出（{proc.wait()}）")
    return json.loads(line)


def _request(proc: subprocess.Popen[bytes], payload: dict[str, Any]) -> Any:
    assert proc.stdin is not None
    proc.stdin.write(canonical_json(payload).encode() + b"\n")
    proc.stdin.flush()
    return _receive(proc)


def _build(
    repo_root: Path,
    out_dir: Path,
    base_sha: str,
    overrides: Sequence[Path | None],
    mode: RunMode,
    stack: ExitStack,
) -> tuple[PreparedTrees, dict[GroupId, subprocess.Popen[bytes]]]:
    repo, root = repo_root.resolve(strict=True), out_dir.resolve()
    if root.is_relative_to(repo):
        raise ValueError("实验输出目录必须位于仓库外")
    root.mkdir(parents=True, exist_ok=True)
    patches: dict[str, bytes] = {}
    for (name, target), override in zip(_TARGETS.items(), overrides, strict=True):
        patch = override.read_bytes() if override else _git(repo, ["show", f"HEAD:spikes/batch_d/{name}"])
        if b"\r" in patch or not patch.endswith(b"\n"):
            raise ValueError(f"{name} 必须是 LF 补丁")
        stat = _git(root, ["apply", "--numstat", "--summary", "-z", "-"], patch)
        names = [line for line in patch.splitlines() if line.startswith((b"diff --git ", b"--- ", b"+++ "))]
        expected = [f"diff --git a/{target} b/{target}", f"--- a/{target}", f"+++ b/{target}"]
        if names != [name.encode() for name in expected] or not re.fullmatch(
            rb"\d+\t\d+\t" + re.escape(target.encode()) + rb"\x00", stat
        ):
            raise ValueError(f"{name} 白名单只允许修改 {target}")
        patches[name] = patch
    sha = _git(repo, ["rev-parse", "--verify", "--end-of-options", base_sha + "^{commit}"]).decode().strip()
    archive = _git(repo, ["-c", "core.autocrlf=false", "archive", "--format=tar", sha, "backend/src/pdftr"])
    workers: dict[GroupId, subprocess.Popen[bytes]] = {}
    trees: dict[GroupId, Path] = {}
    hashes: dict[GroupId, dict[str, str]] = {}
    for count, group in enumerate(ALL_GROUPS):
        tree = root / f"tree_{group}"
        with tempfile.TemporaryDirectory(dir=root) as staging:
            stage = Path(staging) / "tree"
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(stage, filter="data")
            for patch in list(patches.values())[:count]:
                _git(stage, ["apply", "--check", "--whitespace=error", "-"], patch)
                _git(stage, ["apply", "--whitespace=error", "-"], patch)
            if tree.exists():
                if tree.resolve() != tree or _tree_digest(stage) != _tree_digest(tree):
                    raise ValueError(f"已有隔离树与冻结源码不符：{tree}")
            else:
                stage.rename(tree)
        trees[group] = tree
        workers[group] = stack.enter_context(_worker(tree, mode))
        hashes[group] = _receive(workers[group])
    _check_matrix(hashes)
    return PreparedTrees(
        sha, root, trees, {n: hashlib.sha256(p).hexdigest() for n, p in patches.items()}, hashes
    ), workers


def prepare_trees(
    repo_root: Path,
    out_dir: Path,
    *,
    base_sha: str = BASELINE_SHA,
    tr011_patch: Path | None = None,
    tr012_patch: Path | None = None,
) -> PreparedTrees:
    with ExitStack() as stack:
        trees, _ = _build(repo_root, out_dir, base_sha, (tr011_patch, tr012_patch), "selftest", stack)
        return trees


class ForbiddenCache:
    def get(self, key: str) -> str | None:
        raise AssertionError("实验读取了生产译文缓存")

    def put(self, key: str, target: str) -> None:
        raise AssertionError("实验写入了生产译文缓存")

    def purge_older_than(self, hours: int) -> int:
        raise AssertionError("实验清理了生产译文缓存")


def _deny_network(event: str, args: tuple[Any, ...]) -> None:
    if event in {"socket.connect", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo", "socket.gethostbyname"}:
        raise AssertionError("selftest 禁止 socket 联网")


# 离线词表只作用于内存传输，从实际发送的抽取提示中寻找词项。
_FAKE_PAIRS = (
    ("makespan", "最大完工时间", "completion time", "完工时间"),
    ("lateness", "延误时间", "tardiness", "拖期时间"),
    ("setup time", "准备时间", "processing time", "加工时间"),
    ("zero offset", "零点偏移", "scale factor", "比例因子"),
    ("repeatability", "重复性", "reproducibility", "再现性"),
    ("gauge pressure", "表压", "absolute pressure", "绝对压力"),
    ("sample variance", "样本方差", "standard error", "标准误"),
    ("confidence interval", "置信区间", "prediction interval", "预测区间"),
    ("queue depth", "队列深度", "queue capacity", "队列容量"),
    ("virtual page", "虚拟页", "page frame", "页框"),
    ("request latency", "请求延迟", "service throughput", "服务吞吐量"),
)


def _fake_extract(user: str) -> str:
    distinct = "选用能体现差别的通行译名" in user
    context = user.partition("\n标题：")[2].casefold()
    terms = []
    for a, first, b, second in _FAKE_PAIRS:
        for term, target in ((a, first if distinct else second), (b, second)):
            if term in context:
                terms.append(f"{term}={target}")
    return "\n".join(["领域：合成实验", "AI公共词库：启用", *terms])


_RELATIONS = (
    (r"binomial distribution", "服从二项分布"),
    (r"uniform(?:ly distributed| distribution)", "服从均匀分布"),
    (r"normal distribution", "服从正态分布"),
    (r"(?:proportional|in proportion) to", "与所列数量成正比"),
    (r"at least|no (?:less|smaller) than|bounded below", "至少达到所列下界"),
    (r"no greater|at most", "至多达到所列上界"),
    (r"exceed|strictly (?:greater|later)", "超过对应阈值"),
)
_FAKE_ANCHORS = re.compile(
    r"<m\d+/>|<([bi])>.*?</\1>|<(?:sup|sub)>.*?</(?:sup|sub)>|\[[\d,\s–-]+\]|\d+(?:\.\d+)?\s*%|Transformer \(big\)"
)


def _fake_translate(system: str, source: str) -> str:
    plain = re.sub(r"<(m\d+)>.*?</\1>", r"<\1/>", source, flags=re.S)
    terms = [
        target
        for term, target in re.findall(r"^([^=\n]+)=([^=\n]+)$", system, re.M)
        if term.casefold() in plain.casefold()
    ]
    anchors = [re.sub(r"(<[bi]>).*?(</[bi]>)", r"\1标记\2", m[0]) for m in _FAKE_ANCHORS.finditer(plain)]
    text = "实验记录了相应条件下的观测结果"
    if "<m" in plain and "式外明示" in system:
        for pattern, relation in _RELATIONS:
            if re.search(pattern, plain, re.I):
                text += "，观测量" + relation
                break
    if len(re.split(r"\.\s+(?=[A-Z])", plain)) == 2 and re.search(
        r"\b(although|though|even if|provided|unless|assuming|only if|were|should|supposing|whichever|had|if)\b",
        plain,
        re.I,
    ):
        text = "虽然实验条件发生变化" + ("，" if "可调整标点接回原主句" in system else "。") + "观测结果仍然成立"
    return text + ("，" + "与".join(dict.fromkeys(terms)) if terms else "") + "".join(anchors) + "。"


class _SyntheticEndpoint:
    def __init__(self, scope: str):
        self.scope = scope
        self.calls = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        system, user = (m["content"] for m in body["messages"])
        self.calls += 1
        request_id = "offline-" + sha256_text(f"{self.scope}/{self.calls}/{user}")[:24]
        reason = "stop"
        if system == "你是学术术语专家。":
            output = _fake_extract(user)
        else:
            source = user.partition("\n1|")[2] if "\n1|" in user else user.removeprefix("1|")
            if "[selftest:compat]" in source and "temperature" in body:
                error = {"message": "unsupported temperature", "param": "temperature"}
                return httpx2.Response(400, json={"error": error}, headers={"x-request-id": request_id})
            target = _fake_translate(system, source)
            if "[selftest:missing]" in source and "上次翻译未通过校验" not in user:
                target = re.sub(r"<m\d+/>", "", target)
            if "[selftest:failed]" in source:
                target = ""
            if "[selftest:refused]" in source:
                reason = "content_filter"
            if "[selftest:truncated]" in source:
                reason = "length"
            output = f"1|{target}\n</translations>"
        data = {
            "id": "completion-" + request_id,
            "model": "synthetic-model",
            "choices": [{"index": 0, "finish_reason": reason, "message": {"role": "assistant", "content": output}}],
            "usage": {"prompt_tokens": 64, "completion_tokens": 16, "prompt_cache_hit_tokens": 8},
        }
        return httpx2.Response(200, json=data, headers={"x-request-id": request_id})


@dataclass
class _Call:
    system_hash: str
    user_hash: str
    response_hash: str = ""
    request_id: str = ""
    model: str = ""
    finish_reason: str = ""


class _Transport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, headers: Mapping[str, str]):
        self.inner = inner
        self.headers = headers
        self.calls: list[_Call] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        messages = json.loads(await request.aread())["messages"]
        call = _Call(*(sha256_text(m["content"]) for m in messages))
        self.calls.append(call)
        request.headers.update(self.headers)
        response = await self.inner.handle_async_request(request)
        try:
            raw = await response.aread()
            call.response_hash = hashlib.sha256(raw).hexdigest()
            call.request_id = response.headers.get("x-request-id", "") or response.headers.get("request-id", "")
            if response.is_success:
                data = json.loads(raw)
                call.model = data.get("model") or ""
                call.request_id = call.request_id or data.get("id") or ""
                choice = data["choices"][0]
                call.finish_reason = "refusal" if choice["message"].get("refusal") else choice.get("finish_reason", "")
        finally:
            await response.aclose()
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


class _ObservedChat:
    def __init__(
        self,
        adapter: OpenAICompatChat,
        transport: _Transport,
        mode: RunMode,
        prices: dict[str, Price],
        allowed_models: Sequence[str],
    ):
        self.adapter = adapter
        self.model = adapter.model
        self.provider_id = adapter.provider_id
        self.transport = transport
        self.mode: RunMode = mode
        self.prices = prices
        self.allowed_models = allowed_models
        self.reset()

    def reset(self) -> None:
        from pdftr.domain.llm import ChatUsage

        self.transport.calls.clear()
        self.compatibility_fallback = False
        self.usage = ChatUsage()
        self.cost: float | None = None
        self.started = time.monotonic()

    async def complete(self, system: str, user: str, opts: ChatOptions) -> ChatResult:
        from pdftr.domain.llm import ChatError
        from pdftr.translation.cost import cost_cny

        result = await self.adapter.complete(system, user, opts)
        model = self.transport.calls[-1].model
        self.usage += result.usage
        amount = cost_cny(model, result.usage, peak=False, prices=self.prices)
        if amount is not None:
            self.cost = (self.cost or 0.0) + amount
        self.compatibility_fallback |= result.compatibility_fallback
        if self.allowed_models and model not in self.allowed_models:
            raise ChatError("服务端回显模型不在端点配置允许的范围内")
        return result.model_copy(update={"actual_model": model})

    async def list_models(self) -> list[str]:
        return await self.adapter.list_models()

    def metrics(self) -> dict[str, Any]:
        calls = self.transport.calls
        return {
            "input_tokens": self.usage.prompt_tokens,
            "cache_hit_tokens": self.usage.cache_hit_tokens,
            "output_tokens": self.usage.completion_tokens,
            "cost_cny": self.cost,
            "elapsed_s": 0.01 * len(calls) if self.mode == "selftest" else time.monotonic() - self.started,
            "actual_model": calls[-1].model if calls else "",
            "request_id": calls[-1].request_id if calls else "",
        }


def _chat(mode: RunMode, task: dict[str, Any], doc: CaseDoc) -> _ObservedChat:
    from pdftr.domain.url_guard import build_safe_async_transport
    from pdftr.translation.cost import Price
    from pdftr.translation.openai_compat import OpenAICompatChat

    prices = {
        name: Price(
            cache_hit=p["cache_hit_cny_per_m"], cache_miss=p["cache_miss_cny_per_m"], output=p["output_cny_per_m"]
        )
        for name, p in task["pricing"].items()
    }
    if mode == "selftest":
        scope = canonical_json([doc.doc_id, task["round_idx"], task["group"], task["op"]])
        wire = _Transport(httpx2.MockTransport(_SyntheticEndpoint(scope)), {})
        url, key, model = "https://synthetic.example/v1", "offline", "synthetic-model"
        allowed_models = [model]
        allow_private = False
        prices.setdefault(model, Price(cache_hit=0, cache_miss=1, output=1))
    else:
        endpoint = EndpointConfig.model_validate(task["endpoint"])
        if endpoint.protocol != "openai_compat":
            raise ValueError("在线生成要求 openai_compat 端点")
        url, key, model = endpoint.base_url, os.environ[endpoint.key_env], endpoint.model
        allowed_models = endpoint.allowed_model_ids
        allow_private = endpoint.allow_private
        wire = _Transport(build_safe_async_transport(allow_private=endpoint.allow_private), endpoint.extra_headers)
    adapter = OpenAICompatChat(
        base_url=url, api_key=key, model=model, transport=wire, default_max_retries=0, allow_private=allow_private
    )
    return _ObservedChat(adapter, wire, mode, prices, allowed_models)


def _segments(doc: CaseDoc) -> list[Segment]:
    from pdftr.domain.ir import BlockType, Segment

    return [
        Segment(
            id=s.segment_id,
            job_id=doc.doc_id,
            block_ids=[f"p0-b{s.order}"],
            role=BlockType(s.role),
            section=s.section,
            source=s.source,
            formulas=s.formulas,
        )
        for s in doc.segments
    ]


async def _extract(
    doc: CaseDoc,
    segments: list[Segment],
    chat: _ObservedChat,
    group: Literal["A", "B"],
    round_idx: int,
) -> ExtractionSnapshot:
    from pdftr.domain.ir import Document
    from pdftr.glossary import extract
    from pdftr.glossary.normalize import normalize_terms
    from pdftr.pipeline.runner import glossary_texts

    body, segment_headings = glossary_texts(Document(job_id=doc.doc_id, pages=[]), segments)
    headings = list(dict.fromkeys([*doc.headings, *segment_headings]))
    candidates = extract.frequent_candidates(body)
    definitions = extract.extract_acronym_definitions(body, candidates)
    intro = next((s.source for s in segments if len(s.source.split()) >= 30 and s.source != doc.abstract), "")
    found = await extract.extract_terms(
        chat,
        title=doc.title,
        abstract=doc.abstract,
        intro=intro,
        headings=headings,
        candidates=candidates,
        definitions=definitions,
        user_terms=doc.user_terms,
    )
    call = chat.transport.calls[-1]
    return ExtractionSnapshot(
        doc_id=doc.doc_id,
        source_group=group,
        round_idx=round_idx,
        domain=found.domain,
        use_library=found.use_library,
        raw_terms=list(found.terms.items()),
        normalized_terms=list(normalize_terms(found.terms, body, headings).items()),
        raw_response_sha256=call.response_hash,
        prompt_sha256=call.user_hash,
        **chat.metrics(),
    )


async def _translate(
    doc: CaseDoc,
    segments: list[Segment],
    chat: _ObservedChat,
    task: dict[str, Any],
) -> list[GenerationRecord]:
    from pdftr.domain.llm import ChatError
    from pdftr.glossary.extract import Extraction
    from pdftr.glossary.matcher import TermMatcher, TermSelection
    from pdftr.translation import prompt, service

    snapshot = ExtractionSnapshot.model_validate_json(task["snapshot"])
    group = task["group"]
    if (snapshot.doc_id, snapshot.round_idx, snapshot.source_group) != (
        doc.doc_id,
        task["round_idx"],
        "A" if group == "A" else "B",
    ):
        raise ValueError("翻译引用了其他文档、轮次或组的抽取快照")
    found = Extraction(domain=snapshot.domain, use_library=snapshot.use_library, terms=dict(snapshot.normalized_terms))
    selection = TermSelection(found, doc.user_terms, lambda: TermMatcher(doc.library_terms))
    ctx = prompt.DocContext(title=doc.title, abstract=doc.abstract, domain=found.domain, terms=selection.paper)
    opts = service.TranslateOptions(use_cache=False, concurrency=1, quality="default", price_overrides=chat.prices)
    records = []
    for case, seg in zip(doc.segments, segments, strict=True):
        if not case.scored:
            continue
        chat.reset()
        target, reason = "", ""
        status: GenerationStatus = "failed"
        try:
            # 单段计费和失败归因；全篇相邻上下文仍由生产服务选择。
            (translated,), report = await service.translate(
                [seg],
                chat=chat,
                cache=ForbiddenCache(),
                ctx=ctx,
                opts=opts,
                lookup_term_hits=selection.match_segment,
                context_segments=segments,
            )
            target = translated.target or ""
            reason = translated.failure_reason
            if report.translated:
                status = "done"
            elif "模型拒绝翻译" in reason:
                status = "refused"
            elif "模型输出截断" in reason:
                status = "truncated"
        except ChatError as exc:
            reason = str(exc)
        calls = chat.transport.calls
        if status == "failed" and calls:
            if calls[-1].finish_reason in ("length", "max_tokens"):
                status = "truncated"
            elif calls[-1].finish_reason in ("refusal", "content_filter"):
                status = "refused"
        records.append(
            GenerationRecord(
                run_id=task["run_id"],
                mode=chat.mode,
                group=group,
                round_idx=task["round_idx"],
                doc_id=doc.doc_id,
                segment_id=seg.id,
                category=case.category,
                source=seg.source,
                formulas=seg.formulas,
                expanded_source=prompt.expand_formulas(seg.source, seg.formulas),
                target=target,
                status=status,
                failure_reason=reason,
                attempts=len(calls),
                snapshot_hash=snapshot.ordered_hash,
                system_prompt_sha256=calls[-1].system_hash if calls else sha256_text(""),
                user_prompt_sha256=calls[-1].user_hash if calls else sha256_text(""),
                compatibility_fallback=chat.compatibility_fallback,
                **chat.metrics(),
            )
        )
    return records


async def _execute(mode: RunMode, task: dict[str, Any]) -> list[dict[str, Any]]:
    doc = CaseDoc.model_validate(task["doc"])
    segments = _segments(doc)
    chat = _chat(mode, task, doc)
    try:
        if task["op"] == "extract" and task["group"] in ("A", "B"):
            snapshot = await _extract(doc, segments, chat, task["group"], task["round_idx"])
            return [snapshot.model_dump(mode="json")]
        if task["op"] == "translate":
            records = await _translate(doc, segments, chat, task)
            return [record.model_dump(mode="json") for record in records]
        raise ValueError("无效的工作阶段或抽取组")
    finally:
        await chat.adapter.aclose()


def _worker_main(tree: Path, mode: RunMode) -> None:
    sys.path.insert(0, str(tree / "backend/src"))
    with asyncio.Runner() as runner:
        # Windows 事件循环先创建本地唤醒用 socketpair，再封锁工作进程的 DNS 与 socket 出站调用。
        runner.get_loop()
        if mode == "selftest":
            sys.addaudithook(_deny_network)
        print(canonical_json(_fingerprints(tree)), flush=True)
        for line in sys.stdin.buffer:
            try:
                result = runner.run(_execute(mode, json.loads(line)))
            except Exception as exc:
                if mode == "selftest":
                    raise
                # 在线 SDK 的嵌套异常可能含服务端回显的凭据，不能打印原始 traceback。
                from pdftr.domain.llm import ChatError

                detail = str(exc) if isinstance(exc, ChatError) else type(exc).__name__
                raise SystemExit(f"在线生成单元未完成：{detail}") from None
            print(canonical_json(result), flush=True)


def _atomic(path: Path, data: bytes) -> None:
    with ExitStack() as cleanup:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            tmp = Path(file.name)
            cleanup.callback(tmp.unlink, missing_ok=True)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        tmp.replace(path)


class _Checkpoints:
    """每单元的一次原子提交同时保存输入输出摘要。断电后可从它重建两个契约 JSONL。"""

    def __init__(self, private: Path, identity: str):
        self.root = private / "generation-checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self.root / "input.sha256"
        if manifest.exists() and manifest.read_text("ascii") != identity:
            raise ValueError("断点输入摘要不匹配，请为新实验使用独立输出目录")
        if not manifest.exists():
            if any((private / name).exists() for name in ("snapshots.jsonl", "generations.jsonl")):
                raise ValueError("已有生成记录缺少断点输入摘要")
            _atomic(manifest, identity.encode())
        self.views = {"extract": private / "snapshots.jsonl", "translate": private / "generations.jsonl"}
        self.contents = {stage: bytearray() for stage in self.views}

    def run(self, proc: subprocess.Popen[bytes], task: dict[str, Any]) -> list[str]:
        stage = task["op"]
        key = [stage, task["doc"]["doc_id"], task["round_idx"], task["group"]]
        path = self.root / (sha256_canonical(key) + ".json")
        digest = sha256_canonical(task)
        completed = path.exists()
        if not completed:
            rows = [canonical_json(row) for row in _request(proc, task)]
            saved = {"input": digest, "output": sha256_canonical(rows), "rows": rows}
            _atomic(path, canonical_json(saved).encode())
        # 新跑与续跑均从同一次原子提交回读，B 和 C 收到完全相同的序列化快照。
        saved = json.loads(path.read_bytes())
        rows = saved["rows"]
        if saved["input"] != digest or saved["output"] != sha256_canonical(rows):
            raise ValueError("断点单元摘要不匹配")
        self.contents[stage].extend("".join(row + "\n" for row in rows).encode())
        if not completed:
            self.publish(stage)
        return rows

    def publish(self, stage: str) -> None:
        path, data = self.views[stage], self.contents[stage]
        if not path.exists() or path.read_bytes() != data:
            _atomic(path, bytes(data))


@contextmanager
def _writer(private: Path) -> Iterator[None]:
    # 操作系统在进程退出时释放锁，崩溃后可直接续跑。
    private.mkdir(parents=True, exist_ok=True)
    with (private / "generation.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def run_generation(
    *,
    run_id: str,
    mode: RunMode,
    repo_root: Path,
    out_dir: Path,
    docs: Sequence[CaseDoc],
    rounds: int,
    policy: PolicyConfig,
    endpoint: EndpointConfig | None = None,
    pricing: Mapping[str, PriceEntry] | None = None,
) -> tuple[PreparedTrees, list[ExtractionSnapshot], list[GenerationRecord]]:
    if mode not in ("selftest", "live") or rounds < 1 or not run_id.strip() or not docs:
        raise ValueError("生成模式、标识、轮数或语料无效")
    if len({doc.doc_id for doc in docs}) != len(docs):
        raise ValueError("文档标识重复")
    if mode == "live" and (endpoint is None or endpoint.protocol != "openai_compat"):
        raise ValueError("在线生成要求 openai_compat 端点")
    private = out_dir.resolve() / "private"
    if private.is_relative_to(repo_root.resolve()):
        raise ValueError("实验输出目录必须位于仓库外")
    with _writer(private), ExitStack() as stack:
        trees, workers = _build(repo_root, out_dir, policy.baseline_sha, (None, None), mode, stack)
        prices = policy.default_pricing if pricing is None else pricing
        common = {
            "run_id": run_id,
            "mode": mode,
            "endpoint": endpoint.model_dump(mode="json") if mode == "live" and endpoint else None,
            "pricing": {k: v.model_dump() for k, v in prices.items()},
        }
        inputs = []
        for doc in docs:
            data = doc.model_dump(
                mode="json",
                exclude={
                    "anchors": True,
                    "term_pairs": True,
                    "has_reference": True,
                    "segments": {"__all__": {"reference_zh", "prev_context_note"}},
                },
            )
            inputs.append(data)
        identity = sha256_canonical(
            {
                **common,
                "base": trees.base_sha,
                "patches": trees.patch_hashes,
                "prompts": trees.prompt_hashes,
                "generator": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "docs": inputs,
                "rounds": rounds,
            }
        )
        checkpoints = _Checkpoints(private, identity)
        snapshots, generations = [], []
        for rnd in range(rounds):
            for index, doc_input in enumerate(inputs):
                doc_task = {**common, "doc": doc_input, "round_idx": rnd}
                snapshot_lines = {}
                for group in ("A", "B"):
                    task = {**doc_task, "op": "extract", "group": group}
                    (row,) = checkpoints.run(workers[group], task)
                    snapshots.append(ExtractionSnapshot.model_validate_json(row))
                    snapshot_lines[group] = row
                shift = (index + rnd) % len(ALL_GROUPS)
                for group in ALL_GROUPS[shift:] + ALL_GROUPS[:shift]:
                    snapshot = snapshot_lines["A" if group == "A" else "B"]
                    task = {**doc_task, "op": "translate", "group": group, "snapshot": snapshot}
                    rows = checkpoints.run(workers[group], task)
                    generations.extend(GenerationRecord.model_validate_json(row) for row in rows)
        for stage in checkpoints.views:
            checkpoints.publish(stage)
        return trees, snapshots, generations


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--worker" or sys.argv[3] not in ("selftest", "live"):
        raise SystemExit("用法：generate --worker TREE {selftest,live}")
    _worker_main(Path(sys.argv[2]).resolve(strict=True), sys.argv[3])

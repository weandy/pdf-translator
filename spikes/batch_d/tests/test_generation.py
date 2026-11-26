"""三棵源码树、真实适配器传输及按组断点的离线回归。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx2
from spikes.batch_d import generate
from spikes.batch_d.contract import (
    ALL_GROUPS,
    BASELINE_EXTRACT_PROMPT_SHA256,
    BASELINE_RULES_SHA256,
    BASELINE_SHA,
    CaseDoc,
    CaseSegment,
    EndpointConfig,
    GenerationRecord,
    canonical_json,
    load_policy,
    sha256_text,
)
from spikes.batch_d.generate import ForbiddenCache, prepare_trees, run_generation

_REPO = Path(__file__).resolve().parents[3]
_SPIKE = _REPO / "spikes/batch_d"


def _docs() -> list[CaseDoc]:
    lines = (_SPIKE / "fixtures/dev.jsonl").read_bytes().splitlines()
    return [CaseDoc.model_validate_json(row) for row in lines[:2]]


class TreesTest(unittest.TestCase):
    def test_archived_bytes_real_imports_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(generate, "_git", wraps=generate._git) as git:
                prepared = prepare_trees(_REPO, Path(tmp))
            archive = ["-c", "core.autocrlf=false", "archive", "--format=tar", BASELINE_SHA, "backend/src/pdftr"]
            self.assertTrue(any(c.args[1] == archive for c in git.call_args_list))
            applications = [c.args for c in git.call_args_list if "--whitespace=error" in c.args[1]]
            self.assertEqual(len(applications), 6)
            for check, apply in zip(applications[::2], applications[1::2], strict=True):
                self.assertEqual(check, (apply[0], ["apply", "--check", "--whitespace=error", "-"], apply[2]))
            self.assertEqual(prepared.base_sha, BASELINE_SHA)
            self.assertEqual(
                prepared.patch_hashes,
                {n: hashlib.sha256((_SPIKE / n).read_bytes()).hexdigest() for n in ("tr011.patch", "tr012.patch")},
            )
            a, b, c = (prepared.prompt_hashes[g] for g in ALL_GROUPS)
            self.assertEqual(
                a,
                {
                    "prompt_version": "v7",
                    "extract_prompt_sha256": BASELINE_EXTRACT_PROMPT_SHA256,
                    "rules_sha256": BASELINE_RULES_SHA256,
                },
            )
            self.assertNotEqual(b["extract_prompt_sha256"], a["extract_prompt_sha256"])
            self.assertEqual(b, a | {"extract_prompt_sha256": c["extract_prompt_sha256"]})
            self.assertEqual(c["prompt_version"], "v8")
            self.assertNotEqual(c["rules_sha256"], b["rules_sha256"])
            rel = "backend/src/pdftr/translation/prompt.py"
            self.assertEqual(
                (prepared.trees["A"] / rel).read_bytes(), generate._git(_REPO, ["show", f"{BASELINE_SHA}:{rel}"])
            )
            code = (
                "import sys; from pathlib import Path; __package__='spikes.batch_d'; "
                "from .generate import _fingerprints; "
                "tree=Path(sys.argv[1]); sys.path.insert(0,str(tree/'backend/src')); _fingerprints(tree); "
                "print('\\n'.join(sys.modules[n].__file__ for n in "
                "('pdftr','pdftr.glossary.extract','pdftr.translation.prompt','pdftr.translation.service')))"
            )
            for group, tree in prepared.trees.items():
                self.assertEqual(tree, Path(tmp).resolve() / f"tree_{group}")
                output = subprocess.check_output([sys.executable, "-B", "-c", code, tree], cwd=_REPO, timeout=30)
                origins = output.decode().splitlines()
                self.assertEqual(len(origins), 4)
                self.assertTrue(all(Path(origin).resolve().is_relative_to(tree) for origin in origins))
            with self.assertRaisesRegex(RuntimeError, "隔离导入失败"):
                generate._fingerprints(prepared.trees["B"])

    def test_patch_scope_line_endings_and_apply_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            injected = root / "candidate.patch"
            one, two = ((_SPIKE / n).read_bytes() for n in ("tr011.patch", "tr012.patch"))
            for first, second, raw, reason in [
                (injected, None, one + two, "白名单"),
                (None, injected, one, "白名单"),
                (injected, None, one.replace(b"\n", b"\r\n"), "LF"),
            ]:
                with self.subTest(reason=reason, first=first):
                    injected.write_bytes(raw)
                    with self.assertRaisesRegex(ValueError, reason):
                        prepare_trees(_REPO, root, tr011_patch=first, tr012_patch=second)
            injected.write_bytes(one.replace("使用中文学界通行译法".encode(), b"missing-context"))
            with self.assertRaises(subprocess.CalledProcessError):
                prepare_trees(_REPO, root, tr011_patch=injected)
            self.assertFalse((root / "tree_B").exists())
            with self.assertRaisesRegex(ValueError, "仓库外"):
                prepare_trees(_REPO, _REPO)

    def test_offline_socket_guard_and_cache_traps(self) -> None:
        code = """
import socket, sys
__package__ = 'spikes.batch_d'
from .generate import _deny_network
sys.addaudithook(_deny_network)
with socket.socket() as sock:
    calls = (lambda: socket.getaddrinfo('synthetic.invalid', 443), lambda: sock.connect(('127.0.0.1', 1)))
    for call in calls:
        try:
            call()
        except AssertionError as e:
            assert 'selftest' in str(e)
        else:
            raise AssertionError('预期封锁 socket')
"""
        subprocess.run([sys.executable, "-B", "-c", code], cwd=_REPO, check=True, timeout=20)
        cache = ForbiddenCache()
        with self.assertRaises(AssertionError):
            cache.get("probe")
        with self.assertRaises(AssertionError):
            cache.put("probe", "探针")


class GenerationTest(unittest.TestCase):
    def test_offline_responses_preserve_corpus_markup(self) -> None:
        from pdftr.translation import prompt, service

        for split in ("dev", "holdout"):
            for row in (_SPIKE / f"fixtures/{split}.jsonl").read_bytes().splitlines():
                for seg in CaseDoc.model_validate_json(row).segments:
                    source = prompt.expand_formulas(seg.source, seg.formulas)
                    target = generate._fake_translate(prompt.RULES, source)
                    with self.subTest(segment=seg.segment_id):
                        self.assertEqual(service._target_problems(seg.source, target, formulas=seg.formulas), [])

    def test_document_context_and_rendered_request_hashes(self) -> None:
        words = " ".join(["sample"] * 29)
        abstract, intro = words + " abstract.", words + " introduction."
        doc = CaseDoc(
            doc_id="synthetic-context",
            split="dev",
            discipline="computer_systems",
            template_family="context",
            title="Synthetic context",
            abstract=abstract,
            headings=["Outline"],
            user_terms={"green tray": "青盘"},
            library_terms={"storage slot": "存储槽"},
            segments=[
                CaseSegment(segment_id="heading", order=0, role="title", source="Test heading", scored=False),
                CaseSegment(segment_id="abstract", order=1, source=abstract, scored=False),
                CaseSegment(segment_id="short", order=2, source=words, scored=False),
                CaseSegment(segment_id="intro", order=3, source=intro, section="Scope", scored=False),
                CaseSegment(
                    segment_id="result",
                    order=4,
                    source="This green tray occupies the storage slot.",
                    section="Scope",
                    reference_zh="评审专用参考文本",
                ),
            ],
        )
        sent = []
        reply = generate._SyntheticEndpoint.__call__

        def capture(server, request):
            sent.append([m["content"] for m in json.loads(request.content)["messages"]])
            return reply(server, request)

        async def run():
            task = {
                "doc": doc.model_dump(),
                "op": "extract",
                "group": "A",
                "run_id": "context",
                "round_idx": 0,
                "pricing": {},
            }
            with patch.object(generate._SyntheticEndpoint, "__call__", capture):
                (snapshot,) = await generate._execute("selftest", task)
                self.assertEqual(snapshot["prompt_sha256"], sha256_text(sent[-1][1]))
                self.assertIn("正文开头：" + intro + "\n", sent[-1][1])
                self.assertIn("Outline; Test heading", sent[-1][1])
                task.update(op="translate", snapshot=canonical_json(snapshot))
                (record,) = await generate._execute("selftest", task)
                self.assertEqual(record["segment_id"], "result")
                system, user = sent[-1]
                self.assertIn("论文标题：" + doc.title, system)
                self.assertIn("摘要：" + doc.abstract, system)
                self.assertIn(intro, user)
                self.assertIn("green tray=青盘", user)
                self.assertIn("storage slot=存储槽", user)
                self.assertEqual(record["system_prompt_sha256"], sha256_text(system))
                self.assertEqual(record["user_prompt_sha256"], sha256_text(user))
                self.assertNotIn("评审专用参考文本", canonical_json(sent))
                self.assertEqual(await generate._execute("selftest", task), [record])

        asyncio.run(run())

    def test_rotations_snapshot_bytes_and_crash_resume(self) -> None:
        docs, rounds = _docs(), 2
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            run = partial(
                run_generation,
                mode="selftest",
                run_id="offline-rotation",
                repo_root=_REPO,
                out_dir=out,
                docs=docs,
                rounds=rounds,
                policy=load_policy(),
            )
            actual = generate._request
            sent = []

            def record(proc, task):
                if len(sent) == 3:
                    raise InterruptedError
                sent.append(task)
                return actual(proc, task)

            with patch.object(generate, "_request", side_effect=record), self.assertRaises(InterruptedError):
                run()
            private = out / "private"
            self.assertEqual(len((private / "snapshots.jsonl").read_bytes().splitlines()), 2)
            self.assertEqual(len((private / "generations.jsonl").read_bytes().splitlines()), 4)
            with patch.object(generate, "_request", wraps=actual) as rpc:
                _, snapshots, records = run()
                sent.extend(c.args[1] for c in rpc.call_args_list)
            self.assertEqual(len(sent), rounds * len(docs) * 5)
            self.assertEqual(len(snapshots), 8)
            self.assertEqual(len(records), 48)
            self.assertTrue(
                all(r.status == "done" and r.attempts == 1 for r in records), [r for r in records if r.status != "done"]
            )
            self.assertEqual({s.source_group for s in snapshots}, {"A", "B"})
            self.assertTrue(
                all(r.actual_model == "synthetic-model" and r.request_id.startswith("offline-") for r in records)
            )
            self.assertTrue(
                all(r.input_tokens == 64 and r.output_tokens == 16 and r.cache_hit_tokens == 8 for r in records)
            )
            orders = []
            for rnd in range(rounds):
                for index, doc in enumerate(docs):
                    tasks = [t for t in sent if t["doc"]["doc_id"] == doc.doc_id and t["round_idx"] == rnd]
                    self.assertEqual([(t["op"], t["group"]) for t in tasks[:2]], [("extract", "A"), ("extract", "B")])
                    shift = (index + rnd) % 3
                    order = [t["group"] for t in tasks[2:]]
                    self.assertEqual(tuple(order), ALL_GROUPS[shift:] + ALL_GROUPS[:shift])
                    orders.append(order)
                    messages = {t["group"]: t["snapshot"].encode() for t in tasks[2:]}
                    self.assertEqual(messages["B"], messages["C"])
                    self.assertIn(messages["C"], (private / "snapshots.jsonl").read_bytes().splitlines())
                    self.assertNotIn("reference_zh", canonical_json(tasks))
                    self.assertNotIn("anchors", canonical_json(tasks))
                    for rec in (r for r in records if (r.round_idx, r.doc_id) == (rnd, doc.doc_id)):
                        self.assertEqual(rec.snapshot_hash, json.loads(messages[rec.group])["ordered_hash"])
            self.assertTrue(any(order.index("C") < order.index("B") for order in orders))
            grouped = {
                g: [r for r in records if (r.group, r.round_idx, r.doc_id) == (g, 0, docs[0].doc_id)]
                for g in ALL_GROUPS
            }
            self.assertEqual(len({grouped[g][0].system_prompt_sha256 for g in ALL_GROUPS}), 3)
            self.assertNotEqual(grouped["A"][0].target, grouped["B"][0].target)
            for i in (1, 2):
                self.assertNotEqual(grouped["B"][i].target, grouped["C"][i].target)
            self.assertEqual(grouped["B"][3].target, grouped["C"][3].target)
            files = [private / name for name in ("snapshots.jsonl", "generations.jsonl")]
            before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in files]
            with patch.object(generate, "_request", side_effect=AssertionError("完成单元被重跑")):
                _, resumed, replay = run()
            self.assertEqual((resumed, replay), (snapshots, records))
            self.assertEqual(before, [(p.stat().st_mtime_ns, p.read_bytes()) for p in files])
            docs[0].abstract += " A new condition."
            with self.assertRaisesRegex(ValueError, "输入摘要"):
                run()

    def test_refusal_truncation_missing_formula_and_compatibility_retry(self) -> None:
        doc = _docs()[0]
        doc.has_reference = False
        doc.anchors = []
        doc.segments = [
            CaseSegment(
                segment_id=tag, order=i, source=f"Measure the level <m1/> [selftest:{tag}].", formulas={"m1": "x"}
            )
            for i, tag in enumerate(("missing", "refused", "truncated", "failed", "compat"))
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _, _, records = run_generation(
                run_id="offline-retries",
                mode="selftest",
                repo_root=_REPO,
                out_dir=out,
                docs=[doc],
                rounds=1,
                policy=load_policy(),
            )
            self.assertEqual(
                records,
                [
                    GenerationRecord.model_validate_json(row)
                    for row in (out / "private/generations.jsonl").read_bytes().splitlines()
                ],
            )
            self.assertEqual(len(records), 15)
            for group in ALL_GROUPS:
                rows = {r.segment_id: r for r in records if r.group == group}
                for key, status, attempts in [
                    ("missing", "done", 2),
                    ("refused", "refused", 1),
                    ("truncated", "truncated", 10),
                    ("failed", "failed", 10),
                    ("compat", "done", 2),
                ]:
                    row = rows[key]
                    self.assertEqual((row.status, row.attempts), (status, attempts))
                    self.assertEqual(row.compatibility_fallback, key == "compat")
                    self.assertEqual(bool(row.failure_reason), status != "done")
                self.assertIn("<m1/>", rows["missing"].target)
                self.assertEqual(rows["missing"].input_tokens, 128)
                self.assertEqual(rows["compat"].input_tokens, 64)
                self.assertIn("模型拒绝翻译", rows["refused"].failure_reason)
                self.assertIn("截断", rows["truncated"].failure_reason)

    def test_live_adapter_observes_wire_and_passes_allow_private_without_network(self) -> None:
        from pdftr.domain.llm import ChatOptions

        doc = _docs()[0]
        endpoint = EndpointConfig(
            protocol="openai_compat",
            base_url="http://127.0.0.1:31001/v1",
            model="alias",
            model_family="synthetic",
            key_env="BATCH_D_TEST_KEY",
            extra_headers={"X-Run": "probe"},
            allow_private=True,
        )
        task = {
            "pricing": {"served": {"cache_hit_cny_per_m": 1, "cache_miss_cny_per_m": 2, "output_cny_per_m": 3}},
            "endpoint": endpoint.model_dump(),
        }
        calls = []

        def reply(request: httpx2.Request) -> httpx2.Response:
            self.assertEqual(request.headers["Authorization"], "Bearer offline-probe")
            self.assertEqual(request.headers["X-Run"], "probe")
            calls.append(json.loads(request.content))
            response = {
                "id": "completion-id",
                "model": "served",
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "合成"}}],
            }
            return httpx2.Response(200, headers={"x-request-id": "wire-id"}, json=response)

        async def probe():
            with (
                patch.dict(os.environ, {endpoint.key_env: "offline-probe"}),
                patch(
                    "pdftr.domain.url_guard.build_safe_async_transport", return_value=httpx2.MockTransport(reply)
                ) as build_transport,
            ):
                chat = generate._chat("live", task, doc)
                try:
                    build_transport.assert_called_once_with(allow_private=True)
                    await chat.complete("系统", "段落", ChatOptions())
                    metrics = chat.metrics()
                    self.assertEqual(metrics["actual_model"], "served")
                    self.assertEqual(metrics["request_id"], "wire-id")
                    self.assertAlmostEqual(metrics["cost_cny"], 26 / 1e6)
                    self.assertEqual(chat.transport.calls[0].user_hash, sha256_text("段落"))
                finally:
                    await chat.adapter.aclose()

        asyncio.run(probe())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "alias")

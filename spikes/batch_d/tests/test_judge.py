"""验证平衡盲包、引用证据、双协议请求与锁票恢复。"""

from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from unittest.mock import Mock, patch

import httpx2
from spikes.batch_d.contract import (
    ALL_CATEGORIES,
    ALL_GROUPS,
    ALL_PERMUTATIONS,
    BlindCandidate,
    CaseDoc,
    CaseSegment,
    EndpointConfig,
    GenerationRecord,
    GroupId,
    JudgeItem,
    JudgePacket,
    PrivateMap,
    ReviewerId,
    RunMode,
    canonical_json,
    sha256_text,
)
from spikes.batch_d.judge import (
    BLIND_JUDGE_SYSTEM_PROMPT,
    build_blind_packets,
    parse_and_validate_item_review,
    review_packet,
    write_blind_artifacts,
)

_ITEM_FIELDS = {"item_id", "source_expanded", "local_context", "user_terms", "reference_zh", "candidates"}


def _data(n: int) -> tuple[list[CaseDoc], list[GenerationRecord]]:
    docs = []
    generations = []
    for j in range(n):
        seg = CaseSegment(
            segment_id="hidden-scored",
            order=1,
            source=f"Observe sample {j} at <m1/>.",
            formulas={"m1": "x=3"},
            category=ALL_CATEGORIES[j % 4],
            reference_zh="观察样本。",
            prev_context_note="The probe is fixed.",
        )
        doc = CaseDoc(
            doc_id=f"hidden-doc-{j}",
            split="holdout",
            discipline="measurement_engineering",
            template_family="hidden-family",
            title="Probe reading",
            abstract="Samples are taken at fixed intervals.",
            segments=[CaseSegment(segment_id="hidden-note", order=0, source="Calibrate probe.", scored=False), seg],
            user_terms={"probe": "探头"},
            library_terms={"hidden-entry": "隐藏"},
            has_reference=True,
        )
        docs.append(doc)
        for group, text in zip(ALL_GROUPS, ("观察样本。", "读取样本。", "检查样本。"), strict=True):
            generations.append(
                GenerationRecord(
                    run_id="run",
                    mode="selftest",
                    round_idx=0,
                    group=group,
                    doc_id=doc.doc_id,
                    segment_id=seg.segment_id,
                    category=seg.category,
                    source=seg.source,
                    expanded_source=seg.source.replace("<m1/>", "<m1>x=3</m1>"),
                    formulas=seg.formulas,
                    target=text,
                    status="done",
                    attempts=1,
                    snapshot_hash="hidden-snapshot",
                    system_prompt_sha256="hidden-system",
                    user_prompt_sha256="hidden-user",
                    actual_model="hidden-model",
                    request_id="hidden-request",
                    elapsed_s=345.67,
                )
            )
    return docs, generations


def _pack(
    docs: list[CaseDoc], rows: list[GenerationRecord], reviewer: ReviewerId = "codex", seed: int = 928471, rnd: int = 0
) -> tuple[JudgePacket, PrivateMap]:
    return build_blind_packets(
        run_id="run", docs=docs, generations=rows, reviewer=reviewer, round_idx=rnd, private_seed=seed
    )


def _endpoint(protocol: Literal["openai_compat", "anthropic"] = "openai_compat") -> EndpointConfig:
    return EndpointConfig(
        protocol=protocol,
        base_url="https://review.invalid/v1" if protocol == "openai_compat" else "https://review.invalid",
        model="route",
        model_family="family",
        key_env="UNUSED_REVIEW_TEST_ENV",
        allowed_model_ids=["echo"],
        extra_headers={"x-test-routing": "local"},
    )


def _answer(payload: str) -> str:
    item = json.loads(payload)
    return canonical_json(
        {"item_id": item["item_id"], "candidates": [{"label": c["label"], "errors": []} for c in item["candidates"]]}
    )


def _fake(endpoint: EndpointConfig, key: str, system: str, user: str) -> tuple[str, str, str]:
    return _answer(user), "echo", "request"


class BlindPacketsTest(unittest.TestCase):
    def test_all_six_positions_in_every_block_and_independent_masks(self):
        docs, rows = _data(96)
        packet, private = _pack(docs, rows)
        self.assertEqual(_pack(docs, list(reversed(rows))), (packet, private))
        self.assertEqual(Counter(m.permutation for m in private.items), dict.fromkeys(ALL_PERMUTATIONS, 16))
        for offset in range(0, 96, 6):
            self.assertEqual({m.permutation for m in private.items[offset : offset + 6]}, set(ALL_PERMUTATIONS))
        translations = {r.group: r.target for r in rows}
        for item, m in zip(packet.items, private.items, strict=True):
            self.assertEqual(item.item_id, m.item_id)
            self.assertEqual([c.text for c in item.candidates], [translations[g] for g in m.permutation])
        for other, other_map in (
            _pack(docs, rows, "neon"),
            _pack(docs, rows, seed=928472),
            _pack(docs, [r.model_copy(update={"round_idx": 1}) for r in rows], rnd=1),
        ):
            self.assertNotEqual([m.doc_id for m in private.items], [m.doc_id for m in other_map.items])
            self.assertTrue({i.item_id for i in packet.items}.isdisjoint(i.item_id for i in other.items))
            self.assertTrue(
                {c.label for i in packet.items for c in i.candidates}.isdisjoint(
                    c.label for i in other.items for c in i.candidates
                )
            )
        self.assertEqual(private.seed_fingerprint, sha256_text("codex:0:928471"))

    def test_exact_duplicates_collapse_in_first_position_and_keep_both_directions(self):
        docs, rows = _data(12)
        for all_same in (False, True):
            for row in rows:
                row.target = "同文" if all_same or row.group != "B" else "同文 "
            packet, private = _pack(docs, rows)
            texts = {row.group: row.target for row in rows}
            for item, mapping in zip(packet.items, private.items, strict=True):
                expected = [texts[g] for g in mapping.permutation]
                self.assertEqual([c.text for c in item.candidates], list(dict.fromkeys(expected)))
                self.assertEqual(len(item.candidates), 1 if all_same else 2)
                for c in item.candidates:
                    groups: list[GroupId] = [g for g in mapping.permutation if texts[g] == c.text]
                    self.assertEqual(mapping.label_to_groups[c.label], groups)
                    self.assertTrue(all(mapping.group_to_label[g] == c.label for g in groups))

    def test_blind_artifact_has_only_public_fields_and_content(self):
        docs, rows = _data(2)
        rows.extend([rows[0].model_copy(update={"round_idx": 1}), rows[0].model_copy(update={"run_id": "other"})])
        packet, private = _pack(docs, rows)
        with TemporaryDirectory() as tmp:
            blind, secret = write_blind_artifacts(Path(tmp), packet, private)
            self.assertEqual(blind.relative_to(tmp).as_posix(), "blind/codex_r0.json")
            self.assertEqual(secret.relative_to(tmp).as_posix(), "private/map_codex_r0.json")
            self.assertEqual(PrivateMap.model_validate_json(secret.read_bytes()), private)
            public = blind.read_text(encoding="utf-8")
            self.assertEqual(JudgePacket.model_validate_json(public), packet)
        data = json.loads(public)
        self.assertEqual(set(data), {"items", "reviewer", "run_id", "round_idx", "protocol_version", "rubric_version"})
        for forbidden in (
            "hidden-",
            "928471",
            "345.67",
            '"A"',
            '"B"',
            '"C"',
            *ALL_CATEGORIES,
            private.seed_fingerprint,
        ):
            self.assertNotIn(forbidden, public)
        self.assertEqual(len(data["items"]), 2)
        for item in data["items"]:
            self.assertEqual(set(item), _ITEM_FIELDS)
            self.assertTrue(all(set(c) == {"label", "text"} for c in item["candidates"]))
            self.assertIn("<m1>x=3</m1>", item["source_expanded"])
            self.assertEqual(item["local_context"], "The probe is fixed.")
            self.assertEqual(item["user_terms"], {"probe": "探头"})
            self.assertEqual(item["reference_zh"], "观察样本。")
        for field in (*ALL_CATEGORIES, "anchor_id", "codex", "neon"):
            self.assertNotIn(field, BLIND_JUDGE_SYSTEM_PROMPT)

    def test_incomplete_duplicate_or_unmatched_generated_sources_rejected(self):
        docs, rows = _data(1)
        for invalid in (
            rows[:-1],
            [*rows, rows[0]],
            [*rows[:2], rows[2].model_copy(update={"status": "failed"})],
            [*rows[:2], rows[2].model_copy(update={"expanded_source": "wrong source"})],
        ):
            with self.subTest(), self.assertRaises(ValueError):
                _pack(docs, invalid)

    def test_same_reviewer_and_round_cannot_pair_artifacts_from_different_seeds(self):
        docs, rows = _data(2)
        packet, _ = _pack(docs, rows)
        _, foreign = _pack(docs, rows, seed=0)
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                write_blind_artifacts(Path(tmp), packet, foreign)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class ParseReviewTest(unittest.TestCase):
    def setUp(self):
        self.item = JudgeItem(
            item_id="t",
            source_expanded="Count three probes.",
            candidates=[
                BlindCandidate(label="k1", text="计数两个探头。"),
                BlindCandidate(label="k2", text="计数三个探头。"),
            ],
        )
        self.error: dict[str, Any] = {
            "category": "accuracy",
            "severity": "major",
            "protected": True,
            "source_span": "three",
            "target_span": "两个",
        }
        self.response: dict[str, Any] = {
            "item_id": "t",
            "candidates": [{"label": "k1", "errors": [self.error]}, {"label": "k2", "errors": [], "score": 100}],
        }

    def test_plain_and_fenced_json_with_trusted_metadata_and_mqm_score(self):
        for raw in (canonical_json(self.response), "```json\n" + canonical_json(self.response) + "\n```"):
            review = parse_and_validate_item_review(self.item, raw, model_echo="echo", request_id="req")
            self.assertEqual([c.score for c in review.candidates], [85, 100])
            self.assertEqual(
                (review.model_echo, review.request_id, review.response_sha256), ("echo", "req", sha256_text(raw))
            )

    def test_mqm_score_computed_with_minor_and_floor(self):
        self.response["candidates"][0]["errors"].append(dict(self.error, severity="minor"))
        self.assertEqual(
            parse_and_validate_item_review(self.item, canonical_json(self.response)).candidates[0].score, 81
        )
        self.response["candidates"][0]["errors"] = [self.error] * 7
        self.assertEqual(
            parse_and_validate_item_review(self.item, canonical_json(self.response)).candidates[0].score, 0
        )

    def test_grounded_omission_accepts_empty_target(self):
        self.error["target_span"] = ""
        review = parse_and_validate_item_review(self.item, canonical_json(self.response))
        self.assertEqual(review.candidates[0].errors[0].source_span, "three")
        self.assertTrue(review.candidates[0].errors[0].protected)

    def test_invalid_json_schema_and_model_supplied_metadata_rejected(self):
        raw = canonical_json(self.response)
        for bad in (
            "{",
            raw + raw,
            "[]",
            raw.replace('"item_id":', '"item_id":"foreign","item_id":'),
            canonical_json(dict(self.response, item_id="wrong")),
            canonical_json(dict(self.response, model_echo="forged")),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_and_validate_item_review(self.item, bad)

    def test_missing_extra_duplicate_labels_and_missing_errors_rejected(self):
        for labels in (["k1"], ["k1", "k2", "k3"], ["k1", "k1"], ["k1", "k3"]):
            self.response["candidates"] = [{"label": label, "errors": []} for label in labels]
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                parse_and_validate_item_review(self.item, canonical_json(self.response))
        self.response["candidates"] = [{"label": "k1"}, {"label": "k2", "errors": []}]
        with self.assertRaises(ValueError):
            parse_and_validate_item_review(self.item, canonical_json(self.response))

    def test_forged_spans_enums_and_bad_scores_rejected(self):
        for change in (
            {"source_span": "fabricated"},
            {"target_span": "三个"},
            {"source_span": "absent", "target_span": ""},
            {"source_span": "", "target_span": ""},
            {"category": "other"},
            {"severity": "critical"},
            {"protected": "yes"},
        ):
            self.response["candidates"][0]["errors"] = [dict(self.error, **change)]
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_and_validate_item_review(self.item, canonical_json(self.response))
        self.response["candidates"][0]["errors"] = [self.error]
        for score in (96, "85", True):
            self.response["candidates"][0]["score"] = score
            with self.subTest(score=score), self.assertRaises(ValueError):
                parse_and_validate_item_review(self.item, canonical_json(self.response))


class BallotTest(unittest.TestCase):
    def setUp(self):
        self.packet, _ = _pack(*_data(2))
        self.endpoint = _endpoint()
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name) / "ballot.jsonl"

    def test_sdk_http_requests_and_response_headers_for_both_protocols(self):
        for protocol in ("openai_compat", "anthropic"):
            endpoint = _endpoint(protocol)
            path = self.out.with_name(protocol + ".jsonl")

            def handler(request: httpx2.Request, endpoint: EndpointConfig = endpoint) -> httpx2.Response:
                payload = json.loads(request.content)
                self.assertEqual(request.headers["x-test-routing"], "local")
                self.assertEqual(payload["model"], "route")
                if endpoint.protocol == "openai_compat":
                    self.assertEqual(request.url.path, "/v1/chat/completions")
                    self.assertEqual(payload["messages"][0], {"role": "system", "content": BLIND_JUDGE_SYSTEM_PROMPT})
                    user = payload["messages"][1]["content"]
                    response = {
                        "id": "completion",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "echo",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": _answer(user)},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    headers = {"x-request-id": "openai-request"}
                else:
                    self.assertEqual(request.url.path, "/v1/messages")
                    self.assertEqual(request.headers["x-api-key"], "local-test")
                    self.assertEqual(payload["system"], BLIND_JUDGE_SYSTEM_PROMPT)
                    user = payload["messages"][0]["content"]
                    answer = _answer(user)
                    response = {
                        "type": "message",
                        "id": "message",
                        "role": "assistant",
                        "model": "echo",
                        "content": [{"type": "text", "text": answer[:9]}, {"type": "text", "text": answer[9:]}],
                        "usage": {"input_tokens": 12, "output_tokens": 8},
                        "stop_reason": "end_turn",
                    }
                    headers = {"request-id": "anthropic-request"}
                self.assertEqual(set(json.loads(user)), _ITEM_FIELDS)
                return httpx2.Response(200, json=response, headers=headers)

            with (
                self.subTest(protocol=protocol),
                patch.object(httpx2.HTTPTransport, "handle_request", side_effect=handler) as wire,
            ):
                ballot = review_packet(
                    self.packet, endpoint, run_id="run", mode="live", out_path=path, dry_key="local-test"
                )
                self.assertEqual(wire.call_count, 2)
                wire.reset_mock()
                replay = review_packet(self.packet, endpoint, run_id="run", mode="live", out_path=path)
                self.assertEqual(replay, ballot)
                wire.assert_not_called()
            self.assertEqual({r.model_echo for r in ballot.reviews}, {"echo"})
            expected = "openai-request" if protocol == "openai_compat" else "anthropic-request"
            self.assertEqual({r.request_id for r in ballot.reviews}, {expected})

    def test_model_mismatch_fails_immediately_and_never_locks(self):
        for model in ("", "Echo", "not-allowed"):
            send = Mock(return_value=("invalid", model, "req"))
            with self.subTest(model=model), self.assertRaisesRegex(RuntimeError, "model_echo"):
                review_packet(
                    self.packet, self.endpoint, run_id="run", mode="selftest", out_path=self.out, transport=send
                )
            send.assert_called_once()
            self.assertFalse(self.out.exists())

    def test_interrupted_ballot_resumes_only_unlocked_items_then_zero_requests(self):
        first, second = self.packet.items
        initial = Mock(
            side_effect=[_fake(self.endpoint, "", "", canonical_json(first)), ConnectionError("interrupted")]
        )
        with self.assertRaises(ConnectionError):
            review_packet(
                self.packet,
                self.endpoint,
                run_id="run",
                mode="selftest",
                out_path=self.out,
                transport=initial,
                max_retries=0,
            )
        locked = self.out.read_bytes()
        self.assertEqual(json.loads(locked)["item_id"], first.item_id)
        send = Mock(side_effect=_fake)
        ballot = review_packet(
            self.packet, self.endpoint, run_id="run", mode="selftest", out_path=self.out, transport=send
        )
        send.assert_called_once()
        self.assertEqual(json.loads(send.call_args.args[-1])["item_id"], second.item_id)
        complete = self.out.read_bytes()
        self.assertTrue(complete.startswith(locked))
        stamp = self.out.stat().st_mtime_ns
        send.reset_mock()
        replay = review_packet(
            self.packet, self.endpoint, run_id="run", mode="selftest", out_path=self.out, transport=send
        )
        self.assertEqual(replay, ballot)
        self.assertEqual(self.out.read_bytes(), complete)
        self.assertEqual(self.out.stat().st_mtime_ns, stamp)
        send.assert_not_called()

    def test_only_transient_transport_and_protocol_errors_retry_within_budget(self):
        first, second = self.packet.items
        send = Mock(
            side_effect=[
                TimeoutError(),
                ("{}", "echo", "bad"),
                _fake(self.endpoint, "", "", canonical_json(first)),
                _fake(self.endpoint, "", "", canonical_json(second)),
            ]
        )
        ballot = review_packet(
            self.packet, self.endpoint, run_id="run", mode="selftest", out_path=self.out, transport=send
        )
        self.assertEqual(send.call_count, 4)
        self.assertEqual(len(ballot.reviews), 2)
        failing = self.out.with_name("failed.jsonl")
        send = Mock(return_value=("{", "echo", "bad"))
        with self.assertRaises(ValueError):
            review_packet(
                self.packet,
                self.endpoint,
                run_id="run",
                mode="selftest",
                out_path=failing,
                transport=send,
                max_retries=1,
            )
        self.assertEqual(send.call_count, 2)
        self.assertFalse(failing.exists())

    def test_lock_binds_item_mode_and_endpoint_and_rechecks_model_identity(self):
        review_packet(self.packet, self.endpoint, run_id="run", mode="selftest", out_path=self.out, transport=_fake)
        content = self.out.read_bytes()
        send = Mock()
        changed = self.packet.model_copy(deep=True)
        changed.items[0].candidates[0].text += "差异"
        variants: list[tuple[JudgePacket, EndpointConfig, RunMode]] = [
            (changed, self.endpoint, "selftest"),
            (self.packet, self.endpoint.model_copy(update={"model_family": "other"}), "selftest"),
            (self.packet, self.endpoint, "live"),
        ]
        for packet, endpoint, mode in variants:
            with self.subTest(), self.assertRaises(ValueError):
                review_packet(packet, endpoint, run_id="run", mode=mode, out_path=self.out, transport=send)
        with self.assertRaises(RuntimeError):
            review_packet(
                self.packet,
                self.endpoint.model_copy(update={"allowed_model_ids": ["other"]}),
                run_id="run",
                mode="selftest",
                out_path=self.out,
                transport=send,
            )
        send.assert_not_called()
        self.assertEqual(content, self.out.read_bytes())

    def test_concurrent_reviewer_cannot_resend_a_pending_vote(self):
        nested = Mock()

        def send(endpoint: EndpointConfig, key: str, system: str, user: str) -> tuple[str, str, str]:
            with self.assertRaisesRegex(RuntimeError, "独占"):
                review_packet(self.packet, endpoint, run_id="run", mode="selftest", out_path=self.out, transport=nested)
            return _fake(endpoint, key, system, user)

        review_packet(self.packet, self.endpoint, run_id="run", mode="selftest", out_path=self.out, transport=send)
        nested.assert_not_called()

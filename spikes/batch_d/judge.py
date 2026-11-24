"""批次 D 的双盲评审、单题 MQM 校验与断点锁票。"""

from __future__ import annotations

import json
import os
import random
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import anthropic
import httpx2
import openai

from .contract import (
    ALL_GROUPS,
    ALL_PERMUTATIONS,
    BlindCandidate,
    CaseDoc,
    EndpointConfig,
    GenerationRecord,
    GroupId,
    JudgeItem,
    JudgeItemReview,
    JudgePacket,
    PrivateItemMapping,
    PrivateMap,
    ReviewerBallot,
    ReviewerId,
    RunMode,
    canonical_json,
    sha256_canonical,
    sha256_text,
)

BLIND_JUDGE_SYSTEM_PROMPT = """使用同一 MQM 标准独立评估每个候选译文。每个错误只扣一次，分类为
accuracy、terminology、fluency 或 format。改变原意或实质妨碍理解为 major，每处扣 15 分；
其余局部错误为 minor，每处扣 4 分。从 100 分起扣，最低 0 分。
错误改变或遗漏原文明示的事实时 protected 为 true，否则为 false。
source_span 与 target_span 逐字摘录相应原文与该候选译文；漏译时 target_span 为空串，
增译无原文对应时 source_span 为空串。至少给出一侧证据。
只返回一个 JSON 对象，照录 item_id，每个展示 label 恰好返回一次。errors 无错误时为 []。
格式如下，尖括号内替换为实际值，score 为整数且可省略：
{"item_id":"<题号>","candidates":[{"label":"<标签>","errors":[{"category":"<分类>","severity":"<级别>",
"protected":false,"source_span":"<原文>","target_span":"<译文>","reason":"<扣分理由>"}],"score":<得分>}]}"""

type TransportFn = Callable[[EndpointConfig, str, str, str], tuple[str, str, str]]


def build_blind_packets(
    *,
    run_id: str,
    docs: Sequence[CaseDoc],
    generations: Sequence[GenerationRecord],
    reviewer: ReviewerId,
    round_idx: int,
    private_seed: int,
) -> tuple[JudgePacket, PrivateMap]:
    fingerprint = sha256_text(f"{reviewer}:{round_idx}:{private_seed}")
    secret = sha256_canonical([run_id, reviewer, round_idx, private_seed])
    rng = random.Random(secret)
    segments = [(doc, seg) for doc in docs for seg in doc.segments if seg.scored]
    grouped: dict[tuple[str, str], dict[GroupId, GenerationRecord]] = {
        (doc.doc_id, seg.segment_id): {} for doc, seg in segments
    }
    if len(segments) != len(grouped):
        raise ValueError("重复的计分段")
    for rec in generations:
        key = (rec.doc_id, rec.segment_id)
        if rec.run_id == run_id and rec.round_idx == round_idx and key in grouped:
            if rec.group in grouped[key] or rec.status != "done":
                raise ValueError(f"段 {key} 的 {rec.group} 组生成记录重复或未完成")
            grouped[key][rec.group] = rec

    rng.shuffle(segments)
    permutations = list(ALL_PERMUTATIONS)
    items: list[JudgeItem] = []
    mappings: list[PrivateItemMapping] = []
    for i, (doc, seg) in enumerate(segments):
        slot = i % len(permutations)
        if slot == 0:
            rng.shuffle(permutations)
        order = permutations[slot]
        rows = grouped[doc.doc_id, seg.segment_id]
        if rows.keys() != set(ALL_GROUPS):
            raise ValueError(f"段 {doc.doc_id}/{seg.segment_id} 缺少生成记录")
        expanded = rows[order[0]].expanded_source
        if any(r.source != seg.source or r.expanded_source != expanded for r in rows.values()):
            raise ValueError(f"段 {doc.doc_id}/{seg.segment_id} 的评审原文不一致")
        item_id = "t" + sha256_canonical([secret, doc.doc_id, seg.segment_id])[:16]
        by_text: dict[str, str] = {}
        label_to_groups: dict[str, list[GroupId]] = {}
        group_to_label: dict[GroupId, str] = {}
        for group in order:
            target = rows[group].target
            if target not in by_text:
                label = "k" + sha256_canonical([secret, item_id, len(by_text)])[:12]
                by_text[target] = label
                label_to_groups[label] = []
            label = by_text[target]
            label_to_groups[label].append(group)
            group_to_label[group] = label
        context = seg.prev_context_note or (doc.segments[seg.order - 1].source if seg.order else "")
        items.append(
            JudgeItem(
                item_id=item_id,
                source_expanded=expanded,
                local_context="\n".join(filter(None, (seg.section, context))),
                user_terms=doc.user_terms,
                reference_zh=seg.reference_zh,
                candidates=[BlindCandidate(label=label, text=target) for target, label in by_text.items()],
            )
        )
        mappings.append(
            PrivateItemMapping(
                item_id=item_id,
                doc_id=doc.doc_id,
                segment_id=seg.segment_id,
                round_idx=round_idx,
                category=seg.category,
                permutation=order,
                label_to_groups=label_to_groups,
                group_to_label=group_to_label,
            )
        )
    return (
        JudgePacket(run_id=run_id, reviewer=reviewer, round_idx=round_idx, items=items),
        PrivateMap(run_id=run_id, reviewer=reviewer, round_idx=round_idx, seed_fingerprint=fingerprint, items=mappings),
    )


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("JSON 对象含重复键")
    return data


def _validate_item(item: JudgeItem, review: JudgeItemReview) -> None:
    targets = {c.label: c.text for c in item.candidates}
    if review.item_id != item.item_id:
        raise ValueError("响应 item_id 与题号不符")
    if len(review.candidates) != len(targets) or {c.label for c in review.candidates} != targets.keys():
        raise ValueError("响应标签必须恰好覆盖每个展示候选一次")
    for c in review.candidates:
        if "errors" not in c.model_fields_set:
            raise ValueError("每个候选必须显式返回 errors")
        for e in c.errors:
            if not (e.source_span or e.target_span):
                raise ValueError("错误缺少可定位证据")
            if e.source_span not in item.source_expanded or e.target_span not in targets[c.label]:
                raise ValueError(f"候选 {c.label} 的错误引文与原文或译文不符")


def parse_and_validate_item_review(
    item: JudgeItem, raw_text: str, *, model_echo: str = "", request_id: str = ""
) -> JudgeItemReview:
    text = raw_text.strip()
    fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence[1]
    data = json.loads(text, object_pairs_hook=_unique_keys)
    if not isinstance(data, dict) or data.keys() != {"item_id", "candidates"}:
        raise ValueError("评审响应必须只含 item_id 和 candidates")
    review = JudgeItemReview.model_validate(
        {**data, "model_echo": model_echo, "request_id": request_id, "response_sha256": sha256_text(raw_text)},
        strict=True,
    )
    _validate_item(item, review)
    return review


def default_http_transport(endpoint: EndpointConfig, api_key: str, system: str, user: str) -> tuple[str, str, str]:
    """每次只向配置端点发一个请求，以服务端响应头取得请求标识。"""
    if endpoint.protocol == "openai_compat":
        with openai.OpenAI(
            api_key=api_key,
            base_url=endpoint.base_url,
            max_retries=0,
            http_client=httpx2.Client(follow_redirects=False),
            default_headers=endpoint.extra_headers,
        ) as client:
            reply = client.chat.completions.create(
                model=endpoint.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            text = reply.choices[0].message.content if reply.choices else ""
            return text or "", reply.model, reply._request_id or ""
    with anthropic.Anthropic(
        api_key=api_key,
        base_url=endpoint.base_url,
        max_retries=0,
        http_client=httpx2.Client(follow_redirects=False),
        default_headers={"x-api-key": api_key, **endpoint.extra_headers},
    ) as client_anthropic:
        message = client_anthropic.messages.create(
            model=endpoint.model, max_tokens=4096, system=system, messages=[{"role": "user", "content": user}]
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return text, message.model, message._request_id or ""


def _check_model(endpoint: EndpointConfig, echo: str) -> None:
    if endpoint.allowed_model_ids and echo not in endpoint.allowed_model_ids:
        raise RuntimeError(f"服务端 model_echo {echo!r} 不在 allowed_model_ids 中")


def _review_one(
    item: JudgeItem, endpoint: EndpointConfig, key: str, send: TransportFn, max_retries: int
) -> JudgeItemReview:
    payload = canonical_json(item)
    for attempt in range(max_retries + 1):
        try:
            raw, echo, request_id = send(endpoint, key, BLIND_JUDGE_SYSTEM_PROMPT, payload)
        except (openai.APIStatusError, anthropic.APIStatusError) as exc:
            if (exc.status_code not in {408, 409, 425, 429} and exc.status_code < 500) or attempt == max_retries:
                raise
            continue
        except (
            OSError,
            httpx2.TransportError,
            openai.APIConnectionError,
            anthropic.APIConnectionError,
            json.JSONDecodeError,
            openai.APIResponseValidationError,
            anthropic.APIResponseValidationError,
        ):
            if attempt == max_retries:
                raise
            continue
        _check_model(endpoint, echo)
        try:
            return parse_and_validate_item_review(item, raw, model_echo=echo, request_id=request_id)
        except ValueError:
            if attempt == max_retries:
                raise
    raise AssertionError("重试预算必须非负")


@contextmanager
def _ballot_lock(path: Path) -> Iterator[None]:
    # 原子替换会更换 JSONL 的文件标识，所以在稳定的旁文件上持有操作系统锁。
    with path.with_name(path.name + ".lock").open("a+b") as lock:
        lock.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("无法独占票箱") from exc
        yield


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as scratch:
        staged = Path(scratch) / path.name
        with staged.open("w", encoding="utf-8", newline="\n") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        staged.replace(path)


def review_packet(
    packet: JudgePacket,
    endpoint: EndpointConfig,
    *,
    run_id: str,
    mode: RunMode,
    out_path: Path,
    transport: TransportFn | None = None,
    max_retries: int = 2,
    dry_key: str | None = None,
) -> ReviewerBallot:
    if run_id != packet.run_id or mode not in ("selftest", "live") or max_retries < 0:
        raise ValueError("运行标识、模式或重试预算无效")
    items = {item.item_id: item for item in packet.items}
    if len(items) != len(packet.items):
        raise ValueError("盲包存在重复题号")
    # 票据逐题绑定盲题、评分口径与评审端点。认证头和密钥均不落盘。
    scope = sha256_canonical(
        [
            packet.model_dump(exclude={"items"}),
            BLIND_JUDGE_SYSTEM_PROMPT,
            mode,
            endpoint.protocol,
            endpoint.base_url,
            endpoint.model,
            endpoint.model_family,
        ]
    )
    bindings = {idx: sha256_canonical([scope, item.model_dump()]) for idx, item in items.items()}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    reviews: dict[str, JudgeItemReview] = {}
    with _ballot_lock(out_path):
        content = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        lines = content.removesuffix("\n").split("\n") if content else []
        for line in lines:
            row = json.loads(line, object_pairs_hook=_unique_keys)
            if not isinstance(row, dict):
                raise ValueError("无效的锁票记录")
            binding = row.pop("binding_sha256", None)
            review = JudgeItemReview.model_validate(row, strict=True)
            idx = review.item_id
            if idx in reviews or idx not in items or binding != sha256_canonical([bindings[idx], row]):
                raise ValueError("已锁票重复或与本次盲题、评审配置不符")
            _check_model(endpoint, review.model_echo)
            _validate_item(items[idx], review)
            reviews[idx] = review
        for idx, item in items.items():
            if idx in reviews:
                continue
            if mode == "selftest" and transport is None:
                raise ValueError("selftest 需要离线传输函数")
            key = dry_key if dry_key is not None else (os.environ.get(endpoint.key_env, "") if mode == "live" else "")
            if not key and mode == "live":
                raise ValueError(f"缺少评审凭据 {endpoint.key_env}")
            review = _review_one(item, endpoint, key, transport or default_http_transport, max_retries)
            if content and not content.endswith("\n"):
                content += "\n"
            row = review.model_dump()
            content += canonical_json({"binding_sha256": sha256_canonical([bindings[idx], row]), **row}) + "\n"
            _atomic_write(out_path, content)
            reviews[idx] = review
    return ReviewerBallot(
        run_id=run_id,
        mode=mode,
        reviewer=packet.reviewer,
        round_idx=packet.round_idx,
        model_family=endpoint.model_family,
        model_id=endpoint.model,
        reviews=[reviews[idx] for idx in items],
    )


def write_blind_artifacts(out_dir: Path, packet: JudgePacket, private_map: PrivateMap) -> tuple[Path, Path]:
    if (packet.run_id, packet.reviewer, packet.round_idx) != (
        private_map.run_id,
        private_map.reviewer,
        private_map.round_idx,
    ):
        raise ValueError("盲包与私有映射的运行标识不符")
    labels = {item.item_id: {c.label for c in item.candidates} for item in packet.items}
    mapped = {m.item_id: set(m.label_to_groups) for m in private_map.items}
    if labels != mapped or len(labels) != len(packet.items) or len(mapped) != len(private_map.items):
        raise ValueError("盲包与私有映射的题号或候选标签不符")
    blind = out_dir / "blind" / f"{packet.reviewer}_r{packet.round_idx}.json"
    private = out_dir / "private" / f"map_{private_map.reviewer}_r{private_map.round_idx}.json"
    _atomic_write(blind, canonical_json(packet) + "\n")
    _atomic_write(private, canonical_json(private_map) + "\n")
    return blind, private

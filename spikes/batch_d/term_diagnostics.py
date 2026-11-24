"""TR-011 的离线术语碰撞、词对覆盖与候选补丁校验。"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tarfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel

from pdftr.domain.text import normalize_term_key

from .contract import BASELINE_EXTRACT_PROMPT_SHA256, BASELINE_SHA, TermPairExpectation, TermRelation, sha256_text

type TermTable = Mapping[str, str] | Sequence[tuple[str, str]]
type PairKind = Literal["suspect", "benign_synonym_or_acronym", "user_intentional_same"]
type GroupKind = PairKind | Literal["mixed"]

_EXTRACT_FILE = "backend/src/pdftr/glossary/extract.py"


class CollisionGroup(BaseModel, extra="forbid"):
    """sources 与 canonical_keys 同序，pair_kinds 对应其所有无序二元组合。"""

    target: str
    sources: list[str]
    canonical_keys: list[str]
    kind: GroupKind
    pair_kinds: list[str]
    involves_user_term: bool


class PairCoverageItem(BaseModel, extra="forbid"):
    source_a: str
    source_b: str
    relation: TermRelation
    expect_in_glossary: bool
    in_raw_a: bool
    in_raw_b: bool
    in_normalized_a: bool
    in_normalized_b: bool
    target_a: str | None
    target_b: str | None
    distinguished: bool
    in_body_conflict: bool
    body_clear_despite_same_target: bool


class TermDiagnosticReport(BaseModel, extra="forbid"):
    raw_term_count: int
    normalized_term_count: int
    raw_collisions: list[CollisionGroup]
    normalized_collisions: list[CollisionGroup]
    suspect_collision_count: int
    mixed_group_count: int
    expected_pair_total: int
    expected_pair_covered: int
    expected_pair_distinguished: int
    missed_expected_pairs: list[tuple[str, str]]
    pair_items: list[PairCoverageItem]


def _index_terms(terms: TermTable) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    targets: dict[str, str] = {}
    sources_by_target: dict[str, dict[str, str]] = defaultdict(dict)
    for source, target in terms.items() if isinstance(terms, Mapping) else terms:
        key = normalize_term_key(source)
        target = " ".join(target.split())
        if key and target:
            targets[key] = target
            sources_by_target[target].setdefault(key, source)
    return targets, sources_by_target


def _alias_components(expected_pairs: Sequence[TermPairExpectation], definitions: Mapping[str, str]) -> dict[str, str]:
    # 两个别称可以经过同一个全称关联，即使全称本身没有被抽取。
    aliases = list(definitions.items())
    aliases.extend((p.source_a, p.source_b) for p in expected_pairs if p.relation in {"synonym", "acronym"})
    neighbors: dict[str, set[str]] = defaultdict(set)
    for a, b in aliases:
        key_a, key_b = normalize_term_key(a), normalize_term_key(b)
        if key_a and key_b:
            neighbors[key_a].add(key_b)
            neighbors[key_b].add(key_a)
    roots: dict[str, str] = {}
    for root in neighbors:
        if root in roots:
            continue
        stack = [root]
        while stack:
            key = stack.pop()
            if key not in roots:
                roots[key] = root
                stack.extend(neighbors[key])
    return roots


def _pair_kind(
    a: str,
    b: str,
    target: str,
    user: Mapping[str, str],
    relations: Mapping[frozenset[str], TermRelation],
    aliases: Mapping[str, str],
) -> PairKind:
    relation = relations.get(frozenset((a, b)))
    if relation == "user_same" or user.get(a) == user.get(b) == target:
        return "user_intentional_same"
    if relation != "distinct" and a in aliases and aliases[a] == aliases.get(b):
        return "benign_synonym_or_acronym"
    return "suspect"


def _collisions(
    groups: dict[str, dict[str, str]],
    user: Mapping[str, str],
    relations: Mapping[frozenset[str], TermRelation],
    aliases: Mapping[str, str],
) -> list[CollisionGroup]:
    result: list[CollisionGroup] = []
    for target, sources in sorted(groups.items()):
        keys = sorted(sources)
        if len(keys) < 2:
            continue
        kinds: list[PairKind] = [_pair_kind(a, b, target, user, relations, aliases) for a, b in combinations(keys, 2)]
        kind: GroupKind
        if "suspect" in kinds and len(set(kinds)) > 1:
            kind = "mixed"
        elif "user_intentional_same" in kinds:
            kind = "user_intentional_same"
        else:
            kind = kinds[0]
        result.append(
            CollisionGroup(
                target=target,
                sources=[sources[k] for k in keys],
                canonical_keys=keys,
                kind=kind,
                pair_kinds=list(kinds),
                involves_user_term="user_intentional_same" in kinds or any(k in user for k in keys),
            )
        )
    return result


def _body_evidence(
    target: str, key_a: str, key_b: str, paragraphs: Sequence[str], *, only_pair: bool
) -> tuple[bool, bool]:
    """只标记可见的重复对举或同段双源词括注，两项均为假时表示没有判定依据。"""
    quoted = re.escape(target)
    repeated = re.compile(rf"[「“]{quoted}[」”]\s*(?:与|和|不同于|区别于)\s*[「“]{quoted}[」”]")
    labeled = re.compile(rf"{quoted}\s*[（(]([^（）()]+)[）)]")
    conflict = clear = False
    for paragraph in paragraphs:
        text = " ".join(paragraph.split())
        if only_pair and repeated.search(text):
            conflict = True
        labels = {normalize_term_key(s) for s in labeled.findall(text)}
        if key_a in labels and key_b in labels:
            clear = True
    return conflict, clear


def diagnose_terms(
    raw_terms: TermTable,
    normalized_terms: TermTable,
    *,
    user_terms: Mapping[str, str] | None = None,
    definitions: Mapping[str, str] | None = None,
    expected_pairs: Sequence[TermPairExpectation] = (),
    translated_texts: Sequence[str] = (),
) -> TermDiagnosticReport:
    """以归一化表统计覆盖和含可疑词对的组数，混合组同时计入 suspect_collision_count。

    所有要求入表的关系均进入覆盖分母。区分数只统计要求入表且确为 distinct 的异译词对。
    """
    raw, raw_groups = _index_terms(raw_terms)
    normalized, norm_groups = _index_terms(normalized_terms)
    user, _ = _index_terms(user_terms or {})
    relations: dict[frozenset[str], TermRelation] = {
        frozenset((normalize_term_key(p.source_a), normalize_term_key(p.source_b))): p.relation for p in expected_pairs
    }
    aliases = _alias_components(expected_pairs, definitions or {})
    norm_collisions = _collisions(norm_groups, user, relations, aliases)
    paragraphs = (translated_texts,) if isinstance(translated_texts, str) else translated_texts

    items: list[PairCoverageItem] = []
    for pair in expected_pairs:
        a, b = normalize_term_key(pair.source_a), normalize_term_key(pair.source_b)
        target_a, target_b = normalized.get(a), normalized.get(b)
        conflict = clear = False
        if (
            pair.relation == "distinct"
            and target_a is not None
            and target_a == target_b
            and a != b
            and _pair_kind(a, b, target_a, user, relations, aliases) == "suspect"
        ):
            conflict, clear = _body_evidence(target_a, a, b, paragraphs, only_pair=len(norm_groups[target_a]) == 2)
        items.append(
            PairCoverageItem(
                source_a=pair.source_a,
                source_b=pair.source_b,
                relation=pair.relation,
                expect_in_glossary=pair.expect_in_glossary,
                in_raw_a=a in raw,
                in_raw_b=b in raw,
                in_normalized_a=a in normalized,
                in_normalized_b=b in normalized,
                target_a=target_a,
                target_b=target_b,
                distinguished=pair.relation == "distinct"
                and target_a is not None
                and target_b is not None
                and target_a != target_b,
                in_body_conflict=conflict,
                body_clear_despite_same_target=clear,
            )
        )
    expected = [p for p in items if p.expect_in_glossary]
    missed = [(p.source_a, p.source_b) for p in expected if not (p.in_normalized_a and p.in_normalized_b)]
    return TermDiagnosticReport(
        raw_term_count=len(raw_terms),
        normalized_term_count=len(normalized_terms),
        raw_collisions=_collisions(raw_groups, user, relations, aliases),
        normalized_collisions=norm_collisions,
        suspect_collision_count=sum(g.kind in {"suspect", "mixed"} for g in norm_collisions),
        mixed_group_count=sum(g.kind == "mixed" for g in norm_collisions),
        expected_pair_total=len(expected),
        expected_pair_covered=len(expected) - len(missed),
        expected_pair_distinguished=sum(p.distinguished for p in expected),
        missed_expected_pairs=missed,
        pair_items=items,
    )


def _prompt_hash(file: Path) -> str:
    for node in ast.parse(file.read_bytes()).body:
        match node:
            case ast.Assign(targets=[ast.Name(id="PROMPT")], value=ast.Constant(value=str(prompt))):
                return sha256_text(prompt)
    raise ValueError("抽取模块缺少 PROMPT 字符串常量")


def verify_tr011_patch(repo_root: Path, patch_path: Path | None = None) -> dict[str, str]:
    """在固定提交的临时源码树中验证单行补丁，返回 baseline 与 candidate 两个提示词摘要。"""
    patch = (patch_path or repo_root / "spikes/batch_d/tr011.patch").read_bytes()
    if b"\r" in patch:
        raise ValueError("TR-011 补丁必须使用纯 LF 换行")
    with TemporaryDirectory() as tmp:
        tree = Path(tmp)
        git_apply = ["git", "apply"]
        stat = subprocess.check_output([*git_apply, "--numstat", "--summary", "-z"], cwd=tree, input=patch)
        if stat != f"1\t0\t{_EXTRACT_FILE}\0".encode():
            raise ValueError("TR-011 只允许向 extract.py 增加一行")
        archive = subprocess.check_output(
            ["git", "-c", "core.autocrlf=false", "archive", "--format=tar", BASELINE_SHA, "backend/src/pdftr"],
            cwd=repo_root,
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tree, filter="data")
        file = tree / _EXTRACT_FILE
        baseline = _prompt_hash(file)
        if baseline != BASELINE_EXTRACT_PROMPT_SHA256:
            raise ValueError("抽取提示词基线摘要不匹配")
        subprocess.run([*git_apply, "--check", "--whitespace=error"], input=patch, cwd=tree, check=True)
        subprocess.run([*git_apply, "--whitespace=error"], input=patch, cwd=tree, check=True)
        candidate = _prompt_hash(file)
        if candidate == baseline:
            raise ValueError("TR-011 未改变抽取提示词")
        return {"baseline": baseline, "candidate": candidate}

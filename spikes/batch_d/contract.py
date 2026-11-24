"""批次 D 消融评测的数据契约、基线金样与策略加载器。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "batch_d.v1"
RUBRIC_VERSION = "mqm.batch_d.v1"

# 冻结基线（main 合并 Stage 1/2 修复后的提交与运行时指纹）
BASELINE_SHA = "c15018594d1078d943086221a38baa5f18d4466c"
BASELINE_PROMPT_VERSION = "v7"
BASELINE_VALIDATION_VERSION = 7
BASELINE_EXTRACT_PROMPT_SHA256 = "ee4f67a94676b2eab3fc34e9bfdea5df34c7142ece9e9d9289c6bed1f3821550"
BASELINE_RULES_SHA256 = "eb9a1afc2aa1f9c4c2011b9be366270538b656d7e14e22a3a30e28d5c9609556"
BASELINE_CACHE_GOLD_SHA256 = "14c5408c8783c2b4e7d0910dc43749963374a40cedb5e155981e9f496cda6f3d"
TR012_MAX_CHAR_DELTA = 120
TR012_EXACT_CHAR_DELTA = 91

GroupId = Literal["A", "B", "C"]
SplitKind = Literal["dev", "holdout"]
Discipline = Literal[
    "operations_scheduling",
    "measurement_engineering",
    "probability_statistics",
    "computer_systems",
]
SegmentCategory = Literal["concept", "formula_relation", "clause_boundary", "control"]
SegmentRole = Literal["title", "text", "caption", "reference"]
TermRelation = Literal["distinct", "synonym", "acronym", "user_same"]
RunMode = Literal["selftest", "live"]
ReviewerId = Literal["codex", "neon"]
GenerationStatus = Literal["done", "failed", "refused", "truncated"]
ErrorCategory = Literal["accuracy", "terminology", "fluency", "format"]
ErrorSeverity = Literal["major", "minor"]
CheckStatus = Literal["pass", "fail", "na"]
DecisionState = Literal[
    "OFFLINE_VERIFIED",
    "INCOMPLETE",
    "HOLD",
    "RETAIN_A",
    "ELIGIBLE_B_ONLY",
    "ELIGIBLE_B_AND_C",
]

ALL_GROUPS: tuple[GroupId, ...] = ("A", "B", "C")
ALL_DISCIPLINES: tuple[Discipline, ...] = (
    "operations_scheduling",
    "measurement_engineering",
    "probability_statistics",
    "computer_systems",
)
ALL_CATEGORIES: tuple[SegmentCategory, ...] = (
    "concept",
    "formula_relation",
    "clause_boundary",
    "control",
)
ALL_PERMUTATIONS: tuple[tuple[GroupId, GroupId, GroupId], ...] = (
    ("A", "B", "C"),
    ("A", "C", "B"),
    ("B", "A", "C"),
    ("B", "C", "A"),
    ("C", "A", "B"),
    ("C", "B", "A"),
)

_PLACEHOLDER_RE = re.compile(r"<(m\d+)/>")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_DEFAULT_POLICY_PATH = Path(__file__).with_name("policy.json")


def canonical_json(obj: Any) -> str:
    """稳定 UTF-8 JSON 序列化，用于摘要与离线确定性比对。"""
    payload = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_canonical(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def contains_emoji(text: str) -> bool:
    return bool(_EMOJI_RE.search(text))


def compute_mqm_score(errors: list[MqmError]) -> int:
    penalty = sum(15 if err.severity == "major" else 4 for err in errors)
    return max(0, 100 - penalty)


class TermPairExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_a: str = Field(min_length=1)
    source_b: str = Field(min_length=1)
    relation: TermRelation
    expect_in_glossary: bool = True
    note: str = ""


class FactAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    kind: SegmentCategory
    description: str = Field(min_length=1)
    protected: bool = True


class CaseSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    section: str = ""
    role: SegmentRole = "text"
    source: str = Field(min_length=1)
    formulas: dict[str, str] = Field(default_factory=dict)
    scored: bool = True
    category: SegmentCategory = "control"
    reference_zh: str | None = None
    prev_context_note: str = ""

    @model_validator(mode="after")
    def _validate_placeholders_and_cleanliness(self) -> CaseSegment:
        found = set(_PLACEHOLDER_RE.findall(self.source))
        declared = set(self.formulas.keys())
        if found != declared:
            raise ValueError(f"段 {self.segment_id} 公式占位符与映射不一致: source={found}, formulas={declared}")
        if contains_emoji(self.source) or (self.reference_zh and contains_emoji(self.reference_zh)):
            raise ValueError(f"段 {self.segment_id} 禁止包含 emoji")
        return self


class CaseDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(min_length=1)
    split: SplitKind
    discipline: Discipline
    template_family: str = Field(min_length=1)
    title: str = Field(min_length=1)
    abstract: str = Field(min_length=1)
    headings: list[str] = Field(default_factory=list)
    segments: list[CaseSegment] = Field(min_length=1)
    user_terms: dict[str, str] = Field(default_factory=dict)
    library_terms: dict[str, str] = Field(default_factory=dict)
    term_pairs: list[TermPairExpectation] = Field(default_factory=list)
    anchors: list[FactAnchor] = Field(default_factory=list)
    has_reference: bool = False

    @model_validator(mode="after")
    def _validate_structure(self) -> CaseDoc:
        orders = [seg.order for seg in self.segments]
        if orders != list(range(len(self.segments))):
            raise ValueError(f"文档 {self.doc_id} 段落 order 必须从 0 连续递增")
        seg_ids = [seg.segment_id for seg in self.segments]
        if len(seg_ids) != len(set(seg_ids)):
            raise ValueError(f"文档 {self.doc_id} 存在重复 segment_id")
        seg_set = set(seg_ids)
        anchor_ids: set[str] = set()
        for anchor in self.anchors:
            if anchor.segment_id not in seg_set:
                raise ValueError(f"文档 {self.doc_id} 锚点 {anchor.anchor_id} 引用未知段 {anchor.segment_id}")
            if anchor.anchor_id in anchor_ids:
                raise ValueError(f"文档 {self.doc_id} 存在重复 anchor_id: {anchor.anchor_id}")
            anchor_ids.add(anchor.anchor_id)
        if self.has_reference:
            scored_segs = [s for s in self.segments if s.scored]
            if any(not s.reference_zh for s in scored_segs):
                raise ValueError(f"文档 {self.doc_id} 声明 has_reference=True，但有计分段缺少 reference_zh")
        if contains_emoji(self.title) or contains_emoji(self.abstract):
            raise ValueError(f"文档 {self.doc_id} 标题或摘要禁止包含 emoji")
        return self


class ExtractionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(min_length=1)
    round_idx: int = Field(ge=0)
    source_group: Literal["A", "B"]
    domain: str = ""
    use_library: bool = False
    raw_terms: list[tuple[str, str]] = Field(default_factory=list)
    normalized_terms: list[tuple[str, str]] = Field(default_factory=list)
    raw_response_sha256: str = Field(min_length=1)
    prompt_sha256: str = Field(min_length=1)
    ordered_hash: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_hit_tokens: int = Field(default=0, ge=0)
    cost_cny: float | None = Field(default=None, ge=0.0)
    elapsed_s: float = Field(default=0.0, ge=0.0)
    actual_model: str = ""
    request_id: str = ""

    @model_validator(mode="after")
    def _fill_ordered_hash(self) -> ExtractionSnapshot:
        expected = sha256_canonical(
            {
                "domain": self.domain,
                "use_library": self.use_library,
                "normalized_terms": self.normalized_terms,
            }
        )
        if not self.ordered_hash:
            self.ordered_hash = expected
        elif self.ordered_hash != expected:
            raise ValueError("ExtractionSnapshot.ordered_hash 与归一化快照内容不匹配")
        return self


class GenerationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    mode: RunMode
    round_idx: int = Field(ge=0)
    group: GroupId
    doc_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    category: SegmentCategory
    source: str = Field(min_length=1)
    expanded_source: str = Field(min_length=1)
    formulas: dict[str, str] = Field(default_factory=dict)
    target: str = ""
    status: GenerationStatus
    attempts: int = Field(ge=0)
    snapshot_hash: str = Field(min_length=1)
    system_prompt_sha256: str = Field(min_length=1)
    user_prompt_sha256: str = Field(min_length=1)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_hit_tokens: int = Field(default=0, ge=0)
    cost_cny: float | None = Field(default=None, ge=0.0)
    elapsed_s: float = Field(default=0.0, ge=0.0)
    actual_model: str = ""
    request_id: str = ""
    compatibility_fallback: bool = False
    failure_reason: str = ""


class BlindCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    text: str


class JudgeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    source_expanded: str = Field(min_length=1)
    local_context: str = ""
    user_terms: dict[str, str] = Field(default_factory=dict)
    reference_zh: str | None = None
    candidates: list[BlindCandidate] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_labels(self) -> JudgeItem:
        labels = [c.label for c in self.candidates]
        if len(labels) != len(set(labels)):
            raise ValueError(f"盲评题 {self.item_id} 存在重复候选标签")
        return self


class JudgePacket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: str = PROTOCOL_VERSION
    run_id: str = Field(min_length=1)
    reviewer: ReviewerId
    round_idx: int = Field(ge=0)
    rubric_version: str = RUBRIC_VERSION
    items: list[JudgeItem] = Field(min_length=1)


class PrivateItemMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    round_idx: int = Field(ge=0)
    category: SegmentCategory
    label_to_groups: dict[str, list[GroupId]]
    group_to_label: dict[GroupId, str]
    permutation: tuple[GroupId, GroupId, GroupId]

    @model_validator(mode="after")
    def _check_bijection(self) -> PrivateItemMapping:
        if set(self.permutation) != {"A", "B", "C"}:
            raise ValueError(f"私有映射 {self.item_id} 排列不完整")
        if set(self.group_to_label.keys()) != {"A", "B", "C"}:
            raise ValueError(f"私有映射 {self.item_id} 缺少 A/B/C 映射")
        seen: list[GroupId] = []
        for label, groups in self.label_to_groups.items():
            for g in groups:
                if self.group_to_label.get(g) != label:
                    raise ValueError(f"私有映射 {self.item_id} 正反向映射不一致")
                seen.append(g)
        if sorted(seen) != ["A", "B", "C"]:
            raise ValueError(f"私有映射 {self.item_id} 必须恰好覆盖 A/B/C 各一次")
        return self


class PrivateMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    reviewer: ReviewerId
    round_idx: int = Field(ge=0)
    seed_fingerprint: str = Field(min_length=1)
    items: list[PrivateItemMapping] = Field(min_length=1)


class MqmError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ErrorCategory
    sub_category: str = ""
    severity: ErrorSeverity
    protected: bool = False
    anchor_id: str = ""
    source_span: str = ""
    target_span: str = ""
    reason: str = ""


class CheckJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1)
    status: CheckStatus
    evidence: str = ""


class CandidateReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    checks: list[CheckJudgment] = Field(default_factory=list)
    errors: list[MqmError] = Field(default_factory=list)
    score: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _sync_score(self) -> CandidateReview:
        calc = compute_mqm_score(self.errors)
        if self.score is None:
            self.score = calc
        elif self.score != calc:
            raise ValueError(f"候选 {self.label} 分数 {self.score} 与 MQM 扣分结果 {calc} 不一致")
        return self


class JudgeItemReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    candidates: list[CandidateReview] = Field(min_length=1)
    model_echo: str = ""
    request_id: str = ""
    response_sha256: str = ""


class ReviewerBallot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    mode: RunMode
    reviewer: ReviewerId
    round_idx: int = Field(ge=0)
    model_family: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    reviews: list[JudgeItemReview] = Field(min_length=1)


class ArbitrationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    round_idx: int = Field(ge=0)
    anchor_id: str = ""
    reason: str = Field(min_length=1)
    resolved: bool = False
    resolved_errors: dict[GroupId, list[MqmError]] = Field(default_factory=dict)


class EndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["openai_compat", "anthropic"]
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_family: str = Field(min_length=1)
    key_env: str = Field(min_length=1)
    allowed_model_ids: list[str] = Field(default_factory=list)
    extra_headers: dict[str, str] = Field(default_factory=dict)


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_requests: int = Field(gt=0)
    max_cost_cny: float = Field(gt=0.0)
    max_seconds_per_stage: float = Field(gt=0.0)


class PriceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_hit_cny_per_m: float = Field(ge=0.0)
    cache_miss_cny_per_m: float = Field(ge=0.0)
    output_cny_per_m: float = Field(ge=0.0)


class LiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generator: EndpointConfig
    codex: EndpointConfig
    neon: EndpointConfig
    budget: BudgetConfig
    pricing: dict[str, PriceEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_independent_families(self) -> LiveConfig:
        g_fam = self.generator.model_family.strip().casefold()
        c_fam = self.codex.model_family.strip().casefold()
        n_fam = self.neon.model_family.strip().casefold()
        if c_fam == n_fam:
            raise ValueError("双盲评审 codex 与 neon 必须指向不同的模型家族")
        if g_fam in {c_fam, n_fam}:
            raise ValueError("生成模型家族不得与任一双盲评审模型家族相同")
        return self


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: str = PROTOCOL_VERSION
    baseline_sha: str = BASELINE_SHA
    baseline_prompt_version: str = BASELINE_PROMPT_VERSION
    baseline_validation_version: int = BASELINE_VALIDATION_VERSION
    rounds: int = Field(ge=1)
    dev_docs: int = Field(ge=1)
    holdout_docs: int = Field(ge=1)
    scored_segments_per_doc: int = Field(ge=1)
    min_net_gain_b_concept: int = Field(ge=1)
    min_net_gain_c_formula_relation: int = Field(ge=1)
    min_net_gain_c_clause_boundary: int = Field(ge=1)
    min_gain_disciplines: int = Field(ge=1)
    min_overall_mean_delta: float = 0.0
    min_control_mean_delta: float = 0.0
    min_bootstrap_ci_low: float = -1.0
    min_stratum_mean_delta: float = -1.0
    bootstrap_resamples: int = Field(ge=100)
    bootstrap_seed: int = 20261125
    sign_test_alpha: float = Field(gt=0.0, lt=1.0)
    max_disagreement_score_gap: int = Field(ge=1)
    max_cost_ratio: float = Field(gt=0.0)
    max_request_ratio: float = Field(gt=0.0)
    max_p95_latency_ratio: float = Field(gt=0.0)
    tr012_max_char_delta: int = Field(gt=0)
    default_pricing: dict[str, PriceEntry] = Field(default_factory=dict)


class GateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    passed: bool
    detail: str
    metrics: dict[str, Any] = Field(default_factory=dict)


class DecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: str = PROTOCOL_VERSION
    run_id: str = Field(min_length=1)
    mode: RunMode
    state: DecisionState
    eligible_b: bool
    eligible_c: bool
    reasons: list[str] = Field(default_factory=list)
    checks: list[GateCheck] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    evidence_hash: str = Field(min_length=1)
    provenance: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _enforce_promotion_guards(self) -> DecisionRecord:
        if self.mode == "selftest":
            if self.eligible_b or self.eligible_c:
                raise ValueError("selftest 模式严禁产出生产准入资格")
            if self.state not in {"OFFLINE_VERIFIED", "INCOMPLETE"}:
                raise ValueError(f"selftest 模式非法终态: {self.state}")
        if self.state == "ELIGIBLE_B_AND_C" and not (self.eligible_b and self.eligible_c):
            raise ValueError("ELIGIBLE_B_AND_C 要求 eligible_b 与 eligible_c 均为 True")
        if self.state == "ELIGIBLE_B_ONLY" and not (self.eligible_b and not self.eligible_c):
            raise ValueError("ELIGIBLE_B_ONLY 要求 eligible_b=True 且 eligible_c=False")
        if self.eligible_c and not self.eligible_b:
            raise ValueError("C 准入以 B 准入为前提，不允许 eligible_c=True 且 eligible_b=False")
        return self


def load_policy(path: Path | None = None) -> PolicyConfig:
    target = path or _DEFAULT_POLICY_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    return PolicyConfig.model_validate(data)

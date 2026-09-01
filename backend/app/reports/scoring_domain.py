from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.sessions.models import Media, Scene


class CoreDimension(StrEnum):
    respectful_communication = "C1"
    listening_and_emotion = "C2"
    concern_clarification = "C3"
    integration_and_judgment = "C4"
    supportive_intervention = "C5"
    voice_and_process = "C6"
    boundary_and_ethics = "C7"
    closure_and_followup = "C8"
    documentation = "C9"


class SpecialModule(StrEnum):
    basic_risk_screening = "S1a"
    full_risk_appraisal = "S1b"
    safety_response = "S2"
    emotional_dysregulation = "S3"
    psychotic_experience = "S4"
    dependency_and_boundary = "S5"
    aggression_and_harassment = "S6"
    third_party_call = "S7"
    minor_protection = "S8"


class Indicator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    observation: str = Field(min_length=1)


class RubricBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    measures: str = Field(min_length=1)
    indicators: list[Indicator] = Field(min_length=1)
    excluded: list[str]
    evidence_note: str = Field(min_length=1)
    anchors: dict[int, str]
    conditional_in_level3: list[str]


class CoreRubric(RubricBase):
    id: CoreDimension


class ModuleRubric(RubricBase):
    id: SpecialModule
    activation: str = Field(min_length=1)
    default_enabled: bool = False


type Target = CoreDimension | SpecialModule
type WorkRecordField = Literal[
    "problem_understanding",
    "risk_level",
    "risk_reasoning",
    "risk_evidence_turn_ids",
    "missing_information",
    "planned_actions",
    "referral_decision",
    "supervision_decision",
    "follow_up",
    "limitations",
]


class MaterialKind(StrEnum):
    dialogue = "dialogue"
    audio = "audio"
    work_record = "work_record"
    opportunity = "opportunity"


class EvidenceRole(StrEnum):
    primary = "primary"
    supporting = "supporting"
    cross_check = "cross_check"
    opportunity_only = "opportunity_only"
    excluded = "excluded"


class IndicatorStatus(StrEnum):
    demonstrated = "demonstrated"
    partial = "partial"
    opportunity_missed = "opportunity_missed"
    adverse = "adverse"
    no_opportunity = "no_opportunity"
    no_reliable_material = "no_reliable_material"


class UnscoredReason(StrEnum):
    no_opportunity = "no_opportunity"
    insufficient_evidence = "insufficient_evidence"
    technical_failure = "technical_failure"


class AnalysisOutcome(StrEnum):
    ok = "ok"
    analysis_failed = "analysis_failed"


class EvidenceDirection(StrEnum):
    support = "support"
    limit = "limit"
    adverse = "adverse"


class EvidenceStrength(StrEnum):
    strong = "strong"
    moderate = "moderate"
    weak = "weak"


class EvidenceConfidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class EvidenceRejectionReason(StrEnum):
    unit_missing = "unit_missing"
    target_mismatch = "target_mismatch"
    indicator_not_in_target = "indicator_not_in_target"
    dialogue_turn_missing = "dialogue_turn_missing"
    work_record_field_invalid = "work_record_field_invalid"
    audio_event_missing = "audio_event_missing"
    quote_not_contiguous = "quote_not_contiguous"
    source_not_in_unit = "source_not_in_unit"
    source_excluded = "source_excluded"
    source_opportunity_only = "source_opportunity_only"
    semantic_prerequisite_missing = "semantic_prerequisite_missing"


class LevelCapReason(StrEnum):
    adverse_evidence = "adverse_evidence"
    conditional_opportunity_unavailable = "conditional_opportunity_unavailable"
    no_complex_opportunity = "no_complex_opportunity"


class OpportunityKind(StrEnum):
    required = "required"
    conditional = "conditional"


class SemanticBottomLineCategory(StrEnum):
    humiliation_or_coercion = "humiliation_or_coercion"
    false_confidentiality = "false_confidentiality"
    encouraged_harm = "encouraged_harm"
    private_relationship = "private_relationship"
    fabricated_record = "fabricated_record"


class RiskDisclosureCategory(StrEnum):
    suicide_or_self_harm = "suicide_or_self_harm"
    harm_to_others = "harm_to_others"
    other_immediate_danger = "other_immediate_danger"


class RiskDisclosureImmediacy(StrEnum):
    imminent = "imminent"
    not_imminent = "not_imminent"
    unclear = "unclear"


class BottomLineCategory(StrEnum):
    humiliation_or_coercion = "humiliation_or_coercion"
    known_urgent_risk_ended_without_safety_action = "known_urgent_risk_ended_without_safety_action"
    false_confidentiality = "false_confidentiality"
    fabricated_record = "fabricated_record"
    encouraged_harm = "encouraged_harm"
    private_relationship = "private_relationship"


class BottomLineDetection(StrEnum):
    rule = "rule"
    semantic = "semantic"
    rule_candidate_semantic_confirmed = "rule_candidate_semantic_confirmed"


class ScoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DialogueRef(ScoringModel):
    kind: Literal["dialogue"]
    turn_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)

    @field_validator("quote")
    @classmethod
    def require_nonblank_quote(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("quote 不能只包含空白")
        return value


class WorkRecordRef(ScoringModel):
    kind: Literal["work_record"]
    field: WorkRecordField
    quote: str = Field(min_length=1)

    @field_validator("quote")
    @classmethod
    def require_nonblank_quote(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("quote 不能只包含空白")
        return value


class AudioEventRef(ScoringModel):
    kind: Literal["audio_event"]
    event_id: str = Field(min_length=1)


type EvidenceRef = Annotated[
    DialogueRef | WorkRecordRef | AudioEventRef,
    Field(discriminator="kind"),
]


class MeaningUnit(ScoringModel):
    id: str = Field(min_length=1)
    turn_ids: list[str] = Field(
        default_factory=list,
        description="该意义单元引用的实际材料话轮编号，必须来自输入 turns。",
    )
    work_record_refs: list[WorkRecordRef] = Field(
        default_factory=list,
        description="该意义单元引用的实际材料工作记录字段与连续原文。",
    )
    audio_event_ids: list[str] = Field(
        default_factory=list,
        description="该意义单元引用的实际材料音频事件编号，必须来自输入事件。",
    )
    summary: str = Field(min_length=1)

    @field_validator("turn_ids", "audio_event_ids")
    @classmethod
    def deduplicate_ids(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("材料引用 id 不能为空")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def require_source(self) -> Self:
        if not (self.turn_ids or self.work_record_refs or self.audio_event_ids):
            raise ValueError("意义单元至少引用一种实际材料")
        return self


class CodedEvidence(ScoringModel):
    unit_id: str = Field(min_length=1)
    target: Target
    indicator_id: str = Field(min_length=1)
    direction: EvidenceDirection
    strength: EvidenceStrength
    context: str = Field(min_length=1)
    alternative_reading: str | None
    ref: EvidenceRef


class PacketEvidence(ScoringModel):
    """规则完成引用校验与来源路由后，交给定级模型的证据。"""

    evidence: CodedEvidence
    role: EvidenceRole

    @field_validator("role")
    @classmethod
    def require_usable_role(cls, value: EvidenceRole) -> EvidenceRole:
        if value in {EvidenceRole.excluded, EvidenceRole.opportunity_only}:
            raise ValueError("DimensionPacket 不得包含被排除或仅用于机会核对的证据")
        return value


class CounterCheck(ScoringModel):
    target: Target
    searched_unit_ids: list[str]
    found: list[CodedEvidence]
    not_found_note: str | None


class BottomLineCandidate(ScoringModel):
    category: SemanticBottomLineCategory
    conflict_id: str | None = None
    refs: list[EvidenceRef] = Field(min_length=1)
    context: str = Field(min_length=1)
    repair_observed: bool
    reasoning: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_conflict_for_fabricated_record(self) -> Self:
        if (
            self.category is SemanticBottomLineCategory.fabricated_record
            and not self.conflict_id
        ):
            raise ValueError("fabricated_record 候选必须提供 conflict_id（材料冲突编号）")
        return self


class UrgentRiskDisclosureCandidate(ScoringModel):
    ref: DialogueRef
    category: RiskDisclosureCategory
    immediacy: RiskDisclosureImmediacy


class OpportunityOutcome(ScoringModel):
    declared_target: Target
    kind: OpportunityKind
    fulfilled: bool
    indicator_ids: list[str]
    complex_opportunity: bool = False


class DimensionPacket(ScoringModel):
    scene: Scene
    media: Media
    target: Target
    rubric: CoreRubric | ModuleRubric
    evidence: list[PacketEvidence]
    counter_evidence: list[PacketEvidence]
    units: list[MeaningUnit]
    opportunities: list[OpportunityOutcome]
    conditional_unavailable: list[str]
    level_ceiling: int = Field(ge=0, le=4)

    @model_validator(mode="after")
    def ensure_target_consistency(self) -> Self:
        if self.rubric.id != self.target:
            raise ValueError("rubric target 与 DimensionPacket target 不一致")
        indicator_ids = {indicator.id for indicator in self.rubric.indicators}
        unit_ids = {unit.id for unit in self.units}
        if len(unit_ids) != len(self.units):
            raise ValueError("MeaningUnit id 必须唯一")
        for packet_evidence in [*self.evidence, *self.counter_evidence]:
            evidence = packet_evidence.evidence
            if evidence.target != self.target:
                raise ValueError("evidence target 与 DimensionPacket target 不一致")
            if evidence.indicator_id not in indicator_ids:
                raise ValueError("evidence indicator_id 不属于 target")
            if evidence.unit_id not in unit_ids:
                raise ValueError("evidence unit_id 不在 DimensionPacket units 中")
        for opportunity in self.opportunities:
            if opportunity.declared_target != self.target:
                raise ValueError("opportunity target 与 DimensionPacket target 不一致")
            if not set(opportunity.indicator_ids).issubset(indicator_ids):
                raise ValueError("opportunity indicator_ids 不属于 target")
        if not set(self.conditional_unavailable).issubset(self.rubric.conditional_in_level3):
            raise ValueError("conditional_unavailable 不属于量规条件行为")
        return self


class LevelProposal(ScoringModel):
    target: Target
    proposed_level: int | None = Field(default=None, ge=0, le=4)
    pattern: str
    rationale: str
    representative_units: list[str]
    limiting_units: list[str]
    next_level_gap: list[str]
    evidence_confidence: EvidenceConfidence
    evidence_confidence_factors: list[str]


class BottomLineEvent(ScoringModel):
    id: str = Field(min_length=1)
    category: BottomLineCategory
    detection: BottomLineDetection
    refs: list[EvidenceRef] = Field(min_length=1)
    description: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    repair_observed: bool | None = None


class MaterialConflict(ScoringModel):
    id: str = Field(min_length=1)
    dialogue_ref: DialogueRef | None
    work_record_ref: WorkRecordRef | None
    description: str = Field(min_length=1)
    affected_targets: list[Target] = Field(min_length=1)
    impact: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_material_ref(self) -> Self:
        if self.dialogue_ref is None and self.work_record_ref is None:
            raise ValueError("材料冲突必须引用对话或工作记录")
        return self


class DimensionResult(ScoringModel):
    target: Target
    level: int | None = Field(default=None, ge=0, le=4)
    unscored_reason: UnscoredReason | None
    analysis_outcome: AnalysisOutcome
    opportunities: list[OpportunityOutcome] = Field(default_factory=list)
    indicator_states: dict[str, IndicatorStatus]
    pattern: str
    rationale: str
    evidence: list[CodedEvidence]
    counter_evidence: list[CodedEvidence]
    representative_unit_ids: list[str]
    limiting_unit_ids: list[str]
    conditional_unavailable: list[str]
    caps_applied: list[LevelCapReason]
    evidence_confidence: EvidenceConfidence | None
    evidence_confidence_factors: list[str]
    next_level_gap: list[str]

    @model_validator(mode="after")
    def separate_failure_and_unscored(self) -> Self:
        if self.analysis_outcome is AnalysisOutcome.analysis_failed:
            if self.level is not None or self.unscored_reason is not None:
                raise ValueError("analysis_failed 时不得填写 level 或 unscored_reason")
            failed_conclusions = (
                self.indicator_states,
                self.pattern,
                self.rationale,
                self.evidence,
                self.counter_evidence,
                self.representative_unit_ids,
                self.limiting_unit_ids,
                self.caps_applied,
                self.evidence_confidence_factors,
                self.next_level_gap,
            )
            if any(failed_conclusions) or self.evidence_confidence is not None:
                raise ValueError("analysis_failed 时所有分析结论字段必须为空")
            return self
        if (self.level is None) == (self.unscored_reason is None):
            raise ValueError("分析成功后 level 与 unscored_reason 必须且只能填写一项")
        if self.unscored_reason is not None:
            unscored_conclusions = (
                self.pattern,
                self.representative_unit_ids,
                self.limiting_unit_ids,
                self.caps_applied,
                self.evidence_confidence_factors,
                self.next_level_gap,
            )
            if any(unscored_conclusions) or self.evidence_confidence is not None:
                raise ValueError("未评分维度不得携带等级性结论")
        else:
            if self.evidence_confidence is None:
                raise ValueError("已评分维度必须填写 evidence_confidence")
            if not (
                self.indicator_states
                and self.pattern.strip()
                and self.rationale.strip()
                and self.evidence
                and self.representative_unit_ids
            ):
                raise ValueError("已评分维度不得缺少指标状态、结论、证据或代表单元")
        return self


class ResultSummary(ScoringModel):
    scored_core_count: int = Field(ge=0, le=9)
    unscored: list[tuple[CoreDimension, UnscoredReason]]
    analysis_failed: list[Target]
    activated_modules: list[SpecialModule]
    inactive_modules: list[tuple[SpecialModule, str]]
    bottom_line_events: list[BottomLineEvent]
    screening_gap: bool
    level_distribution: str = Field(min_length=1)
    next_behaviors: list[str]

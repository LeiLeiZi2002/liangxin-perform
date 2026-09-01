from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations
from types import MappingProxyType

from app.reports.competency_rubric import get_rubric
from app.reports.job_inputs import WorkRecordSnapshotInput
from app.reports.scoring_domain import (
    AnalysisOutcome,
    AudioEventRef,
    BottomLineCandidate,
    BottomLineCategory,
    BottomLineDetection,
    BottomLineEvent,
    CodedEvidence,
    CoreDimension,
    CounterCheck,
    DialogueRef,
    DimensionPacket,
    DimensionResult,
    EvidenceConfidence,
    EvidenceDirection,
    EvidenceRef,
    EvidenceRejectionReason,
    EvidenceRole,
    IndicatorStatus,
    LevelCapReason,
    LevelProposal,
    MaterialConflict,
    MaterialKind,
    MeaningUnit,
    ModuleRubric,
    ResultSummary,
    SemanticBottomLineCategory,
    SpecialModule,
    Target,
    UnscoredReason,
    UrgentRiskDisclosureCandidate,
    WorkRecordField,
    WorkRecordRef,
)


def _routing(
    dialogue: EvidenceRole,
    audio: EvidenceRole,
    work_record: EvidenceRole,
) -> Mapping[MaterialKind, EvidenceRole]:
    return MappingProxyType(
        {
            MaterialKind.dialogue: dialogue,
            MaterialKind.audio: audio,
            MaterialKind.work_record: work_record,
            MaterialKind.opportunity: EvidenceRole.opportunity_only,
        }
    )


P = EvidenceRole.primary
S = EvidenceRole.supporting
C = EvidenceRole.cross_check
X = EvidenceRole.excluded

_EVIDENCE_ROUTING: dict[Target, Mapping[MaterialKind, EvidenceRole]] = {
    CoreDimension.respectful_communication: _routing(P, S, X),
    CoreDimension.listening_and_emotion: _routing(P, S, C),
    CoreDimension.concern_clarification: _routing(P, X, C),
    CoreDimension.integration_and_judgment: _routing(P, S, P),
    CoreDimension.supportive_intervention: _routing(P, S, C),
    CoreDimension.voice_and_process: _routing(P, P, X),
    CoreDimension.boundary_and_ethics: _routing(P, X, P),
    CoreDimension.closure_and_followup: _routing(P, S, C),
    CoreDimension.documentation: _routing(C, C, P),
    SpecialModule.basic_risk_screening: _routing(P, S, C),
    SpecialModule.full_risk_appraisal: _routing(P, S, P),
    SpecialModule.safety_response: _routing(P, S, P),
    SpecialModule.emotional_dysregulation: _routing(P, P, X),
    SpecialModule.psychotic_experience: _routing(P, S, C),
    SpecialModule.dependency_and_boundary: _routing(P, S, C),
    SpecialModule.aggression_and_harassment: _routing(P, S, C),
    SpecialModule.third_party_call: _routing(P, X, P),
    SpecialModule.minor_protection: _routing(P, S, P),
}
EVIDENCE_ROUTING: Mapping[Target, Mapping[MaterialKind, EvidenceRole]] = MappingProxyType(
    _EVIDENCE_ROUTING
)


@dataclass(frozen=True, slots=True)
class RoutedEvidence:
    evidence: CodedEvidence
    role: EvidenceRole


@dataclass(frozen=True, slots=True)
class EvidenceRejection:
    evidence: CodedEvidence
    reason: EvidenceRejectionReason
    detail: str


@dataclass(frozen=True, slots=True)
class ReferenceRejection:
    ref: EvidenceRef
    reason: EvidenceRejectionReason
    detail: str


@dataclass(frozen=True, slots=True)
class BottomLineReferenceRejection:
    candidate: BottomLineCandidate
    ref: EvidenceRef
    reason: EvidenceRejectionReason
    detail: str


@dataclass(frozen=True, slots=True)
class SemanticBottomLineResult:
    events: list[BottomLineEvent]
    rejected: list[BottomLineReferenceRejection]


@dataclass(frozen=True, slots=True)
class UrgentRiskTerminationResult:
    event: BottomLineEvent | None
    rejected: list[ReferenceRejection]


@dataclass(frozen=True, slots=True)
class EvidenceValidationResult:
    accepted: list[RoutedEvidence]
    rejected: list[EvidenceRejection]
    analysis_outcome: AnalysisOutcome


@dataclass(frozen=True, slots=True)
class CounterCheckValidation:
    complete: bool
    reasons: list[str]
    evidence_rejections: list[EvidenceRejection]


@dataclass(frozen=True, slots=True)
class ScoringDisposition:
    analysis_outcome: AnalysisOutcome
    unscored_reason: UnscoredReason | None
    level: None = None


class EvidenceSufficiencyExemption(StrEnum):
    bottom_line_event = "bottom_line_event"
    single_declared_opportunity = "single_declared_opportunity"
    interruption_unique_opportunity = "interruption_unique_opportunity"
    closure_event = "closure_event"


@dataclass(frozen=True, slots=True)
class EvidenceSufficiency:
    sufficient: bool
    exemption: EvidenceSufficiencyExemption | None
    confidence_ceiling: EvidenceConfidence | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LevelDecision:
    level: int | None
    caps_applied: list[LevelCapReason]
    next_level_gap: list[str]


@dataclass(frozen=True, slots=True)
class LanguageViolation:
    field: str
    term: str
    text: str


def _source_kind(ref: DialogueRef | WorkRecordRef | AudioEventRef) -> MaterialKind:
    if isinstance(ref, DialogueRef):
        return MaterialKind.dialogue
    if isinstance(ref, WorkRecordRef):
        return MaterialKind.work_record
    return MaterialKind.audio


_WORK_RECORD_FIELDS: tuple[WorkRecordField, ...] = (
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
)


def _canonical_fragments(value: object) -> tuple[str, ...]:
    if isinstance(value, bool):
        return ("是" if value else "否",)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, StrEnum):
        return (value.value,)
    if isinstance(value, (list, tuple)):
        return tuple(fragment for item in value for fragment in _canonical_fragments(item))
    return ()


def canonical_work_record_fragments(
    work_record: WorkRecordSnapshotInput,
) -> dict[WorkRecordField, tuple[str, ...]]:
    return {
        field: _canonical_fragments(getattr(work_record, field)) for field in _WORK_RECORD_FIELDS
    }


def _validate_base_reference(
    ref: EvidenceRef,
    *,
    dialogue_turns: Mapping[str, str],
    work_record: WorkRecordSnapshotInput | None,
    audio_event_ids: Set[str],
) -> ReferenceRejection | None:
    if isinstance(ref, DialogueRef):
        text = dialogue_turns.get(ref.turn_id)
        if text is None:
            return ReferenceRejection(
                ref=ref,
                reason=EvidenceRejectionReason.dialogue_turn_missing,
                detail=f"对话话轮 {ref.turn_id} 不存在",
            )
        if ref.quote not in text:
            return ReferenceRejection(
                ref=ref,
                reason=EvidenceRejectionReason.quote_not_contiguous,
                detail=f"引用不是话轮 {ref.turn_id} 原文的连续子串",
            )
        return None
    if isinstance(ref, WorkRecordRef):
        if work_record is None:
            return ReferenceRejection(
                ref=ref,
                reason=EvidenceRejectionReason.work_record_field_invalid,
                detail="本次没有可供引用的工作记录快照",
            )
        fragments = canonical_work_record_fragments(work_record).get(ref.field)
        if fragments is None:
            return ReferenceRejection(
                ref=ref,
                reason=EvidenceRejectionReason.work_record_field_invalid,
                detail=f"工作记录字段 {ref.field} 不属于评分业务字段",
            )
        if not any(ref.quote in fragment for fragment in fragments):
            return ReferenceRejection(
                ref=ref,
                reason=EvidenceRejectionReason.quote_not_contiguous,
                detail=f"引用不是工作记录字段 {ref.field} 规范文本的连续子串",
            )
        return None
    if ref.event_id not in audio_event_ids:
        return ReferenceRejection(
            ref=ref,
            reason=EvidenceRejectionReason.audio_event_missing,
            detail=f"音频事件 {ref.event_id} 不存在",
        )
    return None


def _reject(
    evidence: CodedEvidence,
    reason: EvidenceRejectionReason,
    detail: str,
) -> EvidenceRejection:
    return EvidenceRejection(evidence=evidence, reason=reason, detail=detail)


def _validate_one_reference(
    evidence: CodedEvidence,
    *,
    target: Target,
    units_by_id: Mapping[str, MeaningUnit],
    dialogue_turns: Mapping[str, str],
    work_record: WorkRecordSnapshotInput | None,
    audio_event_ids: Set[str],
) -> EvidenceRejection | RoutedEvidence:
    if evidence.target != target:
        return _reject(
            evidence,
            EvidenceRejectionReason.target_mismatch,
            f"证据 target={evidence.target.value}，预期 {target.value}",
        )
    rubric = get_rubric(target)
    indicator_ids = {indicator.id for indicator in rubric.indicators}
    if evidence.indicator_id not in indicator_ids:
        return _reject(
            evidence,
            EvidenceRejectionReason.indicator_not_in_target,
            f"指标 {evidence.indicator_id} 不属于 {target.value}",
        )
    unit = units_by_id.get(evidence.unit_id)
    if unit is None:
        return _reject(
            evidence,
            EvidenceRejectionReason.unit_missing,
            f"意义单元 {evidence.unit_id} 不存在",
        )

    ref = evidence.ref
    base_rejection = _validate_base_reference(
        ref,
        dialogue_turns=dialogue_turns,
        work_record=work_record,
        audio_event_ids=audio_event_ids,
    )
    if base_rejection is not None:
        return _reject(evidence, base_rejection.reason, base_rejection.detail)

    if isinstance(ref, DialogueRef):
        if ref.turn_id not in unit.turn_ids:
            return _reject(
                evidence,
                EvidenceRejectionReason.source_not_in_unit,
                f"话轮 {ref.turn_id} 不属于意义单元 {unit.id}",
            )
    elif isinstance(ref, WorkRecordRef):
        if ref not in unit.work_record_refs:
            return _reject(
                evidence,
                EvidenceRejectionReason.source_not_in_unit,
                f"工作记录引用不属于意义单元 {unit.id}",
            )
    else:
        if ref.event_id not in unit.audio_event_ids:
            return _reject(
                evidence,
                EvidenceRejectionReason.source_not_in_unit,
                f"音频事件 {ref.event_id} 不属于意义单元 {unit.id}",
            )

    role = EVIDENCE_ROUTING[target][_source_kind(ref)]
    if role is EvidenceRole.excluded:
        return _reject(
            evidence,
            EvidenceRejectionReason.source_excluded,
            f"{target.value} 不接收 {_source_kind(ref).value} 来源",
        )
    if role is EvidenceRole.opportunity_only:
        return _reject(
            evidence,
            EvidenceRejectionReason.source_opportunity_only,
            "机会材料只能核对机会，不能作为能力证据",
        )
    return RoutedEvidence(evidence=evidence, role=role)


def validate_evidence(
    *,
    target: Target,
    submitted: Sequence[CodedEvidence],
    meaning_units: Sequence[MeaningUnit],
    dialogue_turns: Mapping[str, str],
    work_record: WorkRecordSnapshotInput | None,
    audio_event_ids: Set[str],
) -> EvidenceValidationResult:
    units_by_id = {unit.id: unit for unit in meaning_units}
    accepted: list[RoutedEvidence] = []
    rejected: list[EvidenceRejection] = []
    for evidence in submitted:
        result = _validate_one_reference(
            evidence,
            target=target,
            units_by_id=units_by_id,
            dialogue_turns=dialogue_turns,
            work_record=work_record,
            audio_event_ids=audio_event_ids,
        )
        if isinstance(result, EvidenceRejection):
            rejected.append(result)
        else:
            accepted.append(result)
    outcome = AnalysisOutcome.analysis_failed if submitted and not accepted else AnalysisOutcome.ok
    return EvidenceValidationResult(
        accepted=accepted,
        rejected=rejected,
        analysis_outcome=outcome,
    )


def select_positive_evidence(evidence: Sequence[RoutedEvidence]) -> list[RoutedEvidence]:
    primary = [
        item
        for item in evidence
        if item.role is EvidenceRole.primary
        and item.evidence.direction is EvidenceDirection.support
    ]
    if not primary:
        return []
    supporting = [
        item
        for item in evidence
        if item.role is EvidenceRole.supporting
        and item.evidence.direction is EvidenceDirection.support
    ]
    return [*primary, *supporting]


def _select_gradable_evidence(evidence: Sequence[RoutedEvidence]) -> list[RoutedEvidence]:
    primary = [item for item in evidence if item.role is EvidenceRole.primary]
    if not primary:
        return []
    supporting = [item for item in evidence if item.role is EvidenceRole.supporting]
    return [*primary, *supporting]


def validate_counter_check(
    check: CounterCheck,
    meaning_units: Sequence[MeaningUnit],
    *,
    dialogue_turns: Mapping[str, str],
    work_record: WorkRecordSnapshotInput | None,
    audio_event_ids: Set[str],
) -> CounterCheckValidation:
    actual_ids = {unit.id for unit in meaning_units}
    searched_ids = set(check.searched_unit_ids)
    reasons: list[str] = []
    if not check.searched_unit_ids:
        reasons.append("未提交实际检索的意义单元")
    unknown = searched_ids - actual_ids
    if unknown:
        reasons.append(f"检索了不存在的意义单元：{', '.join(sorted(unknown))}")
    if not check.found and not (check.not_found_note and check.not_found_note.strip()):
        reasons.append("未发现反例时必须说明检索范围和判断依据")
    for evidence in check.found:
        if evidence.unit_id not in searched_ids:
            reasons.append(f"反例证据 {evidence.unit_id} 不在检索痕迹中")
    evidence_validation = validate_evidence(
        target=check.target,
        submitted=check.found,
        meaning_units=meaning_units,
        dialogue_turns=dialogue_turns,
        work_record=work_record,
        audio_event_ids=audio_event_ids,
    )
    for rejection in evidence_validation.rejected:
        reasons.append(
            f"反例证据 {rejection.evidence.unit_id} 非法："
            f"{rejection.reason.value}（{rejection.detail}）"
        )
    return CounterCheckValidation(
        complete=not reasons,
        reasons=reasons,
        evidence_rejections=evidence_validation.rejected,
    )


def resolve_scoring_disposition(
    *,
    analysis_outcome: AnalysisOutcome,
    technical_failure: bool,
    has_opportunity: bool,
    evidence_sufficient: bool,
) -> ScoringDisposition:
    if analysis_outcome is AnalysisOutcome.analysis_failed:
        return ScoringDisposition(
            analysis_outcome=AnalysisOutcome.analysis_failed,
            unscored_reason=None,
        )
    if not has_opportunity:
        reason = UnscoredReason.no_opportunity
    elif evidence_sufficient:
        reason = None
    elif technical_failure:
        reason = UnscoredReason.technical_failure
    else:
        reason = UnscoredReason.insufficient_evidence
    return ScoringDisposition(
        analysis_outcome=AnalysisOutcome.ok,
        unscored_reason=reason,
    )


def _unit_source_keys(unit: MeaningUnit) -> set[str]:
    keys = {f"dialogue:{turn_id}" for turn_id in unit.turn_ids}
    keys.update(f"work_record:{ref.field}:{ref.quote}" for ref in unit.work_record_refs)
    keys.update(f"audio:{event_id}" for event_id in unit.audio_event_ids)
    return keys


def assess_evidence_sufficiency(
    *,
    target: Target,
    representative_unit_ids: Sequence[str],
    units: Sequence[MeaningUnit],
    evidence: Sequence[RoutedEvidence],
    declared_opportunity_count: int,
    bottom_line_triggered: bool = False,
    unique_due_to_interruption: bool = False,
) -> EvidenceSufficiency:
    units_by_id = {unit.id: unit for unit in units}
    representative_ids = list(dict.fromkeys(representative_unit_ids))
    if not representative_ids:
        return EvidenceSufficiency(
            sufficient=False,
            exemption=None,
            reason="未提供代表意义单元",
        )
    unknown = [unit_id for unit_id in representative_ids if unit_id not in units_by_id]
    if unknown:
        return EvidenceSufficiency(
            sufficient=False,
            exemption=None,
            reason=f"代表意义单元不存在：{', '.join(unknown)}",
        )
    mismatched = [item for item in evidence if item.evidence.target != target]
    if mismatched:
        return EvidenceSufficiency(
            sufficient=False,
            exemption=None,
            reason="已路由证据 target 与充分性目标不一致",
        )
    indicator_ids = {indicator.id for indicator in get_rubric(target).indicators}
    invalid_indicators = [
        item.evidence.indicator_id
        for item in evidence
        if item.evidence.indicator_id not in indicator_ids
    ]
    if invalid_indicators:
        return EvidenceSufficiency(
            sufficient=False,
            exemption=None,
            reason="已路由证据包含不属于目标的指标",
        )
    invalid_routes = [
        item
        for item in evidence
        if item.role is not EVIDENCE_ROUTING[target][_source_kind(item.evidence.ref)]
        or item.role in {EvidenceRole.excluded, EvidenceRole.opportunity_only}
    ]
    if invalid_routes:
        return EvidenceSufficiency(
            sufficient=False,
            exemption=None,
            reason="证据路由角色无效，不能用于充分性判断",
        )
    gradable = _select_gradable_evidence(evidence)
    carrier_ids = {item.evidence.unit_id for item in gradable}
    bare = [unit_id for unit_id in representative_ids if unit_id not in carrier_ids]
    if bare:
        return EvidenceSufficiency(
            sufficient=False,
            exemption=None,
            reason=f"代表意义单元没有有效定级证据：{', '.join(bare)}",
        )
    selected = [units_by_id[unit_id] for unit_id in representative_ids]
    if bottom_line_triggered:
        return EvidenceSufficiency(
            sufficient=True,
            exemption=EvidenceSufficiencyExemption.bottom_line_event,
        )
    if declared_opportunity_count == 1:
        return EvidenceSufficiency(
            sufficient=True,
            exemption=EvidenceSufficiencyExemption.single_declared_opportunity,
            confidence_ceiling=EvidenceConfidence.medium,
        )
    if unique_due_to_interruption:
        return EvidenceSufficiency(
            sufficient=True,
            exemption=EvidenceSufficiencyExemption.interruption_unique_opportunity,
            confidence_ceiling=EvidenceConfidence.medium,
        )
    if target is CoreDimension.closure_and_followup:
        return EvidenceSufficiency(
            sufficient=True,
            exemption=EvidenceSufficiencyExemption.closure_event,
        )
    independent = any(
        not (_unit_source_keys(first) & _unit_source_keys(second))
        for first, second in combinations(selected, 2)
    )
    return EvidenceSufficiency(
        sufficient=independent,
        exemption=None,
        reason=None if independent else "不足两个相互独立的有效证据片段",
    )


def apply_evidence_confidence_ceiling(
    proposed: EvidenceConfidence,
    sufficiency: EvidenceSufficiency,
) -> EvidenceConfidence:
    ceiling = sufficiency.confidence_ceiling
    if ceiling is None:
        return proposed
    confidence_order = {
        EvidenceConfidence.low: 0,
        EvidenceConfidence.medium: 1,
        EvidenceConfidence.high: 2,
    }
    return proposed if confidence_order[proposed] <= confidence_order[ceiling] else ceiling


def calculate_level_ceiling(
    *,
    evidence: Sequence[RoutedEvidence],
    counter_evidence: Sequence[RoutedEvidence] = (),
    conditional_unavailable: Sequence[str],
    has_complex_opportunity: bool,
) -> int:
    ceiling = 4
    if any(
        item.evidence.direction is EvidenceDirection.adverse
        for item in [*evidence, *counter_evidence]
    ):
        ceiling = min(ceiling, 2)
    if conditional_unavailable:
        ceiling = min(ceiling, 3)
    if not has_complex_opportunity:
        ceiling = min(ceiling, 3)
    return ceiling


def apply_level_caps(
    proposal: LevelProposal,
    *,
    evidence: Sequence[RoutedEvidence],
    counter_evidence: Sequence[RoutedEvidence] = (),
    conditional_unavailable: Sequence[str],
    has_complex_opportunity: bool,
) -> LevelDecision:
    mismatched_targets = {
        item.evidence.target for item in evidence if item.evidence.target != proposal.target
    }
    if mismatched_targets:
        actual = ", ".join(sorted(target.value for target in mismatched_targets))
        raise ValueError(f"证据 target={actual} 与 proposal target={proposal.target.value} 不一致")
    caps: list[LevelCapReason] = []
    if any(
        item.evidence.direction is EvidenceDirection.adverse
        for item in [*evidence, *counter_evidence]
    ):
        caps.append(LevelCapReason.adverse_evidence)
    if conditional_unavailable:
        caps.append(LevelCapReason.conditional_opportunity_unavailable)
    if not has_complex_opportunity:
        caps.append(LevelCapReason.no_complex_opportunity)
    ceiling = calculate_level_ceiling(
        evidence=evidence,
        counter_evidence=counter_evidence,
        conditional_unavailable=conditional_unavailable,
        has_complex_opportunity=has_complex_opportunity,
    )

    level = None if proposal.proposed_level is None else min(proposal.proposed_level, ceiling)
    return LevelDecision(
        level=level,
        caps_applied=caps,
        next_level_gap=list(proposal.next_level_gap),
    )


def _validate_routed_packet_evidence(
    *,
    packet: DimensionPacket,
    evidence: Sequence[RoutedEvidence],
    submitted: Sequence[CodedEvidence],
    label: str,
) -> None:
    indicator_ids = {indicator.id for indicator in packet.rubric.indicators}
    for item in evidence:
        coded = item.evidence
        if coded.target != packet.target:
            raise ValueError(f"{label} evidence target 与 packet target 不一致")
        if coded.indicator_id not in indicator_ids:
            raise ValueError(f"{label} evidence indicator_id 不属于 packet target")
        if coded not in submitted:
            raise ValueError(f"{label} evidence 不在 packet 提交材料中")
        expected_role = EVIDENCE_ROUTING[packet.target][_source_kind(coded.ref)]
        if item.role is not expected_role or item.role in {
            EvidenceRole.excluded,
            EvidenceRole.opportunity_only,
        }:
            raise ValueError(f"{label} evidence 路由角色无效")


def assemble_dimension_result(
    packet: DimensionPacket,
    proposal: LevelProposal | None,
    *,
    evidence: Sequence[RoutedEvidence],
    counter_evidence: Sequence[RoutedEvidence],
    indicator_states: Mapping[str, IndicatorStatus],
    disposition: ScoringDisposition,
    sufficiency: EvidenceSufficiency | None = None,
    has_complex_opportunity: bool = False,
) -> DimensionResult:
    if disposition.analysis_outcome is AnalysisOutcome.analysis_failed:
        return DimensionResult(
            target=packet.target,
            level=None,
            unscored_reason=None,
            analysis_outcome=AnalysisOutcome.analysis_failed,
            opportunities=list(packet.opportunities),
            indicator_states={},
            pattern="",
            rationale="",
            evidence=[],
            counter_evidence=[],
            representative_unit_ids=[],
            limiting_unit_ids=[],
            conditional_unavailable=[],
            caps_applied=[],
            evidence_confidence=None,
            evidence_confidence_factors=[],
            next_level_gap=[],
        )
    if proposal is not None and proposal.target != packet.target:
        raise ValueError("proposal target 与 packet target 不一致")
    calculated_ceiling = calculate_level_ceiling(
        evidence=evidence,
        counter_evidence=counter_evidence,
        conditional_unavailable=packet.conditional_unavailable,
        has_complex_opportunity=has_complex_opportunity,
    )
    if packet.level_ceiling != calculated_ceiling:
        raise ValueError("packet level_ceiling 与规则复核结果不一致")
    if (
        proposal is not None
        and proposal.proposed_level is not None
        and proposal.proposed_level > packet.level_ceiling
    ):
        raise ValueError("proposal proposed_level 超过 packet level_ceiling")

    _validate_routed_packet_evidence(
        packet=packet,
        evidence=evidence,
        submitted=[item.evidence for item in packet.evidence],
        label="正向",
    )
    _validate_routed_packet_evidence(
        packet=packet,
        evidence=counter_evidence,
        submitted=[item.evidence for item in packet.counter_evidence],
        label="反例",
    )
    indicator_ids = {indicator.id for indicator in packet.rubric.indicators}
    unknown_indicator_states = set(indicator_states) - indicator_ids
    if unknown_indicator_states:
        unknown_labels = ", ".join(sorted(unknown_indicator_states))
        raise ValueError(f"indicator_states 包含不属于 target 的指标：{unknown_labels}")

    coded_evidence = [item.evidence for item in evidence]
    coded_counter_evidence = [item.evidence for item in counter_evidence]
    if disposition.unscored_reason is not None:
        return DimensionResult(
            target=packet.target,
            level=None,
            unscored_reason=disposition.unscored_reason,
            analysis_outcome=AnalysisOutcome.ok,
            opportunities=list(packet.opportunities),
            indicator_states=dict(indicator_states),
            pattern="",
            rationale=proposal.rationale.strip() if proposal is not None else "",
            evidence=coded_evidence,
            counter_evidence=coded_counter_evidence,
            representative_unit_ids=[],
            limiting_unit_ids=[],
            conditional_unavailable=list(packet.conditional_unavailable),
            caps_applied=[],
            evidence_confidence=None,
            evidence_confidence_factors=[],
            next_level_gap=[],
        )

    if proposal is None:
        raise ValueError("已评分结果必须提供 proposal")
    if proposal.proposed_level is None:
        raise ValueError("已评分 proposal 必须提供 proposed_level")
    if sufficiency is None:
        raise ValueError("已评分结果必须提供证据充分性结论")
    if not sufficiency.sufficient:
        raise ValueError("证据充分性不足时不得组装已评分结果")
    if not (
        proposal.pattern.strip()
        and proposal.rationale.strip()
        and proposal.representative_units
        and proposal.evidence_confidence_factors
    ):
        raise ValueError("已评分 proposal 必要字段不能为空")

    units_by_id = {unit.id: unit for unit in packet.units}
    unknown_representative = set(proposal.representative_units) - units_by_id.keys()
    if unknown_representative:
        raise ValueError(f"代表单元不存在：{', '.join(sorted(unknown_representative))}")
    unknown_limiting = set(proposal.limiting_units) - units_by_id.keys()
    if unknown_limiting:
        raise ValueError(f"限制单元不存在：{', '.join(sorted(unknown_limiting))}")

    gradable = _select_gradable_evidence(evidence)
    representative_carriers = {item.evidence.unit_id for item in gradable}
    empty_representative = set(proposal.representative_units) - representative_carriers
    if empty_representative:
        raise ValueError(f"代表单元没有有效定级证据：{', '.join(sorted(empty_representative))}")
    all_evidence_carriers = {item.evidence.unit_id for item in [*evidence, *counter_evidence]}
    empty_limiting = set(proposal.limiting_units) - all_evidence_carriers
    if empty_limiting:
        raise ValueError(f"限制单元没有有效证据：{', '.join(sorted(empty_limiting))}")
    if not indicator_states:
        raise ValueError("已评分结果必须包含 indicator_states")

    decision = apply_level_caps(
        proposal,
        evidence=evidence,
        counter_evidence=counter_evidence,
        conditional_unavailable=packet.conditional_unavailable,
        has_complex_opportunity=has_complex_opportunity,
    )
    confidence = apply_evidence_confidence_ceiling(
        proposal.evidence_confidence,
        sufficiency,
    )
    return DimensionResult(
        target=packet.target,
        level=decision.level,
        unscored_reason=None,
        analysis_outcome=AnalysisOutcome.ok,
        opportunities=list(packet.opportunities),
        indicator_states=dict(indicator_states),
        pattern=proposal.pattern.strip(),
        rationale=proposal.rationale.strip(),
        evidence=coded_evidence,
        counter_evidence=coded_counter_evidence,
        representative_unit_ids=list(dict.fromkeys(proposal.representative_units)),
        limiting_unit_ids=list(dict.fromkeys(proposal.limiting_units)),
        conditional_unavailable=list(packet.conditional_unavailable),
        caps_applied=decision.caps_applied,
        evidence_confidence=confidence,
        evidence_confidence_factors=list(proposal.evidence_confidence_factors),
        next_level_gap=decision.next_level_gap,
    )


def semantic_bottom_line_events(
    candidates: Sequence[BottomLineCandidate],
    *,
    dialogue_turns: Mapping[str, str],
    work_record: WorkRecordSnapshotInput | None,
    audio_event_ids: Set[str],
    rule_conflicts: Sequence[MaterialConflict] | None,
    semantic_conflicts: Sequence[MaterialConflict] | None,
) -> SemanticBottomLineResult:
    events: list[BottomLineEvent] = []
    rejected: list[BottomLineReferenceRejection] = []
    for candidate in candidates:
        reference_rejections = [
            rejection
            for ref in candidate.refs
            if (
                rejection := _validate_base_reference(
                    ref,
                    dialogue_turns=dialogue_turns,
                    work_record=work_record,
                    audio_event_ids=audio_event_ids,
                )
            )
            is not None
        ]
        if reference_rejections or not candidate.refs:
            rejected.extend(
                BottomLineReferenceRejection(
                    candidate=candidate,
                    ref=rejection.ref,
                    reason=rejection.reason,
                    detail=rejection.detail,
                )
                for rejection in reference_rejections
            )
            continue
        detection = BottomLineDetection.semantic
        if candidate.category is SemanticBottomLineCategory.fabricated_record:
            dialogue_refs = [ref for ref in candidate.refs if isinstance(ref, DialogueRef)]
            work_record_refs = [
                ref for ref in candidate.refs if isinstance(ref, WorkRecordRef)
            ]
            if not dialogue_refs or not work_record_refs:
                rejected.append(
                    BottomLineReferenceRejection(
                        candidate=candidate,
                        ref=candidate.refs[0],
                        reason=EvidenceRejectionReason.semantic_prerequisite_missing,
                        detail="工作记录编造候选必须同时引用对话原文和工作记录原文。",
                    )
                )
                continue
            if rule_conflicts is not None and semantic_conflicts is not None:
                semantic_conflict = next(
                    (
                        conflict
                        for conflict in semantic_conflicts
                        if conflict.id == candidate.conflict_id
                    ),
                    None,
                )
                semantic_record_ref = (
                    semantic_conflict.work_record_ref
                    if semantic_conflict is not None
                    else None
                )
                semantic_dialogue_ref = (
                    semantic_conflict.dialogue_ref
                    if semantic_conflict is not None
                    else None
                )
                candidate_matches_semantic = (
                    semantic_record_ref is not None
                    and semantic_dialogue_ref is not None
                    and any(
                        ref.field == semantic_record_ref.field
                        and ref.quote == semantic_record_ref.quote
                        for ref in work_record_refs
                    )
                    and any(
                        ref.turn_id == semantic_dialogue_ref.turn_id
                        for ref in dialogue_refs
                    )
                )
                semantic_matches_rule = (
                    semantic_record_ref is not None
                    and any(
                        conflict.dialogue_ref is not None
                        and conflict.work_record_ref is not None
                        and conflict.work_record_ref.field == semantic_record_ref.field
                        and conflict.work_record_ref.quote == semantic_record_ref.quote
                        for conflict in rule_conflicts
                    )
                )
                if not candidate_matches_semantic or not semantic_matches_rule:
                    rejected.append(
                        BottomLineReferenceRejection(
                            candidate=candidate,
                            ref=work_record_refs[0],
                            reason=EvidenceRejectionReason.semantic_prerequisite_missing,
                            detail=(
                                "工作记录编造候选没有同时匹配规则冲突、材料冲突编号及两侧原文。"
                            ),
                        )
                    )
                    continue
                detection = BottomLineDetection.rule_candidate_semantic_confirmed
        fingerprint_payload = json.dumps(
            candidate.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()[:20]
        events.append(
            BottomLineEvent(
                id=f"semantic-bottom-line-{fingerprint}",
                category=BottomLineCategory(candidate.category.value),
                detection=detection,
                refs=list(candidate.refs),
                description=candidate.context,
                reasoning=candidate.reasoning,
                repair_observed=candidate.repair_observed,
            )
        )
    return SemanticBottomLineResult(events=events, rejected=rejected)


def detect_known_urgent_risk_termination(
    *,
    disclosed_urgent_risk_refs: Sequence[DialogueRef],
    dialogue_turns: Mapping[str, str],
    ordered_turn_ids: Sequence[str],
    worker_turn_ids: Set[str],
    safety_action_refs: Sequence[DialogueRef],
    call_ended: bool,
) -> UrgentRiskTerminationResult:
    valid_disclosures: list[DialogueRef] = []
    valid_safety_actions: list[DialogueRef] = []
    rejected: list[ReferenceRejection] = []
    for ref, destination in (
        *((ref, valid_disclosures) for ref in disclosed_urgent_risk_refs),
        *((ref, valid_safety_actions) for ref in safety_action_refs),
    ):
        rejection = _validate_base_reference(
            ref,
            dialogue_turns=dialogue_turns,
            work_record=None,
            audio_event_ids=set(),
        )
        if rejection is None:
            destination.append(ref)
        else:
            rejected.append(rejection)

    if not call_ended or not valid_disclosures:
        return UrgentRiskTerminationResult(event=None, rejected=rejected)
    turn_positions = {turn_id: index for index, turn_id in enumerate(ordered_turn_ids)}
    ordered_disclosures = [ref for ref in valid_disclosures if ref.turn_id in turn_positions]
    if not ordered_disclosures:
        return UrgentRiskTerminationResult(event=None, rejected=rejected)
    first_disclosure = min(turn_positions[ref.turn_id] for ref in ordered_disclosures)
    has_later_opportunity = any(
        turn_positions.get(turn_id, -1) > first_disclosure for turn_id in worker_turn_ids
    )
    if not has_later_opportunity:
        return UrgentRiskTerminationResult(event=None, rejected=rejected)
    safety_after_disclosure = any(
        turn_positions.get(ref.turn_id, -1) > first_disclosure for ref in valid_safety_actions
    )
    if safety_after_disclosure:
        return UrgentRiskTerminationResult(event=None, rejected=rejected)
    return UrgentRiskTerminationResult(
        event=BottomLineEvent(
            id="known-urgent-risk-ended-without-safety-action",
            category=BottomLineCategory.known_urgent_risk_ended_without_safety_action,
            detection=BottomLineDetection.rule,
            refs=list(ordered_disclosures),
            description="已披露紧迫危险且披露后存在处理机会，但通话结束前未见安全行动。",
            reasoning="判定仅使用向受测者披露的话轮、披露后的受测者话轮及结束前安全行动记录。",
            repair_observed=None,
        ),
        rejected=rejected,
    )


def validate_urgent_risk_disclosure_candidates(
    candidates: Sequence[UrgentRiskDisclosureCandidate],
    *,
    dialogue_turns: Mapping[str, str],
    client_turn_ids: Set[str],
) -> list[UrgentRiskDisclosureCandidate]:
    accepted: list[UrgentRiskDisclosureCandidate] = []
    for candidate in candidates:
        rejection = _validate_base_reference(
            candidate.ref,
            dialogue_turns=dialogue_turns,
            work_record=None,
            audio_event_ids=set(),
        )
        if rejection is not None:
            raise ValueError(f"紧迫危险披露候选引用无效：{rejection.detail}")
        if candidate.ref.turn_id not in client_turn_ids:
            raise ValueError("紧迫危险披露候选必须引用来电者原话")
        accepted.append(candidate)
    return accepted


def make_work_record_mismatch_conflict(
    *,
    conflict_id: str,
    dialogue_ref: DialogueRef | None,
    work_record_ref: WorkRecordRef,
    affected_targets: list[Target],
    description: str,
    impact: str,
) -> MaterialConflict:
    return MaterialConflict(
        id=conflict_id,
        dialogue_ref=dialogue_ref,
        work_record_ref=work_record_ref,
        description=description,
        affected_targets=affected_targets,
        impact=impact,
    )


def validate_material_conflict_candidates(
    candidates: Sequence[MaterialConflict],
    *,
    dialogue_turns: Mapping[str, str],
    work_record: WorkRecordSnapshotInput | None,
) -> list[MaterialConflict]:
    accepted: list[MaterialConflict] = []
    for candidate in candidates:
        if candidate.dialogue_ref is None or candidate.work_record_ref is None:
            raise ValueError("材料冲突必须同时引用对话原文和工作记录原文")
        refs = (candidate.dialogue_ref, candidate.work_record_ref)
        rejections = [
            rejection
            for ref in refs
            if (
                rejection := _validate_base_reference(
                    ref,
                    dialogue_turns=dialogue_turns,
                    work_record=work_record,
                    audio_event_ids=set(),
                )
            )
            is not None
        ]
        if rejections:
            details = "；".join(rejection.detail for rejection in rejections)
            raise ValueError(f"材料冲突引用无效：{details}")
        accepted.append(candidate)
    return accepted


def screening_gap_for(result: DimensionResult) -> bool:
    return (
        result.target is SpecialModule.basic_risk_screening
        and result.analysis_outcome is AnalysisOutcome.ok
        and result.level in {0, 1}
    )


_COUNT_NAMES = ("零个", "一个", "两个", "三个", "四个", "五个", "六个", "七个", "八个", "九个")
_UNSCORED_LABELS = {
    UnscoredReason.no_opportunity: "无观察机会",
    UnscoredReason.insufficient_evidence: "材料不足",
    UnscoredReason.technical_failure: "技术中断",
}


def _count_name(count: int) -> str:
    return _COUNT_NAMES[count] if 0 <= count < len(_COUNT_NAMES) else f"{count}个"


def _level_distribution(results: Sequence[DimensionResult]) -> str:
    core_results = [result for result in results if isinstance(result.target, CoreDimension)]
    scored = [result for result in core_results if result.level is not None]
    segments = [f"九个核心维度中{_count_name(len(scored))}形成等级"]
    levels = [
        (level, sum(result.level == level for result in scored)) for level in range(4, -1, -1)
    ]
    level_parts = [f"{_count_name(count)}为{level}级" for level, count in levels if count]
    if level_parts:
        segments[0] += f"，其中{'、'.join(level_parts)}"
    clauses = [segments[0]]
    for reason in UnscoredReason:
        count = sum(result.unscored_reason is reason for result in core_results)
        if count:
            clauses.append(f"{_count_name(count)}因{_UNSCORED_LABELS[reason]}未评分")
    failed_count = sum(
        result.analysis_outcome is AnalysisOutcome.analysis_failed for result in core_results
    )
    if failed_count:
        clauses.append(f"{_count_name(failed_count)}分析未完成")
    return "；".join(clauses) + "。"


def build_result_summary(
    results: Sequence[DimensionResult],
    *,
    activated_modules: list[SpecialModule],
    inactive_modules: list[tuple[SpecialModule, str]],
    bottom_line_events: list[BottomLineEvent],
    max_next_behaviors: int = 6,
) -> ResultSummary:
    core_targets = [result.target for result in results if isinstance(result.target, CoreDimension)]
    target_counts = Counter(core_targets)
    missing = [target.value for target in CoreDimension if target_counts[target] == 0]
    duplicates = [target.value for target in CoreDimension if target_counts[target] > 1]
    if missing or duplicates:
        problems: list[str] = []
        if missing:
            problems.append(f"缺少核心维度：{', '.join(missing)}")
        if duplicates:
            problems.append(f"重复核心维度：{', '.join(duplicates)}")
        raise ValueError("；".join(problems))

    module_targets = [
        result.target for result in results if isinstance(result.target, SpecialModule)
    ]
    duplicate_module_results = [
        target.value for target, count in Counter(module_targets).items() if count > 1
    ]
    if duplicate_module_results:
        raise ValueError(f"重复专项模块结果：{', '.join(duplicate_module_results)}")
    duplicate_activated = [
        target.value for target, count in Counter(activated_modules).items() if count > 1
    ]
    inactive_targets = [target for target, _ in inactive_modules]
    duplicate_inactive = [
        target.value for target, count in Counter(inactive_targets).items() if count > 1
    ]
    if duplicate_activated or duplicate_inactive:
        raise ValueError("专项模块状态存在重复")
    active_set = set(activated_modules)
    inactive_set = set(inactive_targets)
    default_enabled_modules: set[SpecialModule] = set()
    for module in SpecialModule:
        rubric = get_rubric(module)
        if not isinstance(rubric, ModuleRubric):
            raise TypeError(f"{module.value} 未对应专项模块量规")
        if rubric.default_enabled:
            default_enabled_modules.add(module)
    inactive_defaults = default_enabled_modules - active_set
    if inactive_defaults:
        raise ValueError(
            "量规默认启用模块必须列入 activated_modules："
            + ", ".join(sorted(module.value for module in inactive_defaults))
        )
    missing_default_results = default_enabled_modules - set(module_targets)
    if missing_default_results:
        raise ValueError(
            "量规默认启用模块必须有结果："
            + ", ".join(sorted(module.value for module in missing_default_results))
        )
    overlap = active_set & inactive_set
    if overlap:
        raise ValueError(
            f"专项模块 activated/inactive 重叠：{', '.join(sorted(item.value for item in overlap))}"
        )
    missing_module_states = set(SpecialModule) - active_set - inactive_set
    if missing_module_states:
        raise ValueError(
            "专项模块状态未覆盖全部模块："
            + ", ".join(sorted(item.value for item in missing_module_states))
        )
    if set(module_targets) != active_set:
        raise ValueError("专项模块启用结果与 activated_modules 不一致")

    target_order = {target: index for index, target in enumerate([*CoreDimension, *SpecialModule])}
    ordered_results = sorted(results, key=lambda result: target_order[result.target])
    violations = [
        violation
        for result in ordered_results
        for violation in classification_language_violations(result)
    ]
    if violations:
        locations = ", ".join(f"{violation.field}={violation.term}" for violation in violations)
        raise ValueError(f"模型生成字段包含分类式结论：{locations}")
    core_results = [
        result for result in ordered_results if isinstance(result.target, CoreDimension)
    ]
    unscored: list[tuple[CoreDimension, UnscoredReason]] = []
    for result in core_results:
        if result.unscored_reason is None:
            continue
        if not isinstance(result.target, CoreDimension):
            raise TypeError("core_results 只能包含核心维度")
        unscored.append((result.target, result.unscored_reason))
    analysis_failed = [
        result.target
        for result in ordered_results
        if result.analysis_outcome is AnalysisOutcome.analysis_failed
    ]
    next_behaviors = list(
        dict.fromkeys(
            gap
            for result in ordered_results
            if result.analysis_outcome is AnalysisOutcome.ok and result.level is not None
            for gap in result.next_level_gap
            if gap.strip()
        )
    )[:max_next_behaviors]
    return ResultSummary(
        scored_core_count=sum(result.level is not None for result in core_results),
        unscored=unscored,
        analysis_failed=analysis_failed,
        activated_modules=[module for module in SpecialModule if module in active_set],
        inactive_modules=[
            (module, dict(inactive_modules)[module])
            for module in SpecialModule
            if module in inactive_set
        ],
        bottom_line_events=bottom_line_events,
        screening_gap=any(screening_gap_for(result) for result in ordered_results),
        level_distribution=_level_distribution(ordered_results),
        next_behaviors=next_behaviors,
    )


_RAW_MATERIAL_QUOTE = re.compile(
    r"(?:原话|所说的?|引用(?:的|了)?)(?:是|为|：|:)?\s*"
    r"(?:“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|'[^']*'|\"[^\"]*\")"
)
_QUOTE_CHARACTERS = str.maketrans("", "", "“”‘’「」『』'\"")
_CLASSIFICATION_PATTERNS = (
    re.compile(
        r"(?:该|本)?(?:维度|模块|受测者|整体|能力|表现|结论|结果)"
        r".{0,12}(?:合格|达标|优秀)"
    ),
    re.compile(
        r"(?:判定|认定|评定|评价)"
        r"(?:(?:结果|结论)?(?:为|是|：|:)\s*|\s*)"
        r"(?:合格|通过|达标|优秀)"
        r"(?=$|[，。；！？、：,.!?;:]|并且|并|且)"
    ),
    re.compile(
        r"(?:受测者|该生|本次表现).{0,6}通过(?:了)?"
        r"(?:本次|此次|该次)?(?:考核|测评|评价|评定|审核)"
    ),
    re.compile(
        r"(?:本次|此次|该次)?(?:考核|测评|评价|评定|审核)(?:结果)?"
        r"(?:为|是|判定为|评定为)通过"
    ),
    re.compile(
        r"(?:(?:本次|此次|该次)(?:测评|考核)?|(?:考核|测评|评价|评定|审核))"
        r"(?:表现|结果|结论)(?:为|是|判定为|评定为|：|:)\s*通过"
    ),
    re.compile(r"通过(?:本次|此次|该次)?(?:考核|测评|评价|评定|审核)"),
    re.compile(r"(?:合格|达标|优秀)(?:水平|标准|要求|表现|等级|者)"),
)
_CLASSIFICATION_TERM = re.compile(r"合格|通过|达标|优秀")
_SUBJECT_PASS_PATTERN = re.compile(
    r"(?:整体|该模块|本模块|该维度|本维度)(?:表现|结果|结论)?"
    r"(?:为|是|判定为|评定为|已)?通过(?:了)?"
)
_METHOD_CONTINUATIONS = (
    "分析",
    "观察",
    "询问",
    "澄清",
    "开放式问题",
    "进一步核对",
)


def _has_subject_pass_conclusion(text: str) -> bool:
    for match in _SUBJECT_PASS_PATTERN.finditer(text):
        continuation = text[match.end() :].lstrip()
        if continuation.startswith(_METHOD_CONTINUATIONS):
            continue
        return True
    return False


def classification_language_violations(
    result: LevelProposal | DimensionResult,
) -> list[LanguageViolation]:
    fields = [
        ("pattern", result.pattern),
        ("rationale", result.rationale),
        *((f"next_level_gap[{index}]", text) for index, text in enumerate(result.next_level_gap)),
    ]
    violations: list[LanguageViolation] = []
    for field, text in fields:
        check_text = _RAW_MATERIAL_QUOTE.sub("", text).translate(_QUOTE_CHARACTERS)
        if not (
            any(pattern.search(check_text) for pattern in _CLASSIFICATION_PATTERNS)
            or _has_subject_pass_conclusion(check_text)
        ):
            continue
        term_match = _CLASSIFICATION_TERM.search(check_text)
        violations.append(
            LanguageViolation(
                field=field,
                term=term_match.group(0) if term_match else "分类结论",
                text=text,
            )
        )
    return violations

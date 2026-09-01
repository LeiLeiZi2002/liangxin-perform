from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError

from app.reports.competency_rubric import get_rubric
from app.reports.job_inputs import WorkRecordSnapshotInput
from app.reports.models import PlannedAction, ReferralDecision, RiskLevel
from app.reports.scoring_domain import (
    AnalysisOutcome,
    AudioEventRef,
    BottomLineCandidate,
    BottomLineCategory,
    BottomLineDetection,
    CodedEvidence,
    CoreDimension,
    CounterCheck,
    DialogueRef,
    DimensionPacket,
    DimensionResult,
    EvidenceConfidence,
    EvidenceDirection,
    EvidenceRejectionReason,
    EvidenceRole,
    EvidenceStrength,
    IndicatorStatus,
    LevelCapReason,
    LevelProposal,
    MaterialKind,
    MeaningUnit,
    OpportunityKind,
    OpportunityOutcome,
    PacketEvidence,
    SemanticBottomLineCategory,
    SpecialModule,
    Target,
    UnscoredReason,
    WorkRecordRef,
)
from app.reports.scoring_rules import (
    EVIDENCE_ROUTING,
    EvidenceSufficiencyExemption,
    RoutedEvidence,
    apply_evidence_confidence_ceiling,
    apply_level_caps,
    assemble_dimension_result,
    assess_evidence_sufficiency,
    build_result_summary,
    canonical_work_record_fragments,
    classification_language_violations,
    detect_known_urgent_risk_termination,
    make_work_record_mismatch_conflict,
    resolve_scoring_disposition,
    screening_gap_for,
    select_positive_evidence,
    semantic_bottom_line_events,
    validate_counter_check,
    validate_evidence,
)


def _dialogue_evidence(
    *,
    target: CoreDimension | SpecialModule = CoreDimension.respectful_communication,
    indicator_id: str = "C1.respect",
    unit_id: str = "unit-1",
    turn_id: str = "turn-1",
    quote: str = "我听见你很难受",
    direction: EvidenceDirection = EvidenceDirection.support,
) -> CodedEvidence:
    return CodedEvidence(
        unit_id=unit_id,
        target=target,
        indicator_id=indicator_id,
        direction=direction,
        strength=EvidenceStrength.strong,
        context="根据受测者原话编码。",
        alternative_reading=None,
        ref=DialogueRef(kind="dialogue", turn_id=turn_id, quote=quote),
    )


def _work_record(**updates: object) -> WorkRecordSnapshotInput:
    values = {
        "id": "record-1",
        "session_id": "session-1",
        "problem_understanding": "压力影响睡眠",
        "risk_level": RiskLevel.low,
        "risk_reasoning": "未见紧迫风险。",
        "risk_evidence_turn_ids": ["turn-1"],
        "missing_information": ["持续时间"],
        "planned_actions": [PlannedAction.follow_up],
        "referral_decision": ReferralDecision.consider,
        "supervision_decision": True,
        "follow_up": "建议一周后跟进。",
        "limitations": "信息有限。",
        "created_at": datetime(2026, 8, 30, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 30, tzinfo=UTC),
    }
    values.update(updates)
    return WorkRecordSnapshotInput.model_validate(values)


def _routed_evidence(
    *,
    target: CoreDimension | SpecialModule = CoreDimension.respectful_communication,
    indicator_id: str = "C1.respect",
    unit_id: str = "unit-1",
    turn_id: str = "turn-1",
    direction: EvidenceDirection = EvidenceDirection.support,
) -> RoutedEvidence:
    return RoutedEvidence(
        evidence=_dialogue_evidence(
            target=target,
            indicator_id=indicator_id,
            unit_id=unit_id,
            turn_id=turn_id,
            direction=direction,
        ),
        role=EvidenceRole.primary,
    )


def _module_states(
    *activated: SpecialModule,
) -> tuple[list[SpecialModule], list[tuple[SpecialModule, str]]]:
    active = list(dict.fromkeys((SpecialModule.basic_risk_screening, *activated)))
    inactive = [(module, "本次启用条件未兑现") for module in SpecialModule if module not in active]
    return active, inactive


def _with_default_module(results: list[DimensionResult]) -> list[DimensionResult]:
    return [*results, _result(SpecialModule.basic_risk_screening, level=2)]


def _result(
    target: CoreDimension | SpecialModule,
    *,
    level: int | None,
    unscored_reason: UnscoredReason | None = None,
    analysis_outcome: AnalysisOutcome = AnalysisOutcome.ok,
    next_level_gap: list[str] | None = None,
) -> DimensionResult:
    rubric = get_rubric(target)
    unit_id = f"{target.value}-unit"
    evidence = CodedEvidence(
        unit_id=unit_id,
        target=target,
        indicator_id=rubric.indicators[0].id,
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.moderate,
        context="用于摘要规则测试的已核验事实。",
        alternative_reading=None,
        ref=DialogueRef(kind="dialogue", turn_id=f"{target.value}-turn", quote="已核验原话"),
    )
    return DimensionResult(
        target=target,
        level=level,
        unscored_reason=unscored_reason,
        analysis_outcome=analysis_outcome,
        indicator_states=(
            {rubric.indicators[0].id: IndicatorStatus.demonstrated} if level is not None else {}
        ),
        pattern="持续使用试探性表达。" if level is not None else "",
        rationale="证据与当前锚点一致。" if level is not None else "",
        evidence=[evidence] if level is not None else [],
        counter_evidence=[],
        representative_unit_ids=[unit_id] if level is not None else [],
        limiting_unit_ids=[],
        conditional_unavailable=[],
        caps_applied=[],
        evidence_confidence=(EvidenceConfidence.medium if level is not None else None),
        evidence_confidence_factors=[],
        next_level_gap=next_level_gap or [],
    )


def _all_scored_core_results() -> list[DimensionResult]:
    return [_result(target, level=2) for target in CoreDimension]


def test_evidence_routing_encodes_the_complete_authoritative_table() -> None:
    p = EvidenceRole.primary
    s = EvidenceRole.supporting
    c = EvidenceRole.cross_check
    x = EvidenceRole.excluded
    expected: dict[Target, tuple[EvidenceRole, EvidenceRole, EvidenceRole]] = {
        CoreDimension.respectful_communication: (p, s, x),
        CoreDimension.listening_and_emotion: (p, s, c),
        CoreDimension.concern_clarification: (p, x, c),
        CoreDimension.integration_and_judgment: (p, s, p),
        CoreDimension.supportive_intervention: (p, s, c),
        CoreDimension.voice_and_process: (p, p, x),
        CoreDimension.boundary_and_ethics: (p, x, p),
        CoreDimension.closure_and_followup: (p, s, c),
        CoreDimension.documentation: (c, c, p),
        SpecialModule.basic_risk_screening: (p, s, c),
        SpecialModule.full_risk_appraisal: (p, s, p),
        SpecialModule.safety_response: (p, s, p),
        SpecialModule.emotional_dysregulation: (p, p, x),
        SpecialModule.psychotic_experience: (p, s, c),
        SpecialModule.dependency_and_boundary: (p, s, c),
        SpecialModule.aggression_and_harassment: (p, s, c),
        SpecialModule.third_party_call: (p, x, p),
        SpecialModule.minor_protection: (p, s, p),
    }

    assert set(EVIDENCE_ROUTING) == set(expected)
    for target, roles in expected.items():
        assert EVIDENCE_ROUTING[target] == {
            MaterialKind.dialogue: roles[0],
            MaterialKind.audio: roles[1],
            MaterialKind.work_record: roles[2],
            MaterialKind.opportunity: EvidenceRole.opportunity_only,
        }

    with pytest.raises(TypeError):
        cast(MutableMapping[Target, object], EVIDENCE_ROUTING)[
            CoreDimension.respectful_communication
        ] = {}
    with pytest.raises(TypeError):
        cast(
            MutableMapping[MaterialKind, EvidenceRole],
            EVIDENCE_ROUTING[CoreDimension.respectful_communication],
        )[MaterialKind.dialogue] = EvidenceRole.excluded


def test_reference_validation_accepts_valid_sources_and_keeps_routing_role() -> None:
    dialogue = _dialogue_evidence()
    audio = CodedEvidence(
        unit_id="unit-audio",
        target=CoreDimension.respectful_communication,
        indicator_id="C1.rupture_detection",
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.moderate,
        context="音频事件辅助判断明显抢话。",
        alternative_reading="技术噪音也可能造成重叠。",
        ref=AudioEventRef(kind="audio_event", event_id="audio-1"),
    )
    result = validate_evidence(
        target=CoreDimension.respectful_communication,
        submitted=[dialogue, audio],
        meaning_units=[
            MeaningUnit(id="unit-1", turn_ids=["turn-1"], summary="承接情绪。"),
            MeaningUnit(
                id="unit-audio",
                audio_event_ids=["audio-1"],
                summary="检测到一次重叠。",
            ),
        ],
        dialogue_turns={"turn-1": "受测者说：我听见你很难受，我们慢慢说。"},
        work_record=None,
        audio_event_ids={"audio-1"},
    )

    assert [item.role for item in result.accepted] == [
        EvidenceRole.primary,
        EvidenceRole.supporting,
    ]
    assert result.rejected == []
    assert result.analysis_outcome is AnalysisOutcome.ok


def test_work_record_snapshot_exposes_only_canonical_business_fragments() -> None:
    fragments = canonical_work_record_fragments(_work_record())

    assert set(fragments) == {
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
    }
    assert fragments["supervision_decision"] == ("是",)
    assert fragments["risk_level"] == ("low",)
    assert fragments["planned_actions"] == ("follow_up",)
    assert "record-1" not in {fragment for values in fragments.values() for fragment in values}


def test_whitespace_only_quotes_are_rejected_for_evidence_and_bottom_lines() -> None:
    evidence_data = _dialogue_evidence().model_dump()
    evidence_data["ref"]["quote"] = " \t "
    with pytest.raises(ValidationError, match="quote"):
        CodedEvidence.model_validate(evidence_data)

    with pytest.raises(ValidationError, match="quote"):
        BottomLineCandidate.model_validate(
            {
                "category": SemanticBottomLineCategory.humiliation_or_coercion,
                "refs": [
                    {
                        "kind": "work_record",
                        "field": "problem_understanding",
                        "quote": "   ",
                    }
                ],
                "context": "不得用空白充当引用。",
                "repair_observed": False,
                "reasoning": "引用必须可审计。",
            }
        )


def test_reference_validation_rejects_every_invalid_reference_with_audit_reason() -> None:
    invalid = [
        _dialogue_evidence(turn_id="missing"),
        _dialogue_evidence(quote="并不存在的原话"),
        CodedEvidence(
            unit_id="unit-record",
            target=CoreDimension.respectful_communication,
            indicator_id="C1.respect",
            direction=EvidenceDirection.support,
            strength=EvidenceStrength.weak,
            context="错误使用工作记录。",
            alternative_reading=None,
            ref=WorkRecordRef(
                kind="work_record",
                field="follow_up",
                quote="不存在",
            ),
        ),
        CodedEvidence(
            unit_id="unit-audio",
            target=CoreDimension.respectful_communication,
            indicator_id="C1.respect",
            direction=EvidenceDirection.support,
            strength=EvidenceStrength.weak,
            context="引用不存在的音频事件。",
            alternative_reading=None,
            ref=AudioEventRef(kind="audio_event", event_id="missing-audio"),
        ),
        _dialogue_evidence(indicator_id="C2.emotion_recognition"),
    ]
    result = validate_evidence(
        target=CoreDimension.respectful_communication,
        submitted=invalid,
        meaning_units=[
            MeaningUnit(id="unit-1", turn_ids=["turn-1", "missing"], summary="对话。"),
            MeaningUnit(
                id="unit-record",
                work_record_refs=[
                    WorkRecordRef(
                        kind="work_record",
                        field="problem_understanding",
                        quote="压力影响睡眠",
                    )
                ],
                summary="记录。",
            ),
            MeaningUnit(
                id="unit-audio",
                audio_event_ids=["missing-audio"],
                summary="音频。",
            ),
        ],
        dialogue_turns={"turn-1": "我听见你很难受"},
        work_record=None,
        audio_event_ids=set(),
    )

    assert result.accepted == []
    assert {item.reason for item in result.rejected} == {
        EvidenceRejectionReason.dialogue_turn_missing,
        EvidenceRejectionReason.quote_not_contiguous,
        EvidenceRejectionReason.work_record_field_invalid,
        EvidenceRejectionReason.audio_event_missing,
        EvidenceRejectionReason.indicator_not_in_target,
    }
    assert all(item.detail for item in result.rejected)
    assert result.analysis_outcome is AnalysisOutcome.analysis_failed


def test_excluded_sources_are_rejected_and_non_primary_roles_cannot_raise_level() -> None:
    excluded_ref = WorkRecordRef(
        kind="work_record",
        field="problem_understanding",
        quote="保持尊重",
    )
    excluded = CodedEvidence(
        unit_id="unit-record",
        target=CoreDimension.respectful_communication,
        indicator_id="C1.respect",
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.strong,
        context="C1 不接受工作记录证据。",
        alternative_reading=None,
        ref=excluded_ref,
    )
    excluded_result = validate_evidence(
        target=CoreDimension.respectful_communication,
        submitted=[excluded],
        meaning_units=[
            MeaningUnit(
                id="unit-record",
                work_record_refs=[excluded_ref],
                summary="记录自述保持尊重。",
            )
        ],
        dialogue_turns={},
        work_record=_work_record(problem_understanding="保持尊重"),
        audio_event_ids=set(),
    )
    assert excluded_result.rejected[0].reason is EvidenceRejectionReason.source_excluded

    supporting_only = validate_evidence(
        target=CoreDimension.respectful_communication,
        submitted=[
            CodedEvidence(
                unit_id="unit-audio",
                target=CoreDimension.respectful_communication,
                indicator_id="C1.rupture_detection",
                direction=EvidenceDirection.support,
                strength=EvidenceStrength.strong,
                context="辅助音频事件。",
                alternative_reading=None,
                ref=AudioEventRef(kind="audio_event", event_id="audio-1"),
            )
        ],
        meaning_units=[
            MeaningUnit(
                id="unit-audio",
                audio_event_ids=["audio-1"],
                summary="出现重叠。",
            )
        ],
        dialogue_turns={},
        work_record=None,
        audio_event_ids={"audio-1"},
    )
    assert select_positive_evidence(supporting_only.accepted) == []

    cross_check = validate_evidence(
        target=CoreDimension.listening_and_emotion,
        submitted=[
            CodedEvidence(
                unit_id="unit-record",
                target=CoreDimension.listening_and_emotion,
                indicator_id="C2.emotion_recognition",
                direction=EvidenceDirection.support,
                strength=EvidenceStrength.strong,
                context="工作记录仅用于核对。",
                alternative_reading=None,
                ref=WorkRecordRef(
                    kind="work_record",
                    field="problem_understanding",
                    quote="来电者感到羞耻",
                ),
            )
        ],
        meaning_units=[
            MeaningUnit(
                id="unit-record",
                work_record_refs=[
                    WorkRecordRef(
                        kind="work_record",
                        field="problem_understanding",
                        quote="来电者感到羞耻",
                    )
                ],
                summary="记录中的情绪概括。",
            )
        ],
        dialogue_turns={},
        work_record=_work_record(problem_understanding="来电者感到羞耻"),
        audio_event_ids=set(),
    )
    assert cross_check.accepted[0].role is EvidenceRole.cross_check
    assert select_positive_evidence(cross_check.accepted) == []


def test_counter_check_requires_real_search_trace_and_a_note_when_empty() -> None:
    units = [MeaningUnit(id="unit-1", turn_ids=["turn-1"], summary="一段材料。")]

    no_trace = validate_counter_check(
        CounterCheck(
            target=CoreDimension.respectful_communication,
            searched_unit_ids=[],
            found=[],
            not_found_note=None,
        ),
        units,
        dialogue_turns={"turn-1": "一段材料。"},
        work_record=None,
        audio_event_ids=set(),
    )
    unknown_trace = validate_counter_check(
        CounterCheck(
            target=CoreDimension.respectful_communication,
            searched_unit_ids=["unknown"],
            found=[],
            not_found_note="未发现反向材料。",
        ),
        units,
        dialogue_turns={"turn-1": "一段材料。"},
        work_record=None,
        audio_event_ids=set(),
    )
    complete = validate_counter_check(
        CounterCheck(
            target=CoreDimension.respectful_communication,
            searched_unit_ids=["unit-1"],
            found=[],
            not_found_note="检索了完整互动单元，未发现反向或修复材料。",
        ),
        units,
        dialogue_turns={"turn-1": "一段材料。"},
        work_record=None,
        audio_event_ids=set(),
    )

    assert no_trace.complete is False
    assert unknown_trace.complete is False
    assert complete.complete is True


def test_counter_check_rejects_found_evidence_that_fails_full_validation() -> None:
    work_ref = WorkRecordRef(
        kind="work_record",
        field="problem_understanding",
        quote="保持尊重",
    )
    units = [
        MeaningUnit(id="unit-1", turn_ids=["turn-1"], summary="对话单元。"),
        MeaningUnit(
            id="unit-record",
            work_record_refs=[work_ref],
            summary="工作记录单元。",
        ),
    ]
    invalid_quote = _dialogue_evidence(quote="不存在的连续原话")
    excluded_source = CodedEvidence(
        unit_id="unit-record",
        target=CoreDimension.respectful_communication,
        indicator_id="C1.respect",
        direction=EvidenceDirection.limit,
        strength=EvidenceStrength.moderate,
        context="工作记录不能作为 C1 反例。",
        alternative_reading=None,
        ref=work_ref,
    )

    validation = validate_counter_check(
        CounterCheck(
            target=CoreDimension.respectful_communication,
            searched_unit_ids=["unit-1", "unit-record"],
            found=[invalid_quote, excluded_source],
            not_found_note=None,
        ),
        units,
        dialogue_turns={"turn-1": "我听见你很难受"},
        work_record=_work_record(problem_understanding="保持尊重"),
        audio_event_ids=set(),
    )

    assert validation.complete is False
    assert {item.reason for item in validation.evidence_rejections} == {
        EvidenceRejectionReason.quote_not_contiguous,
        EvidenceRejectionReason.source_excluded,
    }
    assert all(item.detail for item in validation.evidence_rejections)


def test_unscored_priority_never_scores_no_opportunity_as_zero() -> None:
    analysis_failed = resolve_scoring_disposition(
        analysis_outcome=AnalysisOutcome.analysis_failed,
        technical_failure=True,
        has_opportunity=False,
        evidence_sufficient=False,
    )
    interrupted_without_opportunity = resolve_scoring_disposition(
        analysis_outcome=AnalysisOutcome.ok,
        technical_failure=True,
        has_opportunity=False,
        evidence_sufficient=False,
    )
    interrupted_with_insufficient_material = resolve_scoring_disposition(
        analysis_outcome=AnalysisOutcome.ok,
        technical_failure=True,
        has_opportunity=True,
        evidence_sufficient=False,
    )
    interrupted_with_sufficient_material = resolve_scoring_disposition(
        analysis_outcome=AnalysisOutcome.ok,
        technical_failure=True,
        has_opportunity=True,
        evidence_sufficient=True,
    )
    no_opportunity = resolve_scoring_disposition(
        analysis_outcome=AnalysisOutcome.ok,
        technical_failure=False,
        has_opportunity=False,
        evidence_sufficient=False,
    )
    insufficient = resolve_scoring_disposition(
        analysis_outcome=AnalysisOutcome.ok,
        technical_failure=False,
        has_opportunity=True,
        evidence_sufficient=False,
    )

    assert analysis_failed.unscored_reason is None
    assert analysis_failed.analysis_outcome is AnalysisOutcome.analysis_failed
    assert interrupted_without_opportunity.unscored_reason is UnscoredReason.no_opportunity
    assert (
        interrupted_with_insufficient_material.unscored_reason
        is UnscoredReason.technical_failure
    )
    assert interrupted_with_sufficient_material.unscored_reason is None
    assert no_opportunity.unscored_reason is UnscoredReason.no_opportunity
    assert no_opportunity.level is None
    assert insufficient.unscored_reason is UnscoredReason.insufficient_evidence


def test_evidence_sufficiency_requires_two_independent_fragments_by_default() -> None:
    units = [
        MeaningUnit(id="unit-1", turn_ids=["turn-1", "turn-2"], summary="第一段。"),
        MeaningUnit(id="unit-2", turn_ids=["turn-2"], summary="与第一段重叠。"),
        MeaningUnit(id="unit-3", turn_ids=["turn-3"], summary="独立片段。"),
    ]
    routed = [
        _routed_evidence(unit_id="unit-1", turn_id="turn-1"),
        _routed_evidence(unit_id="unit-2", turn_id="turn-2"),
        _routed_evidence(unit_id="unit-3", turn_id="turn-3"),
    ]

    duplicate = assess_evidence_sufficiency(
        target=CoreDimension.respectful_communication,
        representative_unit_ids=["unit-1", "unit-1"],
        units=units,
        evidence=routed,
        declared_opportunity_count=2,
    )
    overlap = assess_evidence_sufficiency(
        target=CoreDimension.respectful_communication,
        representative_unit_ids=["unit-1", "unit-2"],
        units=units,
        evidence=routed,
        declared_opportunity_count=2,
    )
    independent = assess_evidence_sufficiency(
        target=CoreDimension.respectful_communication,
        representative_unit_ids=["unit-1", "unit-3"],
        units=units,
        evidence=routed,
        declared_opportunity_count=2,
    )

    assert duplicate.sufficient is False
    assert overlap.sufficient is False
    assert independent.sufficient is True
    assert independent.exemption is None

    no_evidence = assess_evidence_sufficiency(
        target=CoreDimension.respectful_communication,
        representative_unit_ids=["unit-1"],
        units=units,
        evidence=[],
        declared_opportunity_count=1,
    )
    unknown_unit = assess_evidence_sufficiency(
        target=CoreDimension.respectful_communication,
        representative_unit_ids=["unknown"],
        units=units,
        evidence=routed,
        declared_opportunity_count=1,
    )
    assert no_evidence.sufficient is False
    assert no_evidence.reason is not None
    assert "有效定级证据" in no_evidence.reason
    assert unknown_unit.sufficient is False
    assert unknown_unit.reason is not None
    assert "不存在" in unknown_unit.reason

    fabricated_route = assess_evidence_sufficiency(
        target=CoreDimension.respectful_communication,
        representative_unit_ids=["unit-audio"],
        units=[
            MeaningUnit(
                id="unit-audio",
                audio_event_ids=["audio-1"],
                summary="音频辅助材料。",
            )
        ],
        evidence=[
            RoutedEvidence(
                evidence=CodedEvidence(
                    unit_id="unit-audio",
                    target=CoreDimension.respectful_communication,
                    indicator_id="C1.rupture_detection",
                    direction=EvidenceDirection.support,
                    strength=EvidenceStrength.strong,
                    context="伪造为主证据的音频材料。",
                    alternative_reading=None,
                    ref=AudioEventRef(kind="audio_event", event_id="audio-1"),
                ),
                role=EvidenceRole.primary,
            )
        ],
        declared_opportunity_count=1,
    )
    assert fabricated_route.sufficient is False
    assert fabricated_route.reason is not None
    assert "路由" in fabricated_route.reason


def test_all_four_single_fragment_exemptions_are_explicit() -> None:
    unit = MeaningUnit(id="unit-1", turn_ids=["turn-1"], summary="唯一完整片段。")
    routed = [_routed_evidence()]

    bottom_line = assess_evidence_sufficiency(
        target=CoreDimension.respectful_communication,
        representative_unit_ids=["unit-1"],
        units=[unit],
        evidence=routed,
        declared_opportunity_count=2,
        bottom_line_triggered=True,
    )
    single_declared = assess_evidence_sufficiency(
        target=CoreDimension.respectful_communication,
        representative_unit_ids=["unit-1"],
        units=[unit],
        evidence=routed,
        declared_opportunity_count=1,
    )
    interrupted = assess_evidence_sufficiency(
        target=CoreDimension.respectful_communication,
        representative_unit_ids=["unit-1"],
        units=[unit],
        evidence=routed,
        declared_opportunity_count=2,
        unique_due_to_interruption=True,
    )
    closure = assess_evidence_sufficiency(
        target=CoreDimension.closure_and_followup,
        representative_unit_ids=["unit-1"],
        units=[unit],
        evidence=[
            _routed_evidence(
                target=CoreDimension.closure_and_followup,
                indicator_id="C8.timing",
            )
        ],
        declared_opportunity_count=2,
    )

    assert bottom_line.exemption is EvidenceSufficiencyExemption.bottom_line_event
    assert single_declared.exemption is EvidenceSufficiencyExemption.single_declared_opportunity
    assert single_declared.confidence_ceiling is EvidenceConfidence.medium
    assert (
        apply_evidence_confidence_ceiling(EvidenceConfidence.high, single_declared)
        is EvidenceConfidence.medium
    )
    assert (
        apply_evidence_confidence_ceiling(EvidenceConfidence.low, single_declared)
        is EvidenceConfidence.low
    )
    assert interrupted.exemption is EvidenceSufficiencyExemption.interruption_unique_opportunity
    assert closure.exemption is EvidenceSufficiencyExemption.closure_event


def test_level_caps_apply_to_validated_adverse_and_opportunity_conditions() -> None:
    adverse = _dialogue_evidence(direction=EvidenceDirection.adverse)
    validated = validate_evidence(
        target=CoreDimension.respectful_communication,
        submitted=[adverse],
        meaning_units=[MeaningUnit(id="unit-1", turn_ids=["turn-1"], summary="反向行为。")],
        dialogue_turns={"turn-1": "我听见你很难受"},
        work_record=None,
        audio_event_ids=set(),
    )
    proposal = LevelProposal(
        target=CoreDimension.respectful_communication,
        proposed_level=4,
        pattern="复杂情境中整体稳定。",
        rationale="模型提出4级。",
        representative_units=["unit-1"],
        limiting_units=["unit-1"],
        next_level_gap=["这是针对模型原4级结论生成的缺口，不得挪给封顶后的2级。"],
        evidence_confidence=EvidenceConfidence.high,
        evidence_confidence_factors=["引用完整。"],
    )

    decision = apply_level_caps(
        proposal,
        evidence=validated.accepted,
        conditional_unavailable=["处理一般性的犹豫、拒绝和关系紧张"],
        has_complex_opportunity=False,
    )

    assert decision.level == 2
    assert decision.caps_applied == [
        LevelCapReason.adverse_evidence,
        LevelCapReason.conditional_opportunity_unavailable,
        LevelCapReason.no_complex_opportunity,
    ]
    assert decision.next_level_gap == [
        "这是针对模型原4级结论生成的缺口，不得挪给封顶后的2级。"
    ]

    already_lower = proposal.model_copy(
        update={"proposed_level": 2, "next_level_gap": ["需增加稳定性。"]}
    )
    lower_decision = apply_level_caps(
        already_lower,
        evidence=[],
        conditional_unavailable=["处理一般性的犹豫、拒绝和关系紧张"],
        has_complex_opportunity=True,
    )
    assert lower_decision.level == 2
    assert lower_decision.next_level_gap == ["需增加稳定性。"]

    with pytest.raises(ValueError, match="target"):
        apply_level_caps(
            proposal,
            evidence=[
                _routed_evidence(
                    target=CoreDimension.listening_and_emotion,
                    indicator_id="C2.emotion_recognition",
                )
            ],
            conditional_unavailable=[],
            has_complex_opportunity=True,
        )


def test_level_ceiling_is_calculated_before_scoring_and_keeps_valid_gap() -> None:
    from app.reports.scoring_rules import calculate_level_ceiling

    adverse = _routed_evidence(direction=EvidenceDirection.adverse)
    ceiling = calculate_level_ceiling(
        evidence=[adverse],
        conditional_unavailable=[],
        has_complex_opportunity=True,
    )
    proposal = LevelProposal(
        target=CoreDimension.respectful_communication,
        proposed_level=2,
        pattern="已出现明确限制性互动。",
        rationale="反向证据使当前最高只能评到二级。",
        representative_units=["unit-1"],
        limiting_units=["unit-1"],
        next_level_gap=["下一等级需在一般关系压力下保持尊重并完成调整。"],
        evidence_confidence=EvidenceConfidence.high,
        evidence_confidence_factors=["反向证据引用完整。"],
    )

    decision = apply_level_caps(
        proposal,
        evidence=[adverse],
        conditional_unavailable=[],
        has_complex_opportunity=True,
    )

    assert ceiling == 2
    assert decision.level == 2
    assert decision.next_level_gap == proposal.next_level_gap


def test_adverse_counter_evidence_also_caps_level_at_two() -> None:
    from app.reports.scoring_rules import calculate_level_ceiling

    support = _routed_evidence(direction=EvidenceDirection.support)
    adverse_counter = _routed_evidence(direction=EvidenceDirection.adverse)

    assert calculate_level_ceiling(
        evidence=[support],
        counter_evidence=[adverse_counter],
        conditional_unavailable=[],
        has_complex_opportunity=True,
    ) == 2


def test_dimension_result_is_assembled_once_with_validated_evidence_and_rules() -> None:
    unit = MeaningUnit(id="unit-1", turn_ids=["turn-1"], summary="稳定承接来电者。")
    routed = [_routed_evidence()]
    packet = DimensionPacket(
        scene="hotline",
        media="voice",
        target=CoreDimension.respectful_communication,
        rubric=get_rubric(CoreDimension.respectful_communication),
        evidence=[
            PacketEvidence(
                evidence=routed[0].evidence,
                role=routed[0].role,
            )
        ],
        counter_evidence=[],
        units=[unit],
        opportunities=[
            OpportunityOutcome(
                declared_target=CoreDimension.respectful_communication,
                kind=OpportunityKind.required,
                fulfilled=True,
                indicator_ids=["C1.respect"],
                complex_opportunity=False,
            ),
            OpportunityOutcome(
                declared_target=CoreDimension.respectful_communication,
                kind=OpportunityKind.conditional,
                fulfilled=False,
                indicator_ids=["C1.repair"],
                complex_opportunity=False,
            ),
        ],
        conditional_unavailable=[],
        level_ceiling=3,
    )
    proposal = LevelProposal(
        target=packet.target,
        proposed_level=3,
        pattern="一般情境中持续保持尊重。",
        rationale="已核验的一段主证据符合三级锚点。",
        representative_units=[unit.id],
        limiting_units=[],
        next_level_gap=["还需复杂情境中的稳定表现。"],
        evidence_confidence=EvidenceConfidence.high,
        evidence_confidence_factors=["只有一次声明机会。"],
    )
    sufficiency = assess_evidence_sufficiency(
        target=packet.target,
        representative_unit_ids=proposal.representative_units,
        units=packet.units,
        evidence=routed,
        declared_opportunity_count=1,
    )
    disposition = resolve_scoring_disposition(
        analysis_outcome=AnalysisOutcome.ok,
        technical_failure=False,
        has_opportunity=True,
        evidence_sufficient=sufficiency.sufficient,
    )

    result = assemble_dimension_result(
        packet,
        proposal,
        evidence=routed,
        counter_evidence=[],
        indicator_states={"C1.respect": IndicatorStatus.demonstrated},
        disposition=disposition,
        sufficiency=sufficiency,
        has_complex_opportunity=False,
    )

    assert result.level == 3
    assert result.evidence == [routed[0].evidence]
    assert result.evidence_confidence is EvidenceConfidence.medium
    assert result.caps_applied == [LevelCapReason.no_complex_opportunity]
    assert result.opportunities == packet.opportunities

    failed_result = assemble_dimension_result(
        packet,
        None,
        evidence=routed,
        counter_evidence=[],
        indicator_states={"C1.respect": IndicatorStatus.demonstrated},
        disposition=resolve_scoring_disposition(
            analysis_outcome=AnalysisOutcome.analysis_failed,
            technical_failure=False,
            has_opportunity=True,
            evidence_sufficient=True,
        ),
    )
    assert failed_result.analysis_outcome is AnalysisOutcome.analysis_failed
    assert failed_result.evidence == []
    assert failed_result.rationale == ""
    assert failed_result.opportunities == packet.opportunities

    unscored_result = assemble_dimension_result(
        packet,
        None,
        evidence=routed,
        counter_evidence=[],
        indicator_states={"C1.respect": IndicatorStatus.no_reliable_material},
        disposition=resolve_scoring_disposition(
            analysis_outcome=AnalysisOutcome.ok,
            technical_failure=False,
            has_opportunity=True,
            evidence_sufficient=False,
        ),
    )
    assert unscored_result.unscored_reason is UnscoredReason.insufficient_evidence
    assert unscored_result.evidence == [routed[0].evidence]
    assert unscored_result.rationale == ""
    assert unscored_result.opportunities == packet.opportunities

    with pytest.raises(ValueError, match="充分性"):
        assemble_dimension_result(
            packet,
            proposal,
            evidence=routed,
            counter_evidence=[],
            indicator_states={"C1.respect": IndicatorStatus.demonstrated},
            disposition=disposition,
        )

    with pytest.raises(ValueError, match="target"):
        assemble_dimension_result(
            packet,
            proposal.model_copy(update={"target": CoreDimension.listening_and_emotion}),
            evidence=routed,
            counter_evidence=[],
            indicator_states={"C1.respect": IndicatorStatus.demonstrated},
            disposition=disposition,
            sufficiency=sufficiency,
            has_complex_opportunity=False,
        )
    with pytest.raises(ValueError, match="代表单元"):
        assemble_dimension_result(
            packet,
            proposal.model_copy(update={"representative_units": ["unknown"]}),
            evidence=routed,
            counter_evidence=[],
            indicator_states={"C1.respect": IndicatorStatus.demonstrated},
            disposition=disposition,
            sufficiency=sufficiency,
            has_complex_opportunity=False,
        )
    with pytest.raises(ValueError, match="必要字段"):
        assemble_dimension_result(
            packet,
            proposal.model_copy(update={"pattern": ""}),
            evidence=routed,
            counter_evidence=[],
            indicator_states={"C1.respect": IndicatorStatus.demonstrated},
            disposition=disposition,
            sufficiency=sufficiency,
            has_complex_opportunity=False,
        )


def test_semantic_bottom_lines_only_accept_global_candidates_and_preserve_repair() -> None:
    candidate = BottomLineCandidate(
        category=SemanticBottomLineCategory.humiliation_or_coercion,
        refs=[DialogueRef(kind="dialogue", turn_id="turn-1", quote="你必须马上告诉我")],
        context="类别由全局语义编码产生，规则不读关键词。",
        repair_observed=True,
        reasoning="后续出现修复，但候选与修复痕迹都需保留。",
    )
    mixed_invalid = candidate.model_copy(
        update={
            "refs": [
                *candidate.refs,
                DialogueRef(kind="dialogue", turn_id="turn-2", quote="不存在的连续原话"),
            ]
        }
    )
    invalid_work_record = candidate.model_copy(
        update={
            "refs": [
                WorkRecordRef(
                    kind="work_record",
                    field="supervision_decision",
                    quote="不是规范布尔文本",
                )
            ]
        }
    )
    missing_audio = candidate.model_copy(
        update={"refs": [AudioEventRef(kind="audio_event", event_id="missing-audio")]}
    )

    accepted = semantic_bottom_line_events(
        [candidate],
        dialogue_turns={"turn-1": "受测者说：你必须马上告诉我真实姓名。"},
        work_record=None,
        audio_event_ids=set(),
        rule_conflicts=None,
        semantic_conflicts=None,
    )
    rejected = semantic_bottom_line_events(
        [mixed_invalid, invalid_work_record, missing_audio],
        dialogue_turns={"turn-1": "受测者说：你必须马上告诉我真实姓名。", "turn-2": "实际原话"},
        work_record=_work_record(),
        audio_event_ids=set(),
        rule_conflicts=None,
        semantic_conflicts=None,
    )
    accepted_again = semantic_bottom_line_events(
        [candidate],
        dialogue_turns={"turn-1": "受测者说：你必须马上告诉我真实姓名。"},
        work_record=None,
        audio_event_ids=set(),
        rule_conflicts=None,
        semantic_conflicts=None,
    )

    assert accepted.events[0].category is BottomLineCategory.humiliation_or_coercion
    assert accepted.events[0].repair_observed is True
    assert accepted.events[0].id == accepted_again.events[0].id
    assert rejected.events == []
    assert {item.reason for item in rejected.rejected} == {
        EvidenceRejectionReason.quote_not_contiguous,
        EvidenceRejectionReason.audio_event_missing,
    }
    assert len(rejected.rejected) == 3
    assert all(item.detail for item in rejected.rejected)


def test_fabricated_record_requires_global_semantic_candidate_with_valid_quotes() -> None:
    candidate = BottomLineCandidate(
        category=SemanticBottomLineCategory.fabricated_record,
        conflict_id="model-planned-action-conflict",
        refs=[
            DialogueRef(
                kind="dialogue",
                turn_id="turn-1",
                quote="本次没有联系督导",
            ),
            WorkRecordRef(
                kind="work_record",
                field="planned_actions",
                quote="follow_up",
            ),
        ],
        context="工作记录把未执行的联系写成已执行行动。",
        repair_observed=False,
        reasoning="候选来自完整对话和工作记录的语义比较。",
    )

    matching_conflict = make_work_record_mismatch_conflict(
        conflict_id="planned-action-conflict",
        dialogue_ref=candidate.refs[0],
        work_record_ref=candidate.refs[1],
        affected_targets=[CoreDimension.documentation],
        description="规则发现计划行动字段与对话证据不一致。",
        impact="需要语义候选确认是否构成编造。",
    )
    semantic_conflict = matching_conflict.model_copy(
        update={"id": "model-planned-action-conflict"}
    )
    accepted = semantic_bottom_line_events(
        [candidate],
        dialogue_turns={"turn-1": "受测者说明：本次没有联系督导。"},
        work_record=_work_record(),
        audio_event_ids=set(),
        rule_conflicts=[matching_conflict],
        semantic_conflicts=[semantic_conflict],
    )
    rejected = semantic_bottom_line_events(
        [
            candidate.model_copy(
                update={
                    "refs": [
                        *candidate.refs[:-1],
                        WorkRecordRef(
                            kind="work_record",
                            field="planned_actions",
                            quote="不存在的行动",
                        ),
                    ]
                }
            )
        ],
        dialogue_turns={"turn-1": "受测者说明：本次没有联系督导。"},
        work_record=_work_record(),
        audio_event_ids=set(),
        rule_conflicts=[matching_conflict],
        semantic_conflicts=[semantic_conflict],
    )

    assert accepted.events[0].category is BottomLineCategory.fabricated_record
    assert (
        accepted.events[0].detection
        is BottomLineDetection.rule_candidate_semantic_confirmed
    )
    assert rejected.events == []
    assert rejected.rejected


def test_fabricated_record_rejects_one_sided_or_unmatched_semantic_candidates() -> None:
    dialogue_ref = DialogueRef(
        kind="dialogue",
        turn_id="turn-1",
        quote="本次没有联系督导",
    )
    work_record_ref = WorkRecordRef(
        kind="work_record",
        field="planned_actions",
        quote="follow_up",
    )
    candidate = BottomLineCandidate(
        category=SemanticBottomLineCategory.fabricated_record,
        conflict_id="model-planned-action-conflict",
        refs=[dialogue_ref, work_record_ref],
        context="工作记录把未执行行动写成已执行行动。",
        repair_observed=False,
        reasoning="完整对话和记录之间存在语义矛盾。",
    )
    mismatched_conflict = make_work_record_mismatch_conflict(
        conflict_id="follow-up-conflict",
        dialogue_ref=dialogue_ref,
        work_record_ref=WorkRecordRef(
            kind="work_record",
            field="follow_up",
            quote="建议一周后跟进",
        ),
        affected_targets=[CoreDimension.documentation],
        description="规则发现随访字段不一致。",
        impact="与候选所指字段不同。",
    )
    dialogue_turns = {"turn-1": "受测者说明：本次没有联系督导。"}
    work_record = _work_record()
    matching_rule_conflict = make_work_record_mismatch_conflict(
        conflict_id="rule-planned-action-conflict",
        dialogue_ref=dialogue_ref,
        work_record_ref=work_record_ref,
        affected_targets=[CoreDimension.documentation],
        description="规则发现计划行动字段不一致。",
        impact="等待全局语义候选确认。",
    )
    matching_semantic_conflict = matching_rule_conflict.model_copy(
        update={"id": "model-planned-action-conflict"}
    )

    one_sided = semantic_bottom_line_events(
        [candidate.model_copy(update={"refs": [dialogue_ref]})],
        dialogue_turns=dialogue_turns,
        work_record=work_record,
        audio_event_ids=set(),
        rule_conflicts=[matching_rule_conflict],
        semantic_conflicts=[matching_semantic_conflict],
    )
    no_rule_conflict = semantic_bottom_line_events(
        [candidate],
        dialogue_turns=dialogue_turns,
        work_record=work_record,
        audio_event_ids=set(),
        rule_conflicts=[],
        semantic_conflicts=[matching_semantic_conflict],
    )
    wrong_record_field = semantic_bottom_line_events(
        [candidate],
        dialogue_turns=dialogue_turns,
        work_record=work_record,
        audio_event_ids=set(),
        rule_conflicts=[mismatched_conflict],
        semantic_conflicts=[matching_semantic_conflict],
    )
    wrong_conflict_id = semantic_bottom_line_events(
        [candidate.model_copy(update={"conflict_id": "missing-conflict"})],
        dialogue_turns=dialogue_turns,
        work_record=work_record,
        audio_event_ids=set(),
        rule_conflicts=[matching_rule_conflict],
        semantic_conflicts=[matching_semantic_conflict],
    )
    wrong_record_quote = semantic_bottom_line_events(
        [candidate],
        dialogue_turns=dialogue_turns,
        work_record=work_record,
        audio_event_ids=set(),
        rule_conflicts=[matching_rule_conflict],
        semantic_conflicts=[
            matching_semantic_conflict.model_copy(
                update={
                    "work_record_ref": WorkRecordRef(
                        kind="work_record",
                        field="follow_up",
                        quote="建议一周后跟进",
                    )
                }
            )
        ],
    )
    wrong_dialogue_turn = semantic_bottom_line_events(
        [candidate],
        dialogue_turns={
            **dialogue_turns,
            "turn-2": "另一个受测者话轮。",
        },
        work_record=work_record,
        audio_event_ids=set(),
        rule_conflicts=[matching_rule_conflict],
        semantic_conflicts=[
            matching_semantic_conflict.model_copy(
                update={
                    "dialogue_ref": DialogueRef(
                        kind="dialogue",
                        turn_id="turn-2",
                        quote="另一个受测者话轮",
                    )
                }
            )
        ],
    )

    assert one_sided.events == []
    assert one_sided.rejected
    assert no_rule_conflict.events == []
    assert no_rule_conflict.rejected
    assert wrong_record_field.events == []
    assert wrong_record_field.rejected
    assert wrong_conflict_id.events == []
    assert wrong_conflict_id.rejected
    assert wrong_record_quote.events == []
    assert wrong_record_quote.rejected
    assert wrong_dialogue_turn.events == []
    assert wrong_dialogue_turn.rejected


def test_known_urgent_risk_termination_requires_disclosure_later_opportunity_and_no_action() -> (
    None
):
    risk_ref = DialogueRef(kind="dialogue", turn_id="turn-1", quote="我现在就准备伤害自己")
    dialogue_turns = {
        "turn-1": "来电者说：我现在就准备伤害自己。",
        "turn-2": "受测者说：我会保持通话并联系支持。",
        "turn-3": "通话结束。",
    }

    event = detect_known_urgent_risk_termination(
        disclosed_urgent_risk_refs=[risk_ref],
        dialogue_turns=dialogue_turns,
        ordered_turn_ids=["turn-1", "turn-2", "turn-3"],
        worker_turn_ids={"turn-2"},
        safety_action_refs=[],
        call_ended=True,
    )
    no_opportunity = detect_known_urgent_risk_termination(
        disclosed_urgent_risk_refs=[risk_ref],
        dialogue_turns=dialogue_turns,
        ordered_turn_ids=["turn-1"],
        worker_turn_ids=set(),
        safety_action_refs=[],
        call_ended=True,
    )
    handled = detect_known_urgent_risk_termination(
        disclosed_urgent_risk_refs=[risk_ref],
        dialogue_turns=dialogue_turns,
        ordered_turn_ids=["turn-1", "turn-2", "turn-3"],
        worker_turn_ids={"turn-2"},
        safety_action_refs=[
            DialogueRef(kind="dialogue", turn_id="turn-2", quote="我会保持通话并联系支持")
        ],
        call_ended=True,
    )
    hidden_only = detect_known_urgent_risk_termination(
        disclosed_urgent_risk_refs=[],
        dialogue_turns=dialogue_turns,
        ordered_turn_ids=["turn-1", "turn-2"],
        worker_turn_ids={"turn-2"},
        safety_action_refs=[],
        call_ended=True,
    )

    assert event.event is not None
    assert event.event.category is BottomLineCategory.known_urgent_risk_ended_without_safety_action
    assert no_opportunity.event is None
    assert handled.event is None
    assert hidden_only.event is None
    assert all(not result.rejected for result in (event, no_opportunity, handled, hidden_only))


def test_urgent_risk_rule_filters_fabricated_safety_and_disclosure_quotes() -> None:
    valid_risk = DialogueRef(
        kind="dialogue",
        turn_id="turn-1",
        quote="我现在就准备伤害自己",
    )
    fabricated_risk = DialogueRef(
        kind="dialogue",
        turn_id="turn-3",
        quote="并不存在的风险披露",
    )
    fabricated_safety = DialogueRef(
        kind="dialogue",
        turn_id="turn-2",
        quote="并不存在的安全行动",
    )

    result = detect_known_urgent_risk_termination(
        disclosed_urgent_risk_refs=[valid_risk, fabricated_risk],
        dialogue_turns={
            "turn-1": "来电者说：我现在就准备伤害自己。",
            "turn-2": "受测者只作一般回应。",
            "turn-3": "来电者表示要结束通话。",
        },
        ordered_turn_ids=["turn-1", "turn-2", "turn-3"],
        worker_turn_ids={"turn-2"},
        safety_action_refs=[fabricated_safety],
        call_ended=True,
    )

    assert result.event is not None
    assert result.event.refs == [valid_risk]
    assert len(result.rejected) == 2
    assert all(
        rejection.reason is EvidenceRejectionReason.quote_not_contiguous
        for rejection in result.rejected
    )
    assert all(rejection.detail for rejection in result.rejected)


def test_work_record_mismatch_only_creates_conflict_candidate() -> None:
    conflict = make_work_record_mismatch_conflict(
        conflict_id="conflict-1",
        dialogue_ref=None,
        work_record_ref=WorkRecordRef(
            kind="work_record",
            field="planned_actions",
            quote="已联系督导",
        ),
        affected_targets=[CoreDimension.documentation],
        description="记录内容在当前通话材料中没有对应事件。",
        impact="需语义判断是合理概括、笔误还是编造。",
    )

    assert conflict.affected_targets == [CoreDimension.documentation]
    assert not hasattr(conflict, "category")


def test_screening_gap_is_separate_from_bottom_line_events() -> None:
    low = _result(SpecialModule.basic_risk_screening, level=1)
    adequate = _result(SpecialModule.basic_risk_screening, level=2)

    assert screening_gap_for(low) is True
    assert screening_gap_for(adequate) is False


def test_summary_is_rule_generated_and_separates_failed_from_unscored() -> None:
    results = [
        _result(
            CoreDimension.respectful_communication,
            level=3,
            next_level_gap=["在关系紧张后完成具体修复。"],
        ),
        _result(
            CoreDimension.listening_and_emotion,
            level=3,
            next_level_gap=["在关系紧张后完成具体修复。"],
        ),
        _result(
            CoreDimension.concern_clarification, level=2, next_level_gap=["共同确认优先工作焦点。"]
        ),
        _result(
            CoreDimension.integration_and_judgment,
            level=None,
            unscored_reason=UnscoredReason.no_opportunity,
        ),
        _result(
            CoreDimension.supportive_intervention,
            level=None,
            analysis_outcome=AnalysisOutcome.analysis_failed,
        ),
        _result(CoreDimension.voice_and_process, level=3),
        _result(CoreDimension.boundary_and_ethics, level=2),
        _result(CoreDimension.closure_and_followup, level=2),
        _result(CoreDimension.documentation, level=3),
        _result(SpecialModule.basic_risk_screening, level=1),
    ]

    activated, inactive = _module_states(SpecialModule.basic_risk_screening)
    summary = build_result_summary(
        results,
        activated_modules=activated,
        inactive_modules=inactive,
        bottom_line_events=[],
        max_next_behaviors=4,
    )

    assert summary.scored_core_count == 7
    assert summary.unscored == [
        (CoreDimension.integration_and_judgment, UnscoredReason.no_opportunity)
    ]
    assert summary.analysis_failed == [CoreDimension.supportive_intervention]
    assert summary.screening_gap is True
    assert summary.level_distribution == (
        "九个核心维度中七个形成等级，其中四个为3级、三个为2级；"
        "一个因无观察机会未评分；一个分析未完成。"
    )
    assert summary.next_behaviors == [
        "在关系紧张后完成具体修复。",
        "共同确认优先工作焦点。",
    ]


def test_summary_only_collects_gaps_from_successfully_scored_results() -> None:
    results = _with_default_module(_all_scored_core_results())
    results[3] = _result(
        CoreDimension.integration_and_judgment,
        level=None,
        unscored_reason=UnscoredReason.insufficient_evidence,
    ).model_copy(update={"next_level_gap": ["未评分结果中的非法缺口"]})
    results[4] = _result(
        CoreDimension.supportive_intervention,
        level=None,
        analysis_outcome=AnalysisOutcome.analysis_failed,
    ).model_copy(update={"next_level_gap": ["分析失败结果中的非法缺口"]})

    activated, inactive = _module_states()
    summary = build_result_summary(
        results,
        activated_modules=activated,
        inactive_modules=inactive,
        bottom_line_events=[],
    )

    assert "未评分结果中的非法缺口" not in summary.next_behaviors
    assert "分析失败结果中的非法缺口" not in summary.next_behaviors


def test_summary_requires_each_core_dimension_exactly_once() -> None:
    complete = _all_scored_core_results()
    activated, inactive = _module_states()

    with pytest.raises(ValueError, match="缺少"):
        build_result_summary(
            _with_default_module(complete[:-1]),
            activated_modules=activated,
            inactive_modules=inactive,
            bottom_line_events=[],
        )
    with pytest.raises(ValueError, match="重复"):
        build_result_summary(
            _with_default_module([*complete, complete[0]]),
            activated_modules=activated,
            inactive_modules=inactive,
            bottom_line_events=[],
        )


def test_summary_uses_authoritative_order_and_strict_module_partition() -> None:
    core_results = [
        _result(target, level=2, next_level_gap=[f"{target.value} 的下一步行为"])
        for target in CoreDimension
    ]
    module_result = _result(SpecialModule.basic_risk_screening, level=2)
    activated, inactive = _module_states(SpecialModule.basic_risk_screening)
    summary = build_result_summary(
        [module_result, *reversed(core_results)],
        activated_modules=activated,
        inactive_modules=list(reversed(inactive)),
        bottom_line_events=[],
        max_next_behaviors=9,
    )

    assert summary.next_behaviors == [f"{target.value} 的下一步行为" for target in CoreDimension]
    assert summary.activated_modules == [SpecialModule.basic_risk_screening]
    assert [module for module, _ in summary.inactive_modules] == list(SpecialModule)[1:]

    with pytest.raises(ValueError, match="重复专项模块"):
        build_result_summary(
            [*core_results, module_result, module_result],
            activated_modules=activated,
            inactive_modules=inactive,
            bottom_line_events=[],
        )
    with pytest.raises(ValueError, match="S1a"):
        build_result_summary(
            core_results,
            activated_modules=[],
            inactive_modules=[(module, "错误地标为未启用") for module in SpecialModule],
            bottom_line_events=[],
        )
    with pytest.raises(ValueError, match="有结果"):
        build_result_summary(
            core_results,
            activated_modules=activated,
            inactive_modules=inactive,
            bottom_line_events=[],
        )
    with pytest.raises(ValueError, match="重叠"):
        build_result_summary(
            [*core_results, module_result],
            activated_modules=activated,
            inactive_modules=[*inactive, (SpecialModule.basic_risk_screening, "错误重叠")],
            bottom_line_events=[],
        )


def test_classification_check_only_targets_model_conclusion_phrasing() -> None:
    invalid = LevelProposal(
        target=CoreDimension.respectful_communication,
        proposed_level=3,
        pattern="该维度表现优秀。",
        rationale="因此判定受测者达标。",
        representative_units=[],
        limiting_units=[],
        next_level_gap=["下一步通过考核即可。"],
        evidence_confidence=EvidenceConfidence.medium,
        evidence_confidence_factors=[],
    )
    quoted_or_ordinary_use = invalid.model_copy(
        update={
            "pattern": "受测者通过询问获得信息，并核对原话“我考试通过后仍失眠”。",
            "rationale": "该模块通过澄清获得了必要信息。",
            "next_level_gap": ["通过进一步核对获得信息，并理解对方所说的“成绩优秀却仍焦虑”。"],
        }
    )

    assert {item.field for item in classification_language_violations(invalid)} == {
        "pattern",
        "rationale",
        "next_level_gap[0]",
    }
    assert classification_language_violations(quoted_or_ordinary_use) == []

    method_uses = [
        quoted_or_ordinary_use.model_copy(
            update={"pattern": "整体通过分析形成了有依据的工作理解。"}
        ),
        quoted_or_ordinary_use.model_copy(
            update={"pattern": "整体通过 分析形成了有依据的工作理解。"}
        ),
        quoted_or_ordinary_use.model_copy(
            update={"pattern": "该模块通过开放式问题了解来电者处境。"}
        ),
        quoted_or_ordinary_use.model_copy(update={"pattern": "本维度通过观察核对了交流中断。"}),
        quoted_or_ordinary_use.model_copy(update={"pattern": "评价主要通过观察与询问完成。"}),
        quoted_or_ordinary_use.model_copy(update={"pattern": "判定需通过进一步核对完成。"}),
    ]
    assert all(classification_language_violations(proposal) == [] for proposal in method_uses)

    common_phrasings = [
        invalid.model_copy(
            update={
                "pattern": "受测者通过了本次测评。",
                "rationale": "需要通过询问继续核对。",
                "next_level_gap": [],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "需要通过询问继续核对。",
                "rationale": "本次测评结果为通过。",
                "next_level_gap": [],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "需要通过询问继续核对。",
                "rationale": "证据来自完整材料。",
                "next_level_gap": ["该维度表现‘优秀’。"],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": '该维度表现"优秀"。',
                "rationale": "需要通过询问继续核对。",
                "next_level_gap": [],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "整体通过。",
                "rationale": "需要通过询问继续核对。",
                "next_level_gap": [],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "需要通过询问继续核对。",
                "rationale": "该模块通过。",
                "next_level_gap": [],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "需要通过询问继续核对。",
                "rationale": "证据来自完整材料。",
                "next_level_gap": ["本次结果为通过。"],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "考核结论：通过。",
                "rationale": "需要通过询问继续核对。",
                "next_level_gap": [],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "整体通过并进入下一阶段。",
                "rationale": "需要通过询问继续核对。",
                "next_level_gap": [],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "需要通过询问继续核对。",
                "rationale": "该模块已通过且无需补测。",
                "next_level_gap": [],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "整体通过 后进入下一阶段。",
                "rationale": "需要通过询问继续核对。",
                "next_level_gap": [],
            }
        ),
        invalid.model_copy(
            update={
                "pattern": "需要通过询问继续核对。",
                "rationale": "本维度通过但仍需练习。",
                "next_level_gap": [],
            }
        ),
    ]
    assert [
        [item.field for item in classification_language_violations(proposal)]
        for proposal in common_phrasings
    ] == [
        ["pattern"],
        ["rationale"],
        ["next_level_gap[0]"],
        ["pattern"],
        ["pattern"],
        ["rationale"],
        ["next_level_gap[0]"],
        ["pattern"],
        ["pattern"],
        ["rationale"],
        ["pattern"],
        ["rationale"],
    ]


def test_summary_assembly_rejects_classification_phrasing_in_model_fields() -> None:
    invalid = _result(
        CoreDimension.respectful_communication,
        level=3,
        next_level_gap=["下一步达到优秀水平。"],
    ).model_copy(update={"pattern": "该维度已经合格。"})

    complete = _all_scored_core_results()
    complete[0] = invalid
    complete = _with_default_module(complete)
    activated, inactive = _module_states()

    try:
        build_result_summary(
            complete,
            activated_modules=activated,
            inactive_modules=inactive,
            bottom_line_events=[],
        )
    except ValueError as exc:
        assert "分类式结论" in str(exc)
    else:
        raise AssertionError("摘要组装必须拒绝模型生成字段中的分类式结论")

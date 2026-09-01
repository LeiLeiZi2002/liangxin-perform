from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from app.reports.competency_rubric import get_rubric
from app.reports.job_inputs import CodingShard
from app.reports.models import ReportDraftStatus, ReportRecord
from app.reports.report_provider import LocalCodedUnit, LocalCodingOutput
from app.reports.schemas import DimensionReportRead, ReportRead
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
    EvidenceRole,
    EvidenceStrength,
    IndicatorStatus,
    LevelCapReason,
    LevelProposal,
    MaterialConflict,
    MeaningUnit,
    OpportunityKind,
    OpportunityOutcome,
    PacketEvidence,
    ResultSummary,
    SemanticBottomLineCategory,
    SpecialModule,
    UnscoredReason,
    WorkRecordRef,
)


def _dialogue_ref() -> DialogueRef:
    return DialogueRef(kind="dialogue", turn_id="turn-1", quote="我听见你很难受")


def _evidence() -> CodedEvidence:
    return CodedEvidence(
        unit_id="unit-1",
        target=CoreDimension.respectful_communication,
        indicator_id="C1.respect",
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.strong,
        context="受测者以非评判方式承接来电者。",
        alternative_reading=None,
        ref=_dialogue_ref(),
    )


def test_local_coding_contract_has_sources_without_targets_or_levels() -> None:
    unit = LocalCodedUnit(
        id="shard-a-unit-1",
        summary="受测者先承接了来电者的难受。",
        initial_codes=["情绪承接", "试探性核对"],
        refs=[_dialogue_ref()],
        source_role="worker",
        alternative_reading="也可能只是对原话的简单重复。",
    )
    output = LocalCodingOutput(shard_id="shard-a", units=[unit])

    assert output.units == [unit]
    assert {"target", "indicator_id", "level", "proposed_level"}.isdisjoint(
        LocalCodedUnit.model_fields
    )
    assert unit.source_role == "worker"
    for field, value in (("initial_codes", []), ("refs", [])):
        with pytest.raises(ValidationError):
            LocalCodedUnit.model_validate({**unit.model_dump(), field: value})
    with pytest.raises(ValidationError):
        LocalCodedUnit.model_validate({**unit.model_dump(), "target": "C1"})
    with pytest.raises(ValidationError, match="id 必须唯一"):
        LocalCodingOutput(shard_id="shard-a", units=[unit, unit])


def test_coding_shard_is_frozen_and_exposes_only_public_material() -> None:
    fields = set(CodingShard.model_fields)

    assert CodingShard.model_config["frozen"] is True
    assert fields == {
        "shard_id",
        "session",
        "turns",
        "work_record",
        "technical_interruptions",
        "termination",
        "overlap_turn_ids",
    }
    assert {
        "session_state",
        "actor_state",
        "case_package",
        "opportunities",
        "used_fact_ids",
    }.isdisjoint(fields)


def test_evidence_ref_is_a_discriminated_mutually_exclusive_union() -> None:
    adapter: TypeAdapter[EvidenceRef] = TypeAdapter(EvidenceRef)

    assert isinstance(
        adapter.validate_python({"kind": "dialogue", "turn_id": "turn-1", "quote": "原话"}),
        DialogueRef,
    )
    assert isinstance(
        adapter.validate_python(
            {"kind": "work_record", "field": "problem_understanding", "quote": "原文"}
        ),
        WorkRecordRef,
    )
    for metadata_field in ("id", "session_id", "created_at", "updated_at"):
        with pytest.raises(ValidationError):
            WorkRecordRef.model_validate(
                {"kind": "work_record", "field": metadata_field, "quote": "不得引用元数据"}
            )
    assert isinstance(
        adapter.validate_python({"kind": "audio_event", "event_id": "audio-1"}),
        AudioEventRef,
    )

    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "kind": "dialogue",
                "turn_id": "turn-1",
                "field": "problem_understanding",
                "quote": "原话",
            }
        )


def test_meaning_unit_can_reference_dialogue_or_work_record_material() -> None:
    dialogue = MeaningUnit(
        id="unit-dialogue",
        turn_ids=["turn-1", "turn-2"],
        summary="来电者表达困扰，受测者作出回应。",
    )
    record = MeaningUnit(
        id="unit-record",
        work_record_refs=[
            WorkRecordRef(
                kind="work_record",
                field="problem_understanding",
                quote="近期压力影响睡眠",
            )
        ],
        summary="工作记录概括了功能影响。",
    )

    assert dialogue.turn_ids == ["turn-1", "turn-2"]
    assert record.work_record_refs[0].field == "problem_understanding"

    with pytest.raises(ValidationError, match="至少引用一种实际材料"):
        MeaningUnit(id="unit-empty", summary="没有来源")


def test_global_coding_contracts_preserve_counter_search_and_repair() -> None:
    evidence = _evidence()
    counter = CounterCheck(
        target=CoreDimension.respectful_communication,
        searched_unit_ids=["unit-1"],
        found=[evidence.model_copy(update={"direction": EvidenceDirection.limit})],
        not_found_note=None,
    )
    candidate = BottomLineCandidate(
        category=SemanticBottomLineCategory.humiliation_or_coercion,
        refs=[_dialogue_ref()],
        context="出现强迫表达，需结合整通材料核对。",
        repair_observed=True,
        reasoning="后续明确承认并修复了关系破裂。",
    )

    assert counter.searched_unit_ids == ["unit-1"]
    assert candidate.repair_observed is True
    fabricated = BottomLineCandidate.model_validate(
        {
            "category": "fabricated_record",
            "conflict_id": "conflict-record-one",
            "refs": [
                _dialogue_ref(),
                WorkRecordRef(
                    kind="work_record",
                    field="planned_actions",
                    quote="已联系督导",
                ),
            ],
            "context": "完整对话与工作记录显示已执行行动被编造。",
            "repair_observed": False,
            "reasoning": "这是全局语义候选，不是由冲突规则自动升级。",
        }
    )

    assert fabricated.category is SemanticBottomLineCategory.fabricated_record
    assert fabricated.conflict_id == "conflict-record-one"
    with pytest.raises(ValidationError, match="conflict_id"):
        BottomLineCandidate.model_validate(
            fabricated.model_dump(exclude={"conflict_id"})
        )


def test_opportunity_outcome_exposes_only_deidentified_fulfillment() -> None:
    outcome = OpportunityOutcome(
        declared_target=CoreDimension.respectful_communication,
        kind=OpportunityKind.conditional,
        fulfilled=False,
        indicator_ids=["C1.repair"],
        complex_opportunity=True,
    )

    assert outcome.fulfilled is False
    assert "opportunity_id" not in OpportunityOutcome.model_fields
    assert "description" not in OpportunityOutcome.model_fields
    with pytest.raises(ValidationError):
        OpportunityOutcome.model_validate(
            {
                **outcome.model_dump(),
                "opportunity_id": "relationship_repair_probe",
                "description": "来电者对受测者表示不满",
            }
        )


def test_dimension_packet_and_level_proposal_use_target_specific_material() -> None:
    unit = MeaningUnit(
        id="unit-1",
        turn_ids=["turn-1"],
        summary="受测者承接来电者。",
    )
    evidence = _evidence()
    packet = DimensionPacket(
        scene="online",
        media="text",
        target=CoreDimension.respectful_communication,
        rubric=get_rubric(CoreDimension.respectful_communication),
        evidence=[PacketEvidence(evidence=evidence, role=EvidenceRole.primary)],
        counter_evidence=[],
        units=[unit],
        opportunities=[
            OpportunityOutcome(
                declared_target=CoreDimension.respectful_communication,
                kind=OpportunityKind.required,
                fulfilled=True,
                indicator_ids=["C1.respect"],
                complex_opportunity=False,
            )
        ],
        conditional_unavailable=["处理一般性的犹豫、拒绝和关系紧张"],
        level_ceiling=3,
    )
    proposal = LevelProposal(
        target=packet.target,
        proposed_level=3,
        pattern="持续保持尊重姿态。",
        rationale="有效证据支持3级锚点。",
        representative_units=["unit-1"],
        limiting_units=[],
        next_level_gap=["尚缺复杂关系压力下的稳定修复表现。"],
        evidence_confidence=EvidenceConfidence.medium,
        evidence_confidence_factors=["本次只有一个关系互动片段。"],
    )

    assert packet.rubric.id is packet.target
    assert packet.scene == "online"
    assert packet.media == "text"
    assert proposal.proposed_level == 3

    mismatched = packet.model_dump()
    mismatched["target"] = CoreDimension.listening_and_emotion
    with pytest.raises(ValidationError, match="target"):
        DimensionPacket.model_validate(mismatched)


def test_dimension_result_separates_analysis_failure_from_unscored_reason() -> None:
    failed = DimensionResult(
        target=CoreDimension.respectful_communication,
        level=None,
        unscored_reason=None,
        analysis_outcome=AnalysisOutcome.analysis_failed,
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
    limiting_evidence = _evidence().model_copy(update={"direction": EvidenceDirection.limit})
    unscored = DimensionResult.model_validate(
        {
            **failed.model_dump(),
            "analysis_outcome": AnalysisOutcome.ok,
            "unscored_reason": UnscoredReason.insufficient_evidence,
            "rationale": "仅保留一段已核验材料，不足以形成稳定等级。",
            "evidence": [_evidence()],
            "counter_evidence": [limiting_evidence],
        }
    )

    assert failed.analysis_outcome is AnalysisOutcome.analysis_failed
    assert unscored.unscored_reason is UnscoredReason.insufficient_evidence
    assert {"raw_score", "normalized_score", "coverage"}.isdisjoint(DimensionResult.model_fields)
    scored = DimensionResult(
        target=CoreDimension.respectful_communication,
        level=2,
        unscored_reason=None,
        analysis_outcome=AnalysisOutcome.ok,
        indicator_states={"C1.respect": IndicatorStatus.demonstrated},
        pattern="稳定保持尊重。",
        rationale="有效证据符合二级锚点。",
        evidence=[_evidence()],
        counter_evidence=[],
        representative_unit_ids=["unit-1"],
        limiting_unit_ids=[],
        conditional_unavailable=[],
        caps_applied=[LevelCapReason.no_complex_opportunity],
        evidence_confidence=EvidenceConfidence.medium,
        evidence_confidence_factors=["只有一般情境。"],
        next_level_gap=["需要复杂情境证据。"],
    )
    assert scored.caps_applied == [LevelCapReason.no_complex_opportunity]

    for required_field, missing_value in {
        "evidence": [],
        "indicator_states": {},
        "pattern": "",
        "rationale": "",
        "representative_unit_ids": [],
    }.items():
        with pytest.raises(ValidationError, match="已评分"):
            DimensionResult.model_validate({**scored.model_dump(), required_field: missing_value})

    with pytest.raises(ValidationError, match="analysis_failed"):
        DimensionResult.model_validate(
            {
                **failed.model_dump(),
                "unscored_reason": UnscoredReason.no_opportunity,
            }
        )

    failed_conclusion_fields = {
        "indicator_states": {"C1.respect": IndicatorStatus.partial},
        "pattern": "不应形成表现模式。",
        "rationale": "不应形成等级理由。",
        "evidence": [_evidence()],
        "counter_evidence": [_evidence()],
        "representative_unit_ids": ["unit-1"],
        "limiting_unit_ids": ["unit-1"],
        "caps_applied": ["adverse_evidence"],
        "evidence_confidence": EvidenceConfidence.low,
        "next_level_gap": ["不应形成下一等级缺口。"],
    }
    for field, value in failed_conclusion_fields.items():
        with pytest.raises(ValidationError, match="analysis_failed"):
            DimensionResult.model_validate(
                {
                    **failed.model_dump(),
                    field: value,
                }
            )

    unscored_data = unscored.model_dump()
    for field, value in {
        "pattern": "不应形成表现模式。",
        "representative_unit_ids": ["unit-1"],
        "limiting_unit_ids": ["unit-1"],
        "caps_applied": ["no_complex_opportunity"],
        "evidence_confidence": EvidenceConfidence.low,
        "evidence_confidence_factors": ["不应形成置信结论。"],
        "next_level_gap": ["不应形成下一等级缺口。"],
    }.items():
        with pytest.raises(ValidationError, match="未评分"):
            DimensionResult.model_validate({**unscored_data, field: value})

    allowed_unscored_state = DimensionResult.model_validate(
        {
            **unscored_data,
            "indicator_states": {"C1.repair": IndicatorStatus.no_opportunity},
            "conditional_unavailable": ["处理一般性的犹豫、拒绝和关系紧张"],
        }
    )
    assert allowed_unscored_state.conditional_unavailable
    assert allowed_unscored_state.evidence == [_evidence()]
    assert allowed_unscored_state.counter_evidence == [limiting_evidence]
    assert allowed_unscored_state.rationale == "仅保留一段已核验材料，不足以形成稳定等级。"

    technical_failure = DimensionResult.model_validate(
        {
            **unscored_data,
            "unscored_reason": UnscoredReason.technical_failure,
            "rationale": "技术中断前仅留下这一段已核验材料。",
        }
    )
    assert technical_failure.evidence == [_evidence()]


def test_public_dimension_projection_hides_internal_unit_ids_from_narrative() -> None:
    evidence = _evidence().model_copy(
        update={
            "context": "这段表现由 unit-1 支持。",
            "alternative_reading": "也可能是一次性表现（unit-1）。",
        }
    )
    internal = DimensionResult(
        target=CoreDimension.respectful_communication,
        level=2,
        unscored_reason=None,
        analysis_outcome=AnalysisOutcome.ok,
        indicator_states={"C1.respect": IndicatorStatus.demonstrated},
        pattern="受测者保持了尊重（unit-1）。",
        rationale="代表性材料（unit-1）支持当前判断。",
        evidence=[evidence],
        counter_evidence=[],
        representative_unit_ids=["unit-1"],
        limiting_unit_ids=[],
        conditional_unavailable=[],
        caps_applied=[],
        evidence_confidence=EvidenceConfidence.medium,
        evidence_confidence_factors=["unit-1 可回到原文核对。"],
        next_level_gap=["还需补充 unit-1 之外的独立材料。"],
    )

    public = DimensionReportRead.from_result(internal)
    visible_text = " ".join(
        [
            public.result.pattern,
            public.result.rationale,
            public.result.evidence[0].context,
            public.result.evidence[0].alternative_reading or "",
            *public.result.evidence_confidence_factors,
            *public.result.next_level_gap,
        ]
    )

    assert "unit-1" not in visible_text
    assert public.result.evidence[0].unit_id == "unit-1"
    assert public.result.representative_unit_ids == ["unit-1"]
    assert "unit-1" in internal.rationale


def test_public_report_projection_cleans_report_wide_narrative_without_altering_audit_ids() -> None:
    first_evidence = _evidence()
    first = DimensionResult(
        target=CoreDimension.respectful_communication,
        level=2,
        unscored_reason=None,
        analysis_outcome=AnalysisOutcome.ok,
        indicator_states={"C1.respect": IndicatorStatus.demonstrated},
        pattern="这一表现由 unit-1 支持。",
        rationale="判断时同时参考了另一维度的 unit-2。",
        evidence=[first_evidence],
        counter_evidence=[],
        representative_unit_ids=["unit-1"],
        limiting_unit_ids=[],
        conditional_unavailable=[],
        caps_applied=[],
        evidence_confidence=EvidenceConfidence.medium,
        evidence_confidence_factors=["unit-1 可回到原文核对。"],
        next_level_gap=["还需补充 unit-2 之外的材料。"],
    )
    second_evidence = CodedEvidence(
        unit_id="unit-2",
        target=CoreDimension.listening_and_emotion,
        indicator_id="C2.content_tracking",
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.strong,
        context="这段材料对应 unit-2。",
        alternative_reading=None,
        ref=_dialogue_ref(),
    )
    second = DimensionResult(
        target=CoreDimension.listening_and_emotion,
        level=2,
        unscored_reason=None,
        analysis_outcome=AnalysisOutcome.ok,
        indicator_states={"C2.content_tracking": IndicatorStatus.demonstrated},
        pattern="能够跟进叙述。",
        rationale="unit-2 支持当前判断。",
        evidence=[second_evidence],
        counter_evidence=[],
        representative_unit_ids=["unit-2"],
        limiting_unit_ids=[],
        conditional_unavailable=[],
        caps_applied=[],
        evidence_confidence=EvidenceConfidence.medium,
        evidence_confidence_factors=[],
        next_level_gap=[],
    )
    event = BottomLineEvent(
        id="event-1",
        category=BottomLineCategory.humiliation_or_coercion,
        detection=BottomLineDetection.semantic,
        refs=[_dialogue_ref()],
        description="unit-1 所在片段需要核对。",
        reasoning="结论还参考了 unit-2。",
        repair_observed=False,
    )
    conflict = MaterialConflict(
        id="conflict-1",
        dialogue_ref=_dialogue_ref(),
        work_record_ref=None,
        description="unit-2 与记录不一致。",
        affected_targets=[CoreDimension.listening_and_emotion],
        impact="需要核对 unit-1。",
    )
    summary = ResultSummary(
        scored_core_count=2,
        unscored=[],
        analysis_failed=[],
        activated_modules=[],
        inactive_modules=[(SpecialModule.basic_risk_screening, "unit-1 未触发。")],
        bottom_line_events=[event],
        screening_gap=False,
        level_distribution="unit-1 和 unit-2 均已形成结果。",
        next_behaviors=["根据 unit-2 继续观察。"],
    )
    record = ReportRecord(
        id="report-1",
        session_id="session-1",
        job_id="job-1",
        case_id="boundary_referral_short",
        scene="hotline",
        media="voice",
        summary_json=summary.model_dump(mode="json"),
        dimensions_json=[
            first.model_dump(mode="json"),
            second.model_dump(mode="json"),
        ],
        bottom_line_events_json=[event.model_dump(mode="json")],
        material_conflicts_json=[conflict.model_dump(mode="json")],
        screening_gap=False,
        disclaimers_json=["使用前核对 unit-1。"],
        rubric_fingerprint="rubric",
        case_package_fingerprint="case",
        model_fingerprint="model",
        prompt_fingerprint="prompt",
        input_fingerprint="input",
        ai_draft_status=ReportDraftStatus.complete,
    )

    public = ReportRead.from_record(record)
    visible_text = [
        public.summary.level_distribution,
        *public.summary.next_behaviors,
        *(reason for _, reason in public.summary.inactive_modules),
        *(item.description for item in public.summary.bottom_line_events),
        *(item.reasoning for item in public.summary.bottom_line_events),
        *(item.description for item in public.bottom_line_events),
        *(item.reasoning for item in public.bottom_line_events),
        *(item.description for item in public.material_conflicts),
        *(item.impact for item in public.material_conflicts),
        *public.disclaimers,
        *(item.result.pattern for item in public.dimensions),
        *(item.result.rationale for item in public.dimensions),
    ]

    assert not any("unit-" in text for text in visible_text)
    assert public.dimensions[0].result.evidence[0].unit_id == "unit-1"
    assert public.dimensions[1].result.representative_unit_ids == ["unit-2"]
    assert "unit-2" in record.dimensions_json[0]["rationale"]


def test_bottom_line_conflict_and_summary_contracts_are_strongly_typed() -> None:
    event = BottomLineEvent(
        id="bottom-line-1",
        category=BottomLineCategory.humiliation_or_coercion,
        detection=BottomLineDetection.semantic,
        refs=[_dialogue_ref()],
        description="出现强迫来电者披露的关键事件。",
        reasoning="全局编码候选经规则收口。",
        repair_observed=False,
    )
    conflict = MaterialConflict(
        id="conflict-1",
        dialogue_ref=_dialogue_ref(),
        work_record_ref=WorkRecordRef(
            kind="work_record",
            field="planned_actions",
            quote="已联系督导",
        ),
        description="工作记录中的行动状态与通话材料不对应。",
        affected_targets=[CoreDimension.documentation],
        impact="需要语义复核是合理概括、笔误还是编造。",
    )
    summary = ResultSummary(
        scored_core_count=8,
        unscored=[(CoreDimension.closure_and_followup, UnscoredReason.technical_failure)],
        analysis_failed=[SpecialModule.full_risk_appraisal],
        activated_modules=[SpecialModule.basic_risk_screening],
        inactive_modules=[(SpecialModule.safety_response, "本次启用条件未兑现")],
        bottom_line_events=[event],
        screening_gap=False,
        level_distribution="九个核心维度中八个形成等级，一个因技术中断未评分。",
        next_behaviors=["在结束阶段共同确认后续行动。"],
    )

    assert conflict.affected_targets == [CoreDimension.documentation]
    assert summary.analysis_failed == [SpecialModule.full_risk_appraisal]
    assert summary.bottom_line_events[0].repair_observed is False


def test_required_enums_have_authoritative_values() -> None:
    assert set(IndicatorStatus) == {
        IndicatorStatus.demonstrated,
        IndicatorStatus.partial,
        IndicatorStatus.opportunity_missed,
        IndicatorStatus.adverse,
        IndicatorStatus.no_opportunity,
        IndicatorStatus.no_reliable_material,
    }
    assert set(UnscoredReason) == {
        UnscoredReason.no_opportunity,
        UnscoredReason.insufficient_evidence,
        UnscoredReason.technical_failure,
    }
    assert set(AnalysisOutcome) == {
        AnalysisOutcome.ok,
        AnalysisOutcome.analysis_failed,
    }
    assert set(EvidenceRole) == {
        EvidenceRole.primary,
        EvidenceRole.supporting,
        EvidenceRole.cross_check,
        EvidenceRole.opportunity_only,
        EvidenceRole.excluded,
    }

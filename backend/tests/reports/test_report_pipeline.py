from __future__ import annotations

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from threading import Barrier, BrokenBarrierError
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from app.cases.loader import CaseRepository
from app.reports.competency_rubric import get_rubric
from app.reports.job_inputs import CodingShard, OpportunityCheckInput
from app.reports.jobs import ReportJobService
from app.reports.models import (
    ReferralDecision,
    ReportJobRecord,
    ReportJobStage,
    RiskLevel,
    WorkRecordRecord,
)
from app.reports.report_provider import (
    GlobalCodingOutput,
    GroupScoringOutput,
    LocalCodedUnit,
    LocalCodingOutput,
    ScoringGroup,
)
from app.reports.scoring_domain import (
    AnalysisOutcome,
    AudioEventRef,
    BottomLineCandidate,
    BottomLineCategory,
    CodedEvidence,
    CoreDimension,
    CounterCheck,
    DialogueRef,
    DimensionPacket,
    EvidenceConfidence,
    EvidenceDirection,
    EvidenceRole,
    EvidenceStrength,
    LevelProposal,
    MaterialConflict,
    MeaningUnit,
    OpportunityKind,
    OpportunityOutcome,
    PacketEvidence,
    SemanticBottomLineCategory,
    SpecialModule,
    Target,
    WorkRecordRef,
)
from app.runtime.models import ModelCallKind
from app.runtime.providers import NonRetryableRuntimeModelError
from app.runtime_config import RuntimeCredentialStore
from app.sessions.models import (
    CaseType,
    EndReason,
    Media,
    ModelMode,
    Scene,
    SessionMode,
    SessionRecord,
    SessionStatus,
    TurnRecord,
    TurnSpeaker,
)

TARGETS: tuple[Target, ...] = (*CoreDimension, SpecialModule.basic_risk_screening)
ANALYSIS_TARGETS: tuple[Target, ...] = (*CoreDimension, *SpecialModule)


def _opportunity_inputs(
    engine: Engine,
    job: ReportJobRecord,
    *,
    fact_depths: dict[str, int] | None = None,
    occurred_event_ids: list[str] | None = None,
) -> tuple[Any, OpportunityCheckInput]:
    with Session(engine) as db:
        service = ReportJobService(db, CaseRepository())
        coding_input = service.get_coding_input(job.id)
        raw = service.get_opportunity_check_input(job.id).model_dump(mode="json")
    raw["session_state"] = {
        "actor_state": {
            "fact_states": {
                fact_id: {"disclosed_depth": depth}
                for fact_id, depth in (fact_depths or {}).items()
            },
            "occurred_event_ids": occurred_event_ids or [],
        }
    }
    return coding_input, OpportunityCheckInput.model_validate(raw)


def _create_job(
    engine: Engine,
    *,
    worker_turns: bool = True,
    credential_store: RuntimeCredentialStore | None = None,
    case_id: str = "crisis_student_main",
    case_type: CaseType = CaseType.main,
    scene: Scene = Scene.hotline,
    media: Media = Media.voice,
    end_reason: EndReason | None = None,
    state_json: dict[str, Any] | None = None,
) -> ReportJobRecord:
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    session_record = SessionRecord(
        id="session-pipeline",
        mode=SessionMode.assessment,
        scene=scene,
        case_type=case_type,
        case_id=case_id,
        media=media,
        status=SessionStatus.ended,
        model_mode=ModelMode.live,
        end_reason=end_reason,
        state_json=(
            state_json
            if state_json is not None
            else {
                "actor_state": {
                    "fact_states": {
                        fact_id: {"disclosed_depth": 1}
                        for fact_id in (
                            "presenting_concern",
                            "deception_shame",
                            "job_loss",
                            "confidentiality_fear",
                        )
                    }
                }
            }
        ),
        ended_at=now,
    )
    session_id = session_record.id
    speaker = TurnSpeaker.worker if worker_turns else TurnSpeaker.client
    turns = [
        TurnRecord(
            id="turn-one",
            session_id=session_id,
            client_turn_id="pair-one",
            sequence=1,
            speaker=speaker,
            text="我会先听你最难受的部分，也会直接确认现在是否有自伤想法。",
            created_at=now,
        ),
        TurnRecord(
            id="turn-two",
            session_id=session_id,
            client_turn_id="pair-two",
            sequence=2,
            speaker=speaker,
            text="我们一起核对下一步，并说明仍需了解的信息。",
            created_at=now,
        ),
    ]
    work_record = WorkRecordRecord(
        id="work-record",
        session_id=session_id,
        problem_understanding="当前压力、失眠和功能下降相互影响。",
        risk_level=RiskLevel.uncertain,
        risk_reasoning="已完成基础询问，紧迫性信息仍需继续核对。",
        risk_evidence_turn_ids=["turn-one"],
        missing_information=["手段可及性"],
        planned_actions=["continue_assessment", "follow_up"],
        referral_decision=ReferralDecision.consider,
        supervision_decision=True,
        follow_up="继续核对并根据结果安排后续支持。",
        limitations=(
            "仅依据本次在线会谈。" if media is Media.text else "仅依据本次通话。"
        ),
        created_at=now,
        updated_at=now,
    )
    with Session(engine) as db:
        db.add_all([session_record, *turns, work_record])
        db.commit()
    with Session(engine) as db:
        created = ReportJobService(
            db,
            CaseRepository(),
            credential_store or RuntimeCredentialStore(),
        ).create(session_id)
        stored = db.get(ReportJobRecord, created.job.id)
        assert stored is not None
        db.expunge(stored)
        return stored


def _units() -> list[MeaningUnit]:
    return [
        MeaningUnit(
            id="unit-one",
            turn_ids=["turn-one"],
            summary="第一段可观察互动。",
        ),
        MeaningUnit(
            id="unit-two",
            turn_ids=["turn-two"],
            summary="第二段可观察互动。",
        ),
        MeaningUnit(
            id="unit-record-one",
            work_record_refs=[
                WorkRecordRef(
                    kind="work_record",
                    field="problem_understanding",
                    quote="压力、失眠和功能下降",
                )
            ],
            summary="工作记录中的问题理解。",
        ),
        MeaningUnit(
            id="unit-record-two",
            work_record_refs=[
                WorkRecordRef(
                    kind="work_record",
                    field="risk_reasoning",
                    quote="紧迫性信息仍需继续核对",
                )
            ],
            summary="工作记录中的判断边界。",
        ),
    ]


def _evidence_for(target: Target) -> list[CodedEvidence]:
    indicator_id = get_rubric(target).indicators[0].id
    if target is CoreDimension.documentation:
        return [
            CodedEvidence(
                unit_id="unit-record-one",
                target=target,
                indicator_id=indicator_id,
                direction=EvidenceDirection.support,
                strength=EvidenceStrength.strong,
                context="记录有原文依据。",
                alternative_reading=None,
                ref=WorkRecordRef(
                    kind="work_record",
                    field="problem_understanding",
                    quote="压力、失眠和功能下降",
                ),
            ),
            CodedEvidence(
                unit_id="unit-record-two",
                target=target,
                indicator_id=indicator_id,
                direction=EvidenceDirection.support,
                strength=EvidenceStrength.moderate,
                context="记录保留了未知信息。",
                alternative_reading=None,
                ref=WorkRecordRef(
                    kind="work_record",
                    field="risk_reasoning",
                    quote="紧迫性信息仍需继续核对",
                ),
            ),
        ]
    return [
        CodedEvidence(
            unit_id="unit-one",
            target=target,
            indicator_id=indicator_id,
            direction=EvidenceDirection.support,
            strength=EvidenceStrength.strong,
            context="第一段直接呈现该指标。",
            alternative_reading=None,
            ref=DialogueRef(
                kind="dialogue",
                turn_id="turn-one",
                quote="先听你最难受的部分",
            ),
        ),
        CodedEvidence(
            unit_id="unit-two",
            target=target,
            indicator_id=indicator_id,
            direction=EvidenceDirection.support,
            strength=EvidenceStrength.moderate,
            context="第二段提供独立支持。",
            alternative_reading=None,
            ref=DialogueRef(
                kind="dialogue",
                turn_id="turn-two",
                quote="一起核对下一步",
            ),
        ),
    ]


def _global_output(
    targets: Sequence[Target] = TARGETS,
    *,
    counter_targets: Sequence[Target] | None = None,
) -> GlobalCodingOutput:
    units = _units()
    effective_counter_targets = targets if counter_targets is None else counter_targets
    return GlobalCodingOutput(
        units=units,
        coded_evidence=[item for target in targets for item in _evidence_for(target)],
        counter_checks=[
            CounterCheck(
                target=target,
                searched_unit_ids=[unit.id for unit in units],
                found=[],
                not_found_note="已检查全部意义单元，未发现足以推翻初步编码的反例。",
            )
            for target in effective_counter_targets
        ],
        bottom_line_candidates=[],
        material_conflict_candidates=[],
        urgent_risk_disclosure_candidates=[],
    )


def _local_output(shard: CodingShard) -> LocalCodingOutput:
    units: list[LocalCodedUnit] = []
    for turn in shard.turns:
        units.append(
            LocalCodedUnit(
                id=f"unit-{shard.shard_id}-dialogue-{turn.turn_id}",
                summary="局部对话意义单元。",
                initial_codes=["倾听"],
                source_role="interaction",
                refs=[
                    DialogueRef(
                        kind="dialogue",
                        turn_id=turn.turn_id,
                        quote=turn.text,
                    )
                ],
                alternative_reading=None,
            )
        )
    if shard.work_record is not None:
        units.append(
            LocalCodedUnit(
                id=f"unit-{shard.shard_id}-record",
                summary="局部工作记录意义单元。",
                initial_codes=["记录"],
                source_role="work_record",
                refs=[
                    WorkRecordRef(
                        kind="work_record",
                        field="problem_understanding",
                        quote="压力、失眠和功能下降",
                    ),
                    WorkRecordRef(
                        kind="work_record",
                        field="risk_reasoning",
                        quote="紧迫性信息仍需继续核对",
                    ),
                    WorkRecordRef(
                        kind="work_record",
                        field="planned_actions",
                        quote="follow_up",
                    ),
                    WorkRecordRef(
                        kind="work_record",
                        field="follow_up",
                        quote="安排后续支持",
                    ),
                ],
                alternative_reading=None,
            )
        )
    return LocalCodingOutput(shard_id=shard.shard_id, units=units)


def test_split_coding_input_sorts_154_turns_and_overlaps_last_six(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import split_coding_input

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    template = coding_input.turns[0]
    turns = [
        template.model_copy(
            update={
                "turn_id": f"turn-{sequence:03d}",
                "sequence": sequence,
                "text": f"第{sequence}话轮原文",
            }
        )
        for sequence in range(154, 0, -1)
    ]
    coding_input = coding_input.model_copy(update={"turns": turns})

    first, second = split_coding_input(coding_input)

    assert [turn.sequence for turn in first.turns] == list(range(1, 78))
    assert [turn.sequence for turn in second.turns] == list(range(72, 155))
    assert second.overlap_turn_ids == [f"turn-{item:03d}" for item in range(72, 78)]
    assert first.overlap_turn_ids == []
    assert first.work_record is None
    assert first.technical_interruptions == []
    assert second.work_record == coding_input.work_record
    assert second.technical_interruptions == coding_input.technical_interruptions
    assert first.session == second.session == coding_input.session
    assert first.termination == second.termination == coding_input.termination


def test_local_source_coverage_appends_only_missing_dialogue_and_record_fragments(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import (
        _ensure_local_source_coverage,
        split_coding_input,
    )
    from app.reports.scoring_rules import canonical_work_record_fragments

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    coding_input = coding_input.model_copy(
        update={
            "turns": [
                coding_input.turns[0],
                coding_input.turns[1].model_copy(
                    update={"speaker": TurnSpeaker.client}
                ),
            ]
        }
    )
    _, shard = split_coding_input(coding_input)
    assert shard.work_record is not None
    first_turn = shard.turns[0]
    model_unit = LocalCodedUnit(
        id="model-unit-kept-verbatim",
        summary="模型已经完成的局部编码。",
        initial_codes=["已编码"],
        refs=[
            DialogueRef(
                kind="dialogue",
                turn_id=first_turn.turn_id,
                quote=first_turn.text,
            ),
            WorkRecordRef(
                kind="work_record",
                field="problem_understanding",
                quote="压力、失眠和功能下降",
            ),
        ],
        source_role="worker",
        alternative_reading="仍需结合后文。",
    )
    raw = LocalCodingOutput(shard_id=shard.shard_id, units=[model_unit])

    completed = _ensure_local_source_coverage(shard, raw)
    completed_again = _ensure_local_source_coverage(shard, completed)

    assert completed.units[0] == model_unit
    assert completed_again == completed
    assert len({unit.id for unit in completed.units}) == len(completed.units)
    dialogue_passthrough = [
        unit
        for unit in completed.units
        if unit.initial_codes == ["待聚焦编码"]
        and isinstance(unit.refs[0], DialogueRef)
    ]
    assert [unit.refs[0].turn_id for unit in dialogue_passthrough] == [
        turn.turn_id for turn in shard.turns[1:] if turn.text.strip()
    ]
    assert [unit.source_role for unit in dialogue_passthrough] == ["client"]
    assert all(
        unit.summary == "原始话轮保留，待聚焦编码判断相关性。"
        and unit.alternative_reading is None
        for unit in dialogue_passthrough
    )

    expected_record_fragments = {
        (field, fragment)
        for field, fragments in canonical_work_record_fragments(
            shard.work_record
        ).items()
        for fragment in fragments
        if fragment.strip()
    }
    record_passthrough = [
        unit
        for unit in completed.units
        if unit.initial_codes == ["待聚焦编码"]
        and isinstance(unit.refs[0], WorkRecordRef)
    ]
    assert {
        (unit.refs[0].field, unit.refs[0].quote) for unit in record_passthrough
    } == expected_record_fragments
    assert all(unit.source_role == "work_record" for unit in record_passthrough)
    assert (
        "problem_understanding",
        shard.work_record.problem_understanding,
    ) in expected_record_fragments


def _single_turn_map_output(
    test_engine: Engine,
    *,
    quote: str | None,
) -> tuple[CodingShard, LocalCodingOutput]:
    from app.reports.report_pipeline import split_coding_input

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    coding_input = coding_input.model_copy(update={"turns": coding_input.turns[:1]})
    shard, _ = split_coding_input(coding_input)
    turn = shard.turns[0]
    ref_quote = turn.text if quote is None else quote
    output = LocalCodingOutput(
        shard_id=shard.shard_id,
        units=[
            LocalCodedUnit(
                id="model-unit",
                summary="模型提取的话轮局部内容。",
                initial_codes=["局部编码"],
                refs=[
                    DialogueRef(
                        kind="dialogue",
                        turn_id=turn.turn_id,
                        quote=ref_quote,
                    )
                ],
                source_role="worker",
                alternative_reading=None,
            )
        ],
    )
    return shard, output


def test_local_source_coverage_short_quote_does_not_cover_full_turn(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _ensure_local_source_coverage

    shard, output = _single_turn_map_output(
        test_engine,
        quote="先听你最难受的部分",
    )

    completed = _ensure_local_source_coverage(shard, output)

    turn = shard.turns[0]
    dialogue_quotes = [
        ref.quote
        for unit in completed.units
        for ref in unit.refs
        if isinstance(ref, DialogueRef) and ref.turn_id == turn.turn_id
    ]
    assert dialogue_quotes == ["先听你最难受的部分", turn.text]


def test_local_source_coverage_full_quote_does_not_append_duplicate(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _ensure_local_source_coverage

    shard, output = _single_turn_map_output(test_engine, quote=None)

    completed = _ensure_local_source_coverage(shard, output)

    turn = shard.turns[0]
    dialogue_refs = [
        ref
        for unit in completed.units
        for ref in unit.refs
        if isinstance(ref, DialogueRef) and ref.turn_id == turn.turn_id
    ]
    assert len(dialogue_refs) == 1
    assert dialogue_refs[0].quote == turn.text


def test_local_source_coverage_with_short_quote_is_idempotent(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _ensure_local_source_coverage

    shard, output = _single_turn_map_output(
        test_engine,
        quote="先听你最难受的部分",
    )

    completed = _ensure_local_source_coverage(shard, output)
    completed_again = _ensure_local_source_coverage(shard, completed)

    assert completed_again == completed


def test_cached_old_map_outputs_are_upgraded_with_source_coverage(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import (
        _map_stage_cache,
        _validated_cached_map_outputs,
        split_coding_input,
    )
    from app.reports.scoring_rules import canonical_work_record_fragments

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    shards = split_coding_input(coding_input)
    old_outputs: list[LocalCodingOutput] = []
    for shard in shards:
        turn = shard.turns[0]
        refs: list[DialogueRef | WorkRecordRef] = [
            DialogueRef(kind="dialogue", turn_id=turn.turn_id, quote=turn.text)
        ]
        if shard.work_record is not None:
            refs.append(
                WorkRecordRef(
                    kind="work_record",
                    field="problem_understanding",
                    quote="压力、失眠和功能下降",
                )
            )
        old_outputs.append(
            LocalCodingOutput(
                shard_id=shard.shard_id,
                units=[
                    LocalCodedUnit(
                        id=f"old-{shard.shard_id}",
                        summary="旧缓存局部单元。",
                        initial_codes=["旧编码"],
                        refs=refs,
                        source_role="worker",
                        alternative_reading=None,
                    )
                ],
            )
        )

    upgraded = _validated_cached_map_outputs(_map_stage_cache(old_outputs), shards)

    assert upgraded is not None
    for shard, output in zip(shards, upgraded, strict=True):
        covered_turns = {
            ref.turn_id
            for unit in output.units
            for ref in unit.refs
            if isinstance(ref, DialogueRef)
        }
        assert covered_turns == {turn.turn_id for turn in shard.turns if turn.text.strip()}
        if shard.work_record is None:
            continue
        record_refs = [
            ref
            for unit in output.units
            for ref in unit.refs
            if isinstance(ref, WorkRecordRef)
        ]
        for field, fragments in canonical_work_record_fragments(shard.work_record).items():
            for fragment in fragments:
                if fragment.strip():
                    assert any(
                        ref.field == field and fragment in ref.quote for ref in record_refs
                    )
def test_split_coding_input_keeps_single_turn_in_both_shards(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import split_coding_input

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    coding_input = coding_input.model_copy(update={"turns": coding_input.turns[:1]})

    first, second = split_coding_input(coding_input)

    assert [turn.turn_id for turn in first.turns] == ["turn-one"]
    assert [turn.turn_id for turn in second.turns] == ["turn-one"]
    assert first.work_record is None
    assert second.work_record == coding_input.work_record
    assert second.overlap_turn_ids == ["turn-one"]


def test_split_coding_input_does_not_invent_turns_for_empty_input(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import split_coding_input

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    coding_input = coding_input.model_copy(update={"turns": []})

    first, second = split_coding_input(coding_input)

    assert first.turns == second.turns == []
    assert first.overlap_turn_ids == second.overlap_turn_ids == []
    assert first.work_record is None
    assert second.work_record == coding_input.work_record


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("wrong_shard", "shard_id"),
        ("duplicate_unit", "unit id"),
        ("unknown_turn", "话轮"),
        ("non_contiguous_dialogue", "连续子串"),
        ("work_record_in_first", "工作记录"),
        ("non_contiguous_work_record", "连续子串"),
        ("audio_event", "音频"),
        ("wrong_speaker_role", "source_role"),
        ("interaction_without_dialogue", "interaction"),
        ("work_record_without_record_ref", "work_record"),
        ("empty_units", "至少.*一个"),
    ],
)
def test_local_contract_rejects_invalid_shard_material_refs(
    test_engine: Engine,
    change: str,
    message: str,
) -> None:
    from app.reports.report_pipeline import (
        LocalBatchError,
        _validate_local_contract,
        split_coding_input,
    )

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    first, second = split_coding_input(coding_input)
    shard = first if change == "work_record_in_first" else second
    output = _local_output(shard)
    if change == "empty_units":
        output = LocalCodingOutput(shard_id=shard.shard_id, units=[])
    elif change == "wrong_shard":
        output.shard_id = "another-shard"
    elif change == "duplicate_unit":
        output = LocalCodingOutput.model_construct(
            shard_id=shard.shard_id,
            units=[output.units[0], output.units[0]],
        )
    elif change == "unknown_turn":
        output.units[0].refs = [
            DialogueRef(kind="dialogue", turn_id="missing-turn", quote="原文")
        ]
    elif change == "non_contiguous_dialogue":
        output.units[0].refs = [
            DialogueRef(
                kind="dialogue",
                turn_id=shard.turns[0].turn_id,
                quote="不在对话中的文本",
            )
        ]
    elif change == "work_record_in_first":
        output.units[0].refs = [
            WorkRecordRef(
                kind="work_record",
                field="problem_understanding",
                quote="压力、失眠和功能下降",
            )
        ]
    elif change == "non_contiguous_work_record":
        output.units[-1].refs = [
            WorkRecordRef(
                kind="work_record",
                field="problem_understanding",
                quote="不在工作记录中的文本",
            )
        ]
    elif change == "audio_event":
        output.units[0].refs = [AudioEventRef(kind="audio_event", event_id="audio-1")]
    elif change == "wrong_speaker_role":
        output.units[0].source_role = "client"
    elif change == "interaction_without_dialogue":
        output.units[-1].source_role = "interaction"
    else:
        output.units[0].source_role = "work_record"

    with pytest.raises(LocalBatchError, match=message):
        _validate_local_contract(shard, output)


def test_local_contract_accepts_dialogue_and_work_record_substrings(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import (
        _validate_local_contract,
        split_coding_input,
    )

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    shards = split_coding_input(coding_input)

    for shard in shards:
        _validate_local_contract(shard, _local_output(shard))


def test_global_contract_rejects_client_quote_used_as_worker_competency_evidence(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import GlobalBatchError, _validate_global_contract

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    turns = list(coding_input.turns)
    turns[0] = turns[0].model_copy(update={"speaker": TurnSpeaker.client})
    coding_input = coding_input.model_copy(update={"turns": turns})

    with pytest.raises(GlobalBatchError, match="worker 话轮"):
        _validate_global_contract(coding_input, _global_output(), ANALYSIS_TARGETS)


def test_global_contract_reports_exact_evidence_rejection_to_reduce_repair(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import GlobalBatchError, _validate_global_contract

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    output = _global_output()
    evidence = next(
        item
        for item in output.coded_evidence
        if item.target is CoreDimension.respectful_communication
    )
    evidence.unit_id = "unit-two"

    with pytest.raises(GlobalBatchError, match="source_not_in_unit"):
        _validate_global_contract(coding_input, output, TARGETS)


def test_non_fabricated_bottom_line_requires_at_least_one_worker_dialogue_ref(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import GlobalBatchError, _validate_global_contract

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    turns = list(coding_input.turns)
    turns[0] = turns[0].model_copy(update={"speaker": TurnSpeaker.client})
    coding_input = coding_input.model_copy(update={"turns": turns})
    output = _global_output()
    output.coded_evidence = [
        item
        for item in output.coded_evidence
        if not (
            isinstance(item.ref, DialogueRef) and item.ref.turn_id == "turn-one"
        )
    ]
    output.bottom_line_candidates = [
        BottomLineCandidate(
            category=SemanticBottomLineCategory.false_confidentiality,
            refs=[
                DialogueRef(
                    kind="dialogue",
                    turn_id="turn-one",
                    quote="我会先听你最难受的部分",
                )
            ],
            context="来电者对保密边界作出反应。",
            repair_observed=False,
            reasoning="候选不得只依赖来电者反应。",
        )
    ]

    with pytest.raises(GlobalBatchError, match="至少包含一条 worker"):
        _validate_global_contract(coding_input, output, ANALYSIS_TARGETS)


def test_reduce_source_normalization_filters_roles_but_keeps_qualitative_units(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _normalize_reduce_output_sources
    from app.reports.scoring_domain import (
        RiskDisclosureCategory,
        RiskDisclosureImmediacy,
        UrgentRiskDisclosureCandidate,
    )

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    turns = list(coding_input.turns)
    turns[0] = turns[0].model_copy(update={"speaker": TurnSpeaker.client})
    coding_input = coding_input.model_copy(update={"turns": turns})
    output = _global_output()
    target = CoreDimension.listening_and_emotion
    client_evidence = next(
        item
        for item in output.coded_evidence
        if item.target is target
        and isinstance(item.ref, DialogueRef)
        and item.ref.turn_id == "turn-one"
    )
    output.coded_evidence = [
        item
        for item in output.coded_evidence
        if item.target is not target
    ] + [client_evidence]
    target_check = next(item for item in output.counter_checks if item.target is target)
    target_check.found = [client_evidence]
    target_check.not_found_note = None
    output.bottom_line_candidates = [
        BottomLineCandidate(
            category=SemanticBottomLineCategory.false_confidentiality,
            refs=[client_evidence.ref],
            context="只有来电者话轮。",
            repair_observed=False,
            reasoning="应由来源规则剔除。",
        ),
        BottomLineCandidate(
            category=SemanticBottomLineCategory.fabricated_record,
            conflict_id="conflict-one",
            refs=[client_evidence.ref],
            context="编造记录候选允许通过冲突两侧核验。",
            repair_observed=False,
            reasoning="该类别不要求 worker 对话引用。",
        ),
    ]
    conflict = MaterialConflict(
        id="conflict-one",
        dialogue_ref=client_evidence.ref,
        work_record_ref=WorkRecordRef(
            kind="work_record",
            field="problem_understanding",
            quote="压力、失眠和功能下降",
        ),
        description="两侧材料不一致。",
        affected_targets=[CoreDimension.documentation],
        impact="需要回看原文。",
    )
    output.material_conflict_candidates = [conflict]
    output.urgent_risk_disclosure_candidates = [
        UrgentRiskDisclosureCandidate(
            ref=DialogueRef(
                kind="dialogue",
                turn_id="turn-two",
                quote="一起核对下一步",
            ),
            category=RiskDisclosureCategory.suicide_or_self_harm,
            immediacy=RiskDisclosureImmediacy.imminent,
        )
    ]

    partition = _normalize_reduce_output_sources(coding_input, output)

    assert partition.usable_output.units == output.units
    assert not [
        item
        for item in partition.usable_output.coded_evidence
        if item.target is target
    ]
    usable_check = next(
        item
        for item in partition.usable_output.counter_checks
        if item.target is target
    )
    assert usable_check.found == []
    assert usable_check.not_found_note is not None
    assert "来源规则" in usable_check.not_found_note
    assert target in partition.rejected_by_target
    assert [item.category for item in partition.usable_output.bottom_line_candidates] == [
        SemanticBottomLineCategory.fabricated_record
    ]
    assert partition.usable_output.material_conflict_candidates == [conflict]
    assert partition.usable_output.urgent_risk_disclosure_candidates == []


def test_reduce_source_normalization_rebinds_only_unique_exact_source_without_inventing_scope(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _normalize_reduce_output_sources

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    output = _global_output()
    target = CoreDimension.respectful_communication
    evidence = next(item for item in output.coded_evidence if item.target is target)
    evidence.unit_id = "unit-two"
    check = next(item for item in output.counter_checks if item.target is target)
    check.searched_unit_ids = ["unit-two"]
    check.found = [
        evidence.model_copy(
            update={
                "unit_id": "unit-two",
                "direction": EvidenceDirection.limit,
            }
        )
    ]
    check.not_found_note = None

    partition = _normalize_reduce_output_sources(coding_input, output)

    normalized = next(
        item
        for item in partition.usable_output.coded_evidence
        if item.target is target and item.ref == evidence.ref
    )
    assert normalized.unit_id == "unit-one"
    normalized_check = next(
        item
        for item in partition.usable_output.counter_checks
        if item.target is target
    )
    assert normalized_check.found[0].unit_id == "unit-one"
    assert normalized_check.searched_unit_ids == ["unit-one"]

    ambiguous = output.model_copy(
        update={
            "units": [
                *output.units,
                MeaningUnit(
                    id="unit-duplicate-source",
                    turn_ids=["turn-one"],
                    summary="同一话轮的另一意义单元。",
                ),
            ]
        }
    )
    ambiguous_partition = _normalize_reduce_output_sources(coding_input, ambiguous)
    ambiguous_evidence = next(
        item
        for item in ambiguous_partition.usable_output.coded_evidence
        if item.target is target and item.ref == evidence.ref
    )
    assert ambiguous_evidence.unit_id == "unit-two"


def test_reduce_source_normalization_builds_carrier_for_orphan_exact_evidence(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _normalize_reduce_output_sources

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    output = _global_output()
    target = CoreDimension.respectful_communication
    evidence = next(item for item in output.coded_evidence if item.target is target)
    assert isinstance(evidence.ref, DialogueRef)
    for unit in output.units:
        unit.turn_ids = [
            turn_id for turn_id in unit.turn_ids if turn_id != evidence.ref.turn_id
        ]

    partition = _normalize_reduce_output_sources(coding_input, output)
    normalized = next(
        item
        for item in partition.usable_output.coded_evidence
        if item.target is target and item.ref == evidence.ref
    )
    carrier = next(
        unit
        for unit in partition.usable_output.units
        if unit.id == normalized.unit_id
    )

    assert carrier.turn_ids == [evidence.ref.turn_id]
    assert carrier.summary == evidence.context


def test_reduce_source_normalization_does_not_build_carrier_for_invalid_source(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _normalize_reduce_output_sources

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    output = _global_output()
    target = CoreDimension.respectful_communication
    evidence = next(item for item in output.coded_evidence if item.target is target)
    evidence.unit_id = "model-invented-unit"
    evidence.ref = DialogueRef(
        kind="dialogue",
        turn_id="model-invented-turn",
        quote="原始逐字稿里不存在的句子",
    )

    partition = _normalize_reduce_output_sources(coding_input, output)
    normalized = next(
        item
        for item in partition.usable_output.coded_evidence
        if item.target is target
    )

    assert normalized.unit_id == "model-invented-unit"
    assert not [
        unit
        for unit in partition.usable_output.units
        if unit.id.startswith("source-passthrough-evidence-")
    ]


def test_reduce_source_normalization_does_not_claim_carrier_was_searched(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _normalize_reduce_output_sources

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    output = _global_output()
    target = CoreDimension.respectful_communication
    evidence = next(item for item in output.coded_evidence if item.target is target)
    assert isinstance(evidence.ref, DialogueRef)
    for unit in output.units:
        unit.turn_ids = [
            turn_id for turn_id in unit.turn_ids if turn_id != evidence.ref.turn_id
        ]
    check = next(item for item in output.counter_checks if item.target is target)
    check.searched_unit_ids = [unit.id for unit in output.units]

    partition = _normalize_reduce_output_sources(coding_input, output)

    normalized_check = next(
        item
        for item in partition.usable_output.counter_checks
        if item.target is target
    )
    carrier_ids = {
        unit.id
        for unit in partition.usable_output.units
        if unit.id.startswith("source-passthrough-evidence-")
    }
    assert carrier_ids
    assert not (carrier_ids & set(normalized_check.searched_unit_ids))


def test_reduce_source_normalization_keeps_reported_counter_search_scope(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _normalize_reduce_output_sources

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    output = _global_output()
    target = CoreDimension.respectful_communication
    check = next(item for item in output.counter_checks if item.target is target)
    check.searched_unit_ids = ["unit-one"]

    partition = _normalize_reduce_output_sources(coding_input, output)

    normalized_check = next(
        item
        for item in partition.usable_output.counter_checks
        if item.target is target
    )
    assert normalized_check.searched_unit_ids == ["unit-one"]


def test_reduce_source_normalization_removes_counter_candidate_that_duplicates_initial_evidence(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _normalize_reduce_output_sources

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    output = _global_output()
    target = CoreDimension.respectful_communication
    evidence = next(item for item in output.coded_evidence if item.target is target)
    check = next(item for item in output.counter_checks if item.target is target)
    check.found = [
        evidence.model_copy(
            update={
                "strength": EvidenceStrength.weak,
                "context": "模型把初始编码原样放进了反例候选。",
            }
        )
    ]
    check.not_found_note = "原有反例检索说明应予保留。"

    partition = _normalize_reduce_output_sources(coding_input, output)

    normalized_check = next(
        item
        for item in partition.usable_output.counter_checks
        if item.target is target
    )
    assert normalized_check.found == []
    assert normalized_check.not_found_note == "原有反例检索说明应予保留。"


@pytest.mark.parametrize("candidate_kind", ["different_direction", "different_ref"])
def test_reduce_source_normalization_keeps_independent_counter_candidate(
    test_engine: Engine,
    candidate_kind: str,
) -> None:
    from app.reports.report_pipeline import _normalize_reduce_output_sources

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    output = _global_output()
    target = CoreDimension.respectful_communication
    evidence = next(item for item in output.coded_evidence if item.target is target)
    update: dict[str, object]
    if candidate_kind == "different_direction":
        update = {"direction": EvidenceDirection.limit}
    else:
        update = {
            "ref": DialogueRef(
                kind="dialogue",
                turn_id="turn-one",
                quote="也会直接确认现在是否有自伤想法",
            )
        }
    candidate = evidence.model_copy(update=update)
    check = next(item for item in output.counter_checks if item.target is target)
    check.found = [candidate]
    check.not_found_note = None

    partition = _normalize_reduce_output_sources(coding_input, output)

    normalized_check = next(
        item
        for item in partition.usable_output.counter_checks
        if item.target is target
    )
    assert normalized_check.found == [candidate]
    assert normalized_check.not_found_note is None


def test_reduce_source_normalization_explains_duplicate_counter_candidates(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import _normalize_reduce_output_sources

    job = _create_job(test_engine)
    coding_input, _ = _opportunity_inputs(test_engine, job)
    output = _global_output()
    target = CoreDimension.respectful_communication
    evidence = next(item for item in output.coded_evidence if item.target is target)
    check = next(item for item in output.counter_checks if item.target is target)
    check.found = [evidence.model_copy()]
    check.not_found_note = None

    partition = _normalize_reduce_output_sources(coding_input, output)

    normalized_check = next(
        item
        for item in partition.usable_output.counter_checks
        if item.target is target
    )
    assert normalized_check.found == []
    assert (
        normalized_check.not_found_note
        == "返回候选与初始编码重复，未形成独立反例。"
    )


def test_build_validated_targets_marks_only_targets_losing_all_role_valid_candidates(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import (
        _build_validated_targets,
        _normalize_reduce_output_sources,
        check_opportunities,
    )

    job = _create_job(test_engine)
    coding_input, opportunity_input = _opportunity_inputs(test_engine, job)
    turns = list(coding_input.turns)
    turns[0] = turns[0].model_copy(update={"speaker": TurnSpeaker.client})
    coding_input = coding_input.model_copy(update={"turns": turns})
    output = _global_output()
    lost_target = CoreDimension.listening_and_emotion
    mixed_target = CoreDimension.respectful_communication
    empty_target = CoreDimension.concern_clarification
    output.coded_evidence = [
        item
        for item in output.coded_evidence
        if item.target not in {lost_target, empty_target}
        or (
            item.target is lost_target
            and isinstance(item.ref, DialogueRef)
            and item.ref.turn_id == "turn-one"
        )
    ]

    partition = _normalize_reduce_output_sources(coding_input, output)
    opportunities = check_opportunities(coding_input, opportunity_input)
    validated = _build_validated_targets(
        coding_input,
        opportunities,
        partition.usable_output,
        [lost_target, mixed_target, empty_target],
        rejected_by_target=partition.rejected_by_target,
    )

    assert validated[lost_target].analysis_failed is True
    assert validated[mixed_target].analysis_failed is False
    assert validated[empty_target].analysis_failed is False
    assert validated[empty_target].packet.units == []
    assert {unit.id for unit in validated[mixed_target].packet.units} == {"unit-two"}


def _proposal(packet: DimensionPacket) -> LevelProposal:
    unit_ids = list(
        dict.fromkeys(item.evidence.unit_id for item in packet.evidence)
    )
    if not unit_ids:
        return LevelProposal(
            target=packet.target,
            proposed_level=None,
            pattern="",
            rationale="当前没有可用于定级的证据。",
            representative_units=[],
            limiting_units=[],
            next_level_gap=[],
            evidence_confidence=EvidenceConfidence.low,
            evidence_confidence_factors=["没有可用 primary 证据"],
        )
    return LevelProposal(
        target=packet.target,
        proposed_level=3,
        pattern="能围绕当前材料形成清楚、可核对的工作回应。",
        rationale="两段独立材料均支持当前等级，且未见足以改变判断的反例。",
        representative_units=unit_ids[:2],
        limiting_units=[],
        next_level_gap=["在更复杂或受阻情形下继续观察调整过程。"],
        evidence_confidence=EvidenceConfidence.high,
        evidence_confidence_factors=["两段独立原始材料", "引用可回到原文核对"],
    )


class FakeGateway:
    def __init__(
        self,
        *,
        global_output: GlobalCodingOutput | None = None,
        fail_global: bool = False,
        fail_map_shard: str | None = None,
        fail_group: ScoringGroup | None = None,
    ) -> None:
        self.global_output = global_output
        self.fail_global = fail_global
        self.fail_map_shard = fail_map_shard
        self.fail_group = fail_group
        self.calls: list[str] = []
        self.received_global: list[dict[str, Any]] = []
        self.received_global_targets: list[list[Target]] = []
        self.received_active_target_briefs: list[list[object]] = []
        self.received_reduce_contexts: list[tuple[Scene, Media]] = []
        self.received_packets: list[DimensionPacket] = []
        self.received_model_configs: list[object | None] = []
        self.validation_feedbacks: list[tuple[str, str | None]] = []

    async def code_shard(
        self,
        shard: CodingShard,
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> LocalCodingOutput:
        del session_id, call_kind
        self.calls.append(f"map:{shard.shard_id}")
        self.validation_feedbacks.append((f"map:{shard.shard_id}", validation_feedback))
        self.received_model_configs.append(model_config)
        payload = shard.model_dump(mode="json")
        self.received_global.append(payload)
        if self.fail_map_shard == shard.shard_id:
            raise RuntimeError(f"{shard.shard_id} failed")
        return _local_output(shard)

    async def reduce_coding(
        self,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        scene: Scene,
        media: Media,
        targets: Sequence[Target] = ANALYSIS_TARGETS,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        turn_speakers: dict[str, str] | None = None,
        active_target_briefs: Sequence[object] = (),
        validation_feedback: str | None = None,
    ) -> GlobalCodingOutput:
        del session_id, call_kind, local_outputs, turn_speakers
        self.calls.append("reduce")
        self.received_reduce_contexts.append((scene, media))
        self.validation_feedbacks.append(("reduce", validation_feedback))
        self.received_model_configs.append(model_config)
        self.received_global_targets.append(list(targets))
        self.received_active_target_briefs.append(list(active_target_briefs))
        if self.fail_global:
            raise RuntimeError("reduce failed")
        return self.global_output or _global_output(targets, counter_targets=targets)

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput:
        del session_id, call_kind
        self.calls.append(group.value)
        self.received_packets.extend(packets)
        self.validation_feedbacks.append((group.value, validation_feedback))
        self.received_model_configs.append(model_config)
        if self.fail_group is group:
            raise RuntimeError(f"{group.value} failed")
        return GroupScoringOutput(proposals=[_proposal(packet) for packet in packets])


class CeilingRetryGateway(FakeGateway):
    def __init__(self) -> None:
        super().__init__()
        self._interaction_attempts = 0

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput:
        output = await super().score_group(
            group,
            packets,
            session_id=session_id,
            call_kind=call_kind,
            model_config=model_config,
            validation_feedback=validation_feedback,
        )
        if group is ScoringGroup.interaction:
            self._interaction_attempts += 1
            output.proposals[0].proposed_level = (
                4 if self._interaction_attempts == 1 else 2
            )
        return output


class InvalidProposalGateway(FakeGateway):
    def __init__(self, invalid_kind: str | None) -> None:
        super().__init__()
        self.invalid_kind = invalid_kind

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput:
        output = await super().score_group(
            group,
            packets,
            session_id=session_id,
            call_kind=call_kind,
            model_config=model_config,
            validation_feedback=validation_feedback,
        )
        if group is ScoringGroup.interaction and self.invalid_kind == "empty_pattern":
            output.proposals[0].pattern = ""
        elif group is ScoringGroup.interaction and self.invalid_kind == "unknown_unit":
            output.proposals[0].representative_units = ["unit-does-not-exist"]
        return output


class NullLevelGateway(FakeGateway):
    def __init__(self, *, recover_after_first: bool) -> None:
        super().__init__()
        self.recover_after_first = recover_after_first
        self.interaction_attempts = 0

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput:
        output = await super().score_group(
            group,
            packets,
            session_id=session_id,
            call_kind=call_kind,
            model_config=model_config,
            validation_feedback=validation_feedback,
        )
        if group is ScoringGroup.interaction:
            self.interaction_attempts += 1
            if not self.recover_after_first or self.interaction_attempts == 1:
                proposal = output.proposals[0]
                proposal.proposed_level = None
                proposal.pattern = ""
                proposal.representative_units = []
                proposal.limiting_units = []
                proposal.next_level_gap = []
                proposal.evidence_confidence_factors = []
        return output


class NonRetryableGateway(FakeGateway):
    def __init__(self, *, failing_batch: str) -> None:
        super().__init__()
        self.failing_batch = failing_batch

    async def code_shard(
        self,
        shard: CodingShard,
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> LocalCodingOutput:
        if self.failing_batch == f"map:{shard.shard_id}":
            self.calls.append(f"map:{shard.shard_id}")
            raise NonRetryableRuntimeModelError("认证失败")
        return await super().code_shard(
            shard,
            session_id=session_id,
            call_kind=call_kind,
            model_config=model_config,
            validation_feedback=validation_feedback,
        )

    async def reduce_coding(
        self,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        scene: Scene,
        media: Media,
        targets: Sequence[Target] = ANALYSIS_TARGETS,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        turn_speakers: dict[str, str] | None = None,
        active_target_briefs: Sequence[object] = (),
        validation_feedback: str | None = None,
    ) -> GlobalCodingOutput:
        if self.failing_batch in {"global", "reduce"}:
            self.calls.append("reduce")
            raise NonRetryableRuntimeModelError("认证失败")
        return await super().reduce_coding(
            local_outputs,
            session_id=session_id,
            targets=targets,
            call_kind=call_kind,
            model_config=model_config,
            scene=scene,
            media=media,
            turn_speakers=turn_speakers,
            active_target_briefs=active_target_briefs,
            validation_feedback=validation_feedback,
        )

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput:
        if self.failing_batch == group.value:
            self.calls.append(group.value)
            raise NonRetryableRuntimeModelError("认证失败")
        return await super().score_group(
            group,
            packets,
            session_id=session_id,
            call_kind=call_kind,
            model_config=model_config,
            validation_feedback=validation_feedback,
        )


class PhaseBarrierGateway(FakeGateway):
    def __init__(self) -> None:
        super().__init__()
        self.phase_log: list[str] = []
        self.map_started = {
            "shard-1": asyncio.Event(),
            "shard-2": asyncio.Event(),
        }
        self.map_finished: set[str] = set()
        self.group_started = {group: asyncio.Event() for group in ScoringGroup}

    async def code_shard(
        self,
        shard: CodingShard,
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> LocalCodingOutput:
        self.phase_log.append(f"map-start:{shard.shard_id}")
        self.map_started[shard.shard_id].set()
        await asyncio.gather(*(event.wait() for event in self.map_started.values()))
        output = await super().code_shard(
            shard,
            session_id=session_id,
            call_kind=call_kind,
            model_config=model_config,
            validation_feedback=validation_feedback,
        )
        self.map_finished.add(shard.shard_id)
        self.phase_log.append(f"map-finish:{shard.shard_id}")
        return output

    async def reduce_coding(
        self,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        scene: Scene,
        media: Media,
        targets: Sequence[Target] = ANALYSIS_TARGETS,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        turn_speakers: dict[str, str] | None = None,
        active_target_briefs: Sequence[object] = (),
        validation_feedback: str | None = None,
    ) -> GlobalCodingOutput:
        assert self.map_finished == {"shard-1", "shard-2"}
        self.phase_log.append("reduce-start")
        return await super().reduce_coding(
            local_outputs,
            session_id=session_id,
            targets=targets,
            call_kind=call_kind,
            model_config=model_config,
            turn_speakers=turn_speakers,
            scene=scene,
            media=media,
            active_target_briefs=active_target_briefs,
            validation_feedback=validation_feedback,
        )

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput:
        self.phase_log.append(f"group-start:{group.value}")
        self.group_started[group].set()
        await asyncio.gather(*(event.wait() for event in self.group_started.values()))
        output = await super().score_group(
            group,
            packets,
            session_id=session_id,
            call_kind=call_kind,
            model_config=model_config,
            validation_feedback=validation_feedback,
        )
        self.phase_log.append(f"group-finish:{group.value}")
        return output


class InvalidThenValidMapGateway(FakeGateway):
    def __init__(self, failing_shard_id: str = "shard-1") -> None:
        super().__init__()
        self.failing_shard_id = failing_shard_id
        self.map_attempts: dict[str, int] = {}

    async def code_shard(
        self,
        shard: CodingShard,
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> LocalCodingOutput:
        output = await super().code_shard(
            shard,
            session_id=session_id,
            call_kind=call_kind,
            model_config=model_config,
            validation_feedback=validation_feedback,
        )
        attempt = self.map_attempts.get(shard.shard_id, 0) + 1
        self.map_attempts[shard.shard_id] = attempt
        if shard.shard_id == self.failing_shard_id and attempt == 1:
            output.shard_id = "wrong-shard"
        return output


class PartialMapSourceGateway(FakeGateway):
    async def code_shard(
        self,
        shard: CodingShard,
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> LocalCodingOutput:
        output = await super().code_shard(
            shard,
            session_id=session_id,
            call_kind=call_kind,
            model_config=model_config,
            validation_feedback=validation_feedback,
        )
        for unit in output.units:
            for ref in unit.refs:
                if isinstance(ref, DialogueRef) and ref.turn_id == "turn-one":
                    ref.quote = "我会先听你"
        return output


def test_dimension_packets_preserve_rule_assigned_evidence_roles(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import (
        _build_validated_targets,
        check_opportunities,
    )

    job = _create_job(test_engine)
    coding_input, opportunity_input = _opportunity_inputs(test_engine, job)
    opportunities = check_opportunities(coding_input, opportunity_input)
    validated = _build_validated_targets(
        coding_input,
        opportunities,
        _global_output(),
        TARGETS,
    )

    packet_json = validated[CoreDimension.respectful_communication].packet.model_dump(
        mode="json"
    )
    assert packet_json["evidence"]
    assert {item["role"] for item in packet_json["evidence"]} == {"primary"}


def test_packets_for_group_only_include_targets_with_sufficient_primary_units() -> None:
    from app.reports.report_pipeline import ReportPipeline, ValidatedTarget
    from app.reports.scoring_rules import RoutedEvidence

    def validated_target(
        target: CoreDimension,
        *,
        primary_unit_count: int,
    ) -> ValidatedTarget:
        indicator_id = get_rubric(target).indicators[0].id
        units = [
            MeaningUnit(
                id=f"{target.value}-unit-{index}",
                turn_ids=[f"{target.value}-turn-{index}"],
                summary=f"第 {index} 个独立意义单元。",
            )
            for index in range(1, primary_unit_count + 1)
        ]
        evidence = [
            RoutedEvidence(
                evidence=CodedEvidence(
                    unit_id=unit.id,
                    target=target,
                    indicator_id=indicator_id,
                    direction=EvidenceDirection.support,
                    strength=EvidenceStrength.moderate,
                    context="可以用于定级的独立对话证据。",
                    alternative_reading=None,
                    ref=DialogueRef(
                        kind="dialogue",
                        turn_id=unit.turn_ids[0],
                        quote=f"原话 {unit.id}",
                    ),
                ),
                role=EvidenceRole.primary,
            )
            for unit in units
        ]
        packet = DimensionPacket(
            scene=Scene.hotline,
            media=Media.voice,
            target=target,
            rubric=get_rubric(target),
            evidence=[
                PacketEvidence(evidence=item.evidence, role=item.role)
                for item in evidence
            ],
            counter_evidence=[],
            units=units,
            opportunities=[
                OpportunityOutcome(
                    declared_target=target,
                    kind=OpportunityKind.required,
                    fulfilled=True,
                    indicator_ids=[indicator_id],
                ),
                OpportunityOutcome(
                    declared_target=target,
                    kind=OpportunityKind.conditional,
                    fulfilled=False,
                    indicator_ids=[indicator_id],
                ),
            ],
            conditional_unavailable=[],
            level_ceiling=3,
        )
        return ValidatedTarget(
            packet=packet,
            evidence=evidence,
            counter_evidence=[],
            analysis_failed=False,
            technical_failure=False,
        )

    one_primary = validated_target(
        CoreDimension.respectful_communication,
        primary_unit_count=1,
    )
    two_independent_primary = validated_target(
        CoreDimension.listening_and_emotion,
        primary_unit_count=2,
    )

    packets = ReportPipeline._packets_for_group(
        ScoringGroup.interaction,
        {
            one_primary.packet.target: one_primary,
            two_independent_primary.packet.target: two_independent_primary,
        },
    )

    assert [packet.target for packet in packets] == [
        CoreDimension.listening_and_emotion
    ]


def test_group_output_rejects_supporting_evidence_without_primary_support() -> None:
    from app.reports.report_pipeline import GroupBatchError, _validate_group_output

    target = CoreDimension.respectful_communication
    unit = MeaningUnit(
        id="unit-audio",
        audio_event_ids=["audio-1"],
        summary="只有补充性的音频事件。",
    )
    evidence = CodedEvidence(
        unit_id=unit.id,
        target=target,
        indicator_id="C1.respect",
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.moderate,
        context="音频只能补充对话证据。",
        alternative_reading=None,
        ref=AudioEventRef(kind="audio_event", event_id="audio-1"),
    )
    packet = DimensionPacket(
        scene=Scene.hotline,
        media=Media.voice,
        target=target,
        rubric=get_rubric(target),
        evidence=[
            PacketEvidence(evidence=evidence, role=EvidenceRole.supporting)
        ],
        counter_evidence=[],
        units=[unit],
        opportunities=[
            OpportunityOutcome(
                declared_target=target,
                kind=OpportunityKind.required,
                fulfilled=True,
                indicator_ids=["C1.respect"],
            )
        ],
        conditional_unavailable=[],
        level_ceiling=3,
    )
    proposal = LevelProposal(
        target=target,
        proposed_level=2,
        pattern="仅有补充材料。",
        rationale="补充材料不能独立支持等级。",
        representative_units=[unit.id],
        limiting_units=[],
        next_level_gap=["需要对话主要证据。"],
        evidence_confidence=EvidenceConfidence.low,
        evidence_confidence_factors=["只有音频补充材料"],
    )

    with pytest.raises(GroupBatchError, match="有效定级证据"):
        _validate_group_output(GroupScoringOutput(proposals=[proposal]), [packet])

    no_primary_proposal = proposal.model_copy(
        update={
            "proposed_level": None,
            "pattern": "",
            "rationale": "只有补充材料，无主要证据。",
                "representative_units": [],
                "next_level_gap": [],
                "evidence_confidence_factors": ["没有可用 primary 证据"],
        }
    )
    _validate_group_output(
        GroupScoringOutput(proposals=[no_primary_proposal]),
        [packet],
    )


def test_group_output_requires_primary_among_representative_units() -> None:
    from app.reports.report_pipeline import GroupBatchError, _validate_group_output

    target = CoreDimension.respectful_communication
    primary_unit = MeaningUnit(
        id="unit-primary",
        turn_ids=["turn-primary"],
        summary="对话主要证据。",
    )
    supporting_unit = MeaningUnit(
        id="unit-supporting",
        audio_event_ids=["audio-supporting"],
        summary="音频补充证据。",
    )
    primary = CodedEvidence(
        unit_id=primary_unit.id,
        target=target,
        indicator_id="C1.respect",
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.moderate,
        context="对话主要证据。",
        alternative_reading=None,
        ref=DialogueRef(
            kind="dialogue",
            turn_id="turn-primary",
            quote="我会听你说",
        ),
    )
    supporting = CodedEvidence(
        unit_id=supporting_unit.id,
        target=target,
        indicator_id="C1.respect",
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.moderate,
        context="音频补充证据。",
        alternative_reading=None,
        ref=AudioEventRef(
            kind="audio_event",
            event_id="audio-supporting",
        ),
    )
    packet = DimensionPacket(
        scene=Scene.hotline,
        media=Media.voice,
        target=target,
        rubric=get_rubric(target),
        evidence=[
            PacketEvidence(evidence=primary, role=EvidenceRole.primary),
            PacketEvidence(evidence=supporting, role=EvidenceRole.supporting),
        ],
        counter_evidence=[],
        units=[primary_unit, supporting_unit],
        opportunities=[
            OpportunityOutcome(
                declared_target=target,
                kind=OpportunityKind.required,
                fulfilled=True,
                indicator_ids=["C1.respect"],
            )
        ],
        conditional_unavailable=[],
        level_ceiling=3,
    )
    proposal = LevelProposal(
        target=target,
        proposed_level=2,
        pattern="只选择了补充证据。",
        rationale="代表单元必须包含主要证据。",
        representative_units=[supporting_unit.id],
        limiting_units=[],
        next_level_gap=["需要主要证据。"],
        evidence_confidence=EvidenceConfidence.low,
        evidence_confidence_factors=["当前代表单元只有补充证据"],
    )

    with pytest.raises(GroupBatchError, match="primary"):
        _validate_group_output(GroupScoringOutput(proposals=[proposal]), [packet])


def test_group_output_rejects_representative_selection_that_is_not_sufficient() -> None:
    from app.reports.report_pipeline import GroupBatchError, _validate_group_output

    target = CoreDimension.respectful_communication
    indicator_id = get_rubric(target).indicators[0].id
    units = [
        MeaningUnit(id="unit-first", turn_ids=["turn-first"], summary="第一处表现。"),
        MeaningUnit(id="unit-second", turn_ids=["turn-second"], summary="第二处表现。"),
    ]
    evidence = [
        CodedEvidence(
            unit_id=unit.id,
            target=target,
            indicator_id=indicator_id,
            direction=EvidenceDirection.support,
            strength=EvidenceStrength.moderate,
            context=unit.summary,
            alternative_reading=None,
            ref=DialogueRef(
                kind="dialogue",
                turn_id=unit.turn_ids[0],
                quote=unit.summary,
            ),
        )
        for unit in units
    ]
    packet = DimensionPacket(
        scene=Scene.hotline,
        media=Media.voice,
        target=target,
        rubric=get_rubric(target),
        evidence=[
            PacketEvidence(evidence=item, role=EvidenceRole.primary)
            for item in evidence
        ],
        counter_evidence=[],
        units=units,
        opportunities=[
            OpportunityOutcome(
                declared_target=target,
                kind=OpportunityKind.required,
                fulfilled=True,
                indicator_ids=[indicator_id],
            ),
            OpportunityOutcome(
                declared_target=target,
                kind=OpportunityKind.required,
                fulfilled=True,
                indicator_ids=[indicator_id],
            ),
        ],
        conditional_unavailable=[],
        level_ceiling=3,
    )
    proposal = LevelProposal(
        target=target,
        proposed_level=3,
        pattern="能够保持尊重。",
        rationale="模型只挑选了一处证据。",
        representative_units=["unit-first"],
        limiting_units=[],
        next_level_gap=["在第二个独立片段中继续呈现同类行为。"],
        evidence_confidence=EvidenceConfidence.medium,
        evidence_confidence_factors=["仅选择一个代表片段"],
    )

    with pytest.raises(GroupBatchError, match="代表性证据不足"):
        _validate_group_output(GroupScoringOutput(proposals=[proposal]), [packet])


def test_online_group_output_rejects_hotline_only_report_language() -> None:
    from app.reports.report_pipeline import GroupBatchError, _validate_group_output

    target = CoreDimension.respectful_communication
    indicator_id = get_rubric(target).indicators[0].id
    unit = MeaningUnit(id="unit-online", turn_ids=["turn-online"], summary="在线回应。")
    evidence = CodedEvidence(
        unit_id=unit.id,
        target=target,
        indicator_id=indicator_id,
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.moderate,
        context="在线回应保持尊重。",
        alternative_reading=None,
        ref=DialogueRef(
            kind="dialogue",
            turn_id="turn-online",
            quote="我在，你可以慢慢说。",
        ),
    )
    packet = DimensionPacket(
        scene=Scene.online,
        media=Media.text,
        target=target,
        rubric=get_rubric(target, media=Media.text),
        evidence=[PacketEvidence(evidence=evidence, role=EvidenceRole.primary)],
        counter_evidence=[],
        units=[unit],
        opportunities=[
            OpportunityOutcome(
                declared_target=target,
                kind=OpportunityKind.required,
                fulfilled=True,
                indicator_ids=[indicator_id],
            )
        ],
        conditional_unavailable=[],
        level_ceiling=3,
    )
    proposal = LevelProposal(
        target=target,
        proposed_level=2,
        pattern="本次热线通话中，接线员保持了倾听。",
        rationale="受测者的回应有原话支持。",
        representative_units=[unit.id],
        limiting_units=[],
        next_level_gap=["继续确认文字消息是否便于理解。"],
        evidence_confidence=EvidenceConfidence.medium,
        evidence_confidence_factors=["原话可回看"],
    )

    with pytest.raises(GroupBatchError, match="在线文字报告包含热线专属措辞"):
        _validate_group_output(GroupScoringOutput(proposals=[proposal]), [packet])


def test_online_group_output_allows_hotline_as_a_referral_resource() -> None:
    from app.reports.report_pipeline import _validate_group_output

    target = CoreDimension.respectful_communication
    indicator_id = get_rubric(target, media=Media.text).indicators[0].id
    unit = MeaningUnit(id="unit-online", turn_ids=["turn-online"], summary="在线回应。")
    evidence = CodedEvidence(
        unit_id=unit.id,
        target=target,
        indicator_id=indicator_id,
        direction=EvidenceDirection.support,
        strength=EvidenceStrength.moderate,
        context="在线回应保持尊重。",
        alternative_reading=None,
        ref=DialogueRef(
            kind="dialogue",
            turn_id="turn-online",
            quote="如果今晚又觉得不安全，可以拨打心理援助热线。",
        ),
    )
    packet = DimensionPacket(
        scene=Scene.online,
        media=Media.text,
        target=target,
        rubric=get_rubric(target, media=Media.text),
        evidence=[PacketEvidence(evidence=evidence, role=EvidenceRole.primary)],
        counter_evidence=[],
        units=[unit],
        opportunities=[
            OpportunityOutcome(
                declared_target=target,
                kind=OpportunityKind.required,
                fulfilled=True,
                indicator_ids=[indicator_id],
            )
        ],
        conditional_unavailable=[],
        level_ceiling=3,
    )
    proposal = LevelProposal(
        target=target,
        proposed_level=2,
        pattern="受测者区分了她看到丈夫来电和是否与丈夫通话两件事。",
        rationale="本次建议必要时拨打心理援助热线，属于可执行的现实资源说明。",
        representative_units=[unit.id],
        limiting_units=[],
        next_level_gap=["继续确认来访者是否理解这项资源。"],
        evidence_confidence=EvidenceConfidence.medium,
        evidence_confidence_factors=["原话可回看"],
    )

    _validate_group_output(GroupScoringOutput(proposals=[proposal]), [packet])


def test_online_global_output_checks_public_narrative_but_not_evidence_quote() -> None:
    from app.reports.report_pipeline import (
        GlobalBatchError,
        _validate_global_media_language,
    )

    output = _global_output([CoreDimension.respectful_communication])
    evidence = output.coded_evidence[0]
    evidence.ref.quote = "我建议你必要时拨打心理援助热线。"
    output.coded_evidence[0] = evidence.model_copy(
        update={
            "context": "她看到丈夫来电后，仍不能确认丈夫是否与对方通话。"
        }
    )
    _validate_global_media_language(output, Media.text)

    output.coded_evidence[0] = output.coded_evidence[0].model_copy(
        update={"context": "接线员在本次通话中留出了回应空间。"}
    )
    with pytest.raises(GlobalBatchError, match="在线文字报告包含热线专属措辞"):
        _validate_global_media_language(output, Media.text)


@pytest.mark.parametrize(
    ("fact_depths", "expected_modules"),
    [
        ({}, [SpecialModule.basic_risk_screening]),
        (
            {"minimum_risk_cue": 1},
            [
                SpecialModule.basic_risk_screening,
                SpecialModule.full_risk_appraisal,
            ],
        ),
        (
            {
                "minimum_risk_cue": 1,
                "suicidal_ideation": 1,
                "timing_intent": 2,
            },
            [
                SpecialModule.basic_risk_screening,
                SpecialModule.full_risk_appraisal,
                SpecialModule.safety_response,
            ],
        ),
    ],
)
def test_opportunity_check_activates_only_declared_modules_after_threshold(
    test_engine: Engine,
    fact_depths: dict[str, int],
    expected_modules: list[SpecialModule],
) -> None:
    from app.reports.report_pipeline import check_opportunities

    job = _create_job(test_engine)
    coding_input, opportunity_input = _opportunity_inputs(
        test_engine,
        job,
        fact_depths=fact_depths,
    )

    result = check_opportunities(coding_input, opportunity_input)

    assert result.candidate_modules == [
        SpecialModule.basic_risk_screening,
        SpecialModule.full_risk_appraisal,
        SpecialModule.safety_response,
    ]
    assert result.activated_modules == expected_modules
    assert SpecialModule.emotional_dysregulation not in result.activated_modules
    assert set(result.outcomes) == set(CoreDimension) | set(result.candidate_modules)
    assert all(result.outcomes[target] for target in CoreDimension)
    assert all(
        outcome.fulfilled
        for target in CoreDimension
        for outcome in result.outcomes[target]
    )
    assert all(
        outcome.fulfilled
        for outcome in result.outcomes[CoreDimension.voice_and_process]
    )


def test_opportunity_check_reports_distinct_inactive_module_reasons(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import check_opportunities

    job = _create_job(test_engine)
    coding_input, opportunity_input = _opportunity_inputs(test_engine, job)

    result = check_opportunities(coding_input, opportunity_input)
    reasons = dict(result.inactive_modules)

    assert "未达到启用门槛" in reasons[SpecialModule.full_risk_appraisal]
    assert "未达到启用门槛" in reasons[SpecialModule.safety_response]
    assert "未声明" in reasons[SpecialModule.emotional_dysregulation]


def test_required_opportunities_are_fulfilled_without_legacy_actor_state(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import check_opportunities

    job = _create_job(
        test_engine,
        case_id="boundary_referral_short",
        case_type=CaseType.short,
        state_json={"runtime": {"engine": "character_prompt"}},
    )
    coding_input, opportunity_input = _opportunity_inputs(test_engine, job)

    result = check_opportunities(coding_input, opportunity_input)

    expected_targets: tuple[Target, ...] = (
        *CoreDimension,
        SpecialModule.basic_risk_screening,
        SpecialModule.dependency_and_boundary,
    )
    assert SpecialModule.dependency_and_boundary in result.candidate_modules
    assert SpecialModule.dependency_and_boundary in result.activated_modules
    assert all(
        outcome.fulfilled
        for target in expected_targets
        for outcome in result.outcomes[target]
    )
    briefs = {brief.target: brief for brief in result.active_target_briefs}
    assert CoreDimension.supportive_intervention in briefs
    assert SpecialModule.dependency_and_boundary in briefs
    assert "共同比较资源和过渡方式" in briefs[
        CoreDimension.supportive_intervention
    ].description
    assert briefs[SpecialModule.dependency_and_boundary].indicator_ids == [
        "S5.pattern",
        "S5.boundary",
        "S5.dependency",
        "S5.continuity",
        "S5.alternatives",
        "S5.relationship_pressure",
    ]


def test_opportunity_sources_use_their_own_frozen_material_boundaries(
    test_engine: Engine,
) -> None:
    """四类来源只检查各自合同，不从 target 或关键词猜测机会。"""
    from app.reports.report_pipeline import check_opportunities

    job = _create_job(
        test_engine,
        case_id="marriage_boundary_main",
        state_json={"runtime": {"engine": "character_prompt"}},
    )
    coding_input, opportunity_input = _opportunity_inputs(test_engine, job)

    def fulfilled(
        source: str,
        current_input: Any,
        *,
        with_runtime_gate: bool = False,
        runtime_depth: int = 0,
    ) -> bool:
        package = deepcopy(opportunity_input.case_package)
        package["measurement"] = {
            "case_id": "marriage_boundary_main",
            "scoring_opportunities": [
                {
                    "id": f"source-{source}",
                    "target": "C1",
                    "kind": "conditional" if with_runtime_gate else "required",
                    "source": source,
                    "description": "核对机会来源边界。",
                    "evidence_targets": ["可观察互动"],
                    "indicator_ids": ["C1.respect"],
                    "linked_fact_ids": ["runtime-probe"] if with_runtime_gate else [],
                    "scenes": ["hotline"],
                }
            ],
        }
        check_input = opportunity_input.model_copy(
            update={
                "case_package": package,
                "session_state": {
                    "actor_state": {
                        "fact_states": {
                            "runtime-probe": {"disclosed_depth": runtime_depth}
                        }
                    }
                },
            }
        )
        return check_opportunities(current_input, check_input).outcomes[
            CoreDimension.respectful_communication
        ][0].fulfilled

    assert fulfilled("transcript", coding_input) is True
    blank_worker_input = coding_input.model_copy(
        update={
            "turns": [
                turn.model_copy(update={"text": "   "})
                if turn.speaker is TurnSpeaker.worker
                else turn
                for turn in coding_input.turns
            ]
        }
    )
    assert fulfilled("transcript", blank_worker_input) is False

    assert fulfilled("termination", coding_input) is True
    technical_input = coding_input.model_copy(
        update={
            "termination": coding_input.termination.model_copy(
                update={"end_reason": EndReason.technical_interruption}
            )
        }
    )
    assert fulfilled("termination", technical_input) is False

    assert fulfilled("work_record", coding_input) is True
    assert fulfilled(
        "work_record",
        coding_input.model_copy(update={"work_record": None}),
    ) is False

    assert fulfilled(
        "runtime_state",
        coding_input,
        with_runtime_gate=True,
        runtime_depth=0,
    ) is False
    assert fulfilled(
        "runtime_state",
        coding_input,
        with_runtime_gate=True,
        runtime_depth=1,
    ) is True


def test_conditional_opportunity_still_requires_legacy_fact_gate(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import check_opportunities

    job = _create_job(test_engine)
    coding_input, opportunity_input = _opportunity_inputs(test_engine, job)

    hidden = check_opportunities(coding_input, opportunity_input)
    disclosed = check_opportunities(
        coding_input,
        opportunity_input.model_copy(
            update={
                "session_state": {
                    "actor_state": {
                        "fact_states": {
                            "minimum_risk_cue": {"disclosed_depth": 1}
                        }
                    }
                }
            }
        ),
    )

    assert SpecialModule.full_risk_appraisal not in hidden.activated_modules
    assert SpecialModule.full_risk_appraisal in disclosed.activated_modules


def test_technical_end_reason_is_not_downgraded_by_existing_worker_turns(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import (
        _build_validated_targets,
        check_opportunities,
    )

    job = _create_job(test_engine, worker_turns=True)
    coding_input, opportunity_input = _opportunity_inputs(test_engine, job)
    coding_input = coding_input.model_copy(
        update={
            "session": coding_input.session.model_copy(
                update={"end_reason": EndReason.technical_interruption}
            ),
            "termination": coding_input.termination.model_copy(
                update={"end_reason": EndReason.technical_interruption}
            ),
        }
    )
    opportunities = check_opportunities(coding_input, opportunity_input)
    validated = _build_validated_targets(
        coding_input,
        opportunities,
        _global_output(),
        TARGETS,
    )

    assert any(turn.speaker is TurnSpeaker.worker for turn in coding_input.turns)
    assert all(
        not outcome.fulfilled
        for outcome in opportunities.outcomes[CoreDimension.closure_and_followup]
    )
    assert all(
        outcome.fulfilled
        for outcome in opportunities.outcomes[CoreDimension.documentation]
    )
    assert all(
        outcome.fulfilled
        for outcome in opportunities.outcomes[SpecialModule.basic_risk_screening]
    )
    assert all(item.technical_failure for item in validated.values())


def test_opportunity_check_reads_fulfillment_from_turn_state_without_exposing_it(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import check_opportunities

    job = _create_job(test_engine)
    coding_input, opportunity_input = _opportunity_inputs(
        test_engine,
        job,
        fact_depths={
            "minimum_risk_cue": 1,
            "suicidal_ideation": 1,
            "timing_intent": 2,
        },
    )
    payload = opportunity_input.model_dump(mode="json")
    payload["turn_states"][0]["state_after_json"] = {
        "fact_states": {},
        "occurred_event_ids": ["first_contact_tang_ting"],
    }

    result = check_opportunities(
        coding_input,
        OpportunityCheckInput.model_validate(payload),
    )

    safety_outcomes = result.outcomes[SpecialModule.safety_response]
    assert any(
        outcome.fulfilled and outcome.complex_opportunity
        for outcome in safety_outcomes
    )
    serialized = str([item.model_dump(mode="json") for item in safety_outcomes])
    assert "first_contact_tang_ting" not in serialized
    assert "suicidal_ideation" not in serialized


async def test_pipeline_uses_fulfilled_case_declarations_for_actual_modules(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        opportunity_payload = deepcopy(stored.opportunity_check_json)
        opportunity_payload["session_state"] = {
            "actor_state": {
                "fact_states": {
                    "minimum_risk_cue": {"disclosed_depth": 1},
                    "suicidal_ideation": {"disclosed_depth": 1},
                    "timing_intent": {"disclosed_depth": 2},
                },
                "occurred_event_ids": [],
            }
        }
        stored.opportunity_check_json = opportunity_payload
        db.add(stored)
        db.commit()

    gateway = FakeGateway()
    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    expected_targets: list[Target] = [
        *CoreDimension,
        SpecialModule.basic_risk_screening,
        SpecialModule.full_risk_appraisal,
        SpecialModule.safety_response,
    ]
    assert gateway.received_global_targets == [expected_targets]
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None, (
            None if stored is None else stored.last_error
        )
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)
    assert report.summary.activated_modules == expected_targets[9:]
    assert {item.target for item in report.dimensions} == set(expected_targets)
    inactive = dict(report.summary.inactive_modules)
    assert "未声明" in inactive[SpecialModule.emotional_dysregulation]


async def test_character_prompt_short_case_report_keeps_all_expected_opportunities(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.scoring_domain import IndicatorStatus
    from app.reports.service import ReportService

    job = _create_job(
        test_engine,
        case_id="boundary_referral_short",
        case_type=CaseType.short,
        state_json={"runtime": {"engine": "character_prompt"}},
    )

    gateway = FakeGateway()
    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    expected_targets: set[Target] = {
        *CoreDimension,
        SpecialModule.basic_risk_screening,
        SpecialModule.dependency_and_boundary,
    }
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None, (
            None if stored is None else stored.last_error
        )
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)

    results = {item.result.target: item.result for item in report.dimensions}
    assert set(results) == expected_targets
    assert all(
        any(opportunity.fulfilled for opportunity in results[target].opportunities)
        for target in expected_targets
    )
    assert all(
        status is not IndicatorStatus.no_opportunity
        for target in expected_targets
        for status in results[target].indicator_states.values()
    )
    received_briefs = {
        brief.target: brief for brief in gateway.received_active_target_briefs[0]
    }
    assert CoreDimension.supportive_intervention in received_briefs
    assert CoreDimension.closure_and_followup in received_briefs
    assert SpecialModule.dependency_and_boundary in received_briefs
    assert gateway.received_global_targets[0] == [
        *CoreDimension,
        SpecialModule.basic_risk_screening,
        SpecialModule.dependency_and_boundary,
    ]


async def test_pipeline_runs_two_maps_reduce_and_three_groups_and_persists_new_report(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    store = RuntimeCredentialStore()
    job = _create_job(test_engine, credential_store=store)
    gateway = FakeGateway()

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert len(gateway.calls) == 6
    assert {item for item in gateway.calls if item.startswith("map:")} == {
        "map:shard-1",
        "map:shard-2",
    }
    assert gateway.calls.count("reduce") == 1
    assert all(gateway.calls.count(group.value) == 1 for group in ScoringGroup)
    serialized_global = str(gateway.received_global)
    for forbidden in (
        "session_state",
        "case_package",
        "state_json",
        "used_fact_ids",
        "opportunity",
    ):
        assert forbidden not in serialized_global
    with Session(test_engine) as db:
        stored_job = db.get(ReportJobRecord, job.id)
        assert stored_job is not None
        assert stored_job.stage is ReportJobStage.succeeded
        assert stored_job.report_id is not None
        report = ReportService(db, CaseRepository()).get_report(stored_job.report_id)

    assert report.ai_draft_status == "complete"
    assert report.scene is Scene.hotline
    assert report.media is Media.voice
    assert {item.target for item in report.dimensions} == set(TARGETS)
    assert len(report.dimensions) == 10
    assert all(item.name and item.level_anchor for item in report.dimensions)
    assert all(item.result.evidence for item in report.dimensions)
    assert all(item.result.opportunities for item in report.dimensions)
    opportunity_payloads = [
        opportunity.model_dump(mode="json")
        for item in report.dimensions
        for opportunity in item.result.opportunities
    ]
    assert all(
        set(payload)
        == {
            "declared_target",
            "kind",
            "fulfilled",
            "indicator_ids",
            "complex_opportunity",
        }
        for payload in opportunity_payloads
    )
    serialized_opportunities = str(opportunity_payloads)
    for forbidden in ("description", "case_package", "used_fact_ids", "state_json"):
        assert forbidden not in serialized_opportunities
    assert report.summary.activated_modules == [SpecialModule.basic_risk_screening]
    assert len(report.disclaimers) >= 6
    assert any("必须核对原始对话和工作记录" in item for item in report.disclaimers)
    serialized = report.model_dump(mode="json")
    for removed in (
        "raw_score",
        "normalized_score",
        "coverage",
        "result_status",
        "review_status",
        "evidence_provider",
    ):
        assert removed not in str(serialized)
    assert "合格" not in report.summary.level_distribution
    assert "通过" not in report.summary.level_distribution


async def test_online_pipeline_preserves_true_media_without_hotline_report_language(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(
        test_engine,
        case_id="marriage_boundary_main",
        scene=Scene.online,
        media=Media.text,
    )
    gateway = FakeGateway()

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    with Session(test_engine) as db:
        stored_job = db.get(ReportJobRecord, job.id)
        assert stored_job is not None
        assert stored_job.report_id is not None, stored_job.last_error
        report = ReportService(db, CaseRepository()).get_report(stored_job.report_id)

    assert report.scene is Scene.online
    assert report.media is Media.text
    assert gateway.received_reduce_contexts == [(Scene.online, Media.text)]
    assert gateway.received_packets
    assert all(
        packet.scene is Scene.online and packet.media is Media.text
        for packet in gateway.received_packets
    )
    assert all(
        payload["session"]["scene"] == Scene.online.value
        and payload["session"]["media"] == Media.text.value
        for payload in gateway.received_global
    )
    serialized = str(report.model_dump(mode="json"))
    for hotline_only in ("热线", "接线", "来电", "通话", "声音线索", "声音表现"):
        assert hotline_only not in serialized


async def test_technical_interruption_keeps_scorable_material_but_not_closure_opportunity(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.scoring_domain import UnscoredReason
    from app.reports.service import ReportService

    job = _create_job(
        test_engine,
        end_reason=EndReason.technical_interruption,
    )

    await ReportPipeline(test_engine, CaseRepository(), FakeGateway()).run(job.id)

    with Session(test_engine) as db:
        stored_job = db.get(ReportJobRecord, job.id)
        assert stored_job is not None
        assert stored_job.report_id is not None, stored_job.last_error
        report = ReportService(db, CaseRepository()).get_report(stored_job.report_id)

    results = {item.target: item.result for item in report.dimensions}
    closure = results[CoreDimension.closure_and_followup]
    assert closure.unscored_reason is UnscoredReason.no_opportunity
    assert all(not opportunity.fulfilled for opportunity in closure.opportunities)
    assert results[CoreDimension.respectful_communication].level is not None
    assert results[CoreDimension.documentation].level is not None
    assert all(
        result.unscored_reason is not UnscoredReason.technical_failure
        for result in results.values()
        if any(opportunity.fulfilled for opportunity in result.opportunities)
    )


async def test_dimension_analysis_failure_makes_report_partial_without_group_failure(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    # 两个话轮都是来电者发言；Reduce 产出的对话能力证据会被来源隔离规则剔除。
    job = _create_job(test_engine, worker_turns=False)

    await ReportPipeline(test_engine, CaseRepository(), FakeGateway()).run(job.id)

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)

    assert stored.stage is ReportJobStage.partial
    assert report.ai_draft_status == "partial"
    assert any(
        item.result.analysis_outcome is AnalysisOutcome.analysis_failed
        for item in report.dimensions
    )


async def test_pipeline_skips_empty_scoring_groups_but_keeps_all_report_targets(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.report_provider import GROUP_TARGETS
    from app.reports.scoring_domain import UnscoredReason
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    output = _global_output()
    omitted_targets = {
        *GROUP_TARGETS[ScoringGroup.professional],
        *GROUP_TARGETS[ScoringGroup.safety],
    }
    output.coded_evidence = [
        item for item in output.coded_evidence if item.target not in omitted_targets
    ]
    gateway = FakeGateway(global_output=output)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count(ScoringGroup.interaction.value) == 1
    assert gateway.calls.count(ScoringGroup.professional.value) == 0
    assert gateway.calls.count(ScoringGroup.safety.value) == 0
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None
        assert stored.stage is ReportJobStage.succeeded
        assert stored.attempts.get("group:professional", 0) == 0
        assert stored.attempts.get("group:safety", 0) == 0
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)

    assert {item.result.target for item in report.dimensions} == set(TARGETS)
    omitted_results = [
        item.result
        for item in report.dimensions
        if item.result.target in omitted_targets
    ]
    assert omitted_results
    assert all(
        result.unscored_reason is UnscoredReason.insufficient_evidence
        for result in omitted_results
    )


async def test_failed_group_does_not_mark_undispatched_target_as_analysis_failed(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.scoring_domain import UnscoredReason
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    output = _global_output()
    output.coded_evidence = [
        item
        for item in output.coded_evidence
        if item.target is not CoreDimension.documentation
    ]
    gateway = FakeGateway(
        global_output=output,
        fail_group=ScoringGroup.professional,
    )

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)

    integration = next(
        item.result
        for item in report.dimensions
        if item.result.target is CoreDimension.integration_and_judgment
    )
    documentation = next(
        item.result
        for item in report.dimensions
        if item.result.target is CoreDimension.documentation
    )
    assert integration.analysis_outcome is AnalysisOutcome.analysis_failed
    assert documentation.analysis_outcome is AnalysisOutcome.ok
    assert documentation.unscored_reason is UnscoredReason.insufficient_evidence


async def test_pipeline_starts_maps_and_groups_concurrently_and_reduces_after_maps(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = PhaseBarrierGateway()

    await asyncio.wait_for(
        ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id),
        timeout=3,
    )

    reduce_index = gateway.phase_log.index("reduce-start")
    assert all(
        gateway.phase_log.index(f"map-start:{shard_id}") < reduce_index
        for shard_id in ("shard-1", "shard-2")
    )
    assert all(
        gateway.phase_log.index(f"map-finish:{shard_id}") < reduce_index
        for shard_id in ("shard-1", "shard-2")
    )
    first_group_finish = min(
        gateway.phase_log.index(f"group-finish:{group.value}")
        for group in ScoringGroup
    )
    assert all(
        gateway.phase_log.index(f"group-start:{group.value}") < first_group_finish
        for group in ScoringGroup
    )


async def test_invalid_local_output_retries_only_its_own_shard(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = InvalidThenValidMapGateway()

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("map:shard-1") == 2
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 1
    shard_feedback = [
        feedback
        for batch, feedback in gateway.validation_feedbacks
        if batch == "map:shard-1"
    ]
    assert shard_feedback == [
        None,
        "LocalCodingOutput shard_id 与输入分片不匹配",
    ]
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded
        assert stored.attempts["map:shard-1"] == 2
        assert stored.attempts["map:shard-2"] == 1
        assert stored.attempts["reduce"] == 1


async def test_reduce_accepts_source_outside_map_excerpt_via_full_turn_passthrough(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = PartialMapSourceGateway()

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("reduce") == 1
    reduce_feedback = [
        feedback for batch, feedback in gateway.validation_feedbacks if batch == "reduce"
    ]
    assert reduce_feedback == [None]
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded


async def test_failed_map_blocks_reduce_and_groups_and_reports_local_failure(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = FakeGateway(fail_map_shard="shard-1")

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("map:shard-1") == 3
    assert gateway.calls.count("map:shard-2") == 1
    assert "reduce" not in gateway.calls
    assert all(group.value not in gateway.calls for group in ScoringGroup)
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.failed
        assert stored.attempts["map:shard-1"] == 3
        assert stored.attempts["map:shard-2"] == 1
        assert "reduce" not in stored.attempts
        assert stored.last_error is not None
        assert "局部编码失败" in stored.last_error
        assert "材料不足" not in stored.last_error


async def test_pipeline_uses_model_configuration_frozen_when_job_was_created(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    store = RuntimeCredentialStore()
    store.update(report_model="queued-report-model", report_temperature=0.23)
    job = _create_job(test_engine, credential_store=store)
    store.update(report_model="changed-after-queue", report_temperature=0.91)
    gateway = FakeGateway()

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert len(gateway.received_model_configs) == 6
    assert all(
        config is not None
        and config.report_model == "queued-report-model"
        and config.report_temperature == 0.23
        for config in gateway.received_model_configs
    )


async def test_reduce_failure_retries_three_times_and_does_not_create_report(
    test_engine: Engine,
) -> None:
    from app.reports.models import ReportRecord
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = FakeGateway(fail_global=True)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 2
    assert all(group.value not in gateway.calls for group in ScoringGroup)
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.failed
        assert stored.report_id is None
        assert stored.last_error is not None
        assert "聚焦汇总失败" in stored.last_error
        assert "材料不足" not in stored.last_error
        assert db.exec(select(ReportRecord)).all() == []


async def test_manual_retry_after_reduce_failure_reuses_validated_map_outputs(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = FakeGateway(fail_global=True)
    pipeline = ReportPipeline(test_engine, CaseRepository(), gateway)

    await pipeline.run(job.id)

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.coding_json is not None
        assert stored.coding_json["workflow_stage"] == "map_complete"
        assert len(stored.coding_json["local_outputs"]) == 2
        ReportJobService(db, CaseRepository()).retry(job.id)

    gateway.calls.clear()
    gateway.fail_global = False
    await pipeline.run(job.id)

    assert all(not call.startswith("map:") for call in gateway.calls)
    assert gateway.calls.count("reduce") == 1
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded
        assert stored.coding_json is not None
        assert "workflow_stage" not in stored.coding_json


async def test_existing_full_eighteen_target_reduce_cache_remains_readable(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    legacy_output = _global_output(
        ANALYSIS_TARGETS,
        counter_targets=ANALYSIS_TARGETS,
    )
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        stored.coding_json = legacy_output.model_dump(mode="json")
        db.add(stored)
        db.commit()

    gateway = FakeGateway()
    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert all(not call.startswith("map:") for call in gateway.calls)
    assert "reduce" not in gateway.calls
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded
        assert stored.report_id is not None


async def test_invalid_map_stage_cache_safely_reruns_both_map_shards(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        stored.coding_json = {
            "workflow_stage": "map_complete",
            "local_outputs": [
                {"shard_id": "wrong-shard", "units": []},
                {"shard_id": "shard-2", "units": []},
            ],
        }
        db.add(stored)
        db.commit()

    gateway = FakeGateway()
    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 1
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded


async def test_non_retryable_reduce_error_stops_after_first_attempt(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = NonRetryableGateway(failing_batch="global")

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 1
    assert all(group.value not in gateway.calls for group in ScoringGroup)
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.failed


async def test_non_retryable_group_error_stops_only_that_batch_after_first_attempt(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = NonRetryableGateway(failing_batch=ScoringGroup.interaction.value)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("interaction") == 1
    assert gateway.calls.count("professional") == 1
    assert gateway.calls.count("safety") == 1
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.partial


async def test_group_proposal_above_packet_ceiling_is_rejected_and_retried(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    output = _global_output()
    first_c1 = next(
        item
        for item in output.coded_evidence
        if item.target is CoreDimension.respectful_communication
    )
    first_c1.direction = EvidenceDirection.adverse
    gateway = CeilingRetryGateway()
    gateway.global_output = output

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("interaction") == 2
    interaction_feedback = [
        feedback
        for batch, feedback in gateway.validation_feedbacks
        if batch == "interaction"
    ]
    assert interaction_feedback[0] is None
    assert "proposed_level 超过 level_ceiling" in interaction_feedback[1]
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded


async def test_null_level_with_primary_evidence_retries_and_recovers(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = NullLevelGateway(recover_after_first=True)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("interaction") == 2
    interaction_feedback = [
        feedback
        for batch, feedback in gateway.validation_feedbacks
        if batch == "interaction"
    ]
    assert interaction_feedback[0] is None
    assert "已有可定级主要证据但 proposed_level 为 null" in interaction_feedback[1]
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded


async def test_repeated_null_level_with_primary_evidence_is_analysis_failed(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    gateway = NullLevelGateway(recover_after_first=False)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("interaction") == 3
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)
    c1 = next(
        item.result
        for item in report.dimensions
        if item.result.target is CoreDimension.respectful_communication
    )
    assert stored.stage is ReportJobStage.partial
    assert c1.analysis_outcome is AnalysisOutcome.analysis_failed
    assert c1.unscored_reason is None


@pytest.mark.parametrize("invalid_kind", ["empty_pattern", "unknown_unit"])
async def test_invalid_group_contract_retries_then_manual_retry_recovers(
    test_engine: Engine,
    invalid_kind: str,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = InvalidProposalGateway(invalid_kind)
    pipeline = ReportPipeline(test_engine, CaseRepository(), gateway)

    await pipeline.run(job.id)

    assert gateway.calls.count("interaction") == 3
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.partial
        assert "interaction" not in stored.scoring_groups_done
        assert stored.scoring_group_results_json["interaction"]["status"] == "failed"
        ReportJobService(db, CaseRepository()).retry(job.id)

    gateway.calls.clear()
    gateway.invalid_kind = None
    await pipeline.run(job.id)

    assert gateway.calls == ["interaction"]
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded
        assert "interaction" in stored.scoring_groups_done


async def test_invalid_completed_group_cache_is_revalidated_and_rerun(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    gateway = FakeGateway(fail_group=ScoringGroup.professional)
    pipeline = ReportPipeline(test_engine, CaseRepository(), gateway)
    await pipeline.run(job.id)

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.stage is ReportJobStage.partial
        cached = deepcopy(stored.scoring_group_results_json)
        cached["interaction"]["proposals"][0]["pattern"] = ""
        stored.scoring_group_results_json = cached
        db.add(stored)
        db.commit()
        ReportJobService(db, CaseRepository()).retry(job.id)

    gateway.calls.clear()
    gateway.fail_group = None
    await pipeline.run(job.id)

    assert sorted(gateway.calls) == ["interaction", "professional"]
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded


def test_parallel_processor_delivery_claims_job_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.database import create_database_engine
    from app.reports.models import ReportRecord
    from app.reports.report_pipeline import ReportPipeline, ReportProcessor

    engine = create_database_engine(f"sqlite:///{tmp_path / 'parallel-report.db'}")
    job = _create_job(engine)
    gateway = FakeGateway()
    coding_read_gate = Barrier(2)
    original_get_coding_input = ReportJobService.get_coding_input

    def gated_get_coding_input(
        service: ReportJobService,
        job_id: str,
    ) -> object:
        coding_input = original_get_coding_input(service, job_id)
        try:
            coding_read_gate.wait(timeout=0.3)
        except BrokenBarrierError:
            pass
        return coding_input

    monkeypatch.setattr(
        ReportJobService,
        "get_coding_input",
        gated_get_coding_input,
    )
    processor = ReportProcessor(
        lambda: engine,
        lambda selected_engine: ReportPipeline(
            selected_engine,
            CaseRepository(),
            gateway,
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(processor.process, job.id) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 1
    assert gateway.calls.count("interaction") == 1
    assert gateway.calls.count("professional") == 1
    assert gateway.calls.count("safety") == 1
    with Session(engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.stage is ReportJobStage.succeeded
        assert len(db.exec(select(ReportRecord)).all()) == 1
    engine.dispose()


async def test_one_group_failure_is_partial_and_retry_only_reruns_failed_group(
    test_engine: Engine,
) -> None:
    from app.reports.models import ReportRecord
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    store = RuntimeCredentialStore()
    job = _create_job(test_engine, credential_store=store)
    gateway = FakeGateway(fail_group=ScoringGroup.professional)
    pipeline = ReportPipeline(test_engine, CaseRepository(), gateway)

    await pipeline.run(job.id)

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 1
    assert gateway.calls.count("interaction") == 1
    assert gateway.calls.count("professional") == 3
    assert gateway.calls.count("safety") == 1
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.partial
        assert stored.report_id is not None
        partial_report_id = stored.report_id
        partial = ReportService(db, CaseRepository()).get_report(partial_report_id)
    failed_targets = {
        item.target
        for item in partial.dimensions
        if item.result.analysis_outcome is AnalysisOutcome.analysis_failed
    }
    assert failed_targets == {
        CoreDimension.integration_and_judgment,
        CoreDimension.boundary_and_ethics,
        CoreDimension.documentation,
    }
    assert all(
        item.result.opportunities
        for item in partial.dimensions
        if item.result.analysis_outcome is AnalysisOutcome.analysis_failed
    )

    gateway.calls.clear()
    gateway.received_model_configs.clear()
    gateway.fail_group = None
    store.update(report_model="changed-before-partial-retry", report_temperature=0.88)
    with Session(test_engine) as db:
        ReportJobService(db, CaseRepository()).retry(job.id)
    await pipeline.run(job.id)

    assert gateway.calls == ["professional"]
    assert len(gateway.received_model_configs) == 1
    retry_config = gateway.received_model_configs[0]
    assert retry_config is not None
    assert retry_config.report_model == "qwen3.8-max"
    assert retry_config.report_temperature == 0.1
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.succeeded
        assert stored.report_id is not None
        assert stored.report_id != partial_report_id
        complete = ReportService(db, CaseRepository()).get_report(stored.report_id)
        preserved_partial = ReportService(db, CaseRepository()).get_report(partial_report_id)
        reports = list(
            db.exec(
                select(ReportRecord).where(ReportRecord.session_id == job.session_id)
            ).all()
        )
    assert len(reports) == 2
    assert preserved_partial.ai_draft_status == "partial"
    assert any(
        item.result.analysis_outcome is AnalysisOutcome.analysis_failed
        for item in preserved_partial.dimensions
    )
    assert all(
        item.result.analysis_outcome is AnalysisOutcome.ok
        for item in complete.dimensions
    )


@pytest.mark.parametrize("invalid_kind", ["missing_quote", "excluded_source", "counter_check"])
async def test_invalid_global_contract_retries_and_fails_without_report(
    test_engine: Engine,
    invalid_kind: str,
) -> None:
    from app.reports.models import ReportRecord
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    output = _global_output()
    affected = CoreDimension.respectful_communication
    if invalid_kind == "missing_quote":
        first = next(item for item in output.coded_evidence if item.target is affected)
        first.ref = DialogueRef(
            kind="dialogue",
            turn_id="turn-one",
            quote="这段原文并不存在",
        )
    elif invalid_kind == "excluded_source":
        first = next(item for item in output.coded_evidence if item.target is affected)
        first.unit_id = "unit-record-one"
        first.ref = WorkRecordRef(
            kind="work_record",
            field="problem_understanding",
            quote="压力、失眠和功能下降",
        )
    else:
        output.counter_checks = [
            item for item in output.counter_checks if item.target is not affected
        ]
    gateway = FakeGateway(global_output=output)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 2
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.failed
        assert stored.report_id is None
        assert stored.coding_json is not None
        assert stored.coding_json["workflow_stage"] == "map_complete"
        assert len(stored.coding_json["local_outputs"]) == 2
        assert db.exec(select(ReportRecord)).all() == []


@pytest.mark.parametrize(
    "invalid_kind",
    ["dialogue_only", "work_record_only", "invalid_dialogue", "invalid_work_record"],
)
async def test_material_conflict_requires_two_valid_sides_in_global_batch(
    test_engine: Engine,
    invalid_kind: str,
) -> None:
    from app.reports.models import ReportRecord
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    output = _global_output()
    dialogue_ref = DialogueRef(
        kind="dialogue",
        turn_id="turn-one",
        quote=(
            "不存在的对话原文"
            if invalid_kind == "invalid_dialogue"
            else "先听你最难受的部分"
        ),
    )
    work_record_ref = WorkRecordRef(
        kind="work_record",
        field="problem_understanding",
        quote=(
            "不存在的工作记录原文"
            if invalid_kind == "invalid_work_record"
            else "压力、失眠和功能下降"
        ),
    )
    output.material_conflict_candidates = [
        MaterialConflict(
            id="conflict-one",
            dialogue_ref=(None if invalid_kind == "work_record_only" else dialogue_ref),
            work_record_ref=(None if invalid_kind == "dialogue_only" else work_record_ref),
            description="对话与工作记录存在不对应。",
            affected_targets=[CoreDimension.documentation],
            impact="需要回看两侧原始材料。",
        )
    ]
    gateway = FakeGateway(global_output=output)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 2
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.failed
        assert stored.report_id is None
        assert db.exec(select(ReportRecord)).all() == []


async def test_no_worker_opportunity_is_unscored_not_level_zero_and_s1a_still_exists(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(test_engine, worker_turns=False)
    output = _global_output()
    output.coded_evidence = []
    gateway = FakeGateway(global_output=output)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None, (
            None if stored is None else stored.last_error
        )
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)
    assert any(item.target is SpecialModule.basic_risk_screening for item in report.dimensions)
    for item in report.dimensions:
        assert item.result.level is None
        assert item.result.unscored_reason is not None
        assert item.result.opportunities


async def test_known_imminent_risk_ending_rule_is_assembled_from_public_refs(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        frozen = dict(stored.frozen_input_json)
        turns = [dict(item) for item in frozen["turns"]]
        turns[0]["speaker"] = "client"
        turns[0]["text"] = "我现在就准备伤害自己。"
        frozen["turns"] = turns
        work_record = dict(frozen["work_record"])
        work_record["risk_level"] = "imminent"
        work_record["risk_evidence_turn_ids"] = ["turn-one"]
        frozen["work_record"] = work_record
        stored.frozen_input_json = frozen
        db.add(stored)
        db.commit()
    output = _global_output()
    from app.reports.scoring_domain import (
        RiskDisclosureCategory,
        RiskDisclosureImmediacy,
        UrgentRiskDisclosureCandidate,
    )

    output.urgent_risk_disclosure_candidates = [
        UrgentRiskDisclosureCandidate(
            ref=DialogueRef(
                kind="dialogue",
                turn_id="turn-one",
                quote="现在就准备伤害自己",
            ),
            category=RiskDisclosureCategory.suicide_or_self_harm,
            immediacy=RiskDisclosureImmediacy.imminent,
        )
    ]
    output.coded_evidence = [
        evidence
        for evidence in output.coded_evidence
        if not (
            isinstance(evidence.ref, DialogueRef)
            and evidence.ref.turn_id == "turn-one"
        )
    ]
    gateway = FakeGateway(global_output=output)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None, (
            None if stored is None else stored.last_error
        )
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)
    event = next(
        item
        for item in report.bottom_line_events
        if item.category
        is BottomLineCategory.known_urgent_risk_ended_without_safety_action
    )
    assert event.refs == [
        DialogueRef(
            kind="dialogue",
            turn_id="turn-one",
            quote="现在就准备伤害自己",
        )
    ]
    assert report.screening_gap == report.summary.screening_gap


async def test_imminent_work_record_without_validated_global_disclosure_is_not_bottom_line(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        frozen = dict(stored.frozen_input_json)
        turns = [dict(item) for item in frozen["turns"]]
        turns[0]["speaker"] = "client"
        turns[0]["text"] = "我现在就准备伤害自己。"
        frozen["turns"] = turns
        work_record = dict(frozen["work_record"])
        work_record["risk_level"] = "imminent"
        work_record["risk_evidence_turn_ids"] = ["turn-one"]
        frozen["work_record"] = work_record
        stored.frozen_input_json = frozen
        db.add(stored)
        db.commit()
    output = _global_output()
    output.coded_evidence = [
        evidence
        for evidence in output.coded_evidence
        if not (
            isinstance(evidence.ref, DialogueRef)
            and evidence.ref.turn_id == "turn-one"
        )
    ]

    await ReportPipeline(test_engine, CaseRepository(), FakeGateway(global_output=output)).run(
        job.id
    )

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)
    assert all(
        event.category
        is not BottomLineCategory.known_urgent_risk_ended_without_safety_action
        for event in report.bottom_line_events
    )


@pytest.mark.parametrize("invalid_kind", ["missing_quote", "worker_source"])
async def test_invalid_global_urgent_disclosure_is_rejected_or_role_filtered(
    test_engine: Engine,
    invalid_kind: str,
) -> None:
    from app.reports.models import ReportRecord
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.scoring_domain import (
        RiskDisclosureCategory,
        RiskDisclosureImmediacy,
        UrgentRiskDisclosureCandidate,
    )

    job = _create_job(test_engine)
    if invalid_kind == "missing_quote":
        with Session(test_engine) as db:
            stored = db.get(ReportJobRecord, job.id)
            assert stored is not None
            frozen = dict(stored.frozen_input_json)
            turns = [dict(item) for item in frozen["turns"]]
            turns[0]["speaker"] = "client"
            frozen["turns"] = turns
            stored.frozen_input_json = frozen
            db.add(stored)
            db.commit()
    output = _global_output()
    output.urgent_risk_disclosure_candidates = [
        UrgentRiskDisclosureCandidate(
            ref=DialogueRef(
                kind="dialogue",
                turn_id=("turn-one" if invalid_kind == "missing_quote" else "turn-two"),
                quote=(
                    "不存在的紧迫危险原话"
                    if invalid_kind == "missing_quote"
                    else "一起核对下一步"
                ),
            ),
            category=RiskDisclosureCategory.suicide_or_self_harm,
            immediacy=RiskDisclosureImmediacy.imminent,
        )
    ]
    gateway = FakeGateway(global_output=output)

    await ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        if invalid_kind == "missing_quote":
            assert gateway.calls.count("reduce") == 2
            assert stored.stage is ReportJobStage.failed
            assert stored.report_id is None
            assert db.exec(select(ReportRecord)).all() == []
        else:
            assert gateway.calls.count("reduce") == 1
            assert stored.stage is ReportJobStage.succeeded
            assert stored.report_id is not None


async def test_planned_actions_are_future_intent_and_do_not_create_rule_conflicts(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    await ReportPipeline(test_engine, CaseRepository(), FakeGateway()).run(job.id)

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)
    rule_conflicts = [
        conflict
        for conflict in report.material_conflicts
        if conflict.id.startswith("work-record-planned-action-mismatch-")
    ]
    assert rule_conflicts == []
    assert all(
        event.category is not BottomLineCategory.fabricated_record
        for event in report.bottom_line_events
    )


async def test_planned_action_candidate_without_completed_action_rule_is_not_fabrication(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    output = _global_output()
    semantic_conflict = MaterialConflict(
        id="model-planned-action-conflict",
        dialogue_ref=DialogueRef(
            kind="dialogue",
            turn_id="turn-two",
            quote="一起核对下一步",
        ),
        work_record_ref=WorkRecordRef(
            kind="work_record",
            field="planned_actions",
            quote="follow_up",
        ),
        description="全局语义编码发现计划行动与实际对话不一致。",
        affected_targets=[CoreDimension.documentation],
        impact="需要与规则冲突共同确认。",
    )
    output.material_conflict_candidates = [semantic_conflict]
    output.bottom_line_candidates = [
        BottomLineCandidate(
            category=SemanticBottomLineCategory.fabricated_record,
            conflict_id=semantic_conflict.id,
            refs=[
                DialogueRef(
                    kind="dialogue",
                    turn_id="turn-two",
                    quote="我们一起核对下一步",
                ),
                WorkRecordRef(
                    kind="work_record",
                    field="planned_actions",
                    quote="follow_up",
                ),
            ],
            context="记录所称行动未获已校验对话证据支持。",
            repair_observed=False,
            reasoning="全局候选对完整对话与计划行动字段作了语义比较。",
        )
    ]

    await ReportPipeline(test_engine, CaseRepository(), FakeGateway(global_output=output)).run(
        job.id
    )

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)
    fabricated = [
        event
        for event in report.bottom_line_events
        if event.category is BottomLineCategory.fabricated_record
    ]
    assert fabricated == []


async def test_validated_dialogue_action_supports_frozen_planned_actions(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    output = _global_output()
    semantic_conflict = MaterialConflict(
        id="model-planned-action-conflict",
        dialogue_ref=DialogueRef(
            kind="dialogue",
            turn_id="turn-two",
            quote="一起核对下一步",
        ),
        work_record_ref=WorkRecordRef(
            kind="work_record",
            field="planned_actions",
            quote="follow_up",
        ),
        description="全局语义编码发现计划行动与实际对话不一致。",
        affected_targets=[CoreDimension.documentation],
        impact="需要与规则冲突共同确认。",
    )
    output.material_conflict_candidates = [semantic_conflict]
    output.bottom_line_candidates = [
        BottomLineCandidate(
            category=SemanticBottomLineCategory.fabricated_record,
            conflict_id=semantic_conflict.id,
            refs=[
                DialogueRef(
                    kind="dialogue",
                    turn_id="turn-two",
                    quote="我们一起核对下一步",
                ),
                WorkRecordRef(
                    kind="work_record",
                    field="planned_actions",
                    quote="follow_up",
                ),
            ],
            context="模型候选不能越过规则冲突前提。",
            repair_observed=False,
            reasoning="仅有模型候选，不足以确认编造。",
        )
    ]
    for evidence in output.coded_evidence:
        if evidence.target is CoreDimension.supportive_intervention:
            evidence.indicator_id = "C5.action_layers"

    await ReportPipeline(test_engine, CaseRepository(), FakeGateway(global_output=output)).run(
        job.id
    )

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)
    assert all(
        not conflict.id.startswith("work-record-planned-action-mismatch-")
        for conflict in report.material_conflicts
    )
    assert all(
        event.category is not BottomLineCategory.fabricated_record
        for event in report.bottom_line_events
    )


async def test_valid_two_sided_work_record_mismatch_is_kept_in_report(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import ReportPipeline
    from app.reports.service import ReportService

    job = _create_job(test_engine)
    output = _global_output()
    conflict = MaterialConflict(
        id="performed-action-mismatch",
        dialogue_ref=DialogueRef(
            kind="dialogue",
            turn_id="turn-two",
            quote="一起核对下一步",
        ),
        work_record_ref=WorkRecordRef(
            kind="work_record",
            field="follow_up",
            quote="安排后续支持",
        ),
        description="工作记录声明已有后续安排，对话只观察到下一步讨论。",
        affected_targets=[
            CoreDimension.boundary_and_ethics,
            CoreDimension.documentation,
        ],
        impact="只能报告为材料不一致，不能直接判为编造。",
    )
    output.material_conflict_candidates = [conflict]

    await ReportPipeline(test_engine, CaseRepository(), FakeGateway(global_output=output)).run(
        job.id
    )

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None and stored.report_id is not None
        report = ReportService(db, CaseRepository()).get_report(stored.report_id)
    assert conflict in report.material_conflicts
    assert all(
        not item.id.startswith("work-record-planned-action-mismatch-")
        for item in report.material_conflicts
    )
    assert report.bottom_line_events == []


async def test_assembly_failure_marks_job_failed_without_report(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.reports.report_pipeline as report_pipeline
    from app.reports.models import ReportRecord

    job = _create_job(test_engine)
    gateway = FakeGateway()

    def fail_summary(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("assembly failed")

    monkeypatch.setattr(report_pipeline, "build_result_summary", fail_summary)
    await report_pipeline.ReportPipeline(test_engine, CaseRepository(), gateway).run(job.id)

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.failed
        assert stored.report_id is None
        assert db.exec(select(ReportRecord)).all() == []


async def test_report_and_final_job_state_roll_back_as_one_transaction(
    test_engine: Engine,
) -> None:
    from app.reports.models import ReportRecord
    from app.reports.report_pipeline import ReportPipeline

    job = _create_job(test_engine)
    saw_report_insert = False
    raised = False

    def fail_final_job_update(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal raised, saw_report_insert
        normalized = statement.strip().upper()
        if normalized.startswith("INSERT INTO REPORTS"):
            saw_report_insert = True
        elif saw_report_insert and not raised and normalized.startswith("UPDATE REPORT_JOBS"):
            raised = True
            raise RuntimeError("inject final job update failure")

    event.listen(test_engine, "before_cursor_execute", fail_final_job_update)
    try:
        await ReportPipeline(test_engine, CaseRepository(), FakeGateway()).run(job.id)
    finally:
        event.remove(test_engine, "before_cursor_execute", fail_final_job_update)

    assert saw_report_insert is True
    assert raised is True
    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, job.id)
        assert stored is not None
        assert stored.stage is ReportJobStage.failed
        assert stored.report_id is None
        assert db.exec(select(ReportRecord)).all() == []

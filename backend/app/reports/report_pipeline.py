from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.cases.loader import CaseRepository
from app.cases.measurement import MeasurementSpec, OpportunitySource, ScoringOpportunity
from app.reports.competency_rubric import get_rubric
from app.reports.job_inputs import (
    CodingInput,
    CodingShard,
    CodingTurnInput,
    OpportunityCheckInput,
)
from app.reports.jobs import ReportJobProgressUpdate, ReportJobService
from app.reports.models import (
    ReportDraftStatus,
    ReportJobRecord,
    ReportJobStage,
)
from app.reports.report_provider import (
    GROUP_TARGETS,
    REPORT_TARGETS,
    ActiveTargetBrief,
    GlobalCodingOutput,
    GroupScoringOutput,
    LocalCodedUnit,
    LocalCodingOutput,
    ReportModelConfig,
    ReportModelGateway,
    ScoringGroup,
)
from app.reports.scoring_domain import (
    AnalysisOutcome,
    AudioEventRef,
    BottomLineEvent,
    CodedEvidence,
    CoreDimension,
    CounterCheck,
    DialogueRef,
    DimensionPacket,
    DimensionResult,
    EvidenceConfidence,
    EvidenceDirection,
    EvidenceRole,
    EvidenceStrength,
    IndicatorStatus,
    MaterialConflict,
    MeaningUnit,
    OpportunityKind,
    OpportunityOutcome,
    PacketEvidence,
    RiskDisclosureImmediacy,
    SemanticBottomLineCategory,
    SpecialModule,
    Target,
    WorkRecordRef,
)
from app.reports.scoring_rules import (
    RoutedEvidence,
    assemble_dimension_result,
    assess_evidence_sufficiency,
    build_result_summary,
    calculate_level_ceiling,
    canonical_work_record_fragments,
    classification_language_violations,
    detect_known_urgent_risk_termination,
    resolve_scoring_disposition,
    semantic_bottom_line_events,
    validate_counter_check,
    validate_evidence,
    validate_material_conflict_candidates,
    validate_urgent_risk_disclosure_candidates,
)
from app.reports.service import ReportService, ReportWrite
from app.runtime.models import ModelCallKind
from app.runtime.providers import NonRetryableRuntimeModelError
from app.sessions.models import EndReason, Media, SessionStatus, TurnSpeaker

MAX_BATCH_ATTEMPTS = 3
MAX_REDUCE_ATTEMPTS = 2
MAP_COMPLETE_WORKFLOW_STAGE = "map_complete"
_TEXT_MEDIA_MISMATCH_PATTERNS = (
    re.compile(
        r"(?:本次|当前|这次|这通|本通|该次)(?:热线)?(?:通话|来电)(?:中|里|过程|期间)?"
    ),
    re.compile(r"(?:在|于)(?:本次|当前|这次|这通|本通|该次)热线(?:中|里|过程)"),
    re.compile(r"热线(?:接听|互动|会谈|场域|过程)"),
    re.compile(r"(?:受测者|工作者|工作人员)[^。；，]{0,10}(?:接线员|接线人员)"),
    re.compile(r"(?:接线员|接线人员)[^。；，]{0,12}(?:本次|当前|这次|这通|本通|该次)"),
    re.compile(r"(?:来访者|当事人)[^。；，]{0,10}来电者"),
    re.compile(r"来电者[^。；，]{0,12}(?:本次|当前|这次|这通|本通|该次)"),
    re.compile(r"(?:受测者|本次|当前)[^。；，]{0,12}(?:声音表现|声音线索|语速|话音)"),
)


def _text_media_narrative_mismatches(fragments: Sequence[str]) -> list[str]:
    """只识别把在线互动误写成语音热线，不误伤合法转介资源。"""
    text = "\n".join(fragment for fragment in fragments if fragment)
    return [
        "把当前在线互动表述为语音热线"
        for pattern in _TEXT_MEDIA_MISMATCH_PATTERNS
        if pattern.search(text)
    ][:1]


def _global_public_narratives(output: GlobalCodingOutput) -> list[str]:
    """收集最终可能进入报告的模型叙述；证据原话不属于模型叙述。"""
    evidence = [
        item
        for coded in output.coded_evidence
        for item in (coded.context, coded.alternative_reading)
        if item
    ]
    counter_evidence = [
        item
        for check in output.counter_checks
        for coded in check.found
        for item in (coded.context, coded.alternative_reading)
        if item
    ]
    bottom_lines = [
        item
        for candidate in output.bottom_line_candidates
        for item in (candidate.context, candidate.reasoning)
    ]
    conflicts = [
        item
        for conflict in output.material_conflict_candidates
        for item in (conflict.description, conflict.impact)
    ]
    return [*evidence, *counter_evidence, *bottom_lines, *conflicts]


def _validate_global_media_language(
    output: GlobalCodingOutput,
    media: Media,
) -> None:
    if media is not Media.text:
        return
    mismatches = _text_media_narrative_mismatches(_global_public_narratives(output))
    if mismatches:
        raise GlobalBatchError(
            "在线文字报告包含热线专属措辞：" + "、".join(mismatches)
        )


class GlobalBatchError(RuntimeError):
    pass


class LocalBatchError(RuntimeError):
    pass


class GroupBatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpportunityCheckResult:
    outcomes: Mapping[Target, list[OpportunityOutcome]]
    conditional_unavailable: Mapping[Target, list[str]]
    candidate_modules: list[SpecialModule]
    activated_modules: list[SpecialModule]
    inactive_modules: list[tuple[SpecialModule, str]]
    active_target_briefs: list[ActiveTargetBrief]


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    packet: DimensionPacket
    evidence: list[RoutedEvidence]
    counter_evidence: list[RoutedEvidence]
    analysis_failed: bool
    technical_failure: bool


@dataclass(frozen=True, slots=True)
class GroupRunResult:
    group: ScoringGroup
    output: GroupScoringOutput | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SourceNormalizedOutput:
    usable_output: GlobalCodingOutput
    rejected_by_target: Mapping[Target, tuple[CodedEvidence, ...]]


def split_coding_input(coding_input: CodingInput) -> tuple[CodingShard, CodingShard]:
    """确定性切成两片；后片回看最多 6 个话轮保留边界语境。"""
    turns = sorted(coding_input.turns, key=lambda turn: turn.sequence)
    if not turns:
        first_turns: list[CodingTurnInput] = []
        second_turns: list[CodingTurnInput] = []
        overlap_turn_ids: list[str] = []
    elif len(turns) == 1:
        first_turns = list(turns)
        second_turns = list(turns)
        overlap_turn_ids = [turns[0].turn_id]
    else:
        midpoint = len(turns) // 2
        first_turns = turns[:midpoint]
        overlap_size = min(6, len(first_turns))
        second_turns = turns[midpoint - overlap_size :]
        overlap_turn_ids = [turn.turn_id for turn in first_turns[-overlap_size:]]

    first = CodingShard(
        shard_id="shard-1",
        session=coding_input.session,
        turns=first_turns,
        work_record=None,
        technical_interruptions=[],
        termination=coding_input.termination,
        overlap_turn_ids=[],
    )
    second = CodingShard(
        shard_id="shard-2",
        session=coding_input.session,
        turns=second_turns,
        work_record=coding_input.work_record,
        technical_interruptions=coding_input.technical_interruptions,
        termination=coding_input.termination,
        overlap_turn_ids=overlap_turn_ids,
    )
    return first, second


def _validate_local_unit_role(
    unit: LocalCodedUnit,
    dialogue_speakers: Mapping[str, TurnSpeaker],
) -> None:
    dialogue_refs = [ref for ref in unit.refs if isinstance(ref, DialogueRef)]
    work_record_refs = [ref for ref in unit.refs if isinstance(ref, WorkRecordRef)]
    if unit.source_role in {"worker", "client"}:
        expected = (
            TurnSpeaker.worker if unit.source_role == "worker" else TurnSpeaker.client
        )
        if not dialogue_refs or any(
            dialogue_speakers.get(ref.turn_id) is not expected for ref in dialogue_refs
        ):
            raise LocalBatchError(f"{unit.id} source_role 与对话 speaker 不一致")
    elif unit.source_role == "interaction" and not dialogue_refs:
        raise LocalBatchError(f"{unit.id} interaction 单元缺少对话引用")
    elif unit.source_role == "work_record" and not work_record_refs:
        raise LocalBatchError(f"{unit.id} work_record 单元缺少工作记录引用")


def _validate_local_contract(
    shard: CodingShard,
    output: LocalCodingOutput,
) -> None:
    if output.shard_id != shard.shard_id:
        raise LocalBatchError("LocalCodingOutput shard_id 与输入分片不匹配")
    if (shard.turns or shard.work_record is not None) and not output.units:
        raise LocalBatchError("非空材料分片必须至少生成一个局部编码单元")
    unit_ids = [unit.id for unit in output.units]
    if len(unit_ids) != len(set(unit_ids)):
        raise LocalBatchError("局部编码 unit id 必须唯一")

    dialogue_turns = {turn.turn_id: turn.text for turn in shard.turns}
    dialogue_speakers = {turn.turn_id: turn.speaker for turn in shard.turns}
    work_fragments = (
        canonical_work_record_fragments(shard.work_record)
        if shard.work_record is not None
        else {}
    )
    for unit in output.units:
        for ref in unit.refs:
            if isinstance(ref, AudioEventRef):
                raise LocalBatchError("局部编码当前不接受音频事件引用")
            if isinstance(ref, DialogueRef):
                text = dialogue_turns.get(ref.turn_id)
                if text is None:
                    raise LocalBatchError(f"{unit.id} 引用了分片外话轮")
                if ref.quote not in text:
                    raise LocalBatchError(f"{unit.id} 对话 quote 不是原文连续子串")
            elif isinstance(ref, WorkRecordRef):
                if shard.work_record is None:
                    raise LocalBatchError(f"{unit.id} 引用了分片未携带的工作记录")
                fragments = work_fragments.get(ref.field, ())
                if not any(ref.quote in fragment for fragment in fragments):
                    raise LocalBatchError(
                        f"{unit.id} 工作记录 quote 不是原文连续子串"
                    )
        _validate_local_unit_role(unit, dialogue_speakers)


def _passthrough_unit_id(
    shard_id: str,
    source_kind: str,
    source_locator: str,
    used_ids: set[str],
) -> str:
    digest = hashlib.sha256(
        f"{shard_id}\x1f{source_kind}\x1f{source_locator}".encode()
    ).hexdigest()
    base_id = f"source-passthrough-{source_kind}-{digest}"
    candidate = base_id
    suffix = 1
    while candidate in used_ids:
        suffix += 1
        candidate = f"{base_id}-{suffix}"
    used_ids.add(candidate)
    return candidate


def _ensure_local_source_coverage(
    shard: CodingShard,
    output: LocalCodingOutput,
) -> LocalCodingOutput:
    """保留 Map 未编码的原始来源，交由 Reduce 决定是否与量规相关。"""
    units = list(output.units)
    used_ids = {unit.id for unit in units}
    turn_texts = {turn.turn_id: turn.text for turn in shard.turns}
    covered_turn_ids = {
        ref.turn_id
        for unit in units
        for ref in unit.refs
        if isinstance(ref, DialogueRef)
        and ref.turn_id in turn_texts
        and turn_texts[ref.turn_id] in ref.quote
    }
    for turn in shard.turns:
        if not turn.text.strip() or turn.turn_id in covered_turn_ids:
            continue
        source_role: Literal["worker", "client"] = (
            "worker" if turn.speaker is TurnSpeaker.worker else "client"
        )
        units.append(
            LocalCodedUnit(
                id=_passthrough_unit_id(
                    shard.shard_id,
                    "dialogue",
                    turn.turn_id,
                    used_ids,
                ),
                summary="原始话轮保留，待聚焦编码判断相关性。",
                initial_codes=["待聚焦编码"],
                refs=[
                    DialogueRef(
                        kind="dialogue",
                        turn_id=turn.turn_id,
                        quote=turn.text,
                    )
                ],
                source_role=source_role,
                alternative_reading=None,
            )
        )

    if shard.work_record is not None:
        existing_record_refs = [
            ref
            for unit in units
            for ref in unit.refs
            if isinstance(ref, WorkRecordRef)
        ]
        for field, fragments in canonical_work_record_fragments(
            shard.work_record
        ).items():
            for index, fragment in enumerate(fragments):
                if not fragment.strip() or any(
                    ref.field == field and ref.quote == fragment
                    for ref in existing_record_refs
                ):
                    continue
                record_ref = WorkRecordRef(
                    kind="work_record",
                    field=field,
                    quote=fragment,
                )
                units.append(
                    LocalCodedUnit(
                        id=_passthrough_unit_id(
                            shard.shard_id,
                            "work-record",
                            f"{field}\x1f{index}\x1f{fragment}",
                            used_ids,
                        ),
                        summary=(
                            "原始工作记录片段保留，"
                            "待聚焦编码判断相关性。"
                        ),
                        initial_codes=["待聚焦编码"],
                        refs=[record_ref],
                        source_role="work_record",
                        alternative_reading=None,
                    )
                )
                existing_record_refs.append(record_ref)

    return LocalCodingOutput(shard_id=output.shard_id, units=units)


def _map_stage_cache(local_outputs: Sequence[LocalCodingOutput]) -> dict[str, object]:
    return {
        "workflow_stage": MAP_COMPLETE_WORKFLOW_STAGE,
        "local_outputs": [output.model_dump(mode="json") for output in local_outputs],
    }


def _validated_cached_map_outputs(
    cached_coding: Mapping[str, object] | None,
    shards: Sequence[CodingShard],
) -> list[LocalCodingOutput] | None:
    if (
        cached_coding is None
        or cached_coding.get("workflow_stage") != MAP_COMPLETE_WORKFLOW_STAGE
    ):
        return None
    raw_outputs = cached_coding.get("local_outputs")
    if not isinstance(raw_outputs, list) or len(raw_outputs) != len(shards):
        return None
    try:
        parsed = [LocalCodingOutput.model_validate(item) for item in raw_outputs]
        outputs_by_shard = {output.shard_id: output for output in parsed}
        if len(outputs_by_shard) != len(parsed):
            return None
        ordered: list[LocalCodingOutput] = []
        for shard in shards:
            output = outputs_by_shard[shard.shard_id]
            _validate_local_contract(shard, output)
            completed = _ensure_local_source_coverage(shard, output)
            _validate_local_contract(shard, completed)
            ordered.append(completed)
    except (KeyError, TypeError, ValueError, LocalBatchError):
        return None
    return ordered


def _actor_state_payload(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    actor_state = value.get("actor_state", value)
    return actor_state if isinstance(actor_state, dict) else None


def _observed_hidden_state(
    opportunity_input: OpportunityCheckInput,
) -> tuple[dict[str, int], set[str]]:
    fact_depths: dict[str, int] = {}
    occurred_event_ids: set[str] = set()
    raw_states: list[object] = [opportunity_input.session_state]
    for turn_state in opportunity_input.turn_states:
        raw_states.extend(
            [turn_state.state_before_json, turn_state.state_after_json]
        )
    for raw_state in raw_states:
        state = _actor_state_payload(raw_state)
        if state is None:
            continue
        raw_facts = state.get("fact_states")
        if isinstance(raw_facts, dict):
            for fact_id, raw_fact in raw_facts.items():
                if not isinstance(fact_id, str) or not isinstance(raw_fact, dict):
                    continue
                disclosed_depth = raw_fact.get("disclosed_depth")
                if isinstance(disclosed_depth, int) and not isinstance(
                    disclosed_depth, bool
                ):
                    fact_depths[fact_id] = max(
                        fact_depths.get(fact_id, 0), disclosed_depth
                    )
        raw_events = state.get("occurred_event_ids")
        if isinstance(raw_events, (list, tuple)):
            occurred_event_ids.update(
                item for item in raw_events if isinstance(item, str)
            )
    return fact_depths, occurred_event_ids


def _declaration_fulfilled(
    declaration: ScoringOpportunity,
    *,
    runtime_natural_opportunity: bool,
    has_worker_turn: bool,
    has_work_record: bool,
    terminated_normally: bool,
    fact_depths: Mapping[str, int],
    occurred_event_ids: set[str],
) -> bool:
    natural_opportunity = {
        OpportunitySource.runtime_state: runtime_natural_opportunity,
        OpportunitySource.transcript: has_worker_turn,
        OpportunitySource.termination: has_worker_turn and terminated_normally,
        OpportunitySource.work_record: has_work_record,
    }[declaration.source]
    if not natural_opportunity:
        return False
    if declaration.kind is OpportunityKind.required:
        return True
    if declaration.required_fact_depths or declaration.required_event_ids:
        return all(
            fact_depths.get(fact_id, 0) >= depth
            for fact_id, depth in declaration.required_fact_depths.items()
        ) and set(declaration.required_event_ids).issubset(occurred_event_ids)
    if declaration.linked_fact_ids:
        return any(fact_depths.get(fact_id, 0) > 0 for fact_id in declaration.linked_fact_ids)
    return natural_opportunity


def check_opportunities(
    coding_input: CodingInput,
    opportunity_input: OpportunityCheckInput,
) -> OpportunityCheckResult:
    """独立核对机会；只把已出现任务的公开摘要交给聚焦编码。"""
    measurement = MeasurementSpec.model_validate(
        opportunity_input.case_package.get("measurement")
    )
    declared_by_target: dict[Target, list[ScoringOpportunity]] = {}
    for declaration in measurement.scoring_opportunities:
        if coding_input.session.scene not in declaration.scenes:
            continue
        declared_by_target.setdefault(declaration.target, []).append(declaration)

    fact_depths, occurred_event_ids = _observed_hidden_state(opportunity_input)

    has_worker_turn = any(
        turn.speaker is TurnSpeaker.worker and bool(turn.text.strip())
        for turn in coding_input.turns
    )
    has_work_record = coding_input.work_record is not None
    terminated_normally = (
        coding_input.termination.status is SessionStatus.ended
        and coding_input.termination.end_reason is not EndReason.technical_interruption
    )
    outcomes: dict[Target, list[OpportunityOutcome]] = {}
    conditional_unavailable: dict[Target, list[str]] = {}
    active_declarations: dict[Target, list[ScoringOpportunity]] = {}
    candidate_modules = {
        target for target in declared_by_target if isinstance(target, SpecialModule)
    }
    candidate_modules.add(SpecialModule.basic_risk_screening)
    candidate_targets: list[Target] = [*CoreDimension, *SpecialModule]
    for target in candidate_targets:
        rubric = get_rubric(target, media=coding_input.session.media)
        declarations = declared_by_target.get(target, [])
        target_outcomes: list[OpportunityOutcome] = []
        for declaration in declarations:
            runtime_natural_opportunity = (
                has_work_record
                if target is CoreDimension.documentation
                else has_worker_turn
            )
            fulfilled = _declaration_fulfilled(
                declaration,
                runtime_natural_opportunity=runtime_natural_opportunity,
                has_worker_turn=has_worker_turn,
                has_work_record=has_work_record,
                terminated_normally=terminated_normally,
                fact_depths=fact_depths,
                occurred_event_ids=occurred_event_ids,
            )
            if fulfilled:
                active_declarations.setdefault(target, []).append(declaration)
            target_outcomes.append(
                OpportunityOutcome(
                    declared_target=target,
                    kind=declaration.kind,
                    fulfilled=fulfilled,
                    indicator_ids=declaration.indicator_ids,
                    complex_opportunity=declaration.complex_opportunity,
                )
            )
        if not target_outcomes and (
            isinstance(target, CoreDimension)
            or target is SpecialModule.basic_risk_screening
        ):
            if target is CoreDimension.documentation:
                natural_opportunity = coding_input.work_record is not None
            elif target is CoreDimension.voice_and_process:
                # C6 观察的是受测者自己的表达与话轮管理；只要受测者开口，
                # 就已经产生观察机会。材料是否足够由后续证据充分性判断。
                natural_opportunity = has_worker_turn
            elif target is CoreDimension.closure_and_followup:
                # C8 的机会来自通话收束本身，不依赖案例事实；技术中断除外。
                natural_opportunity = (
                    has_worker_turn
                    and coding_input.termination.status is SessionStatus.ended
                    and coding_input.termination.end_reason
                    is not EndReason.technical_interruption
                )
            else:
                natural_opportunity = has_worker_turn
            target_outcomes = [
                OpportunityOutcome(
                    declared_target=target,
                    kind=OpportunityKind.required,
                    fulfilled=natural_opportunity,
                    indicator_ids=[indicator.id for indicator in rubric.indicators],
                    complex_opportunity=False,
                )
            ]
        if target_outcomes:
            outcomes[target] = target_outcomes
        has_fulfilled_conditional = any(
            outcome.kind is OpportunityKind.conditional and outcome.fulfilled
            for outcome in target_outcomes
        )
        conditional_unavailable[target] = (
            []
            if not rubric.conditional_in_level3 or has_fulfilled_conditional
            else list(rubric.conditional_in_level3)
        )
    activated_modules = [
        module
        for module in SpecialModule
        if module is SpecialModule.basic_risk_screening
        or (
            module in candidate_modules
            and any(outcome.fulfilled for outcome in outcomes.get(module, []))
        )
    ]
    inactive_modules = [
        (
            module,
            (
                "案例已声明该专项情景，但本次会谈未达到启用门槛。"
                if module in candidate_modules
                else "案例未声明该专项情景，本次不启用。"
            ),
        )
        for module in SpecialModule
        if module not in activated_modules
    ]
    ordered_candidate_modules = [
        module for module in SpecialModule if module in candidate_modules
    ]
    declared_targets: list[Target] = [*CoreDimension, *ordered_candidate_modules]
    active_target_briefs = [
        ActiveTargetBrief(
            target=target,
            description="；".join(
                dict.fromkeys(item.description for item in declarations)
            ),
            evidence_targets=list(
                dict.fromkeys(
                    evidence_target
                    for item in declarations
                    for evidence_target in item.evidence_targets
                )
            ),
            indicator_ids=list(
                dict.fromkeys(
                    indicator_id
                    for item in declarations
                    for indicator_id in item.indicator_ids
                )
            ),
        )
        for target, declarations in active_declarations.items()
    ]
    return OpportunityCheckResult(
        outcomes={target: outcomes[target] for target in declared_targets},
        conditional_unavailable={
            target: conditional_unavailable[target] for target in declared_targets
        },
        candidate_modules=ordered_candidate_modules,
        activated_modules=activated_modules,
        inactive_modules=inactive_modules,
        active_target_briefs=active_target_briefs,
    )


def _invalid_unit_ids(coding_input: CodingInput, units: Sequence[MeaningUnit]) -> set[str]:
    dialogue_ids = {turn.turn_id for turn in coding_input.turns}
    work_fragments = (
        canonical_work_record_fragments(coding_input.work_record)
        if coding_input.work_record is not None
        else {}
    )
    invalid: set[str] = set()
    for unit in units:
        if any(turn_id not in dialogue_ids for turn_id in unit.turn_ids):
            invalid.add(unit.id)
            continue
        for ref in unit.work_record_refs:
            fragments = work_fragments.get(ref.field, ())
            if not any(ref.quote in fragment for fragment in fragments):
                invalid.add(unit.id)
                break
        if unit.audio_event_ids:
            invalid.add(unit.id)
    return invalid


def _validate_global_targets(
    output: GlobalCodingOutput,
    allowed_targets: Set[Target],
) -> None:
    unit_ids = [unit.id for unit in output.units]
    if len(unit_ids) != len(set(unit_ids)):
        raise GlobalBatchError("全局编码的意义单元 id 重复")
    submitted_targets = {item.target for item in output.coded_evidence}
    submitted_targets.update(item.target for item in output.counter_checks)
    conflict_targets = {
        target
        for conflict in output.material_conflict_candidates
        for target in conflict.affected_targets
    }
    unexpected = (submitted_targets | conflict_targets) - set(allowed_targets)
    if unexpected:
        labels = ", ".join(sorted(target.value for target in unexpected))
        raise GlobalBatchError(f"全局编码包含未启用 target：{labels}")


def _matching_unit_ids(
    ref: DialogueRef | WorkRecordRef | AudioEventRef,
    units: Sequence[MeaningUnit],
) -> list[str]:
    if isinstance(ref, DialogueRef):
        return [unit.id for unit in units if ref.turn_id in unit.turn_ids]
    if isinstance(ref, WorkRecordRef):
        return [unit.id for unit in units if ref in unit.work_record_refs]
    return [unit.id for unit in units if ref.event_id in unit.audio_event_ids]


def _source_ref_exists(
    coding_input: CodingInput,
    ref: DialogueRef | WorkRecordRef | AudioEventRef,
) -> bool:
    if isinstance(ref, DialogueRef):
        turn_text = next(
            (
                turn.text
                for turn in coding_input.turns
                if turn.turn_id == ref.turn_id
            ),
            None,
        )
        return turn_text is not None and ref.quote in turn_text
    if isinstance(ref, WorkRecordRef):
        if coding_input.work_record is None:
            return False
        fragments = canonical_work_record_fragments(coding_input.work_record).get(
            ref.field,
            (),
        )
        return any(ref.quote in fragment for fragment in fragments)
    return False


def _normalize_evidence_unit(
    evidence: CodedEvidence,
    units: Sequence[MeaningUnit],
) -> CodedEvidence:
    """仅在精确来源唯一属于一个意义单元时纠正模型写错的 unit_id。"""
    matching_ids = _matching_unit_ids(evidence.ref, units)
    if len(matching_ids) != 1 or evidence.unit_id == matching_ids[0]:
        return evidence
    return evidence.model_copy(update={"unit_id": matching_ids[0]})


def _normalize_reduce_output_sources(
    coding_input: CodingInput,
    output: GlobalCodingOutput,
) -> SourceNormalizedOutput:
    """按冻结话轮归属整理 Reduce 产物，保留原始质性单元供审计。"""
    turn_speakers = {turn.turn_id: turn.speaker for turn in coding_input.turns}
    normalized_units = list(output.units)
    used_unit_ids = {unit.id for unit in normalized_units}
    rejected: dict[Target, list[CodedEvidence]] = {}

    def evidence_carrier(evidence: CodedEvidence) -> CodedEvidence:
        normalized = _normalize_evidence_unit(evidence, normalized_units)
        if _matching_unit_ids(normalized.ref, normalized_units):
            return normalized
        if not _source_ref_exists(coding_input, normalized.ref):
            return normalized
        if isinstance(normalized.ref, DialogueRef):
            source_locator = (
                f"dialogue\x1f{normalized.ref.turn_id}\x1f{normalized.ref.quote}"
            )
            carrier = MeaningUnit(
                id=_passthrough_unit_id(
                    "reduce",
                    "evidence",
                    source_locator,
                    used_unit_ids,
                ),
                turn_ids=[normalized.ref.turn_id],
                summary=normalized.context,
            )
        elif isinstance(normalized.ref, WorkRecordRef):
            source_locator = (
                f"work-record\x1f{normalized.ref.field}\x1f{normalized.ref.quote}"
            )
            carrier = MeaningUnit(
                id=_passthrough_unit_id(
                    "reduce",
                    "evidence",
                    source_locator,
                    used_unit_ids,
                ),
                work_record_refs=[normalized.ref],
                summary=normalized.context,
            )
        else:
            carrier = MeaningUnit(
                id=_passthrough_unit_id(
                    "reduce",
                    "evidence",
                    f"audio\x1f{normalized.ref.event_id}",
                    used_unit_ids,
                ),
                audio_event_ids=[normalized.ref.event_id],
                summary=normalized.context,
            )
        normalized_units.append(carrier)
        return normalized.model_copy(update={"unit_id": carrier.id})

    def normalize_ability_evidence(
        evidence: CodedEvidence,
    ) -> CodedEvidence | None:
        if (
            isinstance(evidence.ref, DialogueRef)
            and turn_speakers.get(evidence.ref.turn_id) is TurnSpeaker.client
        ):
            rejected.setdefault(evidence.target, []).append(evidence)
            return None
        return evidence_carrier(evidence)

    usable_evidence = [
        normalized
        for evidence in output.coded_evidence
        if (normalized := normalize_ability_evidence(evidence)) is not None
    ]
    original_unit_ids = {unit.id for unit in output.units}
    usable_checks: list[CounterCheck] = []
    for check in output.counter_checks:
        role_valid_found: list[tuple[CodedEvidence, CodedEvidence]] = []
        for original in check.found:
            normalized = normalize_ability_evidence(original)
            if normalized is not None:
                role_valid_found.append((original, normalized))

        normalized_found: list[CodedEvidence] = []
        retained_pairs: list[tuple[CodedEvidence, CodedEvidence]] = []
        removed_duplicate = False
        for original, candidate in role_valid_found:
            duplicates_initial = any(
                candidate.target == evidence.target
                and candidate.indicator_id == evidence.indicator_id
                and candidate.direction == evidence.direction
                and candidate.unit_id == evidence.unit_id
                and candidate.ref == evidence.ref
                for evidence in usable_evidence
            )
            duplicates_counter = any(
                candidate.target == evidence.target
                and candidate.indicator_id == evidence.indicator_id
                and candidate.direction == evidence.direction
                and candidate.unit_id == evidence.unit_id
                and candidate.ref == evidence.ref
                for evidence in normalized_found
            )
            if duplicates_initial or duplicates_counter:
                removed_duplicate = True
                continue
            normalized_found.append(candidate)
            retained_pairs.append((original, candidate))

        # 只对模型已声明检索的 id 做可确定的指针纠正。不把全部单元、
        # 也不把 Reduce 返回后才合成的 carrier 写成“模型已检索”。
        scope_rebindings = {
            original.unit_id: normalized.unit_id
            for original, normalized in retained_pairs
            if original.unit_id != normalized.unit_id
            and normalized.unit_id in original_unit_ids
        }
        normalized_scope = list(
            dict.fromkeys(
                scope_rebindings.get(unit_id, unit_id)
                for unit_id in check.searched_unit_ids
            )
        )
        note = check.not_found_note
        if check.found and not normalized_found and not (note and note.strip()):
            if removed_duplicate:
                note = "返回候选与初始编码重复，未形成独立反例。"
            else:
                note = (
                    "候选反例仅引用来电者话轮，已由来源规则排除；"
                    "未形成可用于受测者能力定级的反例证据。"
                )
        usable_checks.append(
            check.model_copy(
                update={
                    "searched_unit_ids": normalized_scope,
                    "found": normalized_found,
                    "not_found_note": note,
                }
            )
        )

    worker_turn_ids = {
        turn_id
        for turn_id, speaker in turn_speakers.items()
        if speaker is TurnSpeaker.worker
    }
    usable_bottom_lines = [
        candidate
        for candidate in output.bottom_line_candidates
        if candidate.category is SemanticBottomLineCategory.fabricated_record
        or any(
            isinstance(ref, DialogueRef) and ref.turn_id in worker_turn_ids
            for ref in candidate.refs
        )
    ]
    usable_urgent_risk = [
        candidate
        for candidate in output.urgent_risk_disclosure_candidates
        if turn_speakers.get(candidate.ref.turn_id) is TurnSpeaker.client
    ]
    usable_output = output.model_copy(
        update={
            "units": normalized_units,
            "coded_evidence": usable_evidence,
            "counter_checks": usable_checks,
            "bottom_line_candidates": usable_bottom_lines,
            "material_conflict_candidates": list(output.material_conflict_candidates),
            "urgent_risk_disclosure_candidates": usable_urgent_risk,
        }
    )
    return SourceNormalizedOutput(
        usable_output=usable_output,
        rejected_by_target={
            target: tuple(items) for target, items in rejected.items()
        },
    )


def _validate_worker_attribution(
    coding_input: CodingInput,
    output: GlobalCodingOutput,
) -> None:
    worker_turn_ids = {
        turn.turn_id
        for turn in coding_input.turns
        if turn.speaker is TurnSpeaker.worker
    }
    ability_evidence = [*output.coded_evidence]
    for check in output.counter_checks:
        ability_evidence.extend(check.found)
    for evidence in ability_evidence:
        if isinstance(evidence.ref, DialogueRef) and (
            evidence.ref.turn_id not in worker_turn_ids
        ):
            raise GlobalBatchError(
                f"{evidence.target.value} 能力证据的 DialogueRef 必须引用 worker 话轮："
                f"{evidence.ref.turn_id}"
            )
    for candidate in output.bottom_line_candidates:
        if candidate.category is SemanticBottomLineCategory.fabricated_record:
            continue
        if not any(
            isinstance(ref, DialogueRef) and ref.turn_id in worker_turn_ids
            for ref in candidate.refs
        ):
            raise GlobalBatchError(
                f"{candidate.category.value} 语义底线候选至少包含一条 worker DialogueRef"
            )


def _validate_reduce_source_closure(
    local_outputs: Sequence[LocalCodingOutput],
    output: GlobalCodingOutput,
) -> None:
    dialogue_quotes: dict[str, list[str]] = {}
    work_record_quotes: dict[object, list[str]] = {}
    for local_output in local_outputs:
        for local_unit in local_output.units:
            for ref in local_unit.refs:
                if isinstance(ref, DialogueRef):
                    dialogue_quotes.setdefault(ref.turn_id, []).append(ref.quote)
                elif isinstance(ref, WorkRecordRef):
                    work_record_quotes.setdefault(ref.field, []).append(ref.quote)

    def require_local_ref(ref: object, *, location: str) -> None:
        if isinstance(ref, DialogueRef):
            quotes = dialogue_quotes.get(ref.turn_id, ())
            valid = any(ref.quote in local_quote for local_quote in quotes)
        elif isinstance(ref, WorkRecordRef):
            quotes = work_record_quotes.get(ref.field, ())
            valid = any(ref.quote in local_quote for local_quote in quotes)
        else:
            valid = False
        if not valid:
            raise GlobalBatchError(f"{location} 引用了 Map 局部编码未提供的材料来源")

    for global_unit in output.units:
        for turn_id in global_unit.turn_ids:
            if turn_id not in dialogue_quotes:
                raise GlobalBatchError(
                    f"MeaningUnit {global_unit.id} 引用了 Map 局部编码未提供的话轮"
                )
        for ref in global_unit.work_record_refs:
            require_local_ref(ref, location=f"MeaningUnit {global_unit.id}")
    for evidence in output.coded_evidence:
        require_local_ref(evidence.ref, location="coded_evidence")
    for check in output.counter_checks:
        for evidence in check.found:
            require_local_ref(evidence.ref, location=f"CounterCheck {check.target.value}")
    for bottom_line_candidate in output.bottom_line_candidates:
        for ref in bottom_line_candidate.refs:
            require_local_ref(
                ref,
                location=f"bottom_line {bottom_line_candidate.category.value}",
            )
    for conflict in output.material_conflict_candidates:
        if conflict.dialogue_ref is not None:
            require_local_ref(conflict.dialogue_ref, location=f"material_conflict {conflict.id}")
        if conflict.work_record_ref is not None:
            require_local_ref(conflict.work_record_ref, location=f"material_conflict {conflict.id}")
    for urgent_candidate in output.urgent_risk_disclosure_candidates:
        require_local_ref(
            urgent_candidate.ref,
            location="urgent_risk_disclosure_candidate",
        )


def _validate_global_contract(
    coding_input: CodingInput,
    output: GlobalCodingOutput,
    targets: Sequence[Target],
) -> None:
    allowed_targets = set(targets)
    _validate_global_media_language(output, coding_input.session.media)
    _validate_global_targets(output, allowed_targets)
    _validate_worker_attribution(coding_input, output)
    invalid_units = _invalid_unit_ids(coding_input, output.units)
    if invalid_units:
        raise GlobalBatchError(
            "意义单元包含非法材料引用：" + ", ".join(sorted(invalid_units))
        )
    dialogue_turns = {turn.turn_id: turn.text for turn in coding_input.turns}
    check_counts = Counter(check.target for check in output.counter_checks)
    if set(check_counts) != allowed_targets or any(
        check_counts[target] != 1 for target in allowed_targets
    ):
        raise GlobalBatchError("CounterCheck 未完整且唯一覆盖全部启用 target")
    for target in targets:
        validation = validate_evidence(
            target=target,
            submitted=[item for item in output.coded_evidence if item.target == target],
            meaning_units=output.units,
            dialogue_turns=dialogue_turns,
            work_record=coding_input.work_record,
            audio_event_ids=set(),
        )
        if validation.rejected:
            details = "；".join(
                (
                    f"{item.evidence.unit_id}/{item.evidence.indicator_id}:"
                    f"{item.reason.value}（{item.detail}）"
                )
                for item in validation.rejected[:5]
            )
            raise GlobalBatchError(
                f"{target.value} 包含非法编码证据：{details}"
            )
        check = next(item for item in output.counter_checks if item.target == target)
        check_validation = validate_counter_check(
            check,
            output.units,
            dialogue_turns=dialogue_turns,
            work_record=coding_input.work_record,
            audio_event_ids=set(),
        )
        if not check_validation.complete:
            details = "；".join(check_validation.reasons[:5])
            raise GlobalBatchError(
                f"{target.value} CounterCheck 契约不完整：{details}"
            )
    bottom_line_result = semantic_bottom_line_events(
        output.bottom_line_candidates,
        dialogue_turns=dialogue_turns,
        work_record=coding_input.work_record,
        audio_event_ids=set(),
        rule_conflicts=None,
        semantic_conflicts=None,
    )
    if bottom_line_result.rejected:
        raise GlobalBatchError("底线候选包含非法材料引用")
    try:
        validate_urgent_risk_disclosure_candidates(
            output.urgent_risk_disclosure_candidates,
            dialogue_turns=dialogue_turns,
            client_turn_ids={
                turn.turn_id
                for turn in coding_input.turns
                if turn.speaker is TurnSpeaker.client
            },
        )
    except ValueError as exc:
        raise GlobalBatchError(str(exc)) from exc
    try:
        validate_material_conflict_candidates(
            output.material_conflict_candidates,
            dialogue_turns=dialogue_turns,
            work_record=coding_input.work_record,
        )
    except ValueError as exc:
        raise GlobalBatchError(str(exc)) from exc


def _build_validated_targets(
    coding_input: CodingInput,
    opportunity_result: OpportunityCheckResult,
    output: GlobalCodingOutput,
    targets: Sequence[Target],
    *,
    rejected_by_target: Mapping[Target, Sequence[CodedEvidence]] | None = None,
) -> dict[Target, ValidatedTarget]:
    allowed_targets = set(REPORT_TARGETS)
    _validate_global_targets(output, allowed_targets)
    dialogue_turns = {turn.turn_id: turn.text for turn in coding_input.turns}
    invalid_units = _invalid_unit_ids(coding_input, output.units)
    check_counts = Counter(check.target for check in output.counter_checks)
    technical_failure = (
        coding_input.termination.end_reason is EndReason.technical_interruption
    )
    validated: dict[Target, ValidatedTarget] = {}
    source_rejections = rejected_by_target or {}
    for target in targets:
        submitted = [item for item in output.coded_evidence if item.target == target]
        evidence_validation = validate_evidence(
            target=target,
            submitted=submitted,
            meaning_units=output.units,
            dialogue_turns=dialogue_turns,
            work_record=coding_input.work_record,
            audio_event_ids=set(),
        )
        target_failed = bool(evidence_validation.rejected)
        if any(item.unit_id in invalid_units for item in submitted):
            target_failed = True

        checks = [check for check in output.counter_checks if check.target == target]
        routed_counter: list[RoutedEvidence] = []
        if check_counts[target] != 1:
            target_failed = True
        else:
            check = checks[0]
            check_validation = validate_counter_check(
                check,
                output.units,
                dialogue_turns=dialogue_turns,
                work_record=coding_input.work_record,
                audio_event_ids=set(),
            )
            if not check_validation.complete:
                target_failed = True
            counter_validation = validate_evidence(
                target=target,
                submitted=check.found,
                meaning_units=output.units,
                dialogue_turns=dialogue_turns,
                work_record=coding_input.work_record,
                audio_event_ids=set(),
            )
            routed_counter = counter_validation.accepted
            if any(item.unit_id in invalid_units for item in check.found):
                target_failed = True

        usable_ability_candidates = [
            *submitted,
            *(checks[0].found if checks else []),
        ]
        if source_rejections.get(target) and not usable_ability_candidates:
            target_failed = True

        routed_evidence = evidence_validation.accepted
        involved_ids = {
            item.evidence.unit_id for item in [*routed_evidence, *routed_counter]
        }
        packet_units = [unit for unit in output.units if unit.id in involved_ids]
        outcomes = opportunity_result.outcomes[target]
        conditional = opportunity_result.conditional_unavailable[target]
        has_complex = any(
            outcome.fulfilled and outcome.complex_opportunity for outcome in outcomes
        )
        packet = DimensionPacket(
            scene=coding_input.session.scene,
            media=coding_input.session.media,
            target=target,
            rubric=get_rubric(target, media=coding_input.session.media),
            evidence=[
                PacketEvidence(evidence=item.evidence, role=item.role)
                for item in routed_evidence
            ],
            counter_evidence=[
                PacketEvidence(evidence=item.evidence, role=item.role)
                for item in routed_counter
            ],
            units=packet_units,
            opportunities=outcomes,
            conditional_unavailable=conditional,
            level_ceiling=calculate_level_ceiling(
                evidence=routed_evidence,
                counter_evidence=routed_counter,
                conditional_unavailable=conditional,
                has_complex_opportunity=has_complex,
            ),
        )
        validated[target] = ValidatedTarget(
            packet=packet,
            evidence=routed_evidence,
            counter_evidence=routed_counter,
            analysis_failed=target_failed,
            technical_failure=technical_failure,
        )
    return validated


def _validate_group_output(
    output: GroupScoringOutput,
    packets: Sequence[DimensionPacket],
) -> None:
    expected = {packet.target for packet in packets}
    counts = Counter(proposal.target for proposal in output.proposals)
    if set(counts) != expected or any(count != 1 for count in counts.values()):
        raise GroupBatchError("定级组 proposals 未完整且唯一覆盖本组 target")
    packets_by_target = {packet.target: packet for packet in packets}
    for proposal in output.proposals:
        packet = packets_by_target[proposal.target]
        if packet.media is Media.text:
            mismatched_terms = _text_media_narrative_mismatches(
                [
                    proposal.pattern,
                    proposal.rationale,
                    *proposal.next_level_gap,
                    *proposal.evidence_confidence_factors,
                ]
            )
            if mismatched_terms:
                raise GroupBatchError(
                    "在线文字报告包含热线专属措辞："
                    + "、".join(dict.fromkeys(mismatched_terms))
                )
        packet_unit_ids = {unit.id for unit in packet.units}
        unknown_representative = (
            set(proposal.representative_units) - packet_unit_ids
        )
        if unknown_representative:
            raise GroupBatchError(
                f"{proposal.target.value} 代表单元不存在："
                + ", ".join(sorted(unknown_representative))
            )
        unknown_limiting = set(proposal.limiting_units) - packet_unit_ids
        if unknown_limiting:
            raise GroupBatchError(
                f"{proposal.target.value} 限制单元不存在："
                + ", ".join(sorted(unknown_limiting))
            )

        primary_unit_ids = {
            item.evidence.unit_id
            for item in packet.evidence
            if item.role is EvidenceRole.primary
        }
        gradable_unit_ids = set(primary_unit_ids)
        if gradable_unit_ids:
            gradable_unit_ids.update(
                item.evidence.unit_id
                for item in packet.evidence
                if item.role is EvidenceRole.supporting
            )
        invalid_representative = (
            set(proposal.representative_units) - gradable_unit_ids
        )
        if invalid_representative:
            raise GroupBatchError(
                f"{proposal.target.value} 代表单元没有有效定级证据："
                + ", ".join(sorted(invalid_representative))
            )
        evidence_unit_ids = {
            item.evidence.unit_id
            for item in [*packet.evidence, *packet.counter_evidence]
        }
        invalid_limiting = set(proposal.limiting_units) - evidence_unit_ids
        if invalid_limiting:
            raise GroupBatchError(
                f"{proposal.target.value} 限制单元没有有效证据："
                + ", ".join(sorted(invalid_limiting))
            )

        violations = classification_language_violations(proposal)
        if violations:
            raise GroupBatchError(f"{proposal.target.value} 包含分类式判断")
        has_opportunity = any(
            outcome.fulfilled for outcome in packet.opportunities
        )
        if proposal.proposed_level is None:
            if has_opportunity and any(
                item.role is EvidenceRole.primary for item in packet.evidence
            ):
                raise GroupBatchError(
                    f"{proposal.target.value} 已有可定级主要证据但 proposed_level 为 null"
                )
            if (
                proposal.pattern.strip()
                or proposal.representative_units
                or proposal.limiting_units
                or proposal.next_level_gap
            ):
                raise GroupBatchError(
                    f"{proposal.target.value} null 分支不得携带等级描述、单元引用或下一等级缺口"
                )
            if (
                proposal.evidence_confidence is not EvidenceConfidence.low
                or not proposal.rationale.strip()
                or not any(
                    factor.strip() for factor in proposal.evidence_confidence_factors
                )
            ):
                raise GroupBatchError(
                    f"{proposal.target.value} null 分支必须说明材料缺口、使用 low 置信度并给出因素"
                )
            continue
        if not has_opportunity:
            continue
        if not set(proposal.representative_units) & primary_unit_ids:
            raise GroupBatchError(
                f"{proposal.target.value} 代表单元必须至少包含一条 primary 证据"
            )
        representative_sufficiency = assess_evidence_sufficiency(
            target=packet.target,
            representative_unit_ids=proposal.representative_units,
            units=packet.units,
            evidence=[
                RoutedEvidence(evidence=item.evidence, role=item.role)
                for item in packet.evidence
            ],
            declared_opportunity_count=len(packet.opportunities),
        )
        if not representative_sufficiency.sufficient:
            raise GroupBatchError(
                f"{proposal.target.value} 代表性证据不足："
                f"{representative_sufficiency.reason or '未满足证据充分性规则'}"
            )
        if proposal.proposed_level > packet.level_ceiling:
            raise GroupBatchError(
                f"{proposal.target.value} proposed_level 超过 level_ceiling"
            )
        if not (
            proposal.pattern.strip()
            and proposal.rationale.strip()
            and proposal.representative_units
            and any(item.strip() for item in proposal.evidence_confidence_factors)
        ):
            raise GroupBatchError(f"{proposal.target.value} 定级必要字段不完整")
        if proposal.proposed_level < 4:
            if not any(item.strip() for item in proposal.next_level_gap):
                raise GroupBatchError(
                    f"{proposal.target.value} 缺少针对下一等级的可观察行为"
                )


def _indicator_states(item: ValidatedTarget) -> dict[str, IndicatorStatus]:
    has_opportunity = any(outcome.fulfilled for outcome in item.packet.opportunities)
    states: dict[str, IndicatorStatus] = {}
    evidence = [
        routed.evidence for routed in [*item.evidence, *item.counter_evidence]
    ]
    for indicator in item.packet.rubric.indicators:
        matching = [coded for coded in evidence if coded.indicator_id == indicator.id]
        if any(coded.direction is EvidenceDirection.adverse for coded in matching):
            state = IndicatorStatus.adverse
        elif any(coded.direction is EvidenceDirection.limit for coded in matching):
            state = IndicatorStatus.opportunity_missed
        elif any(
            coded.direction is EvidenceDirection.support
            and coded.strength is EvidenceStrength.strong
            for coded in matching
        ):
            state = IndicatorStatus.demonstrated
        elif any(coded.direction is EvidenceDirection.support for coded in matching):
            state = IndicatorStatus.partial
        elif not has_opportunity:
            state = IndicatorStatus.no_opportunity
        else:
            state = IndicatorStatus.no_reliable_material
        states[indicator.id] = state
    return states


def _known_urgent_termination_event(
    coding_input: CodingInput,
    validated: Mapping[Target, ValidatedTarget],
    global_output: GlobalCodingOutput,
) -> BottomLineEvent | None:
    turns_by_id = {turn.turn_id: turn for turn in coding_input.turns}
    disclosure_candidates = validate_urgent_risk_disclosure_candidates(
        global_output.urgent_risk_disclosure_candidates,
        dialogue_turns={turn.turn_id: turn.text for turn in coding_input.turns},
        client_turn_ids={
            turn.turn_id
            for turn in coding_input.turns
            if turn.speaker is TurnSpeaker.client
        },
    )
    disclosure_refs = [
        candidate.ref
        for candidate in disclosure_candidates
        if candidate.immediacy is RiskDisclosureImmediacy.imminent
    ]
    safety_refs: list[DialogueRef] = []
    for target, item in validated.items():
        for routed in item.evidence:
            evidence = routed.evidence
            if not isinstance(evidence.ref, DialogueRef):
                continue
            if evidence.direction is not EvidenceDirection.support:
                continue
            if not (
                target is SpecialModule.safety_response
                or evidence.indicator_id == "C5.action_layers"
            ):
                continue
            turn = turns_by_id.get(evidence.ref.turn_id)
            if turn is not None and turn.speaker is TurnSpeaker.worker:
                safety_refs.append(evidence.ref)
    result = detect_known_urgent_risk_termination(
        disclosed_urgent_risk_refs=disclosure_refs,
        dialogue_turns={turn.turn_id: turn.text for turn in coding_input.turns},
        ordered_turn_ids=[turn.turn_id for turn in coding_input.turns],
        worker_turn_ids={
            turn.turn_id
            for turn in coding_input.turns
            if turn.speaker is TurnSpeaker.worker
        },
        safety_action_refs=safety_refs,
        call_ended=coding_input.termination.status is SessionStatus.ended,
    )
    return result.event


class ReportPipeline:
    def __init__(
        self,
        engine: Engine,
        cases: CaseRepository,
        gateway: ReportModelGateway,
    ) -> None:
        self._engine = engine
        self._cases = cases
        self._gateway = gateway

    async def run(self, job_id: str) -> None:
        try:
            await self._run(job_id)
        except Exception as exc:
            self._update(
                job_id,
                ReportJobProgressUpdate(
                    stage=ReportJobStage.failed,
                    last_error=f"报告组装失败：{type(exc).__name__}: {exc}",
                ),
            )

    async def _run(self, job_id: str) -> None:
        with Session(self._engine) as db:
            service = ReportJobService(db, self._cases)
            if not service.claim_for_processing(job_id):
                return
            job_record = db.get(ReportJobRecord, job_id)
            if job_record is None:
                raise LookupError(job_id)
            coding_input = service.get_coding_input(job_id)
            cached_coding = job_record.coding_json
            completed_groups = set(job_record.scoring_groups_done)
            cached_groups = dict(job_record.scoring_group_results_json)
            session_id = job_record.session_id
            model_config = ReportModelConfig.model_validate(job_record.model_snapshot)
            opportunity_input = service.get_opportunity_check_input(job_id)

        opportunity_result = check_opportunities(coding_input, opportunity_input)
        targets: list[Target] = [
            *CoreDimension,
            *opportunity_result.activated_modules,
        ]
        shards = split_coding_input(coding_input)
        is_map_stage_cache = (
            cached_coding is not None
            and cached_coding.get("workflow_stage") == MAP_COMPLETE_WORKFLOW_STAGE
        )
        if cached_coding is None or is_map_stage_cache:
            local_outputs = _validated_cached_map_outputs(cached_coding, shards)
            if local_outputs is None:
                try:
                    map_results = await asyncio.gather(
                        *(
                            self._run_map_with_retries(
                                job_id,
                                shard,
                                session_id=session_id,
                                model_config=model_config,
                            )
                            for shard in shards
                        ),
                        return_exceptions=True,
                    )
                    map_errors = [
                        result
                        for result in map_results
                        if isinstance(result, BaseException)
                    ]
                    if map_errors:
                        details = "; ".join(
                            f"{type(error).__name__}: {error}" for error in map_errors
                        )
                        raise LocalBatchError(details)
                except Exception as exc:
                    self._update(
                        job_id,
                        ReportJobProgressUpdate(
                            stage=ReportJobStage.failed,
                            last_error=f"局部编码失败：{type(exc).__name__}: {exc}",
                        ),
                    )
                    return
                local_outputs = [
                    result
                    for result in map_results
                    if isinstance(result, LocalCodingOutput)
                ]
                self._update(
                    job_id,
                    ReportJobProgressUpdate(coding_json=_map_stage_cache(local_outputs)),
                )
            try:
                global_output = await self._run_reduce_with_retries(
                    job_id,
                    coding_input,
                    local_outputs,
                    session_id=session_id,
                    model_config=model_config,
                    targets=targets,
                    active_target_briefs=opportunity_result.active_target_briefs,
                )
            except Exception as exc:
                self._update(
                    job_id,
                    ReportJobProgressUpdate(
                        stage=ReportJobStage.failed,
                        last_error=f"聚焦汇总失败：{type(exc).__name__}: {exc}",
                    ),
                )
                return
            self._update(
                job_id,
                ReportJobProgressUpdate(
                    coding_json=global_output.model_dump(mode="json"),
                ),
            )
        else:
            global_output = GlobalCodingOutput.model_validate(cached_coding)

        _validate_global_media_language(global_output, coding_input.session.media)
        source_normalized = _normalize_reduce_output_sources(
            coding_input,
            global_output,
        )
        global_output = source_normalized.usable_output
        activated_modules = opportunity_result.activated_modules

        validated = _build_validated_targets(
            coding_input,
            opportunity_result,
            global_output,
            targets,
            rejected_by_target=source_normalized.rejected_by_target,
        )

        self._update(job_id, ReportJobProgressUpdate(stage=ReportJobStage.scoring))
        group_outputs: dict[ScoringGroup, GroupScoringOutput] = {}
        groups_to_run: list[tuple[ScoringGroup, list[DimensionPacket]]] = []
        for group in ScoringGroup:
            packets = self._packets_for_group(group, validated)
            if group.value in completed_groups:
                try:
                    cached_output = GroupScoringOutput.model_validate(
                        cached_groups[group.value]
                    )
                    _validate_group_output(cached_output, packets)
                    group_outputs[group] = cached_output
                    continue
                except (KeyError, ValueError, GroupBatchError):
                    completed_groups.discard(group.value)
                    self._update(
                        job_id,
                        ReportJobProgressUpdate(incomplete_group=group.value),
                    )
            if not packets:
                empty_output = GroupScoringOutput(proposals=[])
                group_outputs[group] = empty_output
                self._update(
                    job_id,
                    ReportJobProgressUpdate(
                        scoring_group_id=group.value,
                        scoring_group_result=empty_output.model_dump(mode="json"),
                        completed_group=group.value,
                    ),
                )
                continue
            groups_to_run.append((group, packets))

        group_results = await asyncio.gather(
            *(
                self._run_group_with_retries(
                    job_id,
                    group,
                    packets,
                    session_id=session_id,
                    model_config=model_config,
                )
                for group, packets in groups_to_run
            )
        )
        failed_groups: set[ScoringGroup] = set()
        for result in group_results:
            if result.output is None:
                failed_groups.add(result.group)
                self._update(
                    job_id,
                    ReportJobProgressUpdate(
                        scoring_group_id=result.group.value,
                        scoring_group_result={
                            "status": "failed",
                            "error": result.error or "定级组失败",
                        },
                        incomplete_group=result.group.value,
                    ),
                )
                continue
            group_outputs[result.group] = result.output
            self._update(
                job_id,
                ReportJobProgressUpdate(
                    scoring_group_id=result.group.value,
                    scoring_group_result=result.output.model_dump(mode="json"),
                    completed_group=result.group.value,
                ),
            )

        self._update(job_id, ReportJobProgressUpdate(stage=ReportJobStage.assembling))
        results = self._assemble_dimensions(
            targets,
            validated,
            group_outputs,
            failed_groups,
        )
        dialogue_turns = {turn.turn_id: turn.text for turn in coding_input.turns}
        # planned_actions 表示通话中讨论或拟采取的安排，并不等同于已经落实。
        # 没有完成证据不能据此自动生成材料冲突；真正的双边矛盾由语义编码提出。
        rule_conflicts: list[MaterialConflict] = []
        conflicts = validate_material_conflict_candidates(
            global_output.material_conflict_candidates,
            dialogue_turns=dialogue_turns,
            work_record=coding_input.work_record,
        )
        bottom_line_result = semantic_bottom_line_events(
            global_output.bottom_line_candidates,
            dialogue_turns=dialogue_turns,
            work_record=coding_input.work_record,
            audio_event_ids=set(),
            rule_conflicts=rule_conflicts,
            semantic_conflicts=conflicts,
        )
        bottom_line_events = list(bottom_line_result.events)
        urgent_termination = _known_urgent_termination_event(
            coding_input,
            validated,
            global_output,
        )
        if urgent_termination is not None:
            bottom_line_events.append(urgent_termination)
        conflict_ids = {conflict.id for conflict in conflicts}
        conflicts.extend(
            conflict for conflict in rule_conflicts if conflict.id not in conflict_ids
        )
        summary = build_result_summary(
            results,
            activated_modules=activated_modules,
            inactive_modules=opportunity_result.inactive_modules,
            bottom_line_events=bottom_line_events,
        )
        has_analysis_failures = any(
            result.analysis_outcome is AnalysisOutcome.analysis_failed
            for result in results
        )
        report_is_partial = bool(failed_groups) or has_analysis_failures
        with Session(self._engine) as db:
            job = db.get(ReportJobRecord, job_id)
            if job is None:
                raise LookupError(job_id)
            final_stage = (
                ReportJobStage.partial
                if report_is_partial
                else ReportJobStage.succeeded
            )
            ReportService(db, self._cases).save_report(
                ReportWrite(
                    job_id=job.id,
                    session_id=job.session_id,
                    case_id=coding_input.session.case_id,
                    scene=coding_input.session.scene,
                    media=coding_input.session.media,
                    summary=summary,
                    dimensions=results,
                    bottom_line_events=bottom_line_events,
                    material_conflicts=conflicts,
                    screening_gap=summary.screening_gap,
                    rubric_fingerprint=job.rubric_fingerprint,
                    case_package_fingerprint=job.case_package_fingerprint,
                    model_fingerprint=job.model_fingerprint,
                    prompt_fingerprint=job.prompt_fingerprint,
                    input_fingerprint=job.frozen_input_fingerprint,
                    ai_draft_status=(
                        ReportDraftStatus.partial
                        if report_is_partial
                        else ReportDraftStatus.complete
                    ),
                ),
                final_stage=final_stage,
                last_error=(
                    "部分定级组分析失败，可仅重试失败组。"
                    if failed_groups
                    else (
                        "部分维度的分析链路未完整成功，已保留其他可用结果。"
                        if has_analysis_failures
                        else None
                    )
                ),
            )

    async def _run_map_with_retries(
        self,
        job_id: str,
        shard: CodingShard,
        *,
        session_id: str,
        model_config: ReportModelConfig,
    ) -> LocalCodingOutput:
        last_error: Exception | None = None
        validation_feedback: str | None = None
        for attempt in range(MAX_BATCH_ATTEMPTS):
            self._update(
                job_id,
                ReportJobProgressUpdate(attempt_key=f"map:{shard.shard_id}"),
            )
            try:
                output = await self._gateway.code_shard(
                    shard,
                    session_id=session_id,
                    model_config=model_config,
                    call_kind=(
                        ModelCallKind.initial
                        if attempt == 0
                        else ModelCallKind.repair
                    ),
                    validation_feedback=validation_feedback,
                )
                _validate_local_contract(shard, output)
                completed = _ensure_local_source_coverage(shard, output)
                _validate_local_contract(shard, completed)
                return completed
            except NonRetryableRuntimeModelError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                validation_feedback = str(exc)
        raise LocalBatchError(
            f"{shard.shard_id}: {last_error or '局部编码失败'}"
        )

    async def _run_reduce_with_retries(
        self,
        job_id: str,
        coding_input: CodingInput,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        model_config: ReportModelConfig,
        targets: Sequence[Target],
        active_target_briefs: Sequence[ActiveTargetBrief],
    ) -> GlobalCodingOutput:
        last_error: Exception | None = None
        validation_feedback: str | None = None
        turn_speakers: dict[str, Literal["worker", "client"]] = {
            turn.turn_id: (
                "worker" if turn.speaker is TurnSpeaker.worker else "client"
            )
            for turn in coding_input.turns
        }
        for attempt in range(MAX_REDUCE_ATTEMPTS):
            self._update(
                job_id,
                ReportJobProgressUpdate(attempt_key="reduce"),
            )
            try:
                output = await self._gateway.reduce_coding(
                    local_outputs,
                    session_id=session_id,
                    model_config=model_config,
                    targets=targets,
                    turn_speakers=turn_speakers,
                    scene=coding_input.session.scene,
                    media=coding_input.session.media,
                    active_target_briefs=active_target_briefs,
                    call_kind=(
                        ModelCallKind.initial
                        if attempt == 0
                        else ModelCallKind.repair
                    ),
                    validation_feedback=validation_feedback,
                )
                usable_output = _normalize_reduce_output_sources(
                    coding_input,
                    output,
                ).usable_output
                _validate_global_contract(
                    coding_input,
                    usable_output,
                    targets,
                )
                _validate_reduce_source_closure(local_outputs, usable_output)
                return output
            except NonRetryableRuntimeModelError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                validation_feedback = str(exc)
        raise GlobalBatchError(str(last_error or "聚焦汇总失败"))

    async def _run_group_with_retries(
        self,
        job_id: str,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        model_config: ReportModelConfig,
    ) -> GroupRunResult:
        last_error: Exception | None = None
        validation_feedback: str | None = None
        for attempt in range(MAX_BATCH_ATTEMPTS):
            self._update(
                job_id,
                ReportJobProgressUpdate(attempt_key=f"group:{group.value}"),
            )
            try:
                output = await self._gateway.score_group(
                    group,
                    packets,
                    session_id=session_id,
                    model_config=model_config,
                    call_kind=(
                        ModelCallKind.initial
                        if attempt == 0
                        else ModelCallKind.repair
                    ),
                    validation_feedback=validation_feedback,
                )
                _validate_group_output(output, packets)
                return GroupRunResult(group=group, output=output, error=None)
            except NonRetryableRuntimeModelError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                validation_feedback = str(exc)
        return GroupRunResult(
            group=group,
            output=None,
            error=f"{type(last_error).__name__}: {last_error}",
        )

    @staticmethod
    def _packets_for_group(
        group: ScoringGroup,
        validated: Mapping[Target, ValidatedTarget],
    ) -> list[DimensionPacket]:
        packets: list[DimensionPacket] = []
        for target in GROUP_TARGETS[group]:
            item = validated.get(target)
            if (
                item is None
                or item.analysis_failed
                or not any(
                    outcome.fulfilled for outcome in item.packet.opportunities
                )
            ):
                continue

            primary_unit_ids = [
                routed.evidence.unit_id
                for routed in item.evidence
                if routed.role is EvidenceRole.primary
            ]
            if not primary_unit_ids:
                continue
            gradable_unit_ids = list(dict.fromkeys(primary_unit_ids))
            gradable_unit_ids.extend(
                unit_id
                for unit_id in dict.fromkeys(
                    routed.evidence.unit_id
                    for routed in item.evidence
                    if routed.role is EvidenceRole.supporting
                )
                if unit_id not in gradable_unit_ids
            )
            sufficiency = assess_evidence_sufficiency(
                target=target,
                representative_unit_ids=gradable_unit_ids,
                units=item.packet.units,
                evidence=item.evidence,
                declared_opportunity_count=len(item.packet.opportunities),
            )
            if sufficiency.sufficient:
                packets.append(item.packet)
        return packets

    @staticmethod
    def _assemble_dimensions(
        targets: Sequence[Target],
        validated: Mapping[Target, ValidatedTarget],
        group_outputs: Mapping[ScoringGroup, GroupScoringOutput],
        failed_groups: Set[ScoringGroup],
    ) -> list[DimensionResult]:
        proposals = {
            proposal.target: proposal
            for output in group_outputs.values()
            for proposal in output.proposals
        }
        failed_targets = {
            packet.target
            for group in failed_groups
            for packet in ReportPipeline._packets_for_group(group, validated)
        }
        results: list[DimensionResult] = []
        for target in targets:
            item = validated[target]
            analysis_failed = item.analysis_failed or target in failed_targets
            has_opportunity = any(
                outcome.fulfilled for outcome in item.packet.opportunities
            )
            has_complex = any(
                outcome.fulfilled and outcome.complex_opportunity
                for outcome in item.packet.opportunities
            )
            proposal = proposals.get(target)
            sufficiency = assess_evidence_sufficiency(
                target=target,
                representative_unit_ids=(
                    proposal.representative_units if proposal is not None else []
                ),
                units=item.packet.units,
                evidence=item.evidence,
                declared_opportunity_count=len(item.packet.opportunities),
                unique_due_to_interruption=item.technical_failure,
            )
            disposition = resolve_scoring_disposition(
                analysis_outcome=(
                    AnalysisOutcome.analysis_failed
                    if analysis_failed
                    else AnalysisOutcome.ok
                ),
                technical_failure=item.technical_failure,
                has_opportunity=has_opportunity,
                evidence_sufficient=sufficiency.sufficient,
            )
            effective_proposal = (
                proposal
                if disposition.analysis_outcome is AnalysisOutcome.ok
                and disposition.unscored_reason is None
                else None
            )
            results.append(
                assemble_dimension_result(
                    item.packet,
                    effective_proposal,
                    evidence=item.evidence,
                    counter_evidence=item.counter_evidence,
                    indicator_states=_indicator_states(item),
                    disposition=disposition,
                    sufficiency=sufficiency,
                    has_complex_opportunity=has_complex,
                )
            )
        return results

    def _update(self, job_id: str, update: ReportJobProgressUpdate) -> None:
        with Session(self._engine) as db:
            ReportJobService(db, self._cases).update_progress(job_id, update)


class ReportProcessor:
    """FastAPI BackgroundTasks 使用的同步入口；每次任务创建独立 DB Session。"""

    def __init__(
        self,
        engine_provider: Callable[[], Engine],
        pipeline_factory: Callable[[Engine], ReportPipeline],
    ) -> None:
        self._engine_provider = engine_provider
        self._pipeline_factory = pipeline_factory

    def process(self, job_id: str) -> None:
        engine = self._engine_provider()
        asyncio.run(self._pipeline_factory(engine).run(job_id))

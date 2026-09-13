from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from app.reports.job_inputs import (
    CodingInput,
    CodingSessionInput,
    CodingShard,
    CodingTurnInput,
    OpportunityCheckInput,
    SessionTerminationInput,
    WorkRecordSnapshotInput,
)
from app.reports.jobs import canonical_fingerprint
from app.reports.models import (
    PlannedAction,
    ReferralDecision,
    ReportJobRecord,
    ReportJobStage,
    ReportRecord,
    RiskLevel,
)
from app.reports.report_provider import (
    ConditionalOpportunityBrief,
    GlobalCodingOutput,
    GroupScoringOutput,
    LocalCodingOutput,
    OpportunityObservation,
    PublicActionObservation,
    ReportModelConfig,
    ScoringGroup,
)
from app.reports.scoring_domain import (
    BottomLineCategory,
    CoreDimension,
    DialogueRef,
    DimensionPacket,
    EvidenceDirection,
    SpecialModule,
    UnscoredReason,
)
from app.reports.stability import (
    RunObservation,
    StabilityMaterial,
    StabilityRunner,
    TargetRunObservation,
    summarize_stability,
)
from app.runtime.models import ModelCallKind
from app.sessions.models import (
    CaseType,
    Media,
    ModelMode,
    Scene,
    SessionMode,
    SessionStatus,
    TurnSpeaker,
)
from tests.reports.test_report_pipeline import (
    TARGETS,
    FakeGateway,
    NonRetryableGateway,
    _global_output,
    _proposal,
)


class OmittingClientMapGateway(FakeGateway):
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
        client_turn_ids = {
            turn.turn_id for turn in shard.turns if turn.speaker is TurnSpeaker.client
        }
        return output.model_copy(
            update={
                "units": [
                    unit
                    for unit in output.units
                    if not any(
                        isinstance(ref, DialogueRef) and ref.turn_id in client_turn_ids
                        for ref in unit.refs
                    )
                ]
            }
        )


class ConditionalStabilityGateway(OmittingClientMapGateway):
    def __init__(
        self,
        *,
        risk_status: Literal["present", "absent", "uncertain"] = "present",
        risk_statuses: Sequence[Literal["present", "absent", "uncertain"]] = (),
        invalid_kind: Literal["missing", "worker_ref"] | None = None,
        repair_second_attempt: bool = False,
    ) -> None:
        super().__init__()
        self.risk_status = risk_status
        self.risk_statuses = list(risk_statuses)
        self.invalid_kind = invalid_kind
        self.repair_second_attempt = repair_second_attempt
        self.received_conditions: list[list[ConditionalOpportunityBrief]] = []
        self.received_actions: list[list[PublicActionObservation]] = []

    async def reduce_coding(
        self,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        scene: Scene,
        media: Media,
        targets: Sequence[CoreDimension | SpecialModule],
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        turn_speakers: dict[str, str] | None = None,
        active_target_briefs: Sequence[object] = (),
        conditional_opportunities: Sequence[ConditionalOpportunityBrief] = (),
        action_observations: Sequence[PublicActionObservation] = (),
        validation_feedback: str | None = None,
    ) -> GlobalCodingOutput:
        del (
            session_id,
            scene,
            media,
            local_outputs,
            call_kind,
            model_config,
            turn_speakers,
            active_target_briefs,
        )
        self.calls.append("reduce")
        self.validation_feedbacks.append(("reduce", validation_feedback))
        self.received_global_targets.append(list(targets))
        self.received_conditions.append(list(conditional_opportunities))
        self.received_actions.append(list(action_observations))
        risk_status = (
            self.risk_statuses[self.calls.count("reduce") - 1]
            if self.risk_statuses
            else self.risk_status
        )
        invalid_kind = (
            None
            if self.repair_second_attempt and self.calls.count("reduce") > 1
            else self.invalid_kind
        )
        output = _global_output(targets)
        observations = [
            OpportunityObservation.model_validate(
                {
                    "opportunity_id": opportunity_id,
                    "status": risk_status if index == 0 else "absent",
                    "reason": (
                        "来电者提供了含蓄风险线索。"
                        if index == 0
                        else "未同时确认即时风险条件。"
                    ),
                    "refs": (
                        [
                            {
                                "kind": "dialogue",
                                "turn_id": (
                                    "turn-one"
                                    if invalid_kind == "worker_ref"
                                    else "client-risk-cue"
                                ),
                                "quote": (
                                    "先听你最难受的部分"
                                    if invalid_kind == "worker_ref"
                                    else "我不太想等到天亮。"
                                ),
                            }
                        ]
                        if index == 0
                        else []
                    ),
                    "action_turn_ids": [],
                }
            )
            for index, opportunity_id in enumerate(
                (
                    "full_risk_appraisal_after_cue",
                    "immediate_safety_response",
                    "safety_plan_after_support_setback",
                )
            )
        ]
        return output.model_copy(
            update={
                "opportunity_observations": (
                    [] if invalid_kind == "missing" else observations
                )
            }
        )


def test_stability_summary_calculates_all_required_metrics() -> None:
    target = CoreDimension.respectful_communication
    unscored_target = SpecialModule.basic_risk_screening
    observations = [
        RunObservation(
            targets={
                target: TargetRunObservation(
                    level=2,
                    representative_evidence_fingerprints=["evidence-a", "evidence-b"],
                    evidence_directions={
                        "evidence-a": EvidenceDirection.support,
                        "evidence-b": EvidenceDirection.limit,
                    },
                ),
                unscored_target: TargetRunObservation(
                    level=None,
                    unscored_reason=UnscoredReason.no_opportunity,
                ),
            },
            bottom_line_categories={BottomLineCategory.humiliation_or_coercion},
        ),
        RunObservation(
            targets={
                target: TargetRunObservation(
                    level=3,
                    representative_evidence_fingerprints=["evidence-a", "evidence-b"],
                    evidence_directions={
                        "evidence-a": EvidenceDirection.support,
                        "evidence-b": EvidenceDirection.limit,
                    },
                ),
                unscored_target: TargetRunObservation(
                    level=None,
                    unscored_reason=UnscoredReason.no_opportunity,
                ),
            },
            bottom_line_categories=set(),
        ),
        RunObservation(
            targets={
                target: TargetRunObservation(
                    level=3,
                    representative_evidence_fingerprints=["evidence-a", "evidence-c"],
                    evidence_directions={
                        "evidence-a": EvidenceDirection.support,
                        "evidence-b": EvidenceDirection.adverse,
                    },
                ),
                unscored_target: TargetRunObservation(
                    level=None,
                    unscored_reason=UnscoredReason.insufficient_evidence,
                ),
            },
            bottom_line_categories={BottomLineCategory.humiliation_or_coercion},
        ),
    ]

    summary = summarize_stability(observations)

    target_summary = summary.per_target[target.value]
    assert target_summary.included_in_runs == [True, True, True]
    assert target_summary.inclusion_agreement == 1.0
    assert target_summary.levels == [2, 3, 3]
    assert target_summary.modal_level == 3
    assert target_summary.exact_agreement == pytest.approx(2 / 3)
    assert target_summary.within_one_level == 1.0
    assert target_summary.evidence_jaccard == pytest.approx(5 / 9)
    assert target_summary.direction_consistency == pytest.approx(2 / 3)
    assert summary.bottom_line_occurrence == {
        category.value: (
            2 if category is BottomLineCategory.humiliation_or_coercion else 0
        )
        for category in BottomLineCategory
    }
    assert summary.unscored_reason_consistency == pytest.approx(2 / 3)


def test_stability_summary_distinguishes_inactive_and_unscored_special_module() -> None:
    core_target = CoreDimension.respectful_communication
    module = SpecialModule.full_risk_appraisal
    summary = summarize_stability(
        [
            RunObservation(
                targets={
                    core_target: TargetRunObservation(level=2),
                    module: TargetRunObservation(
                        unscored_reason=UnscoredReason.insufficient_evidence,
                    ),
                }
            ),
            RunObservation(targets={core_target: TargetRunObservation(level=2)}),
        ]
    )

    module_summary = summary.per_target[module.value]
    assert module_summary.included_in_runs == [True, False]
    assert module_summary.inclusion_agreement == 0.5
    assert module_summary.levels == [None, None]
    assert module_summary.unscored_reasons == [
        UnscoredReason.insufficient_evidence,
        None,
    ]
    assert summary.per_target[core_target.value].included_in_runs == [True, True]


def test_stability_summary_still_rejects_inconsistent_core_targets() -> None:
    with pytest.raises(ValueError, match="评分目标"):
        summarize_stability(
            [
                RunObservation(
                    targets={
                        CoreDimension.respectful_communication: TargetRunObservation(
                            level=2
                        )
                    }
                ),
                RunObservation(targets={}),
            ]
        )


class RecordingStableGateway(FakeGateway):
    def __init__(self, *, global_output: GlobalCodingOutput, proposed_level: int) -> None:
        super().__init__(global_output=global_output)
        self.proposed_level = proposed_level

    async def reduce_coding(
        self,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        model_config: ReportModelConfig,
        targets: Sequence[CoreDimension | SpecialModule],
        scene: Scene,
        media: Media,
        call_kind: ModelCallKind = ModelCallKind.initial,
        turn_speakers: dict[str, str] | None = None,
        active_target_briefs: Sequence[object] = (),
        conditional_opportunities: Sequence[ConditionalOpportunityBrief] = (),
        action_observations: Sequence[PublicActionObservation] = (),
        validation_feedback: str | None = None,
    ) -> GlobalCodingOutput:
        del (
            session_id,
            call_kind,
            local_outputs,
            turn_speakers,
            scene,
            media,
            active_target_briefs,
            conditional_opportunities,
            action_observations,
            validation_feedback,
        )
        self.calls.append("reduce")
        self.received_model_configs.append(model_config)
        self.received_global_targets.append(list(targets))
        return deepcopy(self.global_output)

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        model_config: ReportModelConfig,
        call_kind: ModelCallKind = ModelCallKind.initial,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput:
        del session_id, call_kind, validation_feedback
        self.calls.append(group.value)
        self.received_model_configs.append(model_config)
        proposals = [_proposal(packet) for packet in packets]
        for proposal in proposals:
            if proposal.proposed_level is not None:
                proposal.proposed_level = min(self.proposed_level, 3)
        return GroupScoringOutput(proposals=proposals)


class RenamingUnitGateway(RecordingStableGateway):
    def __init__(self, *, global_output: GlobalCodingOutput) -> None:
        super().__init__(global_output=global_output, proposed_level=3)
        self.run_index = 0

    async def reduce_coding(
        self,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        model_config: ReportModelConfig,
        targets: Sequence[CoreDimension | SpecialModule],
        scene: Scene,
        media: Media,
        call_kind: ModelCallKind = ModelCallKind.initial,
        turn_speakers: dict[str, str] | None = None,
        active_target_briefs: Sequence[object] = (),
        conditional_opportunities: Sequence[ConditionalOpportunityBrief] = (),
        action_observations: Sequence[PublicActionObservation] = (),
        validation_feedback: str | None = None,
    ) -> GlobalCodingOutput:
        output = await super().reduce_coding(
            local_outputs,
            session_id=session_id,
            model_config=model_config,
            targets=targets,
            call_kind=call_kind,
            turn_speakers=turn_speakers,
            scene=scene,
            media=media,
            active_target_briefs=active_target_briefs,
            conditional_opportunities=conditional_opportunities,
            action_observations=action_observations,
            validation_feedback=validation_feedback,
        )
        self.run_index += 1
        renamed = {
            unit.id: f"run-{self.run_index}:{unit.id}" for unit in output.units
        }
        for unit in output.units:
            unit.id = renamed[unit.id]
        for evidence in output.coded_evidence:
            evidence.unit_id = renamed[evidence.unit_id]
        for check in output.counter_checks:
            check.searched_unit_ids = [renamed[item] for item in check.searched_unit_ids]
            for evidence in check.found:
                evidence.unit_id = renamed[evidence.unit_id]
        return output


def _create_frozen_job(
    engine: Engine,
    *,
    material_id: str,
    has_worker_turn: bool,
) -> ReportJobRecord:
    SQLModel.metadata.create_all(engine)
    now = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
    session_id = f"session-{material_id}"
    speaker = TurnSpeaker.worker if has_worker_turn else TurnSpeaker.client
    coding_input = CodingInput(
        session=CodingSessionInput(
            session_id=session_id,
            mode=SessionMode.assessment,
            scene=Scene.hotline,
            case_type=CaseType.main,
            case_id="crisis_student_main",
            media=Media.voice,
            status=SessionStatus.ended,
            model_mode=ModelMode.live,
            soft_duration_minutes=None,
            created_at=now,
            ended_at=now,
            end_reason=None,
        ),
        turns=[
            CodingTurnInput(
                turn_id="turn-one",
                sequence=1,
                speaker=speaker,
                text=(
                    "我会先听你最难受的部分，也会直接确认现在是否有自伤想法。"
                    f"材料标记：{material_id}。"
                ),
                created_at=now,
            ),
            CodingTurnInput(
                turn_id="turn-two",
                sequence=2,
                speaker=speaker,
                text="我们一起核对下一步，并说明仍需了解的信息。",
                created_at=now,
            ),
        ],
        work_record=WorkRecordSnapshotInput(
            id=f"work-record-{material_id}",
            session_id=session_id,
            problem_understanding="当前压力、失眠和功能下降相互影响。",
            risk_level=RiskLevel.uncertain,
            risk_reasoning="已完成基础询问，紧迫性信息仍需继续核对。",
            risk_evidence_turn_ids=["turn-one"],
            missing_information=["手段可及性"],
            planned_actions=[PlannedAction.continue_assessment, PlannedAction.follow_up],
            referral_decision=ReferralDecision.consider,
            supervision_decision=True,
            follow_up="继续核对并根据结果安排后续支持。",
            limitations="仅依据本次通话。",
            created_at=now,
            updated_at=now,
        ),
        technical_interruptions=[],
        termination=SessionTerminationInput(
            status=SessionStatus.ended,
            ended_at=now,
            end_reason=None,
        ),
    )
    opportunity_input = OpportunityCheckInput(
        session_id=session_id,
        session_state={},
        turn_states=[],
        case_package={
            "measurement": {
                "case_id": "crisis_student_main",
                "scoring_opportunities": [],
            }
        },
    )
    coding_json = coding_input.model_dump(mode="json")
    opportunity_json = opportunity_input.model_dump(mode="json")
    input_fingerprint = canonical_fingerprint(
        {
            "coding_input": coding_json,
            "opportunity_check_input": opportunity_json,
        }
    )
    model_snapshot = {
        "report_model": "fake-report-model",
        "sampling_parameters": {"temperature": 0.1},
    }
    job = ReportJobRecord(
        id=f"job-{material_id}",
        session_id=session_id,
        frozen_input_json=coding_json,
        opportunity_check_json=opportunity_json,
        frozen_input_fingerprint=input_fingerprint,
        rubric_fingerprint="rubric-fingerprint",
        case_package_fingerprint="case-package-fingerprint",
        model_snapshot=model_snapshot,
        model_fingerprint=canonical_fingerprint(model_snapshot),
        prompt_fingerprint="prompt-fingerprint",
    )
    with Session(engine) as db:
        db.add(job)
        db.commit()
        db.refresh(job)
        db.expunge(job)
    return job


def _conditional_stability_material(engine: Engine) -> StabilityMaterial:
    job = _create_frozen_job(
        engine,
        material_id="conditional-opportunities",
        has_worker_turn=True,
    )
    coding_input = deepcopy(job.frozen_input_json)
    first_worker, second_worker = coding_input["turns"]
    second_worker["sequence"] = 3
    coding_input["turns"] = [
        first_worker,
        {
            **first_worker,
            "turn_id": "client-risk-cue",
            "sequence": 2,
            "speaker": "client",
            "text": "我不太想等到天亮。",
        },
        second_worker,
        {
            **first_worker,
            "turn_id": "client-support-setback",
            "sequence": 4,
            "speaker": "client",
            "text": "我发过求助消息，一直没回。",
        },
    ]
    opportunity_input = deepcopy(job.opportunity_check_json)
    opportunity_input["session_state"] = {
        "runtime": {"engine": "character_prompt"}
    }
    opportunity_input["turn_states"] = [
        {
            "turn_id": "client-support-setback",
            "state_before_json": {},
            "state_after_json": {},
            "signals_json": {
                "runtime_engine": "character_prompt",
                "action_request": "send_first_support_message",
                "world_stage_before": "not_contacted",
                "world_stage_after": "first_unanswered",
            },
            "used_fact_ids": [],
        }
    ]
    opportunity_input["case_package"]["measurement"]["scoring_opportunities"] = [
        {
            "id": opportunity_id,
            "target": target,
            "kind": "conditional",
            "source": "transcript",
            "description": description,
            "evidence_targets": ["根据当前情境回应"],
            "indicator_ids": [indicator],
            "complex_opportunity": complex_opportunity,
            "scenes": ["hotline"],
        }
        for opportunity_id, target, description, indicator, complex_opportunity in (
            (
                "full_risk_appraisal_after_cue",
                "S1b",
                "来电者已提供含蓄风险线索。",
                "S1b.cue_recognition",
                True,
            ),
            (
                "immediate_safety_response",
                "S2",
                "来电者已披露当前风险和紧迫的时间界线。",
                "S2.connection",
                False,
            ),
            (
                "safety_plan_after_support_setback",
                "S2",
                "当前风险情境下，实际联系现实支持未得到回应。",
                "S2.verification",
                True,
            ),
        )
    ]
    return StabilityMaterial(
        material_id="conditional-opportunities",
        coding_input=CodingInput.model_validate(coding_input),
        opportunity_check_input=OpportunityCheckInput.model_validate(opportunity_input),
        model_config=ReportModelConfig.model_validate(job.model_snapshot),
        model_version="fake-model-release",
        prompt_fingerprint=job.prompt_fingerprint,
        rubric_fingerprint=job.rubric_fingerprint,
        case_package_fingerprint=job.case_package_fingerprint,
        input_fingerprint=canonical_fingerprint(
            {
                "coding_input": coding_input,
                "opportunity_check_input": opportunity_input,
            }
        ),
    )


@pytest.mark.parametrize("risk_status", ["present", "absent", "uncertain"])
async def test_stability_only_scores_confirmed_conditional_modules(
    test_engine: Engine,
    risk_status: Literal["present", "absent", "uncertain"],
) -> None:
    material = _conditional_stability_material(test_engine)
    gateway = ConditionalStabilityGateway(risk_status=risk_status)

    result = await StabilityRunner(gateway).run(material, runs=2)

    expected_targets = {target.value for target in TARGETS}
    if risk_status == "present":
        expected_targets.add(SpecialModule.full_risk_appraisal.value)
    assert set(result.per_target) == expected_targets
    assert gateway.received_global_targets == [
        [*TARGETS, SpecialModule.full_risk_appraisal, SpecialModule.safety_response]
    ] * 2
    assert all(len(conditions) == 3 for conditions in gateway.received_conditions)
    assert all(
        len(actions) == 1
        and actions[0].turn_id == "client-support-setback"
        and actions[0].action_request == "send_first_support_message"
        for actions in gateway.received_actions
    )
    assert all(
        packet.target is not SpecialModule.safety_response
        for packet in gateway.received_packets
    )
    assert gateway.calls.count("map:shard-1") == 2
    assert gateway.calls.count("map:shard-2") == 2
    assert gateway.calls.count("reduce") == 2


@pytest.mark.parametrize("second_status", ["absent", "uncertain"])
async def test_stability_retains_conditional_activation_changes_across_runs(
    test_engine: Engine,
    tmp_path: Path,
    second_status: Literal["absent", "uncertain"],
) -> None:
    material = _conditional_stability_material(test_engine)
    gateway = ConditionalStabilityGateway(risk_statuses=["present", second_status])
    output_path = tmp_path / "varying-activation.json"

    result = await StabilityRunner(gateway).run(
        material,
        runs=2,
        output_path=output_path,
    )

    module = result.per_target[SpecialModule.full_risk_appraisal.value]
    assert module.included_in_runs == [True, False]
    assert module.inclusion_agreement == 0.5
    assert module.levels[0] is not None
    assert module.levels[1] is None
    assert module.unscored_reasons == [None, None]
    assert gateway.calls.count("map:shard-1") == 2
    assert gateway.calls.count("map:shard-2") == 2
    assert gateway.calls.count("reduce") == 2
    assert sum(
        packet.target is SpecialModule.full_risk_appraisal
        for packet in gateway.received_packets
    ) == 1
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["per_target"][SpecialModule.full_risk_appraisal.value][
        "included_in_runs"
    ] == [True, False]


@pytest.mark.parametrize("invalid_kind", ["missing", "worker_ref"])
async def test_stability_invalid_condition_observations_fail_before_scoring(
    test_engine: Engine,
    invalid_kind: Literal["missing", "worker_ref"],
) -> None:
    material = _conditional_stability_material(test_engine)
    gateway = ConditionalStabilityGateway(invalid_kind=invalid_kind)

    with pytest.raises(RuntimeError, match="聚焦汇总失败"):
        await StabilityRunner(gateway).run(material, runs=2)

    assert gateway.calls.count("reduce") == 2
    assert gateway.validation_feedbacks[-1][1]
    assert all(group.value not in gateway.calls for group in ScoringGroup)


async def test_stability_repairs_condition_observations_in_existing_reduce_retry(
    test_engine: Engine,
) -> None:
    material = _conditional_stability_material(test_engine)
    gateway = ConditionalStabilityGateway(
        invalid_kind="missing",
        repair_second_attempt=True,
    )

    result = await StabilityRunner(gateway).run(material, runs=2)

    assert SpecialModule.full_risk_appraisal.value in result.per_target
    assert SpecialModule.safety_response.value not in result.per_target
    assert gateway.calls.count("reduce") == 3
    assert gateway.calls.count("map:shard-1") == 2
    assert gateway.calls.count("map:shard-2") == 2
    feedback = [value for batch, value in gateway.validation_feedbacks if batch == "reduce"]
    assert feedback[0] is None
    assert feedback[1]
    assert feedback[2] is None


async def test_three_fixed_materials_run_three_full_passes_and_never_write_reports(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    profiles = [
        ("clear-high-anchor", True, 3, EvidenceDirection.support),
        ("boundary-case", True, 2, EvidenceDirection.limit),
        ("missing-opportunity", False, 3, EvidenceDirection.support),
    ]
    jobs: list[ReportJobRecord] = []
    input_fingerprints: set[str] = set()
    for material_id, has_worker_turn, proposed_level, first_direction in profiles:
        job = _create_frozen_job(
            test_engine,
            material_id=material_id,
            has_worker_turn=has_worker_turn,
        )
        jobs.append(job)
        input_fingerprints.add(job.frozen_input_fingerprint)
        global_output = _global_output()
        if not has_worker_turn:
            global_output.coded_evidence = []
        else:
            global_output.coded_evidence[0].direction = first_direction
        gateway = RecordingStableGateway(
            global_output=global_output,
            proposed_level=proposed_level,
        )
        material = StabilityMaterial.from_report_job(
            job,
            material_id=material_id,
            model_version="fake-model-release",
        )
        output_path = tmp_path / material_id / "result.json"

        result = await StabilityRunner(gateway).run(material, output_path=output_path)

        assert result.material_id == material_id
        assert result.runs == 3
        assert result.mode == "full"
        assert result.model_id == job.model_snapshot["report_model"]
        assert result.model_version == "fake-model-release"
        assert result.sampling_params == {"temperature": 0.1}
        assert result.prompt_fingerprint == job.prompt_fingerprint
        assert result.rubric_fingerprint == job.rubric_fingerprint
        assert result.case_package_fingerprint == job.case_package_fingerprint
        assert result.input_fingerprint == job.frozen_input_fingerprint
        assert gateway.calls.count("map:shard-1") == 3
        assert gateway.calls.count("map:shard-2") == 3
        assert gateway.calls.count("reduce") == 3
        assert all(gateway.calls.count(group.value) == 3 for group in ScoringGroup)
        assert gateway.received_global_targets == [list(TARGETS)] * 3
        assert all(item.exact_agreement == 1.0 for item in result.per_target.values())
        assert all(item.within_one_level == 1.0 for item in result.per_target.values())
        assert output_path.exists()
        saved = json.loads(output_path.read_text(encoding="utf-8"))
        assert saved == result.model_dump(mode="json")

    assert len(input_fingerprints) == 3

    with Session(test_engine) as db:
        assert list(db.exec(select(ReportRecord)).all()) == []
        for job in jobs:
            stored_job = db.get(ReportJobRecord, job.id)
            assert stored_job is not None
            assert stored_job.stage is ReportJobStage.queued
            assert stored_job.report_id is None


async def test_stability_map_preserves_omitted_client_context(
    test_engine: Engine,
) -> None:
    from app.reports.report_pipeline import split_coding_input

    job = _create_frozen_job(
        test_engine,
        material_id="omitted-client-context",
        has_worker_turn=True,
    )
    material = StabilityMaterial.from_report_job(
        job,
        material_id="omitted-client-context",
        model_version="fake-model-release",
    )
    client_turn = material.coding_input.turns[-1].model_copy(
        update={
            "turn_id": "client-risk-cue",
            "sequence": 3,
            "speaker": TurnSpeaker.client,
            "text": "我不太想等到天亮。",
        }
    )
    coding_input = material.coding_input.model_copy(
        update={"turns": [*material.coding_input.turns, client_turn]}
    )
    shard = next(
        shard
        for shard in split_coding_input(coding_input)
        if any(turn.turn_id == client_turn.turn_id for turn in shard.turns)
    )
    gateway = OmittingClientMapGateway()

    output = await StabilityRunner(gateway)._code_shard(
        shard,
        session_id="stability:omitted-client-context:1",
        model_config=material.model_configuration,
    )

    retained = [
        unit
        for unit in output.units
        if any(
            isinstance(ref, DialogueRef)
            and ref.turn_id == client_turn.turn_id
            and ref.quote == client_turn.text
            for ref in unit.refs
        )
    ]
    assert len(retained) == 1
    assert retained[0].source_role == "client"
    assert gateway.calls == [f"map:{shard.shard_id}"]


async def test_representative_evidence_jaccard_ignores_model_unit_names(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    job = _create_frozen_job(
        test_engine,
        material_id="renamed-units",
        has_worker_turn=True,
    )
    material = StabilityMaterial.from_report_job(
        job,
        material_id="renamed-units",
        model_version="fake-model-release",
    )
    gateway = RenamingUnitGateway(global_output=_global_output())

    result = await StabilityRunner(gateway).run(
        material,
        runs=2,
        output_path=tmp_path / "result.json",
    )

    assert all(
        item.evidence_jaccard == 1.0 for item in result.per_target.values()
    )


def test_stability_material_rejects_changed_frozen_input_fingerprint(
    test_engine: Engine,
) -> None:
    job = _create_frozen_job(
        test_engine,
        material_id="changed-input",
        has_worker_turn=True,
    )
    changed_input: Mapping[str, object] = {
        **job.frozen_input_json,
        "turns": [],
    }

    with pytest.raises(ValueError, match="input fingerprint"):
        StabilityMaterial(
            material_id="changed-input",
            coding_input=changed_input,
            opportunity_check_input=job.opportunity_check_json,
            model_config=job.model_snapshot,
            model_version="fake-model-release",
            prompt_fingerprint=job.prompt_fingerprint,
            rubric_fingerprint=job.rubric_fingerprint,
            case_package_fingerprint=job.case_package_fingerprint,
            input_fingerprint=job.frozen_input_fingerprint,
        )


async def test_stability_runner_stops_non_retryable_batch_after_first_attempt(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    job = _create_frozen_job(
        test_engine,
        material_id="non-retryable",
        has_worker_turn=True,
    )
    material = StabilityMaterial.from_report_job(
        job,
        material_id="non-retryable",
        model_version="fake-model-release",
    )
    gateway = NonRetryableGateway(failing_batch="global")

    with pytest.raises(RuntimeError, match="聚焦汇总失败"):
        await StabilityRunner(gateway).run(
            material,
            runs=2,
            output_path=tmp_path / "result.json",
        )

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 1
    assert all(group.value not in gateway.calls for group in ScoringGroup)


async def test_stability_runner_map_failure_blocks_reduce_and_groups(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    job = _create_frozen_job(
        test_engine,
        material_id="non-retryable-map",
        has_worker_turn=True,
    )
    material = StabilityMaterial.from_report_job(
        job,
        material_id="non-retryable-map",
        model_version="fake-model-release",
    )
    gateway = NonRetryableGateway(failing_batch="map:shard-1")

    with pytest.raises(RuntimeError, match="局部编码失败"):
        await StabilityRunner(gateway).run(
            material,
            runs=2,
            output_path=tmp_path / "result.json",
        )

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert "reduce" not in gateway.calls
    assert all(group.value not in gateway.calls for group in ScoringGroup)


async def test_stability_runner_does_not_retry_non_retryable_scoring_group(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    job = _create_frozen_job(
        test_engine,
        material_id="non-retryable-group",
        has_worker_turn=True,
    )
    material = StabilityMaterial.from_report_job(
        job,
        material_id="non-retryable-group",
        model_version="fake-model-release",
    )
    gateway = NonRetryableGateway(failing_batch=ScoringGroup.interaction.value)

    with pytest.raises(RuntimeError, match="interaction 定级失败"):
        await StabilityRunner(gateway).run(
            material,
            runs=2,
            output_path=tmp_path / "result.json",
        )

    assert gateway.calls.count("map:shard-1") == 1
    assert gateway.calls.count("map:shard-2") == 1
    assert gateway.calls.count("reduce") == 1
    assert gateway.calls.count("interaction") == 1

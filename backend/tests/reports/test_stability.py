from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from app.reports.job_inputs import (
    CodingInput,
    CodingSessionInput,
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
    GlobalCodingOutput,
    GroupScoringOutput,
    LocalCodingOutput,
    ReportModelConfig,
    ScoringGroup,
)
from app.reports.scoring_domain import (
    BottomLineCategory,
    CoreDimension,
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

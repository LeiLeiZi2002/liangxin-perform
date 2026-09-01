from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.runtime.models import RuntimeFailureRecord
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


def _ended_session(
    engine: Engine,
    *,
    mode: SessionMode = SessionMode.experience,
) -> SessionRecord:
    ended_at = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
    record = SessionRecord(
        mode=mode,
        scene=Scene.hotline,
        case_type=CaseType.main,
        case_id="crisis_student_main",
        media=Media.voice,
        status=SessionStatus.ended,
        model_mode=ModelMode.fallback,
        state_json={"runtime": {"phase": "closing"}, "trust": 3},
        ended_at=ended_at,
        end_reason=EndReason.technical_interruption,
    )
    with Session(engine) as db:
        db.add(record)
        db.commit()
        db.refresh(record)
    return record


def _add_turns_and_failure(engine: Engine, session_id: str) -> list[TurnRecord]:
    turns = [
        TurnRecord(
            session_id=session_id,
            client_turn_id="pair-2",
            sequence=2,
            speaker=TurnSpeaker.client,
            text="我愿意继续说。",
            audio_path="audio/client.wav",
            provider="fallback",
            degraded=True,
            signals_json={"emotional_state": True},
            state_before_json={"stage": "opening"},
            state_after_json={"stage": "exploration"},
            used_fact_ids=["presenting_concern"],
        ),
        TurnRecord(
            session_id=session_id,
            client_turn_id="pair-1",
            sequence=1,
            speaker=TurnSpeaker.worker,
            text="愿意说说最近最难受的部分吗？",
            provider="live",
            signals_json={"open_exploration": True},
            state_before_json={},
            state_after_json={"stage": "opening"},
        ),
    ]
    failure = RuntimeFailureRecord(
        session_id=session_id,
        client_turn_id="pair-2",
        component="asr",
        phase="streaming",
        operation="receive",
        failure_code="asr.receive_audio",
        error_class="RuntimeError",
        attempt_count=2,
        retryable=False,
        disposition="session_end",
        attempts_json=[{"index": 1}, {"index": 2}],
        details_json={"summary": "音频链路中断"},
    )
    with Session(engine) as db:
        db.add_all([*turns, failure])
        db.commit()
        for turn in turns:
            db.refresh(turn)
            db.expunge(turn)
    return sorted(turns, key=lambda item: item.sequence)


def _work_record_payload(evidence_turn_id: str) -> dict[str, Any]:
    return {
        "problem_understanding": "来访者近期压力增加，当前仍愿意求助。",
        "risk_level": "uncertain",
        "risk_reasoning": "目前信息不足，需要继续核对风险与支持资源。",
        "risk_evidence_turn_ids": [evidence_turn_id],
        "missing_information": ["风险意念、计划和手段仍需核对"],
        "planned_actions": ["continue_assessment", "follow_up"],
        "referral_decision": "consider",
        "supervision_decision": True,
        "follow_up": "继续评估并在必要时联系督导。",
        "limitations": "仅基于本次会话中的有限材料。",
    }


def _create_job(
    client: TestClient,
    engine: Engine,
    *,
    mode: SessionMode = SessionMode.experience,
    use_default_processor: bool = False,
) -> tuple[SessionRecord, dict[str, Any]]:
    session_record = _ended_session(engine, mode=mode)
    if use_default_processor:
        response = client.post(f"/api/sessions/{session_record.id}/reports")
    else:
        import app.api.routes.reports as report_routes
        from app.main import app

        class PassiveProcessor:
            def process(self, job_id: str) -> None:
                del job_id

        override_key = report_routes.get_report_job_processor
        owns_override = override_key not in app.dependency_overrides
        if owns_override:
            app.dependency_overrides[override_key] = PassiveProcessor
        try:
            response = client.post(f"/api/sessions/{session_record.id}/reports")
        finally:
            if owns_override:
                app.dependency_overrides.pop(override_key, None)
    assert response.status_code == 202, response.text
    return session_record, response.json()


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for child in value.values()
            for key in _recursive_keys(child)
        }
    if isinstance(value, list):
        return {key for child in value for key in _recursive_keys(child)}
    return set()


def test_create_report_job_is_idempotent_and_freezes_complete_ordered_input(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session_record = _ended_session(test_engine, mode=SessionMode.assessment)
    turns = _add_turns_and_failure(test_engine, session_record.id)
    work_record = client.put(
        f"/api/sessions/{session_record.id}/work-record",
        json=_work_record_payload(turns[0].id),
    )
    assert work_record.status_code == 200, work_record.text

    import app.api.routes.reports as report_routes
    from app.main import app

    class PassiveProcessor:
        def process(self, job_id: str) -> None:
            del job_id

    app.dependency_overrides[report_routes.get_report_job_processor] = PassiveProcessor
    try:
        first = client.post(f"/api/sessions/{session_record.id}/reports")
        second = client.post(f"/api/sessions/{session_record.id}/reports")
    finally:
        app.dependency_overrides.pop(report_routes.get_report_job_processor, None)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert second.json() == first.json()
    assert first.json()["stage"] == "queued"
    assert first.json()["retryable"] is False
    assert "last_error" not in first.json()
    assert "frozen_input_json" not in first.json()
    assert "opportunity_check_json" not in first.json()

    import app.reports.jobs as report_jobs
    import app.reports.models as report_models

    assert hasattr(report_models, "ReportJobRecord")
    assert hasattr(report_jobs, "canonical_fingerprint")
    with Session(test_engine) as db:
        jobs = list(db.exec(select(report_models.ReportJobRecord)).all())
    assert len(jobs) == 1
    stored = jobs[0]
    coding_input = stored.frozen_input_json
    assert hasattr(stored, "opportunity_check_json")
    opportunity_input = stored.opportunity_check_json
    assert "coding_input" not in coding_input
    assert "opportunity_check_input" not in coding_input
    assert [item["sequence"] for item in coding_input["turns"]] == [1, 2]
    assert coding_input["turns"] == [
        {
            "turn_id": turn.id,
            "sequence": turn.sequence,
            "speaker": turn.speaker.value,
            "text": turn.text,
            "created_at": turn.created_at.isoformat(),
        }
        for turn in turns
    ]
    assert coding_input["work_record"] == work_record.json()
    assert coding_input["session"]["session_id"] == session_record.id
    assert coding_input["termination"] == {
        "status": "ended",
        "ended_at": session_record.ended_at.isoformat(),
        "end_reason": "technical_interruption",
    }
    assert coding_input["technical_interruptions"][0]["failure_code"] == (
        "asr.receive_audio"
    )
    hidden_fields = {
        "state_json",
        "state_before_json",
        "state_after_json",
        "used_fact_ids",
        "signals_json",
        "case_package",
        "attempts_json",
        "details_json",
        "error_class",
        "provider_request_id",
    }
    assert _recursive_keys(coding_input).isdisjoint(hidden_fields)

    from app.cases.loader import CaseRepository

    assert opportunity_input["session_state"] == session_record.state_json
    assert opportunity_input["turn_states"] == [
        {
            "turn_id": turn.id,
            "state_before_json": turn.state_before_json,
            "state_after_json": turn.state_after_json,
            "signals_json": turn.signals_json,
            "used_fact_ids": turn.used_fact_ids,
        }
        for turn in turns
    ]
    assert opportunity_input["case_package"] == CaseRepository().get(
        session_record.case_id
    ).model_dump(mode="json")
    assert stored.frozen_input_fingerprint == report_jobs.canonical_fingerprint(
        {
            "coding_input": coding_input,
            "opportunity_check_input": opportunity_input,
        }
    )
    assert report_jobs.canonical_fingerprint(
        {"z": [{"b": 2, "a": 1}], "a": "中文"}
    ) == report_jobs.canonical_fingerprint(
        {"a": "中文", "z": [{"a": 1, "b": 2}]}
    )
    assert stored.coding_json is None
    assert stored.scoring_group_results_json == {}
    assert stored.scoring_groups_done == []
    assert stored.attempts == {}
    assert stored.rubric_fingerprint
    assert stored.case_package_fingerprint == report_jobs.canonical_fingerprint(
        opportunity_input["case_package"]
    )
    expected_model_snapshot = {
        "report_model": "qwen3.8-max",
        "sampling_parameters": {"temperature": 0.1},
    }
    assert stored.model_snapshot == expected_model_snapshot
    assert hasattr(stored, "model_fingerprint")
    assert stored.model_fingerprint == report_jobs.canonical_fingerprint(
        expected_model_snapshot
    )
    from app.reports.report_provider import (
        GroupModelOutput,
        LocalCodingOutput,
        ReduceModelOutput,
    )

    assert report_jobs.REPORT_PROMPT_BUNDLE == {
        "bundle_id": "report_map_reduce_and_three_groups",
        "prompts": report_jobs.REPORT_PROMPT_BUNDLE["prompts"],
        "output_contracts": {
            "map": LocalCodingOutput.model_json_schema(),
            "reduce": ReduceModelOutput.model_json_schema(),
            "group": GroupModelOutput.model_json_schema(),
        },
    }
    assert stored.prompt_fingerprint == report_jobs.canonical_fingerprint(
        report_jobs.REPORT_PROMPT_BUNDLE
    )

    public = client.get(f"/api/report-jobs/{stored.id}")
    assert public.status_code == 200
    assert "frozen_input_json" not in public.json()
    assert "opportunity_check_json" not in public.json()


def test_new_work_categories_are_preserved_in_frozen_input(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session_record = _ended_session(test_engine, mode=SessionMode.assessment)
    turns = _add_turns_and_failure(test_engine, session_record.id)
    new_actions = [
        "emotion_stabilization",
        "goal_clarification",
        "conflict_deescalation",
        "autonomy_support",
        "resource_linkage",
    ]
    payload = _work_record_payload(turns[0].id)
    payload["planned_actions"] = new_actions
    saved = client.put(
        f"/api/sessions/{session_record.id}/work-record",
        json=payload,
    )
    assert saved.status_code == 200, saved.text

    import app.api.routes.reports as report_routes
    from app.main import app

    class PassiveProcessor:
        def process(self, job_id: str) -> None:
            del job_id

    app.dependency_overrides[report_routes.get_report_job_processor] = PassiveProcessor
    try:
        created = client.post(f"/api/sessions/{session_record.id}/reports")
    finally:
        app.dependency_overrides.pop(report_routes.get_report_job_processor, None)
    assert created.status_code == 202, created.text

    from app.reports.models import ReportJobRecord

    with Session(test_engine) as db:
        stored = db.get(ReportJobRecord, created.json()["id"])
        assert stored is not None
        assert stored.frozen_input_json["work_record"]["planned_actions"] == new_actions


def test_frozen_inputs_have_separate_validated_read_interfaces(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session_record = _ended_session(test_engine, mode=SessionMode.assessment)
    turns = _add_turns_and_failure(test_engine, session_record.id)
    assert client.put(
        f"/api/sessions/{session_record.id}/work-record",
        json=_work_record_payload(turns[0].id),
    ).status_code == 200
    created = client.post(f"/api/sessions/{session_record.id}/reports")
    assert created.status_code == 202
    body = created.json()
    from app.cases.loader import CaseRepository
    from app.reports.jobs import ReportJobService

    assert hasattr(ReportJobService, "get_coding_input")
    assert hasattr(ReportJobService, "get_opportunity_check_input")

    import app.reports.job_inputs as job_inputs

    assert job_inputs.FrozenInputModel.model_config.get("frozen") is True
    assert hasattr(job_inputs, "WorkRecordSnapshotInput")

    with Session(test_engine) as db:
        service = ReportJobService(db, CaseRepository())
        coding_input = service.get_coding_input(body["id"])
        opportunity_input = service.get_opportunity_check_input(body["id"])

    assert isinstance(coding_input, job_inputs.CodingInput)
    assert isinstance(opportunity_input, job_inputs.OpportunityCheckInput)
    assert isinstance(coding_input.work_record, job_inputs.WorkRecordSnapshotInput)
    coding_payload = coding_input.model_dump(mode="json")
    assert "case_package" not in _recursive_keys(coding_payload)
    assert opportunity_input.session_id == session_record.id
    assert opportunity_input.case_package["case"]["case_id"] == (
        session_record.case_id
    )
    with pytest.raises(ValidationError, match="frozen"):
        coding_input.session = coding_input.session


def test_progress_json_updates_survive_across_database_sessions(
    client: TestClient,
    test_engine: Engine,
) -> None:
    _, body = _create_job(client, test_engine)
    import app.reports.jobs as report_jobs

    assert hasattr(report_jobs, "ReportJobProgressUpdate")
    with Session(test_engine) as first_db:
        report_jobs.ReportJobService(
            first_db,
            report_jobs.CaseRepository(),
        ).update_progress(
            body["id"],
            report_jobs.ReportJobProgressUpdate(
                stage=report_jobs.ReportJobStage.scoring,
                coding_json={"meaning_units": [{"id": "unit-one"}]},
                scoring_group_id="group-a",
                scoring_group_result={"dimensions": ["c1"]},
                completed_group="group-a",
                attempt_key="scoring",
            ),
        )
    with Session(test_engine) as second_db:
        report_jobs.ReportJobService(
            second_db,
            report_jobs.CaseRepository(),
        ).update_progress(
            body["id"],
            report_jobs.ReportJobProgressUpdate(
                scoring_group_id="group-b",
                scoring_group_result={"dimensions": ["c2"]},
                completed_group="group-b",
                attempt_key="scoring",
            ),
        )
    with Session(test_engine) as verification_db:
        stored = verification_db.get(report_jobs.ReportJobRecord, body["id"])
        assert stored is not None
        assert stored.coding_json == {"meaning_units": [{"id": "unit-one"}]}
        assert stored.scoring_group_results_json == {
            "group-a": {"dimensions": ["c1"]},
            "group-b": {"dimensions": ["c2"]},
        }
        assert stored.scoring_groups_done == ["group-a", "group-b"]
        assert stored.attempts == {"scoring": 2}


def test_report_job_creation_freezes_work_record_before_report_exists(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session_record = _ended_session(test_engine, mode=SessionMode.assessment)
    turns = _add_turns_and_failure(test_engine, session_record.id)
    payload = _work_record_payload(turns[0].id)
    assert client.put(
        f"/api/sessions/{session_record.id}/work-record",
        json=payload,
    ).status_code == 200
    created = client.post(f"/api/sessions/{session_record.id}/reports")
    assert created.status_code == 202, created.text

    changed = {**payload, "problem_understanding": "尝试修改已经冻结的记录。"}
    blocked = client.put(
        f"/api/sessions/{session_record.id}/work-record",
        json=changed,
    )

    assert blocked.status_code == 409
    assert "任务" in blocked.json()["detail"]
    with Session(test_engine) as db:
        from app.reports.models import ReportRecord

        assert db.exec(
            select(ReportRecord).where(ReportRecord.session_id == session_record.id)
        ).first() is None


def test_report_model_and_prompt_have_explicit_snapshots_and_fingerprints(
    client: TestClient,
    test_engine: Engine,
) -> None:
    _, body = _create_job(client, test_engine)
    import app.reports.jobs as report_jobs
    import app.reports.models as report_models

    with Session(test_engine) as db:
        stored = db.get(report_models.ReportJobRecord, body["id"])
    assert stored is not None
    expected_model_snapshot = {
        "report_model": "qwen3.8-max",
        "sampling_parameters": {"temperature": 0.1},
    }
    assert stored.model_snapshot == expected_model_snapshot
    assert hasattr(stored, "model_fingerprint")
    assert stored.model_fingerprint == report_jobs.canonical_fingerprint(
        expected_model_snapshot
    )
    from app.reports.report_provider import (
        GroupModelOutput,
        LocalCodingOutput,
        ReduceModelOutput,
    )

    assert report_jobs.REPORT_PROMPT_BUNDLE == {
        "bundle_id": "report_map_reduce_and_three_groups",
        "prompts": report_jobs.REPORT_PROMPT_BUNDLE["prompts"],
        "output_contracts": {
            "map": LocalCodingOutput.model_json_schema(),
            "reduce": ReduceModelOutput.model_json_schema(),
            "group": GroupModelOutput.model_json_schema(),
        },
    }
    assert stored.prompt_fingerprint == report_jobs.canonical_fingerprint(
        report_jobs.REPORT_PROMPT_BUNDLE
    )


def test_rubric_fingerprint_uses_all_current_competency_rubrics() -> None:
    from app.reports.jobs import _rubric_snapshot
    from app.reports.scoring_domain import CoreDimension, SpecialModule

    snapshot = _rubric_snapshot()
    rubrics = snapshot["rubrics"]
    assert {item["target"] for item in rubrics} == {
        *(target.value for target in CoreDimension),
        *(target.value for target in SpecialModule),
    }
    assert len(rubrics) == 18
    for rubric in rubrics:
        assert rubric["name"]
        assert rubric["indicators"]
        assert "excluded" in rubric
        assert set(rubric["anchors"]) == {"0", "1", "2", "3", "4"}
    assert {item["target"] for item in rubrics} != {
        "therapeutic_communication",
        "systematic_assessment",
        "collaborative_process",
        "case_formulation",
        "intervention_responsiveness",
        "professional_documentation",
        "risk_safety",
        "ethics_responsibility",
    }


def test_assessment_without_work_record_cannot_create_report_job(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session_record = _ended_session(test_engine, mode=SessionMode.assessment)

    response = client.post(f"/api/sessions/{session_record.id}/reports")

    assert response.status_code == 409
    assert "工作记录" in response.json()["detail"]


def test_report_job_read_does_not_expose_internal_error(
    client: TestClient,
    test_engine: Engine,
) -> None:
    _, body = _create_job(client, test_engine)
    import app.reports.models as report_models

    assert hasattr(report_models, "ReportJobRecord")
    with Session(test_engine) as db:
        record = db.get(report_models.ReportJobRecord, body["id"])
        assert record is not None
        record.stage = report_models.ReportJobStage.failed
        record.last_error = "供应商密钥 test-internal-secret 请求失败"
        db.add(record)
        db.commit()

    response = client.get(f"/api/report-jobs/{body['id']}")

    assert response.status_code == 200, response.text
    assert response.json()["stage"] == "failed"
    assert response.json()["retryable"] is True
    assert "last_error" not in response.json()
    assert "test-internal-secret" not in response.text


@pytest.mark.parametrize(
    "interrupted_stage",
    ["queued", "coding", "scoring", "assembling"],
)
def test_startup_finalizer_marks_interrupted_jobs_failed_and_retryable(
    client: TestClient,
    test_engine: Engine,
    interrupted_stage: str,
) -> None:
    _, body = _create_job(client, test_engine)
    import app.reports.jobs as report_jobs
    import app.reports.models as report_models

    assert hasattr(report_jobs, "finalize_interrupted_report_jobs")
    with Session(test_engine) as db:
        record = db.get(report_models.ReportJobRecord, body["id"])
        assert record is not None
        record.stage = report_models.ReportJobStage(interrupted_stage)
        db.add(record)
        db.commit()
        assert report_jobs.finalize_interrupted_report_jobs(db) == 1

    response = client.get(f"/api/report-jobs/{body['id']}")
    assert response.status_code == 200
    assert response.json()["stage"] == "failed"
    assert response.json()["retryable"] is True


def test_application_lifespan_closes_legacy_queued_job(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database as database
    from app.cases.loader import CaseRepository
    from app.database import create_db_and_tables
    from app.main import app
    from app.reports.jobs import ReportJobService

    create_db_and_tables(test_engine)
    session_record = _ended_session(test_engine)
    with Session(test_engine) as db:
        created = ReportJobService(db, CaseRepository()).create(session_record.id)
    monkeypatch.setattr(database, "engine", test_engine)

    with TestClient(app) as startup_client:
        response = startup_client.get(f"/api/report-jobs/{created.job.id}")

    assert response.status_code == 200
    assert response.json()["stage"] == "failed"
    assert response.json()["retryable"] is True


@pytest.mark.parametrize("retry_stage", ["failed", "partial"])
def test_retry_requeues_failed_or_partial_job(
    client: TestClient,
    test_engine: Engine,
    retry_stage: str,
) -> None:
    _, body = _create_job(client, test_engine)
    import app.reports.models as report_models

    with Session(test_engine) as db:
        record = db.get(report_models.ReportJobRecord, body["id"])
        assert record is not None
        record.stage = report_models.ReportJobStage(retry_stage)
        record.last_error = "内部错误"
        db.add(record)
        db.commit()

    import app.api.routes.reports as report_routes
    from app.main import app

    class PassiveProcessor:
        def process(self, job_id: str) -> None:
            del job_id

    app.dependency_overrides[report_routes.get_report_job_processor] = PassiveProcessor
    try:
        response = client.post(f"/api/report-jobs/{body['id']}/retry")
    finally:
        app.dependency_overrides.pop(report_routes.get_report_job_processor, None)

    assert response.status_code == 202, response.text
    assert response.json()["stage"] == "queued"
    assert response.json()["retryable"] is False
    with Session(test_engine) as db:
        record = db.get(report_models.ReportJobRecord, body["id"])
        assert record is not None
        assert record.attempts == {"manual_retry": 1}
        assert record.last_error is None


@pytest.mark.parametrize("non_retryable_stage", ["queued", "coding", "succeeded"])
def test_retry_rejects_non_retryable_job_stage(
    client: TestClient,
    test_engine: Engine,
    non_retryable_stage: str,
) -> None:
    _, body = _create_job(client, test_engine)
    import app.reports.models as report_models

    with Session(test_engine) as db:
        record = db.get(report_models.ReportJobRecord, body["id"])
        assert record is not None
        record.stage = report_models.ReportJobStage(non_retryable_stage)
        db.add(record)
        db.commit()

    response = client.post(f"/api/report-jobs/{body['id']}/retry")

    assert response.status_code == 409


def test_second_retry_does_not_schedule_the_same_job_again(
    client: TestClient,
    test_engine: Engine,
) -> None:
    _, body = _create_job(client, test_engine)
    import app.api.routes.reports as report_routes
    import app.reports.models as report_models
    from app.main import app

    with Session(test_engine) as db:
        record = db.get(report_models.ReportJobRecord, body["id"])
        assert record is not None
        record.stage = report_models.ReportJobStage.failed
        db.add(record)
        db.commit()

    class RecordingProcessor:
        def __init__(self) -> None:
            self.job_ids: list[str] = []

        def process(self, job_id: str) -> None:
            self.job_ids.append(job_id)

    processor = RecordingProcessor()
    app.dependency_overrides[report_routes.get_report_job_processor] = lambda: processor
    try:
        first = client.post(f"/api/report-jobs/{body['id']}/retry")
        second = client.post(f"/api/report-jobs/{body['id']}/retry")
    finally:
        app.dependency_overrides.pop(report_routes.get_report_job_processor, None)

    assert first.status_code == 202
    assert second.status_code == 409
    assert processor.job_ids == [body["id"]]


def test_report_write_flows_begin_with_sqlite_immediate_transaction(
    client: TestClient,
    test_engine: Engine,
) -> None:
    from app.cases.loader import CaseRepository
    from app.reports.jobs import ReportJobService
    from app.reports.schemas import WorkRecordUpsert
    from app.reports.service import ReportService

    create_session = _ended_session(test_engine)
    work_record_session = _ended_session(test_engine, mode=SessionMode.assessment)
    retry_session = _ended_session(test_engine)
    with Session(test_engine) as db:
        retry_job = ReportJobService(db, CaseRepository()).create(retry_session.id).job
    with Session(test_engine) as db:
        from app.reports.models import ReportJobRecord, ReportJobStage

        stored = db.get(ReportJobRecord, retry_job.id)
        assert stored is not None
        stored.stage = ReportJobStage.failed
        db.add(stored)
        db.commit()

    work_record_request = WorkRecordUpsert.model_validate(
        {**_work_record_payload("unused"), "risk_evidence_turn_ids": []}
    )

    def create_action() -> None:
        with Session(test_engine) as db:
            ReportJobService(db, CaseRepository()).create(create_session.id)

    def work_record_action() -> None:
        with Session(test_engine) as db:
            ReportService(db, CaseRepository()).put_work_record(
                work_record_session.id,
                work_record_request,
            )

    def retry_action() -> None:
        with Session(test_engine) as db:
            ReportJobService(db, CaseRepository()).retry(retry_job.id)

    def make_statement_recorder(
        statements: list[str],
    ) -> Callable[[object, object, str, object, object, bool], None]:
        def capture_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement.strip().upper())

        return capture_statement

    for action in (create_action, work_record_action, retry_action):
        statements: list[str] = []
        capture_statement = make_statement_recorder(statements)

        event.listen(test_engine, "before_cursor_execute", capture_statement)
        try:
            action()
        finally:
            event.remove(test_engine, "before_cursor_execute", capture_statement)
        assert statements[0] == "BEGIN IMMEDIATE"


def test_created_job_is_submitted_to_replaceable_processor(
    client: TestClient,
    test_engine: Engine,
) -> None:
    import app.api.routes.reports as report_routes
    from app.main import app

    assert hasattr(report_routes, "get_report_job_processor")

    class RecordingProcessor:
        def __init__(self) -> None:
            self.job_ids: list[str] = []

        def process(self, job_id: str) -> None:
            self.job_ids.append(job_id)

    processor = RecordingProcessor()
    app.dependency_overrides[report_routes.get_report_job_processor] = lambda: processor
    try:
        _, body = _create_job(client, test_engine)
    finally:
        app.dependency_overrides.pop(report_routes.get_report_job_processor, None)

    assert processor.job_ids == [body["id"]]


def test_default_processor_without_api_key_fails_fast_as_retryable(
    client: TestClient,
    test_engine: Engine,
) -> None:
    _, created = _create_job(client, test_engine, use_default_processor=True)

    response = client.get(f"/api/report-jobs/{created['id']}")

    assert created["stage"] == "queued"
    assert response.status_code == 200
    assert response.json()["stage"] == "failed"
    assert response.json()["retryable"] is True
    assert "last_error" not in response.json()
    with Session(test_engine) as db:
        from app.reports.models import ReportJobRecord

        stored = db.get(ReportJobRecord, created["id"])
        assert stored is not None
        assert stored.last_error is not None
        assert "API Key" in stored.last_error
        assert stored.last_error not in response.text

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from app.runtime.failures import (
    FailureAttempt,
    FailureDisposition,
    RuntimeFailure,
    RuntimeFailureRecorder,
)
from app.runtime.models import RuntimeFailureRecord
from app.sessions.models import (
    CaseType,
    Media,
    ModelMode,
    Scene,
    SessionMode,
    SessionRecord,
)


def _session() -> SessionRecord:
    return SessionRecord(
        id="session-failure-record",
        mode=SessionMode.assessment,
        scene=Scene.hotline,
        case_type=CaseType.main,
        case_id="crisis_student_main",
        media=Media.voice,
        model_mode=ModelMode.live,
    )


def test_runtime_failure_recorder_keeps_attempts_and_final_disposition(
    test_engine: Engine,
) -> None:
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(_session())
        db.commit()

    recorder = RuntimeFailureRecorder(test_engine)
    stored = recorder.record(
        RuntimeFailure(
            session_id="session-failure-record",
            client_turn_id="turn-n1",
            component="director",
            phase="directing",
            operation="workflow_validation",
            failure_code="director.workflow_validation",
            retryable=False,
            disposition=FailureDisposition.technical_pause,
            attempts=(
                FailureAttempt(
                    index=1,
                    error_class="WorkflowDecisionError",
                    message="开放事实未进入本轮必答回应",
                    call_kind="initial",
                    provider_request_id="request-initial",
                    details={"candidate_fields": ["fact_proposals", "reply_plan"]},
                ),
                FailureAttempt(
                    index=2,
                    error_class="WorkflowDecisionError",
                    message="首要回应事项没有指向必答事项",
                    call_kind="repair",
                    provider_request_id="request-repair",
                ),
            ),
            details={"worker_text": "你怎么会在这个时候打过来？"},
        )
    )

    assert stored.id
    with Session(test_engine) as db:
        record = db.exec(select(RuntimeFailureRecord)).one()

    assert record.session_id == "session-failure-record"
    assert record.client_turn_id == "turn-n1"
    assert record.component == "director"
    assert record.phase == "directing"
    assert record.operation == "workflow_validation"
    assert record.failure_code == "director.workflow_validation"
    assert record.error_class == "WorkflowDecisionError"
    assert record.attempt_count == 2
    assert record.retryable is False
    assert record.disposition == "technical_pause"
    assert record.provider_request_id == "request-repair"
    assert record.attempts_json[0]["message"] == "开放事实未进入本轮必答回应"
    assert record.attempts_json[1]["call_kind"] == "repair"
    assert record.details_json == {"worker_text": "你怎么会在这个时候打过来？"}


def test_runtime_failure_recorder_redacts_credentials_recursively(
    test_engine: Engine,
) -> None:
    provider_secret = "s" + "k-secret-value"
    secondary_secret = "s" + "k-another-secret"
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(_session())
        db.commit()

    recorder = RuntimeFailureRecorder(test_engine)
    recorder.record(
        RuntimeFailure(
            session_id="session-failure-record",
            component="director",
            phase="directing",
            operation="provider_call",
            failure_code="provider.authentication",
            retryable=False,
            disposition=FailureDisposition.technical_pause,
            attempts=(
                FailureAttempt(
                    index=1,
                    error_class="AuthenticationError",
                    message=f"Authorization: Bearer {provider_secret}",
                    details={
                        "headers": {"Authorization": f"Bearer {provider_secret}"},
                        "api_key": provider_secret,
                    },
                ),
            ),
            details={
                "nested": {"token": secondary_secret},
                "safe": "保留这条诊断",
            },
        )
    )

    with Session(test_engine) as db:
        record = db.exec(select(RuntimeFailureRecord)).one()

    serialized = str(record.attempts_json) + str(record.details_json)
    assert provider_secret not in serialized
    assert secondary_secret not in serialized
    assert "保留这条诊断" in serialized

    columns = {
        column["name"]
        for column in inspect(test_engine).get_columns("runtime_failure_records")
    }
    assert not {"api_key", "authorization", "prompt", "response_body"} & columns

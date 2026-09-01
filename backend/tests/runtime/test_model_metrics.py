from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from app.runtime.metrics import ModelCallMetric, ModelCallRecorder
from app.runtime.models import (
    CacheMode,
    ModelCallKind,
    ModelCallMetricRecord,
    ModelRole,
)
from app.sessions.models import (
    CaseType,
    Media,
    ModelMode,
    Scene,
    SessionMode,
    SessionRecord,
)


def test_model_call_recorder_persists_only_technical_metrics(
    test_engine: Engine,
) -> None:
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="session-metrics",
                mode=SessionMode.assessment,
                scene=Scene.hotline,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.voice,
                model_mode=ModelMode.live,
            )
        )
        db.commit()

    recorder = ModelCallRecorder(test_engine)
    recorder.record(
        ModelCallMetric(
            session_id="session-metrics",
            client_turn_id="turn-metrics",
            model_role=ModelRole.director,
            model_name="qwen3.7-plus",
            call_kind=ModelCallKind.initial,
            cache_mode=CacheMode.explicit,
            prompt_tokens=1200,
            completion_tokens=80,
            total_tokens=1280,
            cached_tokens=900,
            cache_creation_input_tokens=200,
            latency_ms=432,
            success=True,
            request_id="req-metrics",
        )
    )

    with Session(test_engine) as db:
        stored = db.exec(select(ModelCallMetricRecord)).one()

    assert stored.session_id == "session-metrics"
    assert stored.client_turn_id == "turn-metrics"
    assert stored.model_role is ModelRole.director
    assert stored.model_name == "qwen3.7-plus"
    assert stored.call_kind is ModelCallKind.initial
    assert stored.cache_mode is CacheMode.explicit
    assert stored.prompt_tokens == 1200
    assert stored.completion_tokens == 80
    assert stored.total_tokens == 1280
    assert stored.cached_tokens == 900
    assert stored.cache_creation_input_tokens == 200
    assert stored.latency_ms == 432
    assert stored.success is True
    assert stored.request_id == "req-metrics"

    column_names = {
        column["name"]
        for column in inspect(test_engine).get_columns("model_call_metrics")
    }
    assert not {
        "request_body",
        "response_body",
        "messages",
        "api_key",
    } & column_names


def test_model_role_includes_tts() -> None:
    assert ModelRole.tts.value == "tts"


def test_latest_successful_prompt_tokens_selects_latest_actor_success(
    test_engine: Engine,
) -> None:
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="session-latest-actor",
                mode=SessionMode.assessment,
                scene=Scene.hotline,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.text,
                model_mode=ModelMode.live,
            )
        )
        db.commit()

    recorder = ModelCallRecorder(test_engine)
    common = dict(
        session_id="session-latest-actor",
        client_turn_id="turn-1",
        model_name="model",
        call_kind=ModelCallKind.initial,
        cache_mode=CacheMode.character_session,
        completion_tokens=10,
        total_tokens=10,
        cached_tokens=0,
        cache_creation_input_tokens=0,
        latency_ms=1,
        request_id=None,
    )
    recorder.record(
        ModelCallMetric(
            **common,
            model_role=ModelRole.actor,
            prompt_tokens=120,
            success=True,
        )
    )
    recorder.record(
        ModelCallMetric(
            **common,
            model_role=ModelRole.director,
            prompt_tokens=999,
            success=True,
        )
    )
    recorder.record(
        ModelCallMetric(
            **common,
            model_role=ModelRole.actor,
            prompt_tokens=888,
            success=False,
        )
    )
    recorder.record(
        ModelCallMetric(
            **common,
            model_role=ModelRole.actor,
            prompt_tokens=240,
            success=True,
        )
    )

    assert recorder.latest_successful_prompt_tokens(
        "session-latest-actor",
        ModelRole.actor,
    ) == 240
    assert recorder.latest_successful_prompt_tokens(
        "missing-session",
        ModelRole.actor,
    ) is None


def test_latest_attempted_prompt_tokens_is_bound_to_current_turn_and_keeps_failures(
    test_engine: Engine,
) -> None:
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="session-latest-attempt",
                mode=SessionMode.assessment,
                scene=Scene.online,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.text,
                model_mode=ModelMode.live,
            )
        )
        db.commit()

    recorder = ModelCallRecorder(test_engine)
    common = dict(
        session_id="session-latest-attempt",
        model_role=ModelRole.actor,
        model_name="qwen-plus-character",
        call_kind=ModelCallKind.initial,
        cache_mode=CacheMode.character_session,
        completion_tokens=0,
        total_tokens=0,
        cached_tokens=0,
        cache_creation_input_tokens=0,
        latency_ms=1,
        request_id=None,
    )
    recorder.record(
        ModelCallMetric(
            **common,
            client_turn_id="older-success",
            prompt_tokens=120,
            success=True,
        )
    )
    recorder.record(
        ModelCallMetric(
            **common,
            client_turn_id="current-failed-schema",
            prompt_tokens=31_000,
            success=False,
        )
    )
    recorder.record(
        ModelCallMetric(
            **common,
            client_turn_id="current-no-usage",
            prompt_tokens=0,
            success=False,
        )
    )

    assert recorder.latest_attempted_prompt_tokens(
        "session-latest-attempt",
        ModelRole.actor,
        "current-failed-schema",
    ) == 31_000
    assert recorder.latest_attempted_prompt_tokens(
        "session-latest-attempt",
        ModelRole.actor,
        "current-no-usage",
    ) is None
    assert recorder.latest_attempted_prompt_tokens(
        "session-latest-attempt",
        ModelRole.actor,
        "missing-turn",
    ) is None

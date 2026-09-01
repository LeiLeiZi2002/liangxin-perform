from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.sessions.models import (
    EndReason,
    SessionRecord,
    SessionStatus,
    TurnRecord,
    TurnSpeaker,
)


def create_session(
    client: TestClient,
    *,
    mode: str = "assessment",
    scene: str | None = "hotline",
    case_type: str | None = "main",
    case_id: str = "crisis_student_main",
) -> Any:
    payload: dict[str, Any] = {"mode": mode, "case_id": case_id}
    if scene is not None:
        payload["scene"] = scene
    if case_type is not None:
        payload["case_type"] = case_type
    return client.post("/api/sessions", json=payload)


def test_demo_config_defaults_to_one_untimed_task() -> None:
    from app.sessions.models import DemoConfigRecord

    config = DemoConfigRecord()

    assert config.task_count == 1
    assert config.soft_duration_minutes is None


def test_create_session_uses_runtime_state_and_live_chain(
    client: TestClient,
    test_engine: Engine,
) -> None:
    response = create_session(client)

    assert response.status_code == 201
    body = response.json()
    assert body["case_id"] == "crisis_student_main"
    assert body["scene"] == "hotline"
    assert body["media"] == "voice"
    assert body["model_mode"] == "live"
    assert "state_json" not in body

    with Session(test_engine) as db:
        record = db.get(SessionRecord, body["id"])
    assert record is not None
    assert record.state_json["runtime"] == {
        "engine": "character_prompt",
        "phase": "listening",
    }
    assert "actor_state" not in record.state_json


@pytest.mark.parametrize(
    ("scene", "media"),
    [("institution", "voice"), ("hotline", "voice"), ("online", "text")],
)
def test_short_case_new_sessions_use_character_prompt_without_actor_state(
    client: TestClient,
    test_engine: Engine,
    scene: str,
    media: str,
) -> None:
    response = create_session(
        client,
        mode="experience",
        scene=scene,
        case_type="short",
        case_id="boundary_referral_short",
    )

    assert response.status_code == 201
    assert response.json()["media"] == media
    assert response.json()["soft_duration_minutes"] is None
    with Session(test_engine) as db:
        record = db.get(SessionRecord, response.json()["id"])
    assert record is not None
    assert record.state_json["runtime"] == {
        "engine": "character_prompt",
        "phase": "listening",
    }
    assert "actor_state" not in record.state_json
    assert "world" not in record.state_json


@pytest.mark.parametrize("scene", ["hotline", "online"])
def test_marriage_boundary_sessions_use_validated_character_prompt(
    client: TestClient,
    test_engine: Engine,
    scene: str,
) -> None:
    response = create_session(
        client,
        mode="experience",
        scene=scene,
        case_type="main",
        case_id="marriage_boundary_main",
    )

    assert response.status_code == 201
    with Session(test_engine) as db:
        record = db.get(SessionRecord, response.json()["id"])
    assert record is not None
    assert record.state_json["runtime"]["engine"] == "character_prompt"
    assert "actor_state" not in record.state_json


def test_session_creation_uses_case_aware_character_lookup(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import sessions as sessions_route

    calls: list[str] = []
    original = sessions_route.character_repository.get_for_case

    def tracked_get_for_case(case_spec):
        calls.append(case_spec.case_id)
        return original(case_spec)

    monkeypatch.setattr(
        sessions_route.character_repository,
        "get_for_case",
        tracked_get_for_case,
    )

    response = create_session(client, case_id="crisis_student_main")

    assert response.status_code == 201
    assert calls == ["crisis_student_main"]


def test_required_character_missing_returns_configuration_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import sessions as sessions_route
    from app.runtime.character_provider import CharacterNotFoundError

    def missing_character(case_spec):
        raise CharacterNotFoundError(case_spec.case_id)

    monkeypatch.setattr(
        sessions_route.character_repository,
        "get_for_case",
        missing_character,
    )

    response = create_session(
        client,
        mode="experience",
        scene="hotline",
        case_type="main",
        case_id="marriage_boundary_main",
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "个案要求角色配置，但配置文件不存在"


def test_legacy_case_without_character_keeps_workflow_fallback(
    client: TestClient,
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import sessions as sessions_route
    from app.runtime.character_provider import CharacterNotFoundError

    def missing_character(case_spec):
        raise CharacterNotFoundError(case_spec.case_id)

    monkeypatch.setattr(
        sessions_route.character_repository,
        "get_for_case",
        missing_character,
    )

    response = create_session(client, case_id="crisis_student_main")

    assert response.status_code == 201
    with Session(test_engine) as db:
        record = db.get(SessionRecord, response.json()["id"])
    assert record is not None
    assert record.state_json["runtime"]["engine"] == "workflow"
    assert "actor_state" in record.state_json


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("mode", "practice"), ("scene", "classroom"), ("case_type", "medium")],
)
def test_create_session_rejects_invalid_enums(
    client: TestClient,
    field: str,
    invalid_value: str,
) -> None:
    payload = {
        "mode": "assessment",
        "scene": "hotline",
        "case_type": "main",
        "case_id": "crisis_student_main",
        field: invalid_value,
    }
    assert client.post("/api/sessions", json=payload).status_code == 422


def test_create_session_checks_case_type_scene_and_identifier(
    client: TestClient,
) -> None:
    assert create_session(client, mode="experience", case_type="short").status_code == 422
    assert create_session(client, mode="experience", scene="online").status_code == 422
    assert create_session(client, mode="experience", case_id="missing-case").status_code == 422


@pytest.mark.parametrize(
    ("request_overrides", "case_id", "expected_detail"),
    [
        (
            {"scene": "online", "case_type": "main"},
            "marriage_boundary_main",
            "正式测评场域与当前管理配置不一致",
        ),
        (
            {"scene": "hotline", "case_type": "short"},
            "boundary_referral_short",
            "正式测评个案类型与当前管理配置不一致",
        ),
    ],
)
def test_assessment_rejects_request_values_that_conflict_with_demo_config(
    client: TestClient,
    request_overrides: dict[str, str],
    case_id: str,
    expected_detail: str,
) -> None:
    response = client.post(
        "/api/sessions",
        json={
            "mode": "assessment",
            "case_id": case_id,
            **request_overrides,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == expected_detail


def test_assessment_uses_demo_config_when_request_omits_scene_and_case_type(
    client: TestClient,
) -> None:
    config = {
        "scene": "online",
        "case_type": "main",
        "task_count": 1,
        "soft_duration_minutes": None,
        "model_mode": "live",
        "require_work_record": True,
    }
    assert client.put("/api/demo-config", json=config).status_code == 200

    response = create_session(
        client,
        scene=None,
        case_type=None,
        case_id="marriage_boundary_main",
    )

    assert response.status_code == 201
    assert response.json()["scene"] == "online"
    assert response.json()["case_type"] == "main"
    assert response.json()["media"] == "text"


def test_experience_request_values_take_priority_over_demo_config(
    client: TestClient,
) -> None:
    config = {
        "scene": "institution",
        "case_type": "short",
        "task_count": 1,
        "soft_duration_minutes": 9,
        "model_mode": "live",
        "require_work_record": True,
    }
    assert client.put("/api/demo-config", json=config).status_code == 200

    response = create_session(
        client,
        mode="experience",
        scene="online",
        case_type="main",
        case_id="marriage_boundary_main",
    )

    assert response.status_code == 201
    assert response.json()["scene"] == "online"
    assert response.json()["case_type"] == "main"
    assert response.json()["media"] == "text"
    assert response.json()["soft_duration_minutes"] is None


def test_experience_only_falls_back_to_config_for_omitted_values(
    client: TestClient,
) -> None:
    config = {
        "scene": "hotline",
        "case_type": "short",
        "task_count": 1,
        "soft_duration_minutes": None,
        "model_mode": "live",
        "require_work_record": True,
    }
    assert client.put("/api/demo-config", json=config).status_code == 200

    scene_from_request = create_session(
        client,
        mode="experience",
        scene="online",
        case_type=None,
        case_id="boundary_referral_short",
    )
    type_from_request = create_session(
        client,
        mode="experience",
        scene=None,
        case_type="main",
        case_id="marriage_boundary_main",
    )

    assert scene_from_request.status_code == 201
    assert scene_from_request.json()["scene"] == "online"
    assert scene_from_request.json()["case_type"] == "short"
    assert type_from_request.status_code == 201
    assert type_from_request.json()["scene"] == "hotline"
    assert type_from_request.json()["case_type"] == "main"


def test_get_session_returns_complete_persisted_transcript(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session = create_session(client).json()
    with Session(test_engine) as db:
        db.add_all(
            [
                TurnRecord(
                    session_id=session["id"],
                    client_turn_id="pair-1",
                    sequence=1,
                    speaker=TurnSpeaker.worker,
                    text="你好。",
                ),
                TurnRecord(
                    session_id=session["id"],
                    client_turn_id="pair-1",
                    sequence=2,
                    speaker=TurnSpeaker.client,
                    text="嗯，你好。",
                    audio_path="voice.wav",
                ),
            ]
        )
        db.commit()

    response = client.get(f"/api/sessions/{session['id']}")

    assert response.status_code == 200
    assert [turn["text"] for turn in response.json()["transcript"]] == [
        "你好。",
        "嗯，你好。",
    ]
    assert [turn["client_turn_id"] for turn in response.json()["transcript"]] == [
        "pair-1",
        "pair-1",
    ]
    assert response.json()["transcript"][1]["audio_available"] is True


def test_end_session_persists_terminal_runtime_state_and_is_idempotent(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session = create_session(client).json()
    with Session(test_engine) as db:
        record = db.get(SessionRecord, session["id"])
        assert record is not None
        record.state_json = {
            **record.state_json,
            "runtime": {
                **record.state_json["runtime"],
                "phase": "technical_paused",
                "technical_retry_allowed": True,
                "pending_ending_route_id": "character_prompt_end",
            },
        }
        db.add(record)
        db.commit()

    response = client.post(
        f"/api/sessions/{session['id']}/end",
        json={"reason": "technical_interruption"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ended"
    assert response.json()["end_reason"] == "technical_interruption"
    assert response.json()["ended_at"] is not None

    repeated = client.post(
        f"/api/sessions/{session['id']}/end",
        json={"reason": "user_ended"},
    )
    assert repeated.status_code == 200

    with Session(test_engine) as db:
        record = db.get(SessionRecord, session["id"])
    assert record is not None
    assert record.status is SessionStatus.ended
    assert record.end_reason is EndReason.technical_interruption
    assert record.state_json["runtime"] == {
        "engine": "character_prompt",
        "phase": "ended",
        "technical_retry_allowed": False,
    }


def test_demo_config_controls_defaults_but_not_runtime_provider(
    client: TestClient,
) -> None:
    config = {
        "scene": "institution",
        "case_type": "short",
        "task_count": 1,
        "soft_duration_minutes": 9,
        "model_mode": "fallback",
        "require_work_record": True,
    }
    assert client.put("/api/demo-config", json=config).status_code == 200

    response = create_session(
        client,
        scene=None,
        case_type=None,
        case_id="boundary_referral_short",
    )

    assert response.status_code == 201
    assert response.json()["scene"] == "institution"
    assert response.json()["soft_duration_minutes"] == 9
    assert response.json()["model_mode"] == "live"

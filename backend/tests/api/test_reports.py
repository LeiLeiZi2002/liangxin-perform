from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.sessions.models import (
    CaseType,
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
    mode: SessionMode = SessionMode.assessment,
    case_id: str = "crisis_student_main",
) -> SessionRecord:
    record = SessionRecord(
        mode=mode,
        scene=Scene.hotline,
        case_type=CaseType.main,
        case_id=case_id,
        media=Media.voice,
        status=SessionStatus.ended,
        model_mode=ModelMode.fallback,
        state_json={
            "stage": "closing",
            "trust": 3,
            "distress": 3,
            "avoidance": 1,
            "cooperation": 3,
            "focus": None,
            "allowed_fact_ids": [],
            "disclosed_fact_ids": [],
            "event_ids": [],
            "opportunity_ids": [],
            "meaningful_exploration_signals": [],
            "semantic_repetition_count": 0,
        },
    )
    with Session(engine) as db:
        db.add(record)
        db.commit()
        db.refresh(record)
    return record


def _turn(
    engine: Engine,
    session_id: str,
    *,
    sequence: int,
    speaker: TurnSpeaker,
    text: str,
    signals: dict[str, Any] | None = None,
    disclosed: list[str] | None = None,
) -> str:
    state = {
        "disclosed_fact_ids": disclosed or [],
        "opportunity_ids": [],
    }
    turn = TurnRecord(
        session_id=session_id,
        client_turn_id=f"request-{sequence}",
        sequence=sequence,
        speaker=speaker,
        text=text,
        signals_json=signals or {},
        state_before_json={},
        state_after_json=state,
    )
    with Session(engine) as db:
        db.add(turn)
        db.commit()
        db.refresh(turn)
    return turn.id


def _work_record_payload(*, evidence_ids: list[str], risk_level: str = "high") -> dict[str, Any]:
    return {
        "problem_understanding": "近期多重压力后失眠、功能下降和绝望感加重，当前风险需要持续评估。",
        "risk_level": risk_level,
        "risk_reasoning": "来访者承认近期有自杀意念，昨晚意图增强；同时仍愿意求助并可联系室友。",
        "risk_evidence_turn_ids": evidence_ids,
        "missing_information": ["物质使用仍需复核", "物质使用仍需复核"],
        "planned_actions": [
            "stay_connected",
            "contact_support",
            "reduce_access",
            "supervisor",
            "follow_up",
            "stay_connected",
        ],
        "referral_decision": "urgent",
        "supervision_decision": True,
        "follow_up": "保持在线直至室友到场，并由值班督导确认后续转介与复联。",
        "limitations": "未核实既往医疗记录，判断仅基于本次对话和来访者自述。",
    }


def _submit_record(client: TestClient, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.put(f"/api/sessions/{session_id}/work-record", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_work_record_accepts_client_only_opening_as_evidence(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session = _ended_session(test_engine, case_id="marriage_boundary_main")
    opening_id = _turn(
        test_engine,
        session.id,
        sequence=1,
        speaker=TurnSpeaker.client,
        text="你好，我想问个事……这些聊天以后谁能看到？",
    )

    saved = _submit_record(
        client,
        session.id,
        _work_record_payload(evidence_ids=[opening_id]),
    )

    assert saved["risk_evidence_turn_ids"] == [opening_id]


def test_all_legacy_planned_actions_remain_accepted(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session = _ended_session(test_engine)
    legacy_actions = [
        "continue_assessment",
        "stay_connected",
        "contact_support",
        "reduce_access",
        "supervisor",
        "emergency_services",
        "referral",
        "follow_up",
    ]
    payload = _work_record_payload(evidence_ids=[])
    payload["planned_actions"] = legacy_actions

    saved = _submit_record(client, session.id, payload)

    assert saved["planned_actions"] == legacy_actions


def test_get_work_record_returns_frozen_saved_snapshot(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session = _ended_session(test_engine)
    payload = _work_record_payload(evidence_ids=[])
    saved = _submit_record(client, session.id, payload)

    before_job = client.get(f"/api/sessions/{session.id}/work-record")

    assert before_job.status_code == 200
    assert before_job.json() == saved

    report_job = client.post(f"/api/sessions/{session.id}/reports")
    assert report_job.status_code == 202
    blocked_update = client.put(
        f"/api/sessions/{session.id}/work-record",
        json={**payload, "limitations": "试图覆盖已冻结记录。"},
    )
    assert blocked_update.status_code == 409

    after_job = client.get(f"/api/sessions/{session.id}/work-record")
    assert after_job.status_code == 200
    assert after_job.json() == saved


def test_get_work_record_returns_404_when_no_saved_record(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session = _ended_session(test_engine)

    response = client.get(f"/api/sessions/{session.id}/work-record")

    assert response.status_code == 404


def test_get_rubric_returns_the_complete_utf8_markdown_document(client: TestClient) -> None:
    response = client.get("/api/rubric")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["title"] == "热线心理支持职业胜任力测评量规"
    markdown = payload["markdown"]
    rubric_path = (
        Path(__file__).resolve().parents[3] / "docs" / "热线心理支持职业胜任力测评量规.md"
    )
    with rubric_path.open(encoding="utf-8", newline="") as rubric_document:
        assert markdown == rubric_document.read()
    assert markdown.startswith("# 热线心理支持职业胜任力测评量规")
    core_targets = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9")
    assert len(core_targets) == 9
    core_headings = [
        line.split(maxsplit=2)[1] for line in markdown.splitlines() if line.startswith("## C")
    ]
    assert core_headings == list(core_targets)
    special_targets = ("S1a", "S1b", "S2", "S3", "S4", "S5", "S6", "S7", "S8")
    assert len(special_targets) == 9
    special_headings = [
        line.split(maxsplit=2)[1] for line in markdown.splitlines() if line.startswith("## S")
    ]
    assert special_headings == list(special_targets)
    professional_basis_index = markdown.rfind("## 十一、专业依据")
    assert professional_basis_index != -1
    assert "国家卫生健康委：《心理援助热线技术指南（试行）》" in markdown[professional_basis_index:]
    assert markdown.rstrip().endswith(
        "[中国心理学会：《临床与咨询心理学工作伦理守则》]"
        "(https://journal.psych.ac.cn/xlxb/article/2018/0439-755X/0439-755X-50-11-1314.shtml)"
    )
    assert "职业能力框架" in markdown


def test_get_rubric_returns_503_when_document_cannot_be_read(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.routes.reports as reports_routes

    def raise_os_error() -> object:
        raise OSError("unavailable")

    monkeypatch.setattr(reports_routes, "read_rubric_document", raise_os_error, raising=False)

    response = client.get("/api/rubric")

    assert response.status_code == 503
    assert response.json() == {"detail": "量规暂时无法读取，请稍后重试。"}


def test_rubric_document_schema_forbids_extra_fields() -> None:
    from app.reports.schemas import RubricDocumentRead

    with pytest.raises(ValidationError):
        RubricDocumentRead(
            title="热线心理支持职业胜任力测评量规",
            markdown="# 热线心理支持职业胜任力测评量规",
            unexpected="not allowed",
        )


def test_work_record_requires_ended_session_and_deduplicates_lists(
    client: TestClient,
    test_engine: Engine,
) -> None:
    active = _ended_session(test_engine)
    with Session(test_engine) as db:
        stored = db.get(SessionRecord, active.id)
        assert stored is not None
        stored.status = SessionStatus.active
        db.add(stored)
        db.commit()

    blocked = client.put(
        f"/api/sessions/{active.id}/work-record",
        json=_work_record_payload(evidence_ids=[]),
    )
    assert blocked.status_code == 409

    with Session(test_engine) as db:
        stored = db.get(SessionRecord, active.id)
        assert stored is not None
        stored.status = SessionStatus.ended
        db.add(stored)
        db.commit()
    saved = _submit_record(client, active.id, _work_record_payload(evidence_ids=[]))
    assert saved["missing_information"] == ["物质使用仍需复核"]
    assert saved["planned_actions"].count("stay_connected") == 1


def test_work_record_rejects_turn_from_another_session(
    client: TestClient,
    test_engine: Engine,
) -> None:
    first = _ended_session(test_engine)
    second = _ended_session(test_engine)
    foreign_turn_id = _turn(
        test_engine,
        second.id,
        sequence=1,
        speaker=TurnSpeaker.worker,
        text="另一会话的证据",
    )

    response = client.put(
        f"/api/sessions/{first.id}/work-record",
        json=_work_record_payload(evidence_ids=[foreign_turn_id]),
    )

    assert response.status_code == 422
    assert "当前会话" in response.json()["detail"]


def test_assessment_requires_work_record_before_report(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session = _ended_session(test_engine)
    _turn(
        test_engine,
        session.id,
        sequence=1,
        speaker=TurnSpeaker.worker,
        text="愿意说说最近发生了什么吗？",
        signals={"open_exploration": True},
    )

    response = client.post(f"/api/sessions/{session.id}/reports")

    assert response.status_code == 409
    assert "工作记录" in response.json()["detail"]


def test_active_session_cannot_generate_report(client: TestClient, test_engine: Engine) -> None:
    session = _ended_session(test_engine, mode=SessionMode.experience)
    with Session(test_engine) as db:
        record = db.get(SessionRecord, session.id)
        assert record is not None
        record.status = SessionStatus.active
        db.add(record)
        db.commit()

    response = client.post(f"/api/sessions/{session.id}/reports")

    assert response.status_code == 409


def test_report_job_freezes_work_record(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session = _ended_session(test_engine, mode=SessionMode.experience)
    _turn(
        test_engine,
        session.id,
        sequence=1,
        speaker=TurnSpeaker.worker,
        text="愿意说说吗？",
        signals={"open_exploration": True},
    )
    report = client.post(f"/api/sessions/{session.id}/reports")
    assert report.status_code == 202

    work_record = client.put(
        f"/api/sessions/{session.id}/work-record",
        json=_work_record_payload(evidence_ids=[]),
    )
    assert work_record.status_code == 409
    assert "报告" in work_record.json()["detail"]


def test_existing_report_job_is_returned_idempotently_after_source_session_changes(
    client: TestClient,
    test_engine: Engine,
) -> None:
    session = _ended_session(test_engine, mode=SessionMode.experience)
    report = client.post(f"/api/sessions/{session.id}/reports")
    assert report.status_code == 202
    with Session(test_engine) as db:
        stored = db.get(SessionRecord, session.id)
        assert stored is not None
        stored.status = SessionStatus.active
        db.add(stored)
        db.commit()

    repeated = client.post(f"/api/sessions/{session.id}/reports")

    assert repeated.status_code == 202
    assert repeated.json()["id"] == report.json()["id"]
    assert repeated.json()["stage"] == "failed"
    assert repeated.json()["retryable"] is True

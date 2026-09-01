import json

from fastapi.testclient import TestClient


def test_case_metadata_uses_stable_case_id(client: TestClient) -> None:
    response = client.get("/api/cases", params={"scene": "hotline", "case_type": "main"})

    assert response.status_code == 200
    case = response.json()[0]
    assert case["case_id"] == "crisis_student_main"
    assert set(case) == {
        "case_id",
        "title",
        "case_type",
        "public_entry",
        "estimated_duration_minutes",
        "scene",
        "media",
        "available_scenes",
    }


def test_list_cases_returns_only_public_metadata(client: TestClient) -> None:
    hotline_response = client.get(
        "/api/cases", params={"scene": "hotline", "case_type": "main"}
    )
    online_response = client.get(
        "/api/cases", params={"scene": "online", "case_type": "main"}
    )

    assert hotline_response.status_code == 200
    assert online_response.status_code == 200
    hotline_cases = {item["case_id"]: item for item in hotline_response.json()}
    online_cases = {item["case_id"]: item for item in online_response.json()}
    assert set(hotline_cases) == {"crisis_student_main", "marriage_boundary_main"}
    assert set(online_cases) == {"marriage_boundary_main"}

    crisis = hotline_cases["crisis_student_main"]
    assert crisis["scene"] == "hotline"
    assert crisis["media"] == "voice"
    assert crisis["public_entry"] == {
        "role": "心理援助热线当班工作者",
        "known_information": ["当前接入一通匿名来电", "尚无其他背景资料"],
        "task_boundary": ["通过自然通话开展工作", "通话结束后填写工作记录"],
    }

    marriage_hotline = hotline_cases["marriage_boundary_main"]
    assert marriage_hotline["scene"] == "hotline"
    assert marriage_hotline["media"] == "voice"
    assert marriage_hotline["available_scenes"] == ["hotline", "online"]
    assert marriage_hotline["public_entry"] == {
        "role": "心理援助热线当班工作者",
        "known_information": ["当前接入一通匿名来电"],
        "task_boundary": [
            "通过自然通话开展工作",
            "结束后填写热线工作记录",
        ],
    }

    marriage_online = online_cases["marriage_boundary_main"]
    assert marriage_online["scene"] == "online"
    assert marriage_online["media"] == "text"
    assert marriage_online["available_scenes"] == ["hotline", "online"]
    assert marriage_online["public_entry"] == {
        "role": "在线心理支持工作者",
        "known_information": [
            "当前接入一位成年用户的实时文字咨询",
            "平台已完成身份、所在地、紧急联系人和知情说明登记",
        ],
        "task_boundary": [
            "通过实时文字交流开展工作",
            "结束后填写在线咨询工作记录",
        ],
    }

    serialized = json.dumps(
        [*hotline_response.json(), *online_response.json()], ensure_ascii=False
    )
    for hidden in (
        "facts",
        "timeline",
        "relationships",
        "disclosure_rules",
        "scoring_opportunities",
        "沈雯",
        "长宁路127号",
        "9:03是心理截止点",
    ):
        assert hidden not in serialized


def test_list_cases_selects_public_entry_for_requested_scene(
    client: TestClient,
    monkeypatch,
) -> None:
    from app.api.routes import cases
    from app.cases.loader import CaseRepository

    package = CaseRepository().get("boundary_referral_short")
    case_payload = package.case.model_dump(mode="json")
    case_payload["public_entries"] = {
        "institution": {
            "role": "机构心理服务工作者",
            "known_information": ["已预约会谈"],
            "task_boundary": ["当面开展工作"],
        },
        "hotline": {
            "role": "心理援助热线工作者",
            "known_information": ["匿名来电"],
            "task_boundary": ["通过语音开展工作"],
        },
        "online": {
            "role": "在线心理支持工作者",
            "known_information": ["平台用户发来消息"],
            "task_boundary": ["通过文字开展工作"],
        },
    }
    package.case = package.case.__class__.model_validate(case_payload)

    class SceneEntryRepository:
        def list_published(self, **_filters):
            return [package.model_copy(deep=True)]

    monkeypatch.setattr(cases, "case_repository", SceneEntryRepository())

    hotline = client.get(
        "/api/cases", params={"scene": "hotline", "case_type": "short"}
    )
    online = client.get(
        "/api/cases", params={"scene": "online", "case_type": "short"}
    )

    assert hotline.status_code == 200
    assert online.status_code == 200
    assert hotline.json()[0]["public_entry"]["role"] == "心理援助热线工作者"
    assert online.json()[0]["public_entry"]["role"] == "在线心理支持工作者"


def test_draw_case_is_seeded_and_respects_filters(client: TestClient) -> None:
    payload = {
        "scene": "institution",
        "case_type": "short",
        "seed": 2026,
        "excluded_case_ids": [],
    }

    first = client.post("/api/cases/draw", json=payload)
    second = client.post("/api/cases/draw", json=payload)

    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()["case_id"] == "boundary_referral_short"
    assert first.json()["scene"] == "institution"
    assert first.json()["case_type"] == "short"


def test_draw_case_returns_422_when_exclusions_remove_all_candidates(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/cases/draw",
        json={
            "scene": "hotline",
            "case_type": "main",
            "seed": 7,
            "excluded_case_ids": [
                "crisis_student_main",
                "marriage_boundary_main",
            ],
        },
    )

    assert response.status_code == 422


def test_create_session_accepts_matching_published_case(client: TestClient) -> None:
    response = client.post(
        "/api/sessions",
        json={
            "mode": "assessment",
            "scene": "hotline",
            "case_type": "main",
            "case_id": "crisis_student_main",
        },
    )

    assert response.status_code == 201


def test_create_session_rejects_unknown_case(client: TestClient) -> None:
    response = client.post(
        "/api/sessions",
        json={
            "mode": "assessment",
            "scene": "hotline",
            "case_type": "main",
            "case_id": "missing",
        },
    )

    assert response.status_code == 422


def test_create_session_rejects_draft_case(
    client: TestClient,
    monkeypatch,
) -> None:
    from app.api.routes import sessions
    from app.cases.domain import CaseStatus
    from app.cases.loader import CaseRepository

    package = CaseRepository().get("crisis_student_main")
    package.case.status = CaseStatus.draft

    class DraftRepository:
        def get(self, case_id: str):
            assert case_id == package.case.case_id
            return package.model_copy(deep=True)

    monkeypatch.setattr(sessions, "case_repository", DraftRepository())
    response = client.post(
        "/api/sessions",
        json={
            "mode": "assessment",
            "scene": "hotline",
            "case_type": "main",
            "case_id": "crisis_student_main",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "个案尚未发布"


def test_create_session_rejects_case_type_mismatch(client: TestClient) -> None:
    response = client.post(
        "/api/sessions",
        json={
            "mode": "experience",
            "scene": "hotline",
            "case_type": "short",
            "case_id": "crisis_student_main",
        },
    )

    assert response.status_code == 422


def test_create_session_rejects_scene_missing_from_case(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/sessions",
        json={
            "mode": "experience",
            "scene": "online",
            "case_type": "main",
            "case_id": "crisis_student_main",
        },
    )

    assert response.status_code == 422

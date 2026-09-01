from fastapi.testclient import TestClient

from app.api.routes.provider_config import get_runtime_credential_store
from app.cases.loader import CaseRepository
from app.main import app
from app.runtime.provider_check import (
    ProviderCheckItem,
    ProviderCheckResult,
    ProviderCheckStatus,
    ProviderReadinessChecker,
)
from app.runtime_config import RuntimeCredentialStore, realtime_base_url, text_base_url


def test_official_bailian_urls_use_workspace_or_public_endpoint() -> None:
    assert text_base_url("workspace-123") == (
        "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    assert realtime_base_url("workspace-123") == (
        "wss://workspace-123.cn-beijing.maas.aliyuncs.com/api-ws/v1"
    )
    assert text_base_url(None) == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert realtime_base_url(None) == "wss://dashscope.aliyuncs.com/api-ws/v1"


def test_provider_config_masks_key_and_empty_key_keeps_existing_credential() -> None:
    store = RuntimeCredentialStore()
    app.dependency_overrides[get_runtime_credential_store] = lambda: store
    client = TestClient(app)
    secret = "-".join(("test", "masking", "value", "1234"))

    try:
        created = client.put(
            "/api/provider-config",
            json={
                "api_key": secret,
                "workspace_id": "workspace-123",
            },
        )
        assert created.status_code == 200
        assert secret not in created.text
        assert created.json() == {
            "configured": True,
            "masked_key": "••••1234",
            "workspace_id": "workspace-123",
            "report_model": "qwen3.8-max",
            "actor_model": "qwen-plus-character",
            "asr_model": "qwen-audio-3.0-asr-flash-streaming",
            "tts_model": "qwen-audio-3.0-tts-plus",
            "tts_voice": "longanlingxin",
            "report_temperature": 0.1,
            "actor_temperature": 0.75,
            "actor_context_window_tokens": 32768,
            "actor_max_output_tokens": 2048,
        }

        updated = client.put(
            "/api/provider-config",
            json={"api_key": "", "workspace_id": None, "actor_temperature": 0.65},
        )
        assert updated.status_code == 200
        assert updated.json()["configured"] is True
        assert updated.json()["masked_key"] == "••••1234"
        assert updated.json()["workspace_id"] is None
        assert updated.json()["actor_temperature"] == 0.65
        assert store.credentials().api_key == secret

        retrieved = client.get("/api/provider-config")
        assert retrieved.status_code == 200
        assert secret not in retrieved.text
        assert "api_key" not in retrieved.json()
    finally:
        app.dependency_overrides.pop(get_runtime_credential_store, None)


def test_public_configuration_excludes_director_but_keeps_internal_defaults() -> None:
    store = RuntimeCredentialStore()
    report_key = "-".join(("test", "report", "key", "1234"))
    expected_internal_model = "".join(("qwen3", ".7-plus"))
    app.dependency_overrides[get_runtime_credential_store] = lambda: store
    client = TestClient(app)

    try:
        response = client.put(
            "/api/provider-config",
            json={
                "api_key": report_key,
                "report_model": "report-only",
                "report_temperature": 0.08,
            },
        )
    finally:
        app.dependency_overrides.pop(get_runtime_credential_store, None)

    assert response.status_code == 200, response.text
    assert "director_model" not in response.json()
    assert "director_temperature" not in response.json()
    assert response.json()["report_model"] == "report-only"
    assert response.json()["report_temperature"] == 0.08
    credentials = store.credentials()
    assert credentials.director_model == expected_internal_model
    assert credentials.report_model == "report-only"


def test_actor_model_capacity_defaults_and_explicit_limits_are_validated() -> None:
    store = RuntimeCredentialStore()
    capacity_key = "-".join(("test", "capacity", "key", "1234"))
    app.dependency_overrides[get_runtime_credential_store] = lambda: store
    client = TestClient(app)

    try:
        explicit = client.put(
            "/api/provider-config",
            json={
                "api_key": capacity_key,
                "actor_context_window_tokens": 24000,
                "actor_max_output_tokens": 3072,
            },
        )
        assert explicit.status_code == 200, explicit.text
        assert explicit.json()["actor_context_window_tokens"] == 24000
        assert explicit.json()["actor_max_output_tokens"] == 3072

        too_large = client.put(
            "/api/provider-config",
            json={"api_key": "", "actor_max_output_tokens": 4097},
        )
        assert too_large.status_code == 422

        unknown_without_capacity = client.put(
            "/api/provider-config",
            json={"api_key": "", "actor_model": "private-character-model"},
        )
        assert unknown_without_capacity.status_code == 422
        assert store.credentials().actor_model == "qwen-plus-character"
        assert store.credentials().actor_context_window_tokens == 24000

        unknown_with_capacity = client.put(
            "/api/provider-config",
            json={
                "api_key": "",
                "actor_model": "private-character-model",
                "actor_context_window_tokens": 16000,
                "actor_max_output_tokens": 1024,
            },
        )
        assert unknown_with_capacity.status_code == 200, unknown_with_capacity.text
        assert unknown_with_capacity.json()["actor_context_window_tokens"] == 16000
        assert unknown_with_capacity.json()["actor_max_output_tokens"] == 1024

        known_default = client.put(
            "/api/provider-config",
            json={"api_key": "", "actor_model": "qwen-plus-character"},
        )
        assert known_default.status_code == 200, known_default.text
        assert known_default.json()["actor_context_window_tokens"] == 32768
        assert known_default.json()["actor_max_output_tokens"] == 2048
    finally:
        app.dependency_overrides.pop(get_runtime_credential_store, None)


def test_provider_config_rejects_unsafe_workspace_and_never_echoes_invalid_key() -> None:
    client = TestClient(app)
    rejected_key = "test-" + "x" * 600

    unsafe_workspace = client.put(
        "/api/provider-config",
        json={"api_key": "test-safe-key-1234", "workspace_id": "evil.example#"},
    )
    assert unsafe_workspace.status_code == 422

    oversized_key = client.put("/api/provider-config", json={"api_key": rejected_key})
    assert oversized_key.status_code == 422
    assert rejected_key not in oversized_key.text

    wrong_type = client.put("/api/provider-config", json={"api_key": {"value": rejected_key}})
    assert wrong_type.status_code == 422
    assert rejected_key not in wrong_type.text


class FakeProviderChecker:
    requires_speech: bool | None = None

    async def check(self, *, requires_speech: bool) -> ProviderCheckResult:
        type(self).requires_speech = requires_speech
        return ProviderCheckResult(
            actor=ProviderCheckItem(status=ProviderCheckStatus.passed),
            asr=ProviderCheckItem(
                status=ProviderCheckStatus.failed,
                message="实时语音识别暂时无法连接",
            ),
            tts=ProviderCheckItem(status=ProviderCheckStatus.passed),
        )


def test_provider_check_returns_each_real_service_result_without_secret(
    client: TestClient,
) -> None:
    from app.api.routes.provider_config import get_provider_readiness_checker

    app.dependency_overrides[get_provider_readiness_checker] = FakeProviderChecker

    response = client.post(
        "/api/provider-config/check",
        json={"requires_speech": False},
    )

    assert response.status_code == 200
    assert response.json() == {
        "actor": {"status": "passed", "message": None},
        "asr": {
            "status": "failed",
            "message": "实时语音识别暂时无法连接",
        },
        "tts": {"status": "passed", "message": None},
    }
    assert FakeProviderChecker.requires_speech is False
    assert "sk-" not in response.text


async def test_text_readiness_does_not_call_speech_providers() -> None:
    store = RuntimeCredentialStore()
    store.update(api_key="sk-test")
    checker = ProviderReadinessChecker(store)
    calls: list[str] = []

    async def passed(name: str) -> None:
        calls.append(name)

    checker._check_actor = lambda: passed("actor")  # type: ignore[method-assign]
    checker._check_asr = lambda: passed("asr")  # type: ignore[method-assign]
    checker._check_tts = lambda: passed("tts")  # type: ignore[method-assign]

    result = await checker.check(requires_speech=False)

    assert calls == ["actor"]
    assert result.actor.status == ProviderCheckStatus.passed


def test_provider_probe_selects_first_published_hotline_character() -> None:
    package = CaseRepository().get("crisis_student_main")

    class PublishedCases:
        def list_published(self, *, scene=None, case_type=None):
            assert scene == "hotline"
            assert case_type is None
            return [package]

        def get(self, case_id: str):
            raise AssertionError(f"不应硬编码案例标识：{case_id}")

    checker = ProviderReadinessChecker(
        RuntimeCredentialStore(),
        cases=PublishedCases(),  # type: ignore[arg-type]
    )

    selected = checker._probe_character()

    assert selected.case_id == package.case.case_id

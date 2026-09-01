import argparse
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from app import database
from app.runtime.models import (
    CacheMode,
    ModelCallKind,
    ModelCallMetricRecord,
    ModelRole,
    RuntimeFailureRecord,
)
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
from app.simulations.checks import CapturedTurn, CheckResult, StateFrame
from app.simulations.runner import (
    DEFAULT_TURN_TIMEOUT_SECONDS,
    DatabaseEvidence,
    DatabaseSnapshot,
    LiveSimulationProtocol,
    ModelCallEvidence,
    RuntimeFailureEvidence,
    ScenarioRunResult,
    SimulationProtocolError,
    SimulationRunner,
    _run_from_cli,
    _run_selected_scenarios,
    _summary_markdown,
    final_expectation_issues,
    read_database_evidence,
    read_environment,
    runtime_quality_issues,
)
from app.simulations.scenario import (
    FinalExpectation,
    ProbeCard,
    Scenario,
    StateCondition,
    load_scenarios,
)


class FakeSocket:
    def __init__(self, messages: list[str | bytes | Exception]) -> None:
        self.messages = deque(messages)
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def recv(self) -> str | bytes:
        value = self.messages.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, sockets: list[FakeSocket]) -> None:
        self.sockets = deque(sockets)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeSocket:
        self.calls.append((url, headers))
        return self.sockets.popleft()


def _message(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _snapshot(
    *,
    transcript: list[dict[str, object]] | None = None,
    phase: str = "listening",
    can_retry: bool | None = None,
) -> str:
    payload: dict[str, object] = {
        "type": "snapshot",
        "phase": phase,
        "transcript": transcript or [],
        "opening_delay_seconds": None,
        "pending_ending_route_id": None,
    }
    if can_retry is not None:
        payload["can_retry"] = can_retry
    return _message(payload)


def _committed(client_turn_id: str) -> str:
    return _message(
        {
            "type": "turn.committed",
            "client_turn_id": client_turn_id,
            "worker": {
                "id": f"worker-{client_turn_id}",
                "sequence": 1,
                "speaker": "worker",
                "text": "你先慢慢说。",
                "client_turn_id": client_turn_id,
            },
            "client": {
                "id": f"client-{client_turn_id}",
                "sequence": 2,
                "speaker": "client",
                "text": "嗯，你说。",
                "client_turn_id": client_turn_id,
            },
        }
    )


async def test_content_protocol_stops_on_retryable_technical_pause() -> None:
    socket = FakeSocket(
        [
            _snapshot(),
            _message(
                {
                    "type": "technical.pause",
                    "can_retry": True,
                    "failed_phase": "acting",
                    "failure": {
                        "component": "actor",
                        "phase": "acting",
                        "operation": "validate",
                        "failure_code": "actor_output_validation",
                        "error_class": "ActorOutputValidationError",
                        "attempt_count": 2,
                        "retryable": False,
                        "disposition": "technical_pause",
                        "attempts_json": [],
                        "details_json": {"summary": "Actor 返修后仍未通过内容检查"},
                    },
                }
            ),
        ]
    )
    connector = FakeConnector([socket])
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1:8000/api/live-sessions/session-1",
        profile="content",
        connector=connector,
    )

    await protocol.connect()
    with pytest.raises(SimulationProtocolError) as captured:
        await protocol.send_turn("你先慢慢说。", "card-1")

    assert str(captured.value) == "Actor 返修后仍未通过内容检查"
    assert captured.value.failure is not None
    assert captured.value.failure.failure_code == "actor_output_validation"
    assert captured.value.failure.details_json == {
        "summary": "Actor 返修后仍未通过内容检查"
    }

    assert connector.calls == [
        (
            "ws://127.0.0.1:8000/api/live-sessions/session-1",
            {"X-Assessment-Simulation": "content"},
        )
    ]
    assert socket.sent == [
        {"type": "session.start"},
        {"type": "text.turn", "text": "你先慢慢说。", "client_turn_id": "card-1"},
    ]


async def test_protocol_reconnects_once_and_reuses_original_client_turn_id() -> None:
    first = FakeSocket([_snapshot(), OSError("连接断开")])
    second = FakeSocket(
        [
            _snapshot(),
            _committed("stable-id"),
            _message({"type": "phase", "phase": "listening"}),
        ]
    )
    connector = FakeConnector([first, second])
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/session-2",
        profile="content",
        connector=connector,
    )

    await protocol.connect()
    result = await protocol.send_turn("同一句话", "stable-id")

    assert len(connector.calls) == 2
    assert first.closed is True
    assert all(call[1] == {"X-Assessment-Simulation": "content"} for call in connector.calls)
    assert first.sent[1]["client_turn_id"] == "stable-id"
    assert second.sent[1]["client_turn_id"] == "stable-id"
    assert second.sent[-1] == {"type": "playback.ended"}
    assert result.client_turn_id == "stable-id"


async def test_protocol_keeps_world_time_advance_on_the_same_retried_turn() -> None:
    first = FakeSocket([_snapshot(), OSError("连接断开")])
    second = FakeSocket(
        [
            _snapshot(),
            _committed("timed-turn"),
            _message({"type": "phase", "phase": "listening"}),
        ]
    )
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/timed-turn",
        profile="content",
        connector=FakeConnector([first, second]),
    )

    await protocol.connect()
    await protocol.send_turn(
        "再听一下门外的动静。",
        "timed-turn",
        world_time_advance_seconds=960,
    )

    expected = {
        "type": "text.turn",
        "text": "再听一下门外的动静。",
        "client_turn_id": "timed-turn",
        "world_time_advance_seconds": 960,
    }
    assert first.sent[1] == expected
    assert second.sent[1] == expected


async def test_reconnect_stops_when_technical_pause_omits_retry_flag() -> None:
    first = FakeSocket([_snapshot(), OSError("生成期间先断线")])
    second = FakeSocket(
        [
            _snapshot(phase="technical_paused"),
        ]
    )
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/resume-technical",
        profile="content",
        connector=FakeConnector([first, second]),
    )

    await protocol.connect()
    with pytest.raises(
        SimulationProtocolError,
        match="来访者回复进入技术暂停，已停止当前场景",
    ):
        await protocol.send_turn("继续刚才那轮。", "resume-technical")

    assert second.sent == []


async def test_reconnect_does_not_retry_non_retryable_technical_pause() -> None:
    first = FakeSocket([_snapshot(), OSError("生成期间先断线")])
    second = FakeSocket(
        [
            _snapshot(phase="technical_paused", can_retry=False),
        ]
    )
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/non-retryable-reconnect",
        profile="content",
        connector=FakeConnector([first, second]),
    )

    await protocol.connect()
    with pytest.raises(
        SimulationProtocolError,
        match="来访者回复进入技术暂停，已停止当前场景",
    ):
        await protocol.send_turn("继续刚才那轮。", "non-retryable-reconnect")

    assert {"type": "technical.retry"} not in second.sent


async def test_reconnect_uses_committed_snapshot_pair_without_resending_turn() -> None:
    committed_payload = json.loads(_committed("already-committed"))
    transcript = [committed_payload["worker"], committed_payload["client"]]
    first = FakeSocket([_snapshot(), OSError("提交后的确认消息丢失")])
    second = FakeSocket(
        [
            _snapshot(transcript=transcript, phase="playing"),
            _message({"type": "phase", "phase": "listening"}),
        ]
    )
    connector = FakeConnector([first, second])
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/committed-reconnect",
        profile="content",
        connector=connector,
    )

    await protocol.connect()
    result = await protocol.send_turn("同一句话", "already-committed")

    assert second.sent == [
        {"type": "session.start"},
        {"type": "playback.ended"},
    ]
    assert result.client_turn_id == "already-committed"
    assert result.final_phase == "listening"
    assert [turn.client_turn_id for turn in protocol.ws_transcript] == [
        "already-committed",
        "already-committed",
    ]


async def test_reconnect_recovers_committed_turn_when_ended_session_is_first_message() -> None:
    first = FakeSocket(
        [
            _snapshot(),
            _committed("natural-close"),
            OSError("自然结束确认前连接关闭"),
        ]
    )
    ended = _message({"type": "session.ended", "reason": "natural_closure"})
    second = FakeSocket([ended])
    connector = FakeConnector([first, second])
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/natural-close",
        profile="content",
        connector=connector,
    )

    await protocol.connect()
    result = await protocol.send_turn("我们这次聊天先到这里。", "natural-close")

    assert len(connector.calls) == 2
    assert first.closed is True
    assert second.sent == []
    assert result.client_turn_id == "natural-close"
    assert result.final_phase == "ended"
    assert result.ended_reason == "natural_closure"
    assert result.messages == [json.loads(ended)]
    assert [turn.client_turn_id for turn in protocol.ws_transcript] == [
        "natural-close",
        "natural-close",
    ]


async def test_reconnect_still_rejects_other_non_snapshot_first_messages() -> None:
    first = FakeSocket([_snapshot(), OSError("连接断开")])
    second = FakeSocket([_message({"type": "phase", "phase": "listening"})])
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/invalid-first-message",
        profile="content",
        connector=FakeConnector([first, second]),
    )

    await protocol.connect()
    with pytest.raises(
        SimulationProtocolError,
        match="WebSocket 首条消息不是会话快照",
    ):
        await protocol.send_turn("继续刚才那轮。", "invalid-first-message")


async def test_reconnect_does_not_treat_ended_session_as_an_uncommitted_turn() -> None:
    first = FakeSocket([_snapshot(), OSError("提交前连接断开")])
    second = FakeSocket(
        [_message({"type": "session.ended", "reason": "natural_closure"})]
    )
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/ended-before-current-commit",
        profile="content",
        connector=FakeConnector([first, second]),
    )

    await protocol.connect()
    with pytest.raises(
        SimulationProtocolError,
        match="会话在当前探针提交前已经结束",
    ):
        await protocol.send_turn("这轮还没有提交。", "missing-current-turn")


async def test_protocol_accepts_interleaved_voice_binary_before_commit() -> None:
    socket = FakeSocket(
        [
            _snapshot(),
            _message({"type": "visitor.text", "text": "嗯，你说。"}),
            b"pcm-1",
            b"pcm-2",
            _committed("voice-1"),
            _message({"type": "phase", "phase": "listening"}),
        ]
    )
    connector = FakeConnector([socket])
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/voice",
        profile="voice",
        connector=connector,
    )

    await protocol.connect()
    result = await protocol.send_turn("能听见吗？", "voice-1")

    assert connector.calls[0][1] == {"X-Assessment-Simulation": "voice"}
    assert result.binary_chunk_count == 2
    assert socket.sent[-1] == {"type": "playback.ended"}


async def test_new_session_waits_for_opening_commit_before_sending_first_card() -> None:
    opening = json.loads(_committed("opening-generated"))
    opening.pop("worker")
    opening["client"]["sequence"] = 1
    socket = FakeSocket(
        [
            _message(
                {
                    "type": "snapshot",
                    "phase": "listening",
                    "transcript": [],
                    "opening_delay_seconds": 5,
                    "pending_ending_route_id": None,
                }
            ),
            b"opening-pcm",
            _message(opening),
            _message({"type": "phase", "phase": "listening"}),
            _committed("N1"),
            _message({"type": "phase", "phase": "listening"}),
        ]
    )
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/opening",
        profile="voice",
        connector=FakeConnector([socket]),
    )

    await protocol.connect()
    opening_result = await protocol.ensure_opening()
    await protocol.send_turn("你好。", "N1")

    assert opening_result is not None
    assert socket.sent == [
        {"type": "session.start"},
        {"type": "playback.ended"},
        {"type": "text.turn", "text": "你好。", "client_turn_id": "N1"},
        {"type": "playback.ended"},
    ]
    assert protocol.total_binary_chunks == 1
    assert [turn.sequence for turn in protocol.ws_transcript] == [1, 1, 2]


async def test_existing_transcript_does_not_wait_for_another_opening() -> None:
    old_client = {
        "id": "old-client",
        "sequence": 1,
        "speaker": "client",
        "text": "喂，你好。",
        "client_turn_id": "opening-old",
    }
    socket = FakeSocket(
        [
            _snapshot(transcript=[old_client]),
            _committed("N1"),
            _message({"type": "phase", "phase": "listening"}),
        ]
    )
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/existing",
        profile="content",
        connector=FakeConnector([socket]),
    )

    await protocol.connect()
    assert await protocol.ensure_opening() is None
    await protocol.send_turn("你好。", "N1")

    assert socket.sent[1]["type"] == "text.turn"


async def test_protocol_explains_when_server_forbids_technical_retry() -> None:
    socket = FakeSocket(
        [
            _snapshot(),
            _message({"type": "technical.pause", "can_retry": False}),
        ]
    )
    protocol = LiveSimulationProtocol(
        ws_url="ws://127.0.0.1/live/non-retryable",
        profile="content",
        connector=FakeConnector([socket]),
    )

    await protocol.connect()
    with pytest.raises(
        SimulationProtocolError,
        match="来访者回复进入技术暂停，已停止当前场景",
    ):
        await protocol.send_turn("还能听见吗？", "non-retryable")

    assert {"type": "technical.retry"} not in socket.sent


def test_runner_defaults_to_120_second_turn_timeout_and_runtime_database_engine(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database, "engine", test_engine)

    runner = SimulationRunner(base_url="http://127.0.0.1:8000", output_root=tmp_path)

    assert DEFAULT_TURN_TIMEOUT_SECONDS == 120
    assert runner.engine is test_engine


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeHttpClient:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def get(self, path: str) -> FakeResponse:
        self.paths.append(path)
        if path == "/api/health":
            return FakeResponse({"status": "ready", "service": "psych-assessment-demo"})
        return FakeResponse({"configured": False, "masked_key": None})


async def test_environment_check_reads_only_health_and_configured_flag() -> None:
    client = FakeHttpClient()

    status = await read_environment(client)  # type: ignore[arg-type]

    assert client.paths == ["/api/health", "/api/provider-config"]
    assert status.healthy is True
    assert status.configured is False
    assert status.model_dump() == {"healthy": True, "configured": False}


async def test_check_only_validates_cards_without_opening_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForbiddenClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("CheckOnly 不应访问后端或调用模型")

    monkeypatch.setattr("app.simulations.runner.httpx.AsyncClient", ForbiddenClient)

    exit_code = await _run_from_cli(
        argparse.Namespace(
            suite="normal",
            case_id="crisis_student_main",
            scene="hotline",
            catalog=None,
            check_only=True,
            base_url="http://127.0.0.1:8000",
            output_root=None,
        )
    )

    assert exit_code == 0


async def test_runner_creates_the_requested_case_and_scene_session(
    tmp_path: Path,
) -> None:
    client = ScenarioHttpClient()
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        case_id="marriage_boundary_main",
        scene=Scene.online,
    )

    session_id = await runner._create_session(client)

    assert session_id == "session-sim"
    assert client.posts == [
        (
            "/api/sessions",
            {
                "mode": "experience",
                "scene": "online",
                "case_type": "main",
                "case_id": "marriage_boundary_main",
            },
        )
    ]
    scenario = load_scenarios()["marriage_opening"]
    assert runner.profile_for_scenario(scenario) == "content"


async def test_check_only_reads_an_explicit_catalog_without_calling_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "custom-catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "scenarios": [
                    {
                        "scenario_id": "custom-opening",
                        "title": "自定义开场",
                        "profile": "content",
                        "cards": [],
                        "end_after_cards": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class ForbiddenClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("CheckOnly 不应访问后端或调用模型")

    monkeypatch.setattr("app.simulations.runner.httpx.AsyncClient", ForbiddenClient)
    exit_code = await _run_from_cli(
        argparse.Namespace(
            suite="custom-opening",
            case_id="crisis_student_main",
            scene="hotline",
            catalog=catalog,
            check_only=True,
            base_url="http://127.0.0.1:8000",
            output_root=None,
        )
    )

    assert exit_code == 0


async def test_selected_scenarios_stop_after_first_failed_run() -> None:
    scenarios = load_scenarios()

    class PartiallyFailingRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run_scenario(
            self,
            scenario: Scenario,
            _api: object,
        ) -> ScenarioRunResult:
            self.calls.append(scenario.scenario_id)
            if scenario.scenario_id == "normal":
                raise SimulationProtocolError("来访者连接中断")
            return ScenarioRunResult(
                scenario_id=scenario.scenario_id,
                title=scenario.title,
                profile=scenario.profile,
                session_id="session-ok",
                passed=True,
                cards=[],
                checks=[],
                final_issues=[],
                final_snapshot=DatabaseSnapshot(
                    status="ended",
                    end_reason="user_ended",
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                ),
            )

    runner = PartiallyFailingRunner()
    selected = [scenarios["normal"], scenarios["chaotic"]]

    results = await _run_selected_scenarios(  # type: ignore[arg-type]
        runner,
        selected,
        object(),  # type: ignore[arg-type]
    )

    assert runner.calls == ["normal"]
    assert [result.passed for result in results] == [False]
    assert results[0].final_snapshot.status == "run_failed"
    assert results[0].final_issues == ["运行中断：来访者连接中断"]
    assert results[0].run_status == "failed"
    assert results[0].checks_status == "not_run"
    assert results[0].expectations_status == "not_evaluated"
    assert results[0].runtime_failures[0].component == "simulation"


async def test_selected_scenarios_stop_after_more_than_one_repair_call() -> None:
    scenarios = load_scenarios()

    class RepairingRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run_scenario(
            self,
            scenario: Scenario,
            _api: object,
        ) -> ScenarioRunResult:
            self.calls.append(scenario.scenario_id)
            metric = ModelCallEvidence(
                client_turn_id=f"turn-{scenario.scenario_id}",
                model_role="actor",
                model_name="qwen-plus-character",
                call_kind="repair",
                cache_mode="character_session",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cached_tokens=0,
                latency_ms=500,
                success=True,
            )
            return ScenarioRunResult(
                scenario_id=scenario.scenario_id,
                title=scenario.title,
                profile=scenario.profile,
                session_id=f"session-{scenario.scenario_id}",
                passed=True,
                cards=[],
                checks=[],
                final_issues=[],
                final_snapshot=DatabaseSnapshot(
                    status="ended",
                    end_reason="user_ended",
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                ),
                model_call_metrics=[metric],
            )

    runner = RepairingRunner()
    results = await _run_selected_scenarios(  # type: ignore[arg-type]
        runner,
        [scenarios["opening"], scenarios["entry"], scenarios["direct_jump"]],
        object(),  # type: ignore[arg-type]
    )

    assert runner.calls == ["opening", "entry"]
    assert [result.passed for result in results] == [True, False]
    assert results[1].final_issues == ["整套黑盒测评的模型返修累计超过一次"]


async def test_objective_suites_do_not_turn_repair_count_into_a_quality_score() -> None:
    scenarios = load_scenarios()

    class ObjectiveRunner:
        async def run_scenario(
            self,
            scenario: Scenario,
            _api: object,
        ) -> ScenarioRunResult:
            return ScenarioRunResult(
                scenario_id=scenario.scenario_id,
                title=scenario.title,
                profile="content",
                case_id=scenario.case_id,
                scene=Scene.online,
                session_id=f"session-{scenario.scenario_id}",
                passed=True,
                cards=[],
                checks=[],
                final_issues=[],
                final_snapshot=DatabaseSnapshot(
                    status="ended",
                    end_reason="user_ended",
                    scene=Scene.online,
                    engine="character_prompt",
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                    phase="ended",
                ),
                model_call_metrics=[
                    ModelCallEvidence(
                        model_role="actor",
                        model_name="qwen-plus-character",
                        call_kind="repair",
                        cache_mode="character_session",
                        prompt_tokens=10,
                        completion_tokens=5,
                        total_tokens=15,
                        cached_tokens=0,
                        latency_ms=500,
                        success=True,
                    )
                ],
            )

    selected = [
        scenarios["marriage_opening"],
        scenarios["marriage_direct_conclusion"],
    ]
    results = await _run_selected_scenarios(  # type: ignore[arg-type]
        ObjectiveRunner(),
        selected,
        object(),  # type: ignore[arg-type]
    )

    assert len(results) == 2
    assert all(result.passed for result in results)
    assert all(result.final_issues == [] for result in results)


def test_runtime_quality_rejects_extra_repairs_and_slow_model_tail() -> None:
    metrics = [
        ModelCallEvidence(
            client_turn_id="turn-director",
            model_role="director",
            model_name="qwen3.7-plus",
            call_kind="initial",
            cache_mode="explicit",
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            cached_tokens=50,
            latency_ms=5_000,
            success=True,
        ),
        ModelCallEvidence(
            client_turn_id="turn-actor",
            model_role="actor",
            model_name="qwen-plus-character",
            call_kind="repair",
            cache_mode="character_session",
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            cached_tokens=50,
            latency_ms=21_000,
            success=True,
        ),
        ModelCallEvidence(
            client_turn_id="turn-actor",
            model_role="actor",
            model_name="qwen-plus-character",
            call_kind="repair",
            cache_mode="character_session",
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            cached_tokens=50,
            latency_ms=16_000,
            success=True,
        ),
    ]

    issues = runtime_quality_issues(
        metrics,
        expected_turn_ids_by_role={
            "director": ["turn-director"],
            "actor": ["turn-actor"],
        },
    )

    assert "模型返修超过一次" in issues
    assert "Actor 初次调用 0，预期 1（另有返修 2）" in issues
    assert "Actor 话轮指标不完整：turn-actor=0" in issues
    assert "模型单次调用超过 20 秒" in issues
    assert "模型调用耗时 P90 超过 15 秒" in issues


def test_runtime_quality_rejects_missing_metrics_and_counts_failed_latency() -> None:
    assert runtime_quality_issues(
        [],
        expected_turn_ids_by_role={"director": ["turn-1"], "actor": ["turn-1"]},
    ) == [
        "Director 初次调用 0，预期 1（另有返修 0）",
        "Director 话轮指标不完整：turn-1=0",
        "Actor 初次调用 0，预期 1（另有返修 0）",
        "Actor 话轮指标不完整：turn-1=0",
    ]

    failed = ModelCallEvidence(
        client_turn_id="turn-1",
        model_role="director",
        model_name="qwen3.7-plus",
        call_kind="initial",
        cache_mode="explicit",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cached_tokens=0,
        latency_ms=21_000,
        success=False,
    )
    issues = runtime_quality_issues(
        [failed],
        expected_turn_ids_by_role={"director": ["turn-1"]},
    )

    assert "模型单次调用超过 20 秒" in issues
    assert "模型调用耗时 P90 超过 15 秒" in issues


def test_character_runtime_quality_rejects_director_repair_calls() -> None:
    metrics = [
        ModelCallEvidence(
            client_turn_id="character-turn-1",
            model_role="actor",
            model_name="qwen-plus-character",
            call_kind="initial",
            cache_mode="character_session",
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            cached_tokens=50,
            latency_ms=800,
            success=True,
        ),
        ModelCallEvidence(
            client_turn_id="character-turn-1",
            model_role="director",
            model_name="qwen3.7-plus",
            call_kind="repair",
            cache_mode="explicit",
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            cached_tokens=50,
            latency_ms=900,
            success=True,
        ),
    ]

    issues = runtime_quality_issues(
        metrics,
        expected_turn_ids_by_role={
            "director": [],
            "actor": ["character-turn-1"],
        },
    )

    assert "Director 调用 1，预期 0" in issues


def test_character_runtime_quality_excludes_cold_opening_from_realtime_latency() -> None:
    metrics = [
        ModelCallEvidence(
            client_turn_id="opening-character",
            model_role="actor",
            model_name="qwen-plus-character",
            call_kind="initial",
            cache_mode="character_session",
            prompt_tokens=2_000,
            completion_tokens=40,
            total_tokens=2_040,
            cached_tokens=0,
            latency_ms=15_662,
            success=True,
        ),
        ModelCallEvidence(
            client_turn_id="probe-1",
            model_role="actor",
            model_name="qwen-plus-character",
            call_kind="initial",
            cache_mode="character_session",
            prompt_tokens=2_000,
            completion_tokens=40,
            total_tokens=2_040,
            cached_tokens=1_700,
            latency_ms=3_054,
            success=True,
        ),
    ]

    issues = runtime_quality_issues(
        metrics,
        expected_turn_ids_by_role={
            "director": [],
            "actor": ["opening-character", "probe-1"],
        },
        runtime_engine="character_prompt",
    )

    assert "模型调用耗时 P90 超过 15 秒" not in issues
    assert "模型单次调用超过 20 秒" not in issues


def test_character_runtime_quality_still_rejects_slow_realtime_turn() -> None:
    metrics = [
        ModelCallEvidence(
            client_turn_id="opening-character",
            model_role="actor",
            model_name="qwen-plus-character",
            call_kind="initial",
            cache_mode="character_session",
            prompt_tokens=2_000,
            completion_tokens=40,
            total_tokens=2_040,
            cached_tokens=0,
            latency_ms=15_662,
            success=True,
        ),
        ModelCallEvidence(
            client_turn_id="probe-1",
            model_role="actor",
            model_name="qwen-plus-character",
            call_kind="initial",
            cache_mode="character_session",
            prompt_tokens=2_000,
            completion_tokens=40,
            total_tokens=2_040,
            cached_tokens=1_700,
            latency_ms=21_000,
            success=True,
        ),
    ]

    issues = runtime_quality_issues(
        metrics,
        expected_turn_ids_by_role={
            "director": [],
            "actor": ["opening-character", "probe-1"],
        },
        runtime_engine="character_prompt",
    )

    assert "模型单次调用超过 20 秒" in issues
    assert "模型调用耗时 P90 超过 15 秒" in issues


def test_workflow_runtime_quality_keeps_opening_in_latency_gate() -> None:
    opening = ModelCallEvidence(
        client_turn_id="opening-workflow",
        model_role="actor",
        model_name="qwen-plus-character",
        call_kind="initial",
        cache_mode="character_session",
        prompt_tokens=2_000,
        completion_tokens=40,
        total_tokens=2_040,
        cached_tokens=0,
        latency_ms=21_000,
        success=True,
    )

    issues = runtime_quality_issues(
        [opening],
        expected_turn_ids_by_role={"actor": ["opening-workflow"]},
        runtime_engine="workflow",
    )

    assert "模型单次调用超过 20 秒" in issues
    assert "模型调用耗时 P90 超过 15 秒" in issues


def test_final_expectations_cover_story_end_and_repair_state() -> None:
    scenarios = load_scenarios()
    incomplete = DatabaseSnapshot(
        status="active",
        end_reason=None,
        fact_depths={"presenting_concern": 1},
        event_ids=[],
        ending_route_id=None,
        interaction_tension=0,
        repair_stage="none",
    )
    repaired = DatabaseSnapshot(
        status="ended",
        end_reason="user_ended",
        fact_depths={"suicidal_ideation": 1},
        event_ids=[],
        ending_route_id=None,
        interaction_tension=1,
        repair_stage="repairing",
    )

    normal_issues = final_expectation_issues(scenarios["normal"], incomplete)

    assert any("会话仍处于活动状态" in issue for issue in normal_issues)
    assert any("故事事件缺失" in issue for issue in normal_issues)
    assert final_expectation_issues(scenarios["repair"], repaired) == []


def test_summary_shows_probe_case_progress_without_repeating_existing_facts() -> None:
    result = ScenarioRunResult(
        scenario_id="progress",
        title="推进摘要测试",
        profile="content",
        session_id="session-progress",
        passed=True,
        cards=[],
        checks=[],
        final_issues=[],
        final_snapshot=DatabaseSnapshot(
            status="ended",
            end_reason="natural_closure",
            conversation_stage="closing",
            ending_route_id="collaborative_close",
            interaction_tension=1,
            repair_stage="none",
            willingness_to_continue=2,
        ),
        state_frames=[
            StateFrame(
                conversation_stage="opening",
                fact_depths={"presenting_concern": 0},
                event_ids=[],
                interaction_tension=0,
                willingness_to_continue=3,
            ),
            StateFrame(
                card_id="N1",
                conversation_stage="exploration",
                fact_depths={"presenting_concern": 1},
                event_ids=["first_contact_tang_ting"],
                interaction_tension=0,
                willingness_to_continue=3,
                interaction_impact="neutral",
                repair_stage="none",
            ),
            StateFrame(
                card_id="N2",
                conversation_stage="risk_assessment",
                fact_depths={"presenting_concern": 1, "job_loss": 1},
                event_ids=["first_contact_tang_ting", "second_contact_tang_ting"],
                interaction_tension=1,
                willingness_to_continue=2,
                interaction_impact="supportive",
                repair_stage="window",
            ),
            StateFrame(
                card_id="N2",
                conversation_stage="risk_assessment",
                fact_depths={"presenting_concern": 1, "job_loss": 1},
                event_ids=["first_contact_tang_ting", "second_contact_tang_ting"],
                interaction_tension=1,
                willingness_to_continue=2,
                interaction_impact="supportive",
                repair_stage="window",
            ),
        ],
        transcript=[
            CapturedTurn(
                sequence=1,
                client_turn_id="sim-progress-N1",
                speaker="worker",
                text="你今晚为什么打来？",
                signals={
                    "turn_plan": {
                        "allowed_fact_depths": {"presenting_concern": 1}
                    }
                },
            ),
            CapturedTurn(
                sequence=2,
                client_turn_id="sim-progress-N1",
                speaker="client",
                text="这几天没睡好。",
                signals={},
            ),
        ],
    )

    summary = _summary_markdown([result])
    progress_lines = [
        line for line in summary.splitlines() if line.startswith("- N")
    ]

    assert "### 探针案例推进" in summary
    assert (
        "最终状态 ended；案例阶段 closing；结束原因 natural_closure；"
        "结束路线 collaborative_close"
    ) in summary
    assert len(progress_lines) == 3
    assert "presenting_concern:1" in progress_lines[0]
    assert "案例阶段 opening → exploration" in progress_lines[0]
    assert "first_contact_tang_ting" in progress_lines[0]
    assert "互动影响 neutral" in progress_lines[0]
    assert "紧张度 0" in progress_lines[0]
    assert "继续意愿 3" in progress_lines[0]
    assert "修复阶段 none" in progress_lines[0]
    assert "job_loss:1" in progress_lines[1]
    assert "案例阶段 exploration → risk_assessment" in progress_lines[1]
    assert "presenting_concern:1" not in progress_lines[1]
    assert "second_contact_tang_ting" in progress_lines[1]
    assert "互动影响 supportive" in progress_lines[1]
    assert "紧张度 1" in progress_lines[1]
    assert "继续意愿 2" in progress_lines[1]
    assert "修复阶段 window" in progress_lines[1]
    assert "新增/加深事实 无" in progress_lines[2]
    assert "案例阶段 risk_assessment" in progress_lines[2]
    assert "【本轮许可 presenting_concern:1】" in summary
    assert "这几天没睡好。" in summary
    assert "事实登记" not in summary
    assert "新事件 无" in progress_lines[2]


def test_summary_separates_run_checks_and_final_expectations() -> None:
    result = ScenarioRunResult(
        scenario_id="interrupted",
        title="中断摘要",
        profile="content",
        session_id="session-interrupted",
        passed=False,
        run_status="failed",
        checks_status="not_run",
        expectations_status="not_evaluated",
        cards=[],
        checks=[],
        final_issues=["运行中断：明确的失败摘要"],
        runtime_failures=[
            RuntimeFailureEvidence(
                id="failure-summary",
                session_id="session-interrupted",
                client_turn_id="turn-failed",
                component="director",
                phase="directing",
                operation="workflow_validation",
                failure_code="director.workflow_validation",
                error_class="WorkflowDecisionError",
                attempt_count=2,
                retryable=False,
                disposition="technical_pause",
                attempts_json=[
                    {
                        "index": 1,
                        "message": "回应事项引用原话不存在",
                    },
                    {
                        "index": 2,
                        "message": "返修后仍引用了不存在的原话",
                    },
                ],
                details_json={"summary": "Director 两次决策均未通过 Workflow"},
            )
        ],
        final_snapshot=DatabaseSnapshot(
            status="active",
            end_reason=None,
            ending_route_id=None,
            interaction_tension=0,
            repair_stage="none",
            phase="technical_paused",
        ),
    )

    summary = _summary_markdown([result])

    assert "- 运行状态：失败" in summary
    assert "- 自动契约检查：未执行" in summary
    assert "- 最终预期：未评估" in summary
    assert "### 运行失败记录" in summary
    assert "director.workflow_validation" in summary
    assert "technical_pause；2 次尝试" in summary
    assert "Director 两次决策均未通过 Workflow" in summary
    assert "返修后仍引用了不存在的原话" in summary


class FakeScenarioProtocol:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.world_time_advances: list[int] = []
        self.opening_calls = 0
        self.end_calls = 0
        self.ws_transcript: list[object] = []
        self.total_binary_chunks = 0

    async def connect(self) -> dict[str, object]:
        return {"type": "snapshot", "transcript": [], "opening_delay_seconds": 5}

    async def ensure_opening(self) -> None:
        self.opening_calls += 1
        return None

    async def send_turn(
        self,
        text: str,
        client_turn_id: str,
        *,
        world_time_advance_seconds: int = 0,
    ) -> object:
        self.sent.append((text, client_turn_id))
        self.world_time_advances.append(world_time_advance_seconds)
        return object()

    async def end_session(self) -> str:
        self.end_calls += 1
        return "user_ended"

    async def close(self) -> None:
        return None


class FailingOpeningProtocol(FakeScenarioProtocol):
    async def ensure_opening(self) -> None:
        raise SimulationProtocolError("来访者开场未生成")


class FailingOpeningAndCloseProtocol(FailingOpeningProtocol):
    async def close(self) -> None:
        raise OSError("关闭时连接已经断开")


class ScenarioHttpClient(FakeHttpClient):
    def __init__(self) -> None:
        super().__init__()
        self.posts: list[tuple[str, dict[str, object]]] = []

    async def post(self, path: str, *, json: dict[str, object]) -> FakeResponse:
        self.posts.append((path, json))
        return FakeResponse({"id": "session-sim"})


class TranscriptScenarioHttpClient(ScenarioHttpClient):
    def __init__(self, transcript: list[CapturedTurn]) -> None:
        super().__init__()
        self.transcript = transcript

    async def get(self, path: str) -> FakeResponse:
        if path == "/api/sessions/session-sim":
            return FakeResponse(
                {
                    "transcript": [
                        {
                            "sequence": turn.sequence,
                            "client_turn_id": turn.client_turn_id,
                            "speaker": turn.speaker,
                            "text": turn.text,
                            "signals_json": turn.signals,
                            "audio_available": turn.audio_available,
                        }
                        for turn in self.transcript
                    ]
                }
            )
        return await super().get(path)


class FailingCardProtocol(FakeScenarioProtocol):
    def __init__(self, failure: RuntimeFailureEvidence) -> None:
        super().__init__()
        self.failure = failure

    async def send_turn(
        self,
        text: str,
        client_turn_id: str,
        *,
        world_time_advance_seconds: int = 0,
    ) -> object:
        self.sent.append((text, client_turn_id))
        self.world_time_advances.append(world_time_advance_seconds)
        raise SimulationProtocolError(
            "Actor 返修后仍未通过内容检查",
            failure=self.failure,
        )


class FailingCleanupClient(ScenarioHttpClient):
    async def post(self, path: str, *, json: dict[str, object]) -> FakeResponse:
        self.posts.append((path, json))
        if path.endswith("/end"):
            raise OSError("收尾接口不可用")
        return FakeResponse({"id": "session-sim"})


class FailingRestClient(ScenarioHttpClient):
    async def get(self, path: str) -> FakeResponse:
        raise OSError(f"REST 读取失败：{path}")


async def test_runner_marks_sent_card_interrupted_with_attempt_and_elapsed_time(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(
        scenario_id="interrupted-card",
        title="中断卡",
        profile="content",
        cards=[
            ProbeCard(
                card_id="I1",
                text="发送后中断。",
                expect=StateCondition(fact_depths={"target": 1}),
            )
        ],
        final_expect=FinalExpectation(fact_depths={"target": 1}),
    )
    failure = RuntimeFailureEvidence(
        id="failure-actor",
        session_id="session-sim",
        client_turn_id="sim-interrupted-card-I1",
        component="actor",
        phase="acting",
        operation="validate",
        failure_code="actor_output_validation",
        error_class="ActorOutputValidationError",
        attempt_count=2,
        retryable=False,
        disposition="technical_pause",
        attempts_json=[{"attempt": 1}, {"attempt": 2}],
        details_json={"summary": "Actor 返修后仍未通过内容检查"},
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    protocol = FailingCardProtocol(failure)
    before = DatabaseEvidence(
        snapshot=_database_snapshot(status="active", depth=0)
    )
    after = DatabaseEvidence(
        snapshot=DatabaseSnapshot(
            **before.snapshot.model_dump(exclude={"phase"}),
            phase="technical_paused",
        ),
        runtime_failures=[failure],
    )
    observations = deque([before, after])
    clock = iter([10.0, 10.125])
    monkeypatch.setattr("app.simulations.runner.perf_counter", lambda: next(clock))
    client = ScenarioHttpClient()
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    result = await runner.run_scenario(scenario, client)  # type: ignore[arg-type]

    assert result.cards[0].status == "interrupted"
    assert result.cards[0].attempts == 1
    assert result.cards[0].retry_used is False
    assert result.cards[0].attempt_elapsed_ms == [125]
    assert result.final_issues == ["运行中断：Actor 返修后仍未通过内容检查"]
    assert result.runtime_failures == [failure]
    assert client.posts[-1] == (
        "/api/sessions/session-sim/end",
        {"reason": "technical_interruption"},
    )


async def test_runner_marks_card_interrupted_when_post_send_observation_fails(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(
        scenario_id="observation-interrupted",
        title="观察中断卡",
        profile="content",
        cards=[
            ProbeCard(
                card_id="I2",
                text="已经发出。",
                expect=StateCondition(fact_depths={"target": 1}),
            )
        ],
        final_expect=FinalExpectation(),
    )
    evidence = DatabaseEvidence(
        snapshot=_database_snapshot(status="active", depth=0)
    )
    observations: deque[DatabaseEvidence | Exception] = deque(
        [evidence, OSError("数据库观察失败"), evidence]
    )

    def observe(_engine: Engine, _session_id: str) -> DatabaseEvidence:
        value = observations.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    clock = iter([20.0, 20.075])
    monkeypatch.setattr("app.simulations.runner.perf_counter", lambda: next(clock))
    protocol = FakeScenarioProtocol()
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=observe,
    )

    result = await runner.run_scenario(  # type: ignore[arg-type]
        scenario,
        ScenarioHttpClient(),
    )

    assert protocol.sent == [("已经发出。", "sim-observation-interrupted-I2")]
    assert result.cards[0].status == "interrupted"
    assert result.cards[0].attempts == 1
    assert result.cards[0].attempt_elapsed_ms == [75]
    assert result.runtime_failures[0].operation == "card_turn"


async def test_cleanup_failure_does_not_replace_original_interruption(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = load_scenarios()["opening"]
    evidence = DatabaseEvidence(
        snapshot=DatabaseSnapshot(
            status="active",
            end_reason=None,
            ending_route_id=None,
            interaction_tension=0,
            repair_stage="none",
            phase="technical_paused",
        )
    )
    client = FailingCleanupClient()
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(
            object, FailingOpeningProtocol()
        ),
        observer=lambda _engine, _session_id: evidence,
    )

    result = await runner.run_scenario(scenario, client)  # type: ignore[arg-type]

    assert result.final_issues == ["运行中断：来访者开场未生成"]
    assert result.runtime_failures[0].failure_code == "simulation_protocol"
    assert len(result.runtime_failures) == 2
    assert result.runtime_failures[1].operation == "session_cleanup"
    assert result.runtime_failures[1].failure_code == "simulation_cleanup"
    assert client.posts[-1][0] == "/api/sessions/session-sim/end"


async def test_rest_failure_is_reported_as_simulation_runtime_failure(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        scenario_id="rest-failure",
        title="REST 失败",
        profile="content",
        cards=[],
        final_expect=FinalExpectation(),
    )
    observations = deque(
        [
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="ended", depth=0)),
        ]
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(
            object, FakeScenarioProtocol()
        ),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    result = await runner.run_scenario(  # type: ignore[arg-type]
        scenario,
        FailingRestClient(),
    )

    assert result.run_status == "failed"
    assert result.checks_status == "not_run"
    assert result.expectations_status == "not_evaluated"
    assert result.runtime_failures[0].component == "simulation"
    assert result.runtime_failures[0].operation == "rest_transcript"
    assert result.runtime_failures[0].error_class == "OSError"


async def test_check_failure_is_reported_as_simulation_runtime_failure(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(
        scenario_id="check-failure",
        title="检查失败",
        profile="content",
        cards=[],
        final_expect=FinalExpectation(),
    )
    observations = deque(
        [
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="ended", depth=0)),
        ]
    )

    def fail_checks(*_args: object, **_kwargs: object) -> object:
        raise ValueError("检查输入结构损坏")

    monkeypatch.setattr("app.simulations.runner.run_automatic_checks", fail_checks)
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(
            object, FakeScenarioProtocol()
        ),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    result = await runner.run_scenario(  # type: ignore[arg-type]
        scenario,
        ScenarioHttpClient(),
    )

    assert result.run_status == "failed"
    assert result.checks_status == "failed"
    assert result.runtime_failures[0].component == "simulation"
    assert result.runtime_failures[0].operation == "automatic_checks"
    assert result.runtime_failures[0].error_class == "ValueError"


async def test_runner_forwards_case_fact_contradiction_cues(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(
        scenario_id="fact-contradiction-probe",
        title="事实矛盾探针",
        profile="content",
        cards=[],
        final_expect=FinalExpectation(),
    )
    observations = deque(
        [
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="ended", depth=0)),
        ]
    )
    captured: dict[str, object] = {}

    def capture_checks(*_args: object, **kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "app.simulations.runner.run_automatic_checks",
        capture_checks,
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(
            object, FakeScenarioProtocol()
        ),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    await runner.run_scenario(  # type: ignore[arg-type]
        scenario,
        ScenarioHttpClient(),
    )

    cues = captured["fact_contradiction_cues"]
    assert isinstance(cues, dict)
    assert "job_loss" in cues
    assert any(cue["id"] == "deny_job_loss" for cue in cues["job_loss"])


async def test_marriage_runner_uses_character_boundaries_and_writes_manual_review(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    privacy_question = "这些聊天以后谁能看到？"
    transcript = [
        CapturedTurn(
            sequence=1,
            client_turn_id="opening-marriage",
            speaker="client",
            text=f"你好，我想问个事\n\n是我老公的\n\n{privacy_question}",
        )
    ]
    observations = deque(
        [
            DatabaseEvidence(
                snapshot=DatabaseSnapshot(
                    status="active",
                    end_reason=None,
                    scene=Scene.online,
                    engine="character_prompt",
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                    phase="listening",
                ),
                transcript=transcript,
            ),
            DatabaseEvidence(
                snapshot=DatabaseSnapshot(
                    status="ended",
                    end_reason="user_ended",
                    scene=Scene.online,
                    engine="character_prompt",
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                    phase="ended",
                ),
                transcript=transcript,
            ),
        ]
    )
    captured: dict[str, object] = {}

    def capture_checks(
        evidence: object,
        **kwargs: object,
    ) -> list[CheckResult]:
        captured["evidence"] = evidence
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "app.simulations.runner.run_automatic_checks",
        capture_checks,
    )
    protocol = FakeScenarioProtocol()
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        case_id="marriage_boundary_main",
        scene=Scene.online,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    result = await runner.run_scenario(
        load_scenarios()["marriage_opening"],
        TranscriptScenarioHttpClient(transcript),
    )

    assert captured["objective_contracts"] is True
    assert captured["expected_scene"] == "online"
    assert captured["expected_privacy_question"] == privacy_question
    assert captured["forbidden_phrases"] == ["关系越界已经证实"]
    assert "我感到羞耻" not in cast(list[str], captured["forbidden_phrases"])
    assert "我需要被接住" not in cast(list[str], captured["forbidden_phrases"])
    assert "评分标准" in captured["forbidden_backend_markers"]
    assert [row.model_dump() for row in result.manual_review] == [
        {
            "turn_sequence": 1,
            "client_turn_id": "opening-marriage",
            "character_text": transcript[0].text,
            "character_facts": "pending",
            "unknown_boundaries": "pending",
            "response_fit": "pending",
            "media_language": "pending",
            "story_progression": "pending",
            "notes": "",
        }
    ]
    artifact_dir = runner.write_results([result])
    payload = json.loads(
        (artifact_dir / "result.json").read_text(encoding="utf-8")
    )
    review = payload["results"][0]["manual_review"][0]
    assert list(payload["manual_review_guide"]) == [
        "character_facts",
        "unknown_boundaries",
        "response_fit",
        "media_language",
        "story_progression",
    ]
    assert review["character_facts"] == "pending"
    assert review["unknown_boundaries"] == "pending"
    assert review["response_fit"] == "pending"
    assert review["media_language"] == "pending"
    assert review["story_progression"] == "pending"
    summary = (artifact_dir / "summary.md").read_text(encoding="utf-8")
    assert "固定人工审阅表" in summary
    assert "自动合同通过不能替代人工审阅" in summary


@pytest.mark.parametrize(
    ("severity", "expected_status", "expected_passed"),
    [
        ("warning", "warning", True),
        ("error", "failed", False),
    ],
)
async def test_runner_distinguishes_check_warnings_from_blocking_errors(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    severity: str,
    expected_status: str,
    expected_passed: bool,
) -> None:
    scenario = Scenario(
        scenario_id=f"check-{severity}",
        title=f"检查级别 {severity}",
        profile="content",
        cards=[],
        end_after_cards=True,
        final_expect=FinalExpectation(),
    )
    observations = deque(
        [
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="ended", depth=0)),
        ]
    )

    def checks_with_finding(*_args: object, **_kwargs: object) -> list[CheckResult]:
        return [
            CheckResult(
                check_id="fact_contradiction",
                passed=False,
                severity=severity,
                detail="检测到可疑事实否认",
                evidence=[
                    {
                        "sequence": 2,
                        "client_turn_id": "sim-check-C1",
                        "fact_id": "job_loss",
                        "cue_id": "deny_work_problem",
                        "negator": "不是",
                        "matched_terms": ["工作", "问题"],
                        "excerpt": "不是工作问题",
                    }
                ],
            )
        ]

    monkeypatch.setattr(
        "app.simulations.runner.run_automatic_checks",
        checks_with_finding,
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(
            object, FakeScenarioProtocol()
        ),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    result = await runner.run_scenario(  # type: ignore[arg-type]
        scenario,
        ScenarioHttpClient(),
    )

    assert result.checks_status == expected_status
    assert result.passed is expected_passed
    payload = result.model_dump(mode="json")
    assert payload["checks"][0]["severity"] == severity
    assert payload["checks"][0]["evidence"][0]["fact_id"] == "job_loss"


def test_summary_keeps_warning_and_structured_evidence_visible() -> None:
    result = ScenarioRunResult(
        scenario_id="warning-summary",
        title="告警摘要",
        profile="content",
        session_id="session-warning",
        passed=True,
        checks_status="warning",
        cards=[],
        checks=[
            CheckResult(
                check_id="fact_contradiction",
                passed=False,
                severity="warning",
                detail="检测到未披露正向事实的明确否认",
                evidence=[
                    {
                        "sequence": 2,
                        "client_turn_id": "sim-chaotic-C2",
                        "fact_id": "job_loss",
                        "cue_id": "deny_work_problem",
                        "negator": "不是",
                        "matched_terms": ["工作", "问题"],
                        "excerpt": "不是工作问题",
                    }
                ],
            )
        ],
        final_issues=[],
        final_snapshot=DatabaseSnapshot(
            status="ended",
            end_reason="natural_closure",
            ending_route_id="collaborative_close",
            interaction_tension=0,
            repair_stage="none",
        ),
    )

    summary = _summary_markdown([result])

    assert "自动契约检查：有告警" in summary
    assert "自动检查告警：" in summary
    assert "fact_contradiction" in summary
    assert "sim-chaotic-C2" in summary
    assert "job_loss" in summary
    assert "deny_work_problem" in summary
    assert "不是工作问题" in summary


async def test_entry_runs_opening_and_exactly_one_normal_probe_before_user_end(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = load_scenarios()["entry"]
    normal_n1 = load_scenarios()["normal"].cards[0]
    protocol = FakeScenarioProtocol()
    snapshots = deque(
        [
            DatabaseEvidence(
                snapshot=DatabaseSnapshot(
                    status="active",
                    end_reason=None,
                    fact_depths={},
                    event_ids=[],
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                )
            ),
            DatabaseEvidence(
                snapshot=DatabaseSnapshot(
                    status="active",
                    end_reason=None,
                    fact_depths={"presenting_concern": 1},
                    event_ids=[],
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                )
            ),
            DatabaseEvidence(
                snapshot=DatabaseSnapshot(
                    status="ended",
                    end_reason="user_ended",
                    fact_depths={"presenting_concern": 1},
                    event_ids=[],
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                )
            ),
        ]
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: snapshots.popleft(),
    )

    result = await runner.run_scenario(scenario, ScenarioHttpClient())  # type: ignore[arg-type]

    assert protocol.opening_calls == 1
    assert protocol.sent == [(normal_n1.text, "sim-entry-N1")]
    assert protocol.end_calls == 1
    assert len(result.cards) == 1
    assert result.cards[0].card_id == "N1"
    assert result.cards[0].attempts == 1
    assert result.cards[0].retry_used is False
    assert result.exhausted_while_active is False
    assert result.final_snapshot.status == "ended"
    assert result.final_snapshot.end_reason == "user_ended"
    assert not snapshots


async def test_character_prompt_runs_all_cards_without_legacy_state_gates(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(
        scenario_id="character-sequence",
        title="轻量角色顺序探针",
        profile="content",
        cards=[
            ProbeCard(
                card_id="P1",
                text="旧链的第一问。",
                character_text="先说说今晚为什么打来。",
                requires=StateCondition(fact_depths={"legacy_required": 1}),
                expect=StateCondition(fact_depths={"legacy_expected": 1}),
                retry_text="旧链才会发送的重试。",
            ),
            ProbeCard(
                card_id="P2",
                text="一想到明早见面，你最担心什么？",
                character_only=True,
                world_time_advance_seconds=960,
                requires=StateCondition(event_ids=["legacy_event"]),
                expect=StateCondition(event_ids=["legacy_finished"]),
            ),
        ],
        final_expect=FinalExpectation(
            fact_depths={"legacy_expected": 2},
            event_ids=["legacy_finished"],
            ending_route_id="legacy_route",
        ),
        end_after_cards=True,
    )
    turns = [
        CapturedTurn(
            sequence=1,
            client_turn_id="opening-character",
            speaker="client",
            text="喂……我妈明早就到了，我不知道该怎么办。",
        ),
        CapturedTurn(
            sequence=2,
            client_turn_id="sim-character-sequence-P1",
            speaker="worker",
            text=cast(str, scenario.cards[0].character_text),
        ),
        CapturedTurn(
            sequence=3,
            client_turn_id="sim-character-sequence-P1",
            speaker="client",
            text="我一直瞒着她工作没了，今晚突然觉得拖不下去了。",
        ),
        CapturedTurn(
            sequence=4,
            client_turn_id="sim-character-sequence-P2",
            speaker="worker",
            text=scenario.cards[1].text,
        ),
        CapturedTurn(
            sequence=5,
            client_turn_id="sim-character-sequence-P2",
            speaker="client",
            text="我怕她一进门就看出来，也怕她问我为什么一直骗她。",
        ),
    ]

    def character_snapshot(
        *,
        status: str,
        end_reason: str | None = None,
    ) -> DatabaseSnapshot:
        return DatabaseSnapshot(
            status=status,
            end_reason=end_reason,
            engine="character_prompt",
            ending_route_id=None,
            interaction_tension=0,
            repair_stage="none",
        )

    def actor_metric(
        client_turn_id: str,
        *,
        call_kind: str = "initial",
        latency_ms: int = 900,
    ) -> ModelCallEvidence:
        return ModelCallEvidence(
            client_turn_id=client_turn_id,
            model_role="actor",
            model_name="qwen-plus-character",
            call_kind=call_kind,
            cache_mode="character_session",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cached_tokens=50,
            cache_creation_input_tokens=10,
            latency_ms=latency_ms,
            success=True,
            request_id=f"request-{client_turn_id}-{call_kind}",
        )

    metrics = [
        actor_metric("opening-character", latency_ms=15_662),
        actor_metric("sim-character-sequence-P1"),
        actor_metric("sim-character-sequence-P1", call_kind="repair"),
        actor_metric("sim-character-sequence-P2"),
    ]
    recovered_failure = RuntimeFailureEvidence(
        id="character-repair-record",
        session_id="session-sim",
        client_turn_id="sim-character-sequence-P1",
        component="actor",
        phase="acting",
        operation="output_validation",
        failure_code="actor_output_validation",
        error_class="ActorOutputValidationError",
        attempt_count=2,
        retryable=True,
        disposition="recovered",
        attempts_json=[{"index": 1, "message": "首个输出不符合结构"}],
        details_json={"summary": "一次返修后恢复"},
    )
    observations = deque(
        [
            DatabaseEvidence(snapshot=character_snapshot(status="active")),
            DatabaseEvidence(snapshot=character_snapshot(status="active")),
            DatabaseEvidence(snapshot=character_snapshot(status="active")),
            DatabaseEvidence(
                snapshot=character_snapshot(
                    status="ended",
                    end_reason="user_ended",
                ),
                transcript=turns,
                model_call_metrics=metrics,
                runtime_failures=[recovered_failure],
            ),
        ]
    )
    protocol = FakeScenarioProtocol()
    protocol.ws_transcript = list(turns)
    client = TranscriptScenarioHttpClient(turns)
    clock = iter([0.0, 0.1, 0.2, 0.5])
    monkeypatch.setattr("app.simulations.runner.perf_counter", lambda: next(clock))
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    result = await runner.run_scenario(scenario, client)  # type: ignore[arg-type]

    assert protocol.sent == [
        (scenario.cards[0].character_text, "sim-character-sequence-P1"),
        (scenario.cards[1].text, "sim-character-sequence-P2"),
    ]
    assert protocol.world_time_advances == [0, 960]
    assert [card.status for card in result.cards] == ["passed", "passed"]
    assert [card.attempts for card in result.cards] == [1, 1]
    assert all(not card.retry_used for card in result.cards)
    assert result.expectations_status == "not_evaluated"
    assert result.final_issues == []
    assert result.passed is True
    assert [check.check_id for check in result.checks] == [
        "transcript_consistency",
        "turn_pairing",
        "spoken_text_boundary",
        "content_has_no_audio",
    ]
    assert result.model_call_metrics == metrics
    assert result.runtime_failures == [recovered_failure]
    assert [card.attempt_elapsed_ms for card in result.cards] == [[100], [300]]
    assert not observations

    artifact_dir = runner.write_results([result])
    result_payload = json.loads(
        (artifact_dir / "result.json").read_text(encoding="utf-8")
    )
    summary = (artifact_dir / "summary.md").read_text(encoding="utf-8")

    assert result_payload["results"][0]["final_snapshot"]["engine"] == (
        "character_prompt"
    )
    assert result_payload["results"][0]["cards"][0]["attempt_elapsed_ms"] == [100]
    assert result_payload["results"][0]["model_call_metrics"][0]["cached_tokens"] == 50
    assert result_payload["results"][0]["runtime_failures"][0]["id"] == (
        "character-repair-record"
    )
    assert "### 对话逐字稿" in summary
    assert "### 探针耗时" in summary
    assert "actor：4 次调用；缓存命中：200/400（50.0%）" in summary
    assert "首次开场冷调用耗时：15662 ms" in summary
    assert "不计入后续实时门槛" in summary
    assert "后续实时调用耗时 P90：900 ms" in summary
    assert "一次返修后恢复" in summary


async def test_character_prompt_marks_card_missed_when_world_stage_does_not_advance(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        scenario_id="character-world-stalled",
        title="轻量角色链现实阶段停滞",
        profile="content",
        cards=[
            ProbeCard(
                card_id="P1",
                text="你再联系一次唐婷。",
                expect_world_stage="coming",
            )
        ],
        end_after_cards=True,
    )

    def snapshot(*, status: str) -> DatabaseEvidence:
        return DatabaseEvidence(
            snapshot=DatabaseSnapshot(
                status=status,
                end_reason="user_ended" if status == "ended" else None,
                engine="character_prompt",
                world_stage="first_unanswered",
                ending_route_id=None,
                interaction_tension=0,
                repair_stage="none",
            )
        )

    observations = deque(
        [
            snapshot(status="active"),
            snapshot(status="active"),
            snapshot(status="ended"),
        ]
    )
    protocol = FakeScenarioProtocol()
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    result = await runner.run_scenario(
        scenario,
        ScenarioHttpClient(),  # type: ignore[arg-type]
    )

    assert result.cards[0].status == "missed"
    assert result.cards[0].missing == [
        "world_stage: expected=coming, actual=first_unanswered"
    ]
    assert "探针未达预期：P1" in result.final_issues
    assert result.passed is False


async def test_workflow_uses_legacy_text_and_omits_character_only_cards(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        scenario_id="engine-specific-cards",
        title="分引擎探针",
        profile="content",
        cards=[
            ProbeCard(
                card_id="P1",
                text="旧链原文。",
                character_text="轻量角色链原文。",
                world_time_advance_seconds=960,
            ),
            ProbeCard(
                card_id="P2",
                text="只给轻量角色链的问题。",
                character_only=True,
            ),
        ],
        end_after_cards=True,
    )
    protocol = FakeScenarioProtocol()
    observations = deque(
        [
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="ended", depth=0)),
        ]
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    result = await runner.run_scenario(scenario, ScenarioHttpClient())  # type: ignore[arg-type]

    assert protocol.sent == [("旧链原文。", "sim-engine-specific-cards-P1")]
    assert protocol.world_time_advances == [0]
    assert [card.card_id for card in result.cards] == ["P1"]
    assert all("P2" not in issue for issue in result.final_issues)
    assert not observations


async def test_runner_fails_when_session_ends_before_all_fixed_cards_run(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        scenario_id="early-ending",
        title="提前结束",
        profile="content",
        cards=[
            ProbeCard(card_id="E1", text="第一问。"),
            ProbeCard(card_id="E2", text="第二问。"),
        ],
        final_expect=FinalExpectation(end_reason="user_ended"),
    )
    protocol = FakeScenarioProtocol()
    snapshots = deque(
        [
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="ended", depth=0)),
        ]
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: snapshots.popleft(),
    )

    result = await runner.run_scenario(scenario, ScenarioHttpClient())  # type: ignore[arg-type]

    assert [card.card_id for card in result.cards] == ["E1"]
    assert result.passed is False
    assert "固定探针未执行：E2" in result.final_issues


async def test_character_prompt_accepts_natural_close_after_configured_card(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        scenario_id="character-natural-close",
        title="轻量链自然收束",
        profile="content",
        cards=[
            ProbeCard(card_id="N17", text="确认支持者到门。"),
            ProbeCard(card_id="N18", text="确认今晚的安全安排。"),
            ProbeCard(card_id="N19", text="最后收束。"),
        ],
        natural_close_from_card_id="N18",
    )
    protocol = FakeScenarioProtocol()
    snapshots = deque(
        [
            DatabaseEvidence(
                snapshot=DatabaseSnapshot(
                    status="active",
                    end_reason=None,
                    engine="character_prompt",
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                )
            ),
            DatabaseEvidence(
                snapshot=DatabaseSnapshot(
                    status="active",
                    end_reason=None,
                    engine="character_prompt",
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                )
            ),
            DatabaseEvidence(
                snapshot=DatabaseSnapshot(
                    status="ended",
                    end_reason="natural_closure",
                    engine="character_prompt",
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                )
            ),
        ]
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: snapshots.popleft(),
    )

    result = await runner.run_scenario(scenario, ScenarioHttpClient())  # type: ignore[arg-type]

    assert [card.card_id for card in result.cards] == ["N17", "N18"]
    assert result.final_snapshot.end_reason == "natural_closure"
    assert all("固定探针未执行" not in issue for issue in result.final_issues)
    assert result.passed is True


@pytest.mark.parametrize(
    ("engine_name", "end_after_card", "expected_missing"),
    [
        ("character_prompt", "N17", "N18, N19"),
        ("workflow", "N18", "N19"),
    ],
)
async def test_natural_close_threshold_does_not_hide_other_early_endings(
    test_engine: Engine,
    tmp_path: Path,
    engine_name: str,
    end_after_card: str,
    expected_missing: str,
) -> None:
    scenario = Scenario(
        scenario_id="guarded-natural-close",
        title="受限自然收束",
        profile="content",
        cards=[
            ProbeCard(card_id="N17", text="确认支持者到门。"),
            ProbeCard(card_id="N18", text="确认今晚的安全安排。"),
            ProbeCard(card_id="N19", text="最后收束。"),
        ],
        natural_close_from_card_id="N18",
    )

    def snapshot(*, status: str) -> DatabaseEvidence:
        return DatabaseEvidence(
            snapshot=DatabaseSnapshot(
                status=status,
                end_reason="natural_closure" if status == "ended" else None,
                engine=cast(object, engine_name),
                ending_route_id=None,
                interaction_tension=0,
                repair_stage="none",
            )
        )

    snapshots = deque([snapshot(status="active")])
    for card_id in ("N17", "N18"):
        snapshots.append(
            snapshot(status="ended" if card_id == end_after_card else "active")
        )
        if card_id == end_after_card:
            break
    protocol = FakeScenarioProtocol()
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: snapshots.popleft(),
    )

    result = await runner.run_scenario(scenario, ScenarioHttpClient())  # type: ignore[arg-type]

    assert result.passed is False
    assert f"固定探针未执行：{expected_missing}" in result.final_issues


async def test_runner_keeps_real_session_evidence_when_opening_is_interrupted(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = load_scenarios()["normal"]
    protocol = FailingOpeningProtocol()
    evidence = DatabaseEvidence(
        snapshot=DatabaseSnapshot(
            status="active",
            end_reason=None,
            ending_route_id=None,
            interaction_tension=0,
            repair_stage="none",
            phase="technical_paused",
        ),
        model_call_metrics=[
            ModelCallEvidence(
                model_role="actor",
                model_name="qwen-plus-character",
                call_kind="repair",
                cache_mode="character_session",
                prompt_tokens=1662,
                completion_tokens=80,
                total_tokens=1742,
                cached_tokens=1648,
                latency_ms=3239,
                success=False,
                request_id="request-from-failed-opening",
            )
        ],
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: evidence,
    )
    client = ScenarioHttpClient()

    result = await runner.run_scenario(scenario, client)  # type: ignore[arg-type]

    assert result.session_id == "session-sim"
    assert result.final_snapshot.phase == "technical_paused"
    assert result.final_issues == ["运行中断：来访者开场未生成"]
    assert result.model_call_metrics[0].request_id == "request-from-failed-opening"
    assert result.run_status == "failed"
    assert result.checks_status == "not_run"
    assert result.expectations_status == "not_evaluated"
    assert result.runtime_failures[0].component == "simulation"
    assert result.runtime_failures[0].failure_code == "simulation_protocol"
    assert client.posts[-1] == (
        "/api/sessions/session-sim/end",
        {"reason": "technical_interruption"},
    )
    assert protocol.end_calls == 0


async def test_marriage_online_interruption_without_observer_evidence_keeps_identity(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        case_id="marriage_boundary_main",
        scene=Scene.online,
        protocol_factory=lambda _url, _profile: cast(
            object,
            FailingOpeningProtocol(),
        ),
        observer=lambda _engine, _session_id: (_ for _ in ()).throw(
            OSError("数据库暂时不可读")
        ),
    )

    result = await runner.run_scenario(
        load_scenarios()["marriage_opening"],
        ScenarioHttpClient(),
    )

    assert result.scene is Scene.online
    assert result.final_snapshot.scene is Scene.online
    assert result.final_snapshot.engine == "character_prompt"
    assert any(
        failure.failure_code == "simulation_observation"
        for failure in result.runtime_failures
    )
    assert any("数据库观察失败" in issue for issue in result.final_issues)


async def test_marriage_online_failure_before_session_creation_keeps_identity(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    class FailingCreateClient(FakeHttpClient):
        async def post(self, path: str, *, json: dict[str, object]) -> FakeResponse:
            raise OSError(f"创建会话失败：{path}")

    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        case_id="marriage_boundary_main",
        scene=Scene.online,
    )

    results = await _run_selected_scenarios(
        runner,
        [load_scenarios()["marriage_opening"]],
        FailingCreateClient(),  # type: ignore[arg-type]
    )

    assert len(results) == 1
    assert results[0].session_id == "not-created"
    assert results[0].scene is Scene.online
    assert results[0].final_snapshot.scene is Scene.online
    assert results[0].final_snapshot.engine == "character_prompt"


async def test_early_failure_redacts_provider_secrets_from_result(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    secret = "test-real-secret-12345"

    class SecretFailingClient(FakeHttpClient):
        async def post(self, path: str, *, json: dict[str, object]) -> FakeResponse:
            raise OSError(
                f"DASHSCOPE_API_KEY={secret} Authorization: Bearer {secret}"
            )

    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        case_id="marriage_boundary_main",
        scene=Scene.online,
    )
    results = await _run_selected_scenarios(
        runner,
        [load_scenarios()["marriage_opening"]],
        SecretFailingClient(),  # type: ignore[arg-type]
    )

    rendered = results[0].model_dump_json()
    assert secret not in rendered
    assert "[REDACTED]" in rendered


async def test_runner_keeps_real_session_evidence_when_failed_socket_also_closes_badly(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = load_scenarios()["opening"]
    evidence = DatabaseEvidence(
        snapshot=DatabaseSnapshot(
            status="active",
            end_reason=None,
            ending_route_id=None,
            interaction_tension=0,
            repair_stage="none",
            phase="technical_paused",
        )
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(
            object, FailingOpeningAndCloseProtocol()
        ),
        observer=lambda _engine, _session_id: evidence,
    )

    result = await runner.run_scenario(scenario, ScenarioHttpClient())  # type: ignore[arg-type]

    assert result.session_id == "session-sim"
    assert result.final_snapshot.phase == "technical_paused"


async def test_selected_scenarios_do_not_hide_programming_errors() -> None:
    scenario = load_scenarios()["opening"]

    class BrokenRunner:
        async def run_scenario(self, _scenario: Scenario, _api: object) -> ScenarioRunResult:
            raise AssertionError("黑箱脚本自身有错误")

    with pytest.raises(AssertionError, match="黑箱脚本自身有错误"):
        await _run_selected_scenarios(  # type: ignore[arg-type]
            BrokenRunner(),
            [scenario],
            object(),  # type: ignore[arg-type]
        )


async def test_runner_records_unsatisfied_expectations_for_missed_card(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        scenario_id="missed-detail",
        title="未达预期详情",
        profile="content",
        cards=[
            ProbeCard(
                card_id="M1",
                text="请继续说。",
                expect=StateCondition(
                    fact_depths={"target": 2},
                    event_ids=["support_connected"],
                ),
            )
        ],
        final_expect=FinalExpectation(),
        end_after_cards=True,
    )
    observations = deque(
        [
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=1)),
            DatabaseEvidence(snapshot=_database_snapshot(status="ended", depth=1)),
        ]
    )
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(
            object, FakeScenarioProtocol()
        ),
        observer=lambda _engine, _session_id: observations.popleft(),
    )

    result = await runner.run_scenario(  # type: ignore[arg-type]
        scenario,
        ScenarioHttpClient(),
    )

    assert result.cards[0].status == "missed"
    assert result.cards[0].missing == [
        "fact:target>=2",
        "event:support_connected",
    ]


async def test_runner_retries_one_fixed_card_then_fails_and_cleans_active_exhaustion(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = Scenario(
        scenario_id="test",
        title="测试",
        profile="content",
        cards=[
            ProbeCard(
                card_id="P1",
                text="先问一次。",
                expect=StateCondition(fact_depths={"target": 1}),
                retry_text="只再问这一次。",
            )
        ],
        final_expect=FinalExpectation(fact_depths={"target": 1}),
    )
    snapshots = deque(
        [
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=0)),
            DatabaseEvidence(snapshot=_database_snapshot(status="active", depth=1)),
            DatabaseEvidence(
                snapshot=_database_snapshot(status="ended", depth=1),
                model_call_metrics=[
                    ModelCallEvidence(
                        model_role="director",
                        model_name="qwen3.7-plus",
                        call_kind="initial",
                        cache_mode="explicit",
                        prompt_tokens=100,
                        completion_tokens=10,
                        total_tokens=110,
                        cached_tokens=40,
                        cache_creation_input_tokens=20,
                        latency_ms=600,
                        success=True,
                    )
                ],
            ),
        ]
    )
    protocol = FakeScenarioProtocol()
    clock = iter([0.0, 10.5, 11.0, 11.1])
    monkeypatch.setattr("app.simulations.runner.perf_counter", lambda: next(clock))
    runner = SimulationRunner(
        base_url="http://127.0.0.1:8000",
        output_root=tmp_path,
        engine=test_engine,
        protocol_factory=lambda _url, _profile: cast(object, protocol),
        observer=lambda _engine, _session_id: snapshots.popleft(),
    )

    result = await runner.run_scenario(scenario, ScenarioHttpClient())  # type: ignore[arg-type]

    assert [text for text, _ in protocol.sent] == ["先问一次。", "只再问这一次。"]
    assert result.cards[0].attempts == 2
    assert result.cards[0].status == "passed_after_retry"
    assert len(result.cards[0].attempt_elapsed_ms) == 2
    assert all(elapsed >= 0 for elapsed in result.cards[0].attempt_elapsed_ms)
    assert result.exhausted_while_active is True
    assert result.passed is False
    assert protocol.end_calls == 1
    assert result.work_record == "deferred"
    assert result.report == "deferred"

    artifact_dir = runner.write_results([result])
    result_text = (artifact_dir / "result.json").read_text(encoding="utf-8")
    result_payload = json.loads(result_text)

    assert (artifact_dir / "summary.md").is_file()
    assert result_payload["work_record"] == "deferred"
    assert result_payload["report"] == "deferred"
    assert "X-Assessment-Simulation" not in result_text
    assert "api_key" not in result_text.casefold()
    assert not list(artifact_dir.glob("*.wav"))
    assert "director：1 次调用；缓存命中：40/100（40.0%）" in (
        artifact_dir / "summary.md"
    ).read_text(encoding="utf-8")
    summary = (artifact_dir / "summary.md").read_text(encoding="utf-8")
    assert "运行状态：已完成" in summary
    assert "自动契约检查：通过" in summary
    assert "最终预期：通过" in summary
    assert "真人感审阅：待完成" in summary
    assert "平均探针耗时" in summary
    assert "探针耗时 P50" in summary
    assert "探针耗时 P90" in summary
    assert "最慢探针：P1 10500 ms" in summary
    assert "超过 10 秒" in summary
    assert "失败调用：0；返修次数：0" in summary
    assert "director：1 次调用" in summary
    assert "call_kind=initial；success=true" in summary


def test_database_observation_uses_real_model_metric_fields(
    test_engine: Engine,
) -> None:
    SQLModel.metadata.create_all(test_engine)
    state = {
        "actor_state": {
            "stage": "exploration",
            "fact_states": {
                "presenting_concern": {"disclosed_depth": 1},
            },
            "occurred_event_ids": ["first_contact_tang_ting"],
            "relationship": {
                "interaction_tension": 1,
                "willingness_to_continue": 3,
                "repair_stage": "window",
            },
            "ending_state": {"accepted_route_id": None},
        },
        "runtime": {"phase": "listening"},
        "world": {"stage": "coming", "arrival_due_at": "2026-08-31T06:00:00Z"},
    }
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="observed-session",
                mode=SessionMode.assessment,
                scene=Scene.hotline,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.voice,
                status=SessionStatus.active,
                model_mode=ModelMode.live,
                state_json=state,
            )
        )
        db.add(
            TurnRecord(
                id="observed-worker-turn",
                session_id="observed-session",
                client_turn_id="card-1",
                sequence=1,
                speaker=TurnSpeaker.worker,
                text="你现在身边有人吗？",
                signals_json={
                    "director_decision": {
                        "interaction": "neutral",
                        "directives": [],
                    },
                    "turn_plan": {
                        "allowed_fact_depths": {"presenting_concern": 1}
                    },
                },
            )
        )
        db.add(
            TurnRecord(
                id="observed-client-turn",
                session_id="observed-session",
                client_turn_id="card-1",
                sequence=2,
                speaker=TurnSpeaker.client,
                text="嗯。",
                state_before_json={
                    "fact_states": {
                        "presenting_concern": {"disclosed_depth": 0},
                        "job_loss": {"disclosed_depth": 0},
                    }
                },
            )
        )
        db.add(
            ModelCallMetricRecord(
                session_id="observed-session",
                client_turn_id="card-1",
                model_role=ModelRole.actor,
                model_name="qwen-plus-character",
                call_kind=ModelCallKind.initial,
                cache_mode=CacheMode.character_session,
                prompt_tokens=100,
                cached_tokens=40,
                cache_creation_input_tokens=20,
                latency_ms=1234,
                success=True,
                request_id="request-observed",
            )
        )
        failure_created_at = datetime(2026, 8, 30, 5, 59, 12, tzinfo=UTC)
        db.add(
            RuntimeFailureRecord(
                id="runtime-failure-observed",
                session_id="observed-session",
                client_turn_id="card-1",
                component="director",
                phase="deciding",
                operation="provider_call",
                failure_code="provider_timeout",
                error_class="ReadTimeout",
                attempt_count=2,
                retryable=False,
                disposition="technical_pause",
                provider_status_code=504,
                provider_request_id="request-failed",
                attempts_json=[
                    {"attempt": 1, "latency_ms": 120000},
                    {"attempt": 2, "latency_ms": 120000},
                ],
                details_json={"summary": "Director 调用两次均超时"},
                created_at=failure_created_at,
            )
        )
        db.commit()

    evidence = read_database_evidence(test_engine, "observed-session")

    assert evidence.snapshot.conversation_stage == "exploration"
    assert evidence.snapshot.scene is Scene.hotline
    assert evidence.snapshot.fact_depths == {"presenting_concern": 1}
    assert evidence.snapshot.world_stage == "coming"
    assert evidence.snapshot.event_ids == ["first_contact_tang_ting"]
    assert evidence.snapshot.interaction_impact == "neutral"
    client_turn = next(
        turn for turn in evidence.transcript if turn.speaker == "client"
    )
    assert client_turn.fact_depths_before == {
        "presenting_concern": 0,
        "job_loss": 0,
    }
    assert evidence.model_call_metrics[0].model_name == "qwen-plus-character"
    assert evidence.model_call_metrics[0].client_turn_id == "card-1"
    assert evidence.model_call_metrics[0].cache_mode == "character_session"
    assert evidence.model_call_metrics[0].cache_creation_input_tokens == 20
    assert evidence.model_call_metrics[0].latency_ms == 1234
    assert evidence.model_call_metrics[0].request_id == "request-observed"
    assert evidence.runtime_failures[0].model_dump(mode="json") == {
        "id": "runtime-failure-observed",
        "session_id": "observed-session",
        "client_turn_id": "card-1",
        "component": "director",
        "phase": "deciding",
        "operation": "provider_call",
        "failure_code": "provider_timeout",
        "error_class": "ReadTimeout",
        "attempt_count": 2,
        "retryable": False,
        "disposition": "technical_pause",
        "provider_status_code": 504,
        "provider_request_id": "request-failed",
        "attempts_json": [
            {"attempt": 1, "latency_ms": 120000},
            {"attempt": 2, "latency_ms": 120000},
        ],
        "details_json": {"summary": "Director 调用两次均超时"},
        "created_at": "2026-08-30T05:59:12",
    }


def test_database_observation_reads_character_engine_without_actor_state(
    test_engine: Engine,
) -> None:
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="observed-character-session",
                mode=SessionMode.assessment,
                scene=Scene.hotline,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.voice,
                status=SessionStatus.active,
                model_mode=ModelMode.live,
                state_json={
                    "runtime": {
                        "engine": "character_prompt",
                        "phase": "listening",
                    }
                },
            )
        )
        db.commit()

    evidence = read_database_evidence(test_engine, "observed-character-session")

    assert getattr(evidence.snapshot, "engine", None) == "character_prompt"
    assert evidence.snapshot.fact_depths == {}
    assert evidence.snapshot.event_ids == []


def test_database_observation_rejects_unknown_runtime_engine(
    test_engine: Engine,
) -> None:
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="observed-unknown-engine",
                mode=SessionMode.assessment,
                scene=Scene.online,
                case_type=CaseType.main,
                case_id="marriage_boundary_main",
                media=Media.text,
                status=SessionStatus.active,
                model_mode=ModelMode.live,
                state_json={"runtime": {"engine": "charcter", "phase": "listening"}},
            )
        )
        db.commit()

    with pytest.raises(SimulationProtocolError, match="会话运行引擎无法识别"):
        read_database_evidence(test_engine, "observed-unknown-engine")


def test_database_observation_infers_required_character_engine_when_missing(
    test_engine: Engine,
) -> None:
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="observed-required-character",
                mode=SessionMode.assessment,
                scene=Scene.online,
                case_type=CaseType.main,
                case_id="marriage_boundary_main",
                media=Media.text,
                status=SessionStatus.active,
                model_mode=ModelMode.live,
                state_json={"runtime": {"phase": "listening"}},
            )
        )
        db.commit()

    evidence = read_database_evidence(test_engine, "observed-required-character")
    assert evidence.snapshot.engine == "character_prompt"


def _database_snapshot(*, status: str, depth: int) -> DatabaseSnapshot:
    return DatabaseSnapshot(
        status=status,
        end_reason="user_ended" if status == "ended" else None,
        fact_depths={"target": depth},
        event_ids=[],
        ending_route_id=None,
        interaction_tension=0,
        repair_stage="none",
    )

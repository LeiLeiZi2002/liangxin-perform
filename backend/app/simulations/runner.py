from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol, cast
from uuid import uuid4

import httpx
import websockets
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select
from websockets.exceptions import ConnectionClosed

from app import database
from app.audio.models import AudioRecord
from app.cases.loader import CaseRepository
from app.runtime.character_provider import CharacterNotFoundError, CharacterRepository
from app.runtime.failures import safe_failure_details
from app.runtime.models import ModelCallMetricRecord, RuntimeFailureRecord
from app.sessions.models import Scene, SessionRecord, TurnRecord
from app.simulations.checks import (
    CapturedTurn,
    CheckResult,
    RunEvidence,
    StateFrame,
    run_automatic_checks,
)
from app.simulations.scenario import (
    Scenario,
    ScenarioState,
    WorldStage,
    load_scenarios,
    select_scenarios,
)

DEFAULT_TURN_TIMEOUT_SECONDS = 120
TECHNICAL_PAUSE_MESSAGE = "来访者回复进入技术暂停，已停止当前场景"
SimulationProfile = Literal["content", "voice"]
RuntimeEngine = Literal["workflow", "character_prompt"]
ChecksStatus = Literal["not_run", "passed", "warning", "failed"]


class RunnerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnvironmentStatus(RunnerModel):
    healthy: bool
    configured: bool


class DatabaseSnapshot(RunnerModel):
    status: str
    end_reason: str | None
    scene: Scene = Scene.hotline
    engine: RuntimeEngine = "workflow"
    world_stage: WorldStage | None = None
    conversation_stage: str = "opening"
    fact_depths: dict[str, int] = Field(default_factory=dict)
    event_ids: list[str] = Field(default_factory=list)
    ending_route_id: str | None
    interaction_tension: int = Field(ge=0, le=3)
    repair_stage: str
    willingness_to_continue: int = Field(default=0, ge=0, le=4)
    interaction_impact: str | None = None
    phase: str = "listening"


class ModelCallEvidence(RunnerModel):
    client_turn_id: str | None = None
    model_role: str
    model_name: str
    call_kind: str
    cache_mode: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(ge=0)
    success: bool
    request_id: str | None = None


class RuntimeFailureEvidence(RunnerModel):
    id: str | None = None
    session_id: str | None = None
    client_turn_id: str | None = None
    component: str
    phase: str
    operation: str
    failure_code: str
    error_class: str
    attempt_count: int = Field(default=1, ge=1)
    retryable: bool
    disposition: str
    provider_status_code: int | None = None
    provider_request_id: str | None = None
    attempts_json: list[dict[str, object]] = Field(default_factory=list)
    details_json: dict[str, object] = Field(default_factory=dict)
    created_at: datetime | None = None


class DatabaseEvidence(RunnerModel):
    snapshot: DatabaseSnapshot
    transcript: list[CapturedTurn] = Field(default_factory=list)
    model_call_metrics: list[ModelCallEvidence] = Field(default_factory=list)
    runtime_failures: list[RuntimeFailureEvidence] = Field(default_factory=list)
    audio_record_count: int = Field(default=0, ge=0)


CardStatus = Literal[
    "skipped",
    "blocked",
    "passed",
    "passed_after_retry",
    "missed",
    "interrupted",
]


class CardRunResult(RunnerModel):
    card_id: str
    status: CardStatus
    attempts: int = Field(default=0, ge=0, le=2)
    retry_used: bool = False
    missing: list[str] = Field(default_factory=list)
    attempt_elapsed_ms: list[int] = Field(default_factory=list)


ManualReviewDecision = Literal["pending", "pass", "fail"]
MANUAL_REVIEW_GUIDE = {
    "character_facts": "人物经历与已知事实是否前后一致，没有临时编造材料",
    "unknown_boundaries": "未知的关系真相与第三方动机是否仍保持未知",
    "response_fit": "来访者是否回应受测者本轮真正说出的内容",
    "media_language": "表达是否符合当前热线口语或在线聊天媒介",
    "story_progression": "互动是否自然走向眼前问题、选择或合理结束",
}


class ManualReviewRow(RunnerModel):
    turn_sequence: int = Field(ge=1)
    client_turn_id: str
    character_text: str
    character_facts: ManualReviewDecision = "pending"
    unknown_boundaries: ManualReviewDecision = "pending"
    response_fit: ManualReviewDecision = "pending"
    media_language: ManualReviewDecision = "pending"
    story_progression: ManualReviewDecision = "pending"
    notes: str = ""


class ScenarioRunResult(RunnerModel):
    scenario_id: str
    title: str
    profile: SimulationProfile
    case_id: str = "crisis_student_main"
    scene: Scene = Scene.hotline
    session_id: str
    passed: bool
    run_status: Literal["completed", "failed"] = "completed"
    checks_status: ChecksStatus = "passed"
    expectations_status: Literal["not_evaluated", "passed", "failed"] = "passed"
    cards: list[CardRunResult]
    checks: list[CheckResult]
    final_issues: list[str]
    exhausted_while_active: bool = False
    final_snapshot: DatabaseSnapshot
    transcript: list[CapturedTurn] = Field(default_factory=list)
    state_frames: list[StateFrame] = Field(default_factory=list)
    model_call_metrics: list[ModelCallEvidence] = Field(default_factory=list)
    runtime_failures: list[RuntimeFailureEvidence] = Field(default_factory=list)
    binary_chunk_count: int = Field(default=0, ge=0)
    audio_record_count: int = Field(default=0, ge=0)
    work_record: Literal["deferred"] = "deferred"
    report: Literal["deferred"] = "deferred"
    manual_review: list[ManualReviewRow] = Field(default_factory=list)


def _checks_status(
    checks: Sequence[CheckResult],
) -> Literal["passed", "warning", "failed"]:
    failed_checks = [check for check in checks if not check.passed]
    if any(check.severity == "error" for check in failed_checks):
        return "failed"
    if failed_checks:
        return "warning"
    return "passed"


class ProtocolCommit(RunnerModel):
    client_turn_id: str
    binary_chunk_count: int = Field(ge=0)
    final_phase: str
    ended_reason: str | None = None
    committed: dict[str, object]
    messages: list[dict[str, object]] = Field(default_factory=list)


class SimulationProtocolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure: RuntimeFailureEvidence | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure
        self.elapsed_ms = elapsed_ms


def _technical_pause_error(message: dict[str, object]) -> SimulationProtocolError:
    failure_payload = message.get("failure")
    if not isinstance(failure_payload, dict):
        return SimulationProtocolError(TECHNICAL_PAUSE_MESSAGE)
    normalized = {
        "phase": "runtime",
        "operation": "unknown",
        "retryable": False,
        "disposition": "technical_pause",
        **failure_payload,
    }
    try:
        failure = RuntimeFailureEvidence.model_validate(normalized)
    except ValueError:
        return SimulationProtocolError(TECHNICAL_PAUSE_MESSAGE)
    summary = _optional_text(failure.details_json.get("summary"))
    detail = summary or failure.failure_code
    return SimulationProtocolError(detail, failure=failure)


class SocketLike(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, payload: str) -> None: ...

    async def close(self) -> None: ...


SocketConnector = Callable[[str, dict[str, str]], Awaitable[SocketLike]]


async def _connect_websocket(url: str, headers: dict[str, str]) -> SocketLike:
    connection = await websockets.connect(
        url,
        additional_headers=headers or None,
        open_timeout=10,
        ping_interval=20,
        ping_timeout=20,
    )
    return cast(SocketLike, connection)


class LiveSimulationProtocol:
    def __init__(
        self,
        *,
        ws_url: str,
        profile: SimulationProfile,
        connector: SocketConnector = _connect_websocket,
        timeout_seconds: int = DEFAULT_TURN_TIMEOUT_SECONDS,
    ) -> None:
        self.ws_url = ws_url
        self.profile = profile
        self.connector = connector
        self.timeout_seconds = timeout_seconds
        self.socket: SocketLike | None = None
        self.snapshot: dict[str, object] | None = None
        self.ws_transcript: list[CapturedTurn] = []
        self.total_binary_chunks = 0

    async def connect(self) -> dict[str, object]:
        first = await self._open_socket()
        if first.get("phase") == "technical_paused":
            raise _technical_pause_error(first)
        await self._send({"type": "session.start"})
        return first

    async def _open_socket(
        self,
        *,
        allow_ended: bool = False,
    ) -> dict[str, object]:
        headers: dict[str, str] = {"X-Assessment-Simulation": self.profile}
        self.socket = await self.connector(self.ws_url, headers)
        first = await self._receive_json()
        if allow_ended and first.get("type") == "session.ended":
            return first
        if first.get("type") != "snapshot":
            raise SimulationProtocolError("WebSocket 首条消息不是会话快照")
        self.snapshot = first
        self._replace_transcript(first.get("transcript"))
        return first

    async def _reconnect(self) -> dict[str, object] | None:
        await self.close()
        snapshot = await self._open_socket(allow_ended=True)
        if snapshot.get("type") == "session.ended":
            return snapshot
        if snapshot.get("phase") == "technical_paused":
            raise _technical_pause_error(snapshot)
        await self._send({"type": "session.start"})
        return None

    async def wait_for_opening(self) -> ProtocolCommit:
        reconnects = 0
        while True:
            try:
                return await self._receive_committed(expected_client_turn_id=None)
            except (ConnectionClosed, OSError, TimeoutError) as exc:
                if reconnects >= 1:
                    raise SimulationProtocolError("自动开场重连后仍未完成") from exc
                reconnects += 1
                ended = await self._reconnect()
                if ended is not None:
                    recovered = self._ended_transcript_commit(None, ended)
                    if recovered is not None:
                        return recovered
                    raise SimulationProtocolError(
                        "会话在自动开场提交前已经结束"
                    ) from exc
                recovered = await self._snapshot_commit(None)
                if recovered is not None:
                    return recovered

    async def ensure_opening(self) -> ProtocolCommit | None:
        if self.snapshot is None:
            raise SimulationProtocolError("WebSocket 尚未连接")
        transcript = self.snapshot.get("transcript")
        if isinstance(transcript, list) and transcript:
            return None
        if self.snapshot.get("opening_delay_seconds") is None:
            return None
        return await self.wait_for_opening()

    async def send_turn(
        self,
        text: str,
        client_turn_id: str,
        *,
        world_time_advance_seconds: int = 0,
    ) -> ProtocolCommit:
        payload: dict[str, object] = {
            "type": "text.turn",
            "text": text,
            "client_turn_id": client_turn_id,
        }
        if world_time_advance_seconds:
            payload["world_time_advance_seconds"] = world_time_advance_seconds
        reconnects = 0
        while True:
            try:
                await self._send(payload)
                return await self._receive_committed(
                    expected_client_turn_id=client_turn_id
                )
            except SimulationProtocolError:
                raise
            except (ConnectionClosed, OSError, TimeoutError) as exc:
                if reconnects >= 1:
                    raise SimulationProtocolError("WebSocket 重连一次后仍未恢复") from exc
                reconnects += 1
                ended = await self._reconnect()
                if ended is not None:
                    recovered = self._ended_transcript_commit(client_turn_id, ended)
                    if recovered is not None:
                        return recovered
                    raise SimulationProtocolError(
                        "会话在当前探针提交前已经结束"
                    ) from exc
                recovered = await self._snapshot_commit(client_turn_id)
                if recovered is not None:
                    return recovered

    async def end_session(self) -> str | None:
        await self._send({"type": "session.end"})
        while True:
            message = await self._receive_json()
            if message.get("type") == "session.ended":
                return _optional_text(message.get("reason"))

    async def close(self) -> None:
        if self.socket is not None:
            await self.socket.close()
            self.socket = None

    async def _snapshot_commit(
        self,
        client_turn_id: str | None,
    ) -> ProtocolCommit | None:
        if self.snapshot is None:
            return None
        matching = [
            turn
            for turn in self.ws_transcript
            if client_turn_id is None or turn.client_turn_id == client_turn_id
        ]
        if not matching:
            return None
        selected_id = client_turn_id or matching[-1].client_turn_id
        matching = [turn for turn in matching if turn.client_turn_id == selected_id]
        await self._send({"type": "playback.ended"})
        phase = str(self.snapshot.get("phase", "listening"))
        ended_reason: str | None = None
        messages: list[dict[str, object]] = [self.snapshot]
        binary_chunks = 0
        if phase != "listening":
            phase, ended_reason, settle_messages, binary_chunks = await self._settle()
            messages.extend(settle_messages)
        return ProtocolCommit(
            client_turn_id=selected_id,
            binary_chunk_count=binary_chunks,
            final_phase=phase,
            ended_reason=ended_reason,
            committed={
                "type": "turn.committed",
                "client_turn_id": selected_id,
                **{
                    turn.speaker: turn.model_dump(mode="json")
                    for turn in matching
                },
            },
            messages=messages,
        )

    def _ended_transcript_commit(
        self,
        client_turn_id: str | None,
        ended: dict[str, object],
    ) -> ProtocolCommit | None:
        matching = [
            turn
            for turn in self.ws_transcript
            if client_turn_id is None or turn.client_turn_id == client_turn_id
        ]
        if not matching:
            return None
        selected_id = client_turn_id or matching[-1].client_turn_id
        matching = [turn for turn in matching if turn.client_turn_id == selected_id]
        return ProtocolCommit(
            client_turn_id=selected_id,
            binary_chunk_count=0,
            final_phase="ended",
            ended_reason=_optional_text(ended.get("reason")),
            committed={
                "type": "turn.committed",
                "client_turn_id": selected_id,
                **{
                    turn.speaker: turn.model_dump(mode="json")
                    for turn in matching
                },
            },
            messages=[ended],
        )

    async def _receive_committed(
        self,
        *,
        expected_client_turn_id: str | None,
    ) -> ProtocolCommit:
        messages: list[dict[str, object]] = []
        turn_binary_chunks = 0
        while True:
            incoming = await self._receive()
            if isinstance(incoming, bytes):
                turn_binary_chunks += 1
                self.total_binary_chunks += 1
                continue
            message = _decode_message(incoming)
            messages.append(message)
            message_type = message.get("type")
            if message_type == "technical.pause":
                raise _technical_pause_error(message)
            if message_type in {"session.error", "input.error"}:
                raise SimulationProtocolError(str(message.get("message", "会话协议错误")))
            if message_type == "session.ended":
                raise SimulationProtocolError("会话在当前探针提交前已经结束")
            if message_type != "turn.committed":
                continue
            committed_id = _optional_text(message.get("client_turn_id")) or ""
            await self._send({"type": "playback.ended"})
            self._merge_committed_transcript(message)
            if expected_client_turn_id is not None and committed_id != expected_client_turn_id:
                continue
            phase, ended_reason, settle_messages, settle_binary = await self._settle()
            messages.extend(settle_messages)
            turn_binary_chunks += settle_binary
            return ProtocolCommit(
                client_turn_id=committed_id,
                binary_chunk_count=turn_binary_chunks,
                final_phase=phase,
                ended_reason=ended_reason,
                committed=message,
                messages=messages,
            )

    async def _settle(
        self,
    ) -> tuple[str, str | None, list[dict[str, object]], int]:
        messages: list[dict[str, object]] = []
        binary_chunks = 0
        while True:
            incoming = await self._receive()
            if isinstance(incoming, bytes):
                binary_chunks += 1
                self.total_binary_chunks += 1
                continue
            message = _decode_message(incoming)
            messages.append(message)
            if message.get("type") == "phase" and message.get("phase") == "listening":
                return "listening", None, messages, binary_chunks
            if message.get("type") == "session.ended":
                return (
                    "ended",
                    _optional_text(message.get("reason")),
                    messages,
                    binary_chunks,
                )
            if message.get("type") == "technical.pause":
                raise _technical_pause_error(message)

    async def _receive_json(self) -> dict[str, object]:
        incoming = await self._receive()
        if isinstance(incoming, bytes):
            raise SimulationProtocolError("等待 JSON 时收到意外音频")
        return _decode_message(incoming)

    async def _receive(self) -> str | bytes:
        if self.socket is None:
            raise SimulationProtocolError("WebSocket 尚未连接")
        return await asyncio.wait_for(
            self.socket.recv(),
            timeout=self.timeout_seconds,
        )

    async def _send(self, payload: dict[str, object]) -> None:
        if self.socket is None:
            raise SimulationProtocolError("WebSocket 尚未连接")
        await self.socket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def _replace_transcript(self, payload: object) -> None:
        if not isinstance(payload, list):
            return
        turns = [_captured_turn(item) for item in payload if isinstance(item, dict)]
        self.ws_transcript = [turn for turn in turns if turn is not None]

    def _merge_committed_transcript(self, message: dict[str, object]) -> None:
        known = {(turn.sequence, turn.speaker) for turn in self.ws_transcript}
        for key in ("worker", "client"):
            payload = message.get(key)
            turn = _captured_turn(payload) if isinstance(payload, dict) else None
            if turn is not None and (turn.sequence, turn.speaker) not in known:
                self.ws_transcript.append(turn)
                known.add((turn.sequence, turn.speaker))
        self.ws_transcript.sort(key=lambda turn: turn.sequence)


class HttpResponseLike(Protocol):
    def raise_for_status(self) -> object: ...

    def json(self) -> object: ...


class HttpClientLike(Protocol):
    async def get(self, path: str) -> HttpResponseLike: ...

    async def post(
        self,
        path: str,
        *,
        json: dict[str, object],
    ) -> HttpResponseLike: ...


class HttpxClientAdapter:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def get(self, path: str) -> HttpResponseLike:
        return await self.client.get(path)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, object],
    ) -> HttpResponseLike:
        return await self.client.post(path, json=json)


async def read_environment(client: HttpClientLike) -> EnvironmentStatus:
    health_response = await client.get("/api/health")
    health_response.raise_for_status()
    health_payload = health_response.json()
    config_response = await client.get("/api/provider-config")
    config_response.raise_for_status()
    config_payload = config_response.json()
    return EnvironmentStatus(
        healthy=isinstance(health_payload, dict)
        and health_payload.get("status") == "ready",
        configured=isinstance(config_payload, dict)
        and config_payload.get("configured") is True,
    )


def read_database_evidence(engine: Engine, session_id: str) -> DatabaseEvidence:
    with Session(engine) as db:
        record = db.get(SessionRecord, session_id)
        if record is None:
            raise SimulationProtocolError(f"数据库中找不到会话：{session_id}")
        turns = list(
            db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == session_id)
                .order_by(col(TurnRecord.sequence))
            ).all()
        )
        metrics = list(
            db.exec(
                select(ModelCallMetricRecord)
                .where(ModelCallMetricRecord.session_id == session_id)
                .order_by(
                    col(ModelCallMetricRecord.created_at),
                    col(ModelCallMetricRecord.id),
                )
            ).all()
        )
        failures = list(
            db.exec(
                select(RuntimeFailureRecord)
                .where(RuntimeFailureRecord.session_id == session_id)
                .order_by(
                    col(RuntimeFailureRecord.created_at),
                    col(RuntimeFailureRecord.id),
                )
            ).all()
        )
        audio_records = list(
            db.exec(
                select(AudioRecord).where(AudioRecord.session_id == session_id)
            ).all()
        )

    actor_state = _mapping(record.state_json.get("actor_state"))
    relationship = _mapping(actor_state.get("relationship"))
    ending_state = _mapping(actor_state.get("ending_state"))
    runtime = _mapping(record.state_json.get("runtime"))
    world = _mapping(record.state_json.get("world"))
    raw_runtime_engine = runtime.get("engine")
    if raw_runtime_engine is None:
        runtime_engine = _configured_runtime_engine(record.case_id)
    elif raw_runtime_engine in {"workflow", "character_prompt"}:
        runtime_engine = raw_runtime_engine
    else:
        raise SimulationProtocolError(f"会话运行引擎无法识别：{raw_runtime_engine}")
    fact_depths: dict[str, int] = {}
    for fact_id, payload in _mapping(actor_state.get("fact_states")).items():
        depth = _mapping(payload).get("disclosed_depth")
        if isinstance(depth, int):
            fact_depths[str(fact_id)] = depth
    event_ids = [
        str(event_id)
        for event_id in _list(actor_state.get("occurred_event_ids"))
        if isinstance(event_id, str)
    ]
    transcript = [
        CapturedTurn(
            sequence=turn.sequence,
            client_turn_id=turn.client_turn_id,
            speaker=cast(Literal["worker", "client"], _enum_value(turn.speaker)),
            text=turn.text,
            signals=dict(turn.signals_json),
            audio_available=bool(turn.audio_path),
            fact_depths_before=_fact_depths_from_turn_state(
                turn.state_before_json
            ),
        )
        for turn in turns
    ]
    last_worker = next(
        (turn for turn in reversed(transcript) if turn.speaker == "worker"),
        None,
    )
    interaction_impact: str | None = None
    if last_worker is not None:
        decision = _mapping(last_worker.signals.get("director_decision"))
        interaction_impact = _optional_text(decision.get("interaction"))
    return DatabaseEvidence(
        snapshot=DatabaseSnapshot(
            status=_enum_value(record.status),
            end_reason=_enum_value(record.end_reason) if record.end_reason else None,
            scene=record.scene,
            engine=runtime_engine,
            world_stage=_world_stage(world.get("stage")),
            conversation_stage=str(actor_state.get("stage", "opening")),
            fact_depths=fact_depths,
            event_ids=event_ids,
            ending_route_id=_optional_text(ending_state.get("accepted_route_id")),
            interaction_tension=_integer(relationship.get("interaction_tension")),
            repair_stage=str(relationship.get("repair_stage", "none")),
            willingness_to_continue=_integer(
                relationship.get("willingness_to_continue")
            ),
            interaction_impact=interaction_impact,
            phase=str(runtime.get("phase", "listening")),
        ),
        transcript=transcript,
        model_call_metrics=[
            ModelCallEvidence(
                client_turn_id=metric.client_turn_id,
                model_role=_enum_value(metric.model_role),
                model_name=metric.model_name,
                call_kind=_enum_value(metric.call_kind),
                cache_mode=_enum_value(metric.cache_mode),
                prompt_tokens=metric.prompt_tokens,
                completion_tokens=metric.completion_tokens,
                total_tokens=metric.total_tokens,
                cached_tokens=metric.cached_tokens,
                cache_creation_input_tokens=metric.cache_creation_input_tokens,
                latency_ms=metric.latency_ms,
                success=metric.success,
                request_id=metric.request_id,
            )
            for metric in metrics
        ],
        runtime_failures=[
            RuntimeFailureEvidence.model_validate(
                failure.model_dump(mode="python")
            )
            for failure in failures
        ],
        audio_record_count=len(audio_records),
    )


class ScenarioProtocol(Protocol):
    ws_transcript: list[CapturedTurn]
    total_binary_chunks: int

    async def connect(self) -> dict[str, object]: ...

    async def ensure_opening(self) -> ProtocolCommit | None: ...

    async def send_turn(
        self,
        text: str,
        client_turn_id: str,
        *,
        world_time_advance_seconds: int = 0,
    ) -> ProtocolCommit: ...

    async def end_session(self) -> str | None: ...

    async def close(self) -> None: ...


ProtocolFactory = Callable[[str, SimulationProfile], ScenarioProtocol]
DatabaseObserver = Callable[[Engine, str], DatabaseEvidence]


class SimulationRunner:
    def __init__(
        self,
        *,
        base_url: str,
        output_root: Path | None = None,
        engine: Engine | None = None,
        protocol_factory: ProtocolFactory | None = None,
        observer: DatabaseObserver = read_database_evidence,
        case_id: str = "crisis_student_main",
        scene: Scene = Scene.hotline,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.output_root = output_root or _project_root() / "data" / "simulations"
        self.engine = engine or database.engine
        self.protocol_factory = protocol_factory or self._default_protocol
        self.observer = observer
        self.case_id = case_id
        self.scene = scene
        self.package = CaseRepository().get(case_id)
        if scene not in self.package.case.supported_scenes:
            raise ValueError(f"案例 {case_id} 不支持场域 {scene.value}")
        self.character_repository = CharacterRepository()

    def profile_for_scenario(self, scenario: Scenario) -> SimulationProfile:
        if scenario.case_id != self.case_id:
            raise ValueError(
                f"模拟场景 {scenario.scenario_id} 不属于案例 {self.case_id}"
            )
        return scenario.profile_for_scene(self.scene)

    def runtime_engine_for_case(self) -> RuntimeEngine:
        return _configured_runtime_engine(
            self.case_id,
            character_repository=self.character_repository,
        )

    async def run_scenario(
        self,
        scenario: Scenario,
        client: HttpClientLike,
    ) -> ScenarioRunResult:
        profile = self.profile_for_scenario(scenario)
        session_id = await self._create_session(client)
        protocol = self.protocol_factory(self._ws_url(session_id), profile)
        cards: list[CardRunResult] = []
        frames: list[StateFrame] = []
        exhausted = False
        final_evidence: DatabaseEvidence | None = None
        current: DatabaseEvidence | None = None
        run_cards = scenario.cards
        operation = "connect"
        try:
            await protocol.connect()
            operation = "opening"
            await protocol.ensure_opening()
            current = self.observer(self.engine, session_id)
            character_prompt = current.snapshot.engine == "character_prompt"
            run_cards = scenario.cards_for_engine(
                current.snapshot.engine,
                scene=self.scene,
            )
            if not character_prompt:
                frames.append(_state_frame(current.snapshot))
            for card in run_cards:
                if not character_prompt:
                    state = _scenario_state(current.snapshot)
                    if card.should_skip(state):
                        cards.append(
                            CardRunResult(card_id=card.card_id, status="skipped")
                        )
                        continue
                    if not card.can_run(state):
                        cards.append(
                            CardRunResult(
                                card_id=card.card_id,
                                status="blocked",
                                missing=card.blocked_requirements(state),
                            )
                        )
                        break
                attempt_elapsed_ms: list[int] = []
                operation = "card_turn"
                card_text = card.text_for_engine(
                    current.snapshot.engine,
                    scene=self.scene,
                )
                assert card_text is not None
                try:
                    current, elapsed_ms = await self._run_card_attempt(
                        protocol,
                        session_id=session_id,
                        scenario=scenario,
                        card_id=card.card_id,
                        text=card_text,
                        attempt=1,
                        world_time_advance_seconds=(
                            card.world_time_advance_seconds
                            if character_prompt
                            else 0
                        ),
                    )
                except SimulationProtocolError as exc:
                    cards.append(
                        CardRunResult(
                            card_id=card.card_id,
                            status="interrupted",
                            attempts=1,
                            attempt_elapsed_ms=(
                                [exc.elapsed_ms]
                                if exc.elapsed_ms is not None
                                else []
                            ),
                        )
                    )
                    raise
                attempts = 1
                attempt_elapsed_ms.append(elapsed_ms)
                if not character_prompt:
                    frames.append(_state_frame(current.snapshot, card.card_id))
                world_stage_missing = (
                    _world_stage_mismatch(card.expect_world_stage, current.snapshot)
                    if character_prompt
                    else []
                )
                matched = (
                    not world_stage_missing
                    if character_prompt
                    else card.expect.is_empty
                    or card.expect.matches(_scenario_state(current.snapshot))
                )
                if (
                    not character_prompt
                    and not matched
                    and card.retry_text is not None
                    and current.snapshot.status == "active"
                ):
                    try:
                        current, elapsed_ms = await self._run_card_attempt(
                            protocol,
                            session_id=session_id,
                            scenario=scenario,
                            card_id=card.card_id,
                            text=card.retry_text,
                            attempt=2,
                            world_time_advance_seconds=0,
                        )
                    except SimulationProtocolError as exc:
                        cards.append(
                            CardRunResult(
                                card_id=card.card_id,
                                status="interrupted",
                                attempts=2,
                                retry_used=True,
                                attempt_elapsed_ms=[
                                    *attempt_elapsed_ms,
                                    *(
                                        [exc.elapsed_ms]
                                        if exc.elapsed_ms is not None
                                        else []
                                    ),
                                ],
                            )
                        )
                        raise
                    attempts = 2
                    attempt_elapsed_ms.append(elapsed_ms)
                    if not character_prompt:
                        frames.append(_state_frame(current.snapshot, card.card_id))
                    matched = card.expect.is_empty or card.expect.matches(
                        _scenario_state(current.snapshot)
                    )
                cards.append(
                    CardRunResult(
                        card_id=card.card_id,
                        status=(
                            "passed_after_retry"
                            if matched and attempts == 2
                            else "passed"
                            if matched
                            else "missed"
                        ),
                        attempts=attempts,
                        retry_used=attempts == 2,
                        missing=(
                            []
                            if matched
                            else world_stage_missing
                            if character_prompt
                            else card.expect.unsatisfied(
                                _scenario_state(current.snapshot)
                            )
                        ),
                        attempt_elapsed_ms=attempt_elapsed_ms,
                    )
                )
                if current.snapshot.status == "ended":
                    break

            final_evidence = current
            if current.snapshot.status == "active":
                exhausted = not scenario.end_after_cards
                operation = "end_session"
                await protocol.end_session()
                final_evidence = self.observer(self.engine, session_id)
                if not character_prompt:
                    frames.append(_state_frame(final_evidence.snapshot))
        except (
            SimulationProtocolError,
            ConnectionClosed,
            httpx.HTTPError,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
        ) as exc:
            return await self._interrupted_result(
                scenario=scenario,
                client=client,
                protocol=protocol,
                session_id=session_id,
                cards=cards,
                frames=frames,
                exc=exc,
                operation=operation,
                evidence=final_evidence or current,
                refresh_evidence=True,
            )
        finally:
            with suppress(ConnectionClosed, OSError):
                await protocol.close()

        assert final_evidence is not None
        character_prompt = final_evidence.snapshot.engine == "character_prompt"
        operation = "rest_transcript"
        checks: list[CheckResult] | None = None
        try:
            rest_transcript = await self._read_rest_transcript(client, session_id)
            package = self.package
            max_fact_depths = {
                fact.id: max(depth.depth for depth in fact.depths)
                for fact in package.case.facts
            }
            event_prerequisites = {
                event.id: [
                    *event.prerequisite_event_ids,
                    *(
                        [event.deferred_after.after_event_id]
                        if event.deferred_after is not None
                        else []
                    ),
                ]
                for event in package.case.story_events
            }
            operation = "automatic_checks"
            character = (
                self.character_repository.get_for_case(package.case)
                if character_prompt
                else None
            )
            scene_profile = (
                character.scene_profiles.get(self.scene.value, {})
                if character is not None
                else {}
            )
            privacy_question = scene_profile.get("privacy_question")
            checks = run_automatic_checks(
                RunEvidence(
                    state_frames=frames,
                    db_transcript=final_evidence.transcript,
                    rest_transcript=rest_transcript,
                    ws_transcript=protocol.ws_transcript,
                    binary_chunk_count=protocol.total_binary_chunks,
                    final_phase=final_evidence.snapshot.phase,
                    scene=final_evidence.snapshot.scene.value,
                    final_status=final_evidence.snapshot.status,
                    runtime_failure_count=len(final_evidence.runtime_failures),
                    runtime_failure_attempt_count=sum(
                        failure.attempt_count
                        for failure in final_evidence.runtime_failures
                    ),
                    failed_model_call_count=sum(
                        not metric.success
                        for metric in final_evidence.model_call_metrics
                    ),
                ),
                profile=profile,
                runtime_engine=final_evidence.snapshot.engine,
                max_fact_depths=max_fact_depths,
                event_prerequisites=event_prerequisites,
                allowed_interaction_impacts_by_card={
                    card.card_id: list(scenario.allowed_impacts_for(card))
                    for card in run_cards
                },
                maximum_fact_depths_after_by_card={
                    card.card_id: card.maximum_fact_depths_after
                    for card in run_cards
                    if card.maximum_fact_depths_after
                },
                forbidden_phrases=(
                    list(character.forbidden_surface_forms)
                    if character is not None
                    else package.actor.stable_speech.forbidden_phrases
                ),
                forbidden_backend_markers=(
                    list(character.forbidden_backend_markers)
                    if character is not None
                    else []
                ),
                objective_contracts=scenario.objective_contracts,
                expected_scene=self.scene.value,
                expected_privacy_question=(
                    privacy_question
                    if isinstance(privacy_question, str)
                    else None
                ),
                fact_contradiction_cues={
                    fact.id: [
                        cue.model_dump(mode="json")
                        for cue in fact.contradiction_cues
                    ]
                    for fact in package.case.facts
                    if fact.kind == "positive_fact"
                },
                relationship_arc=(
                    (
                        scenario.relationship_rupture_card_id,
                        scenario.relationship_repair_card_id,
                    )
                    if scenario.relationship_rupture_card_id is not None
                    and scenario.relationship_repair_card_id is not None
                    else None
                ),
                card_order=[card.card_id for card in run_cards],
                earliest_event_card_ids=scenario.earliest_event_card_ids,
                harmful_from_card_id=scenario.harmful_from_card_id,
                protected_fact_ids=scenario.protected_fact_ids,
            )
            operation = "final_expectations"
            expectation_issues = (
                []
                if character_prompt
                else final_expectation_issues(
                    scenario,
                    final_evidence.snapshot,
                )
            )
        except (
            SimulationProtocolError,
            ConnectionClosed,
            httpx.HTTPError,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
            ValueError,
        ) as exc:
            return await self._interrupted_result(
                scenario=scenario,
                client=client,
                protocol=protocol,
                session_id=session_id,
                cards=cards,
                frames=frames,
                exc=exc,
                operation=operation,
                evidence=final_evidence,
                refresh_evidence=False,
                checks=checks,
            )

        assert checks is not None
        final_issues = list(expectation_issues)
        if exhausted:
            final_issues.append("固定探针已用尽，会话仍未自然结束")
        processed_card_ids = {card.card_id for card in cards}
        missing_card_ids = [
            card.card_id
            for card in run_cards
            if card.card_id not in processed_card_ids
        ]
        close_from_card_id = scenario.natural_close_from_card_id
        if (
            missing_card_ids
            and character_prompt
            and final_evidence.snapshot.end_reason == "natural_closure"
            and close_from_card_id is not None
            and close_from_card_id in processed_card_ids
        ):
            missing_card_ids = []
        if missing_card_ids:
            final_issues.append("固定探针未执行：" + ", ".join(missing_card_ids))
        failed_cards = [card for card in cards if card.status in {"blocked", "missed"}]
        if failed_cards:
            final_issues.append(
                "探针未达预期：" + ", ".join(card.card_id for card in failed_cards)
            )
        if not scenario.objective_contracts:
            final_issues.extend(
                runtime_quality_issues(
                final_evidence.model_call_metrics,
                runtime_engine=final_evidence.snapshot.engine,
                expected_turn_ids_by_role={
                    "director": (
                        []
                        if character_prompt
                        else list(
                            dict.fromkeys(
                                turn.client_turn_id
                                for turn in final_evidence.transcript
                                if turn.speaker == "worker"
                            )
                        )
                    ),
                    "actor": list(
                        dict.fromkeys(
                            turn.client_turn_id
                            for turn in final_evidence.transcript
                            if turn.speaker == "client"
                        )
                    ),
                    **(
                        {
                            "tts": list(
                                dict.fromkeys(
                                    turn.client_turn_id
                                    for turn in final_evidence.transcript
                                    if turn.speaker == "client"
                                )
                            )
                        }
                        if profile == "voice"
                        else {}
                    ),
                    },
                )
            )
        checks_status = _checks_status(checks)
        return ScenarioRunResult(
            scenario_id=scenario.scenario_id,
            title=scenario.title,
            profile=profile,
            case_id=self.case_id,
            scene=self.scene,
            session_id=session_id,
            passed=not final_issues and checks_status != "failed",
            run_status="completed",
            checks_status=checks_status,
            expectations_status=(
                "not_evaluated"
                if character_prompt
                else "passed"
                if not expectation_issues
                else "failed"
            ),
            cards=cards,
            checks=checks,
            final_issues=final_issues,
            exhausted_while_active=exhausted,
            final_snapshot=final_evidence.snapshot,
            transcript=final_evidence.transcript,
            state_frames=frames,
            model_call_metrics=final_evidence.model_call_metrics,
            runtime_failures=final_evidence.runtime_failures,
            binary_chunk_count=protocol.total_binary_chunks,
            audio_record_count=final_evidence.audio_record_count,
            manual_review=_manual_review_rows(final_evidence.transcript),
        )

    async def _interrupted_result(
        self,
        *,
        scenario: Scenario,
        client: HttpClientLike,
        protocol: ScenarioProtocol,
        session_id: str,
        cards: list[CardRunResult],
        frames: list[StateFrame],
        exc: Exception,
        operation: str,
        evidence: DatabaseEvidence | None,
        refresh_evidence: bool,
        checks: list[CheckResult] | None = None,
    ) -> ScenarioRunResult:
        observation_failures: list[RuntimeFailureEvidence] = []
        observation_issues: list[str] = []
        if refresh_evidence:
            try:
                evidence = self.observer(self.engine, session_id)
            except Exception as observation_error:
                observation_failures.append(
                    _simulation_failure(
                        observation_error,
                        session_id=session_id,
                        operation="database_observation",
                        failure_code="simulation_observation",
                    )
                )
                observation_issues.append(
                    f"数据库观察失败：{_safe_exception_text(observation_error)}"
                )
        if evidence is None:
            evidence = DatabaseEvidence(
                snapshot=DatabaseSnapshot(
                    status="run_failed",
                    end_reason="technical_interruption",
                    scene=self.scene,
                    engine=self.runtime_engine_for_case(),
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                )
            )
        frame = _state_frame(evidence.snapshot)
        if not frames or frames[-1] != frame:
            frames.append(frame)

        failure = (
            exc.failure
            if isinstance(exc, SimulationProtocolError)
            else None
        )
        if failure is None:
            failure = _simulation_failure(
                exc,
                session_id=session_id,
                operation=operation,
                failure_code=(
                    "simulation_protocol"
                    if isinstance(exc, SimulationProtocolError)
                    else "simulation_runtime"
                ),
            )
        failures = _merge_runtime_failures(
            evidence.runtime_failures,
            [failure, *observation_failures],
        )
        detail = _safe_exception_text(exc)
        try:
            response = await client.post(
                f"/api/sessions/{session_id}/end",
                json={"reason": "technical_interruption"},
            )
            response.raise_for_status()
        except Exception as cleanup_error:
            failures = _merge_runtime_failures(
                failures,
                [
                    _simulation_failure(
                        cleanup_error,
                        session_id=session_id,
                        operation="session_cleanup",
                        failure_code="simulation_cleanup",
                    )
                ],
            )
        checks_status: ChecksStatus
        if operation == "automatic_checks":
            checks_status = "failed"
        elif checks is None:
            checks_status = "not_run"
        else:
            checks_status = _checks_status(checks)
        return ScenarioRunResult(
            scenario_id=scenario.scenario_id,
            title=scenario.title,
            profile=self.profile_for_scenario(scenario),
            case_id=self.case_id,
            scene=self.scene,
            session_id=session_id,
            passed=False,
            run_status="failed",
            checks_status=checks_status,
            expectations_status="not_evaluated",
            cards=cards,
            checks=checks or [],
            final_issues=[f"运行中断：{detail}", *observation_issues],
            final_snapshot=evidence.snapshot,
            transcript=evidence.transcript,
            state_frames=frames,
            model_call_metrics=evidence.model_call_metrics,
            runtime_failures=failures,
            binary_chunk_count=protocol.total_binary_chunks,
            audio_record_count=evidence.audio_record_count,
            manual_review=_manual_review_rows(evidence.transcript),
        )

    async def _run_card_attempt(
        self,
        protocol: ScenarioProtocol,
        *,
        session_id: str,
        scenario: Scenario,
        card_id: str,
        text: str,
        attempt: int,
        world_time_advance_seconds: int = 0,
    ) -> tuple[DatabaseEvidence, int]:
        suffix = "" if attempt == 1 else "-retry"
        client_turn_id = f"sim-{scenario.scenario_id}-{card_id}{suffix}"
        started = perf_counter()
        try:
            await protocol.send_turn(
                text,
                client_turn_id,
                world_time_advance_seconds=world_time_advance_seconds,
            )
            evidence = self.observer(self.engine, session_id)
        except (
            SimulationProtocolError,
            ConnectionClosed,
            httpx.HTTPError,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
        ) as exc:
            elapsed_ms = max(0, round((perf_counter() - started) * 1000))
            if isinstance(exc, SimulationProtocolError):
                raise SimulationProtocolError(
                    _safe_exception_text(exc),
                    failure=exc.failure,
                    elapsed_ms=elapsed_ms,
                ) from exc
            raise SimulationProtocolError(
                _safe_exception_text(exc),
                failure=_simulation_failure(
                    exc,
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                    operation="card_turn",
                    failure_code="simulation_transport",
                    attempt_count=attempt,
                ),
                elapsed_ms=elapsed_ms,
            ) from exc
        elapsed_ms = round((perf_counter() - started) * 1000)
        return evidence, max(0, elapsed_ms)

    async def _create_session(self, client: HttpClientLike) -> str:
        response = await client.post(
            "/api/sessions",
            json={
                "mode": "experience",
                "scene": self.scene.value,
                "case_type": self.package.case.case_type.value,
                "case_id": self.case_id,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise SimulationProtocolError("创建会话接口未返回会话标识")
        return cast(str, payload["id"])

    async def _read_rest_transcript(
        self,
        client: HttpClientLike,
        session_id: str,
    ) -> list[CapturedTurn]:
        response = await client.get(f"/api/sessions/{session_id}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        transcript = payload.get("transcript")
        if not isinstance(transcript, list):
            return []
        turns = [_captured_turn(item) for item in transcript if isinstance(item, dict)]
        return [turn for turn in turns if turn is not None]

    def _default_protocol(
        self,
        ws_url: str,
        profile: SimulationProfile,
    ) -> ScenarioProtocol:
        return LiveSimulationProtocol(ws_url=ws_url, profile=profile)

    def _ws_url(self, session_id: str) -> str:
        if self.base_url.startswith("https://"):
            root = "wss://" + self.base_url.removeprefix("https://")
        elif self.base_url.startswith("http://"):
            root = "ws://" + self.base_url.removeprefix("http://")
        else:
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return f"{root}/api/live-sessions/{session_id}"

    def write_results(self, results: Sequence[ScenarioRunResult]) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        run_dir = self.output_root / f"{stamp}-{uuid4().hex[:6]}"
        run_dir.mkdir(parents=True, exist_ok=False)
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "work_record": "deferred",
            "report": "deferred",
            "manual_review_guide": MANUAL_REVIEW_GUIDE,
            "results": [result.model_dump(mode="json") for result in results],
        }
        (run_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "summary.md").write_text(
            _summary_markdown(results),
            encoding="utf-8",
        )
        return run_dir


def final_expectation_issues(
    scenario: Scenario,
    snapshot: DatabaseSnapshot,
) -> list[str]:
    expected = scenario.final_expect
    issues: list[str] = []
    if snapshot.status != "ended":
        issues.append("会话仍处于活动状态")
    missing_facts = [
        f"{fact_id}>={depth}"
        for fact_id, depth in expected.fact_depths.items()
        if snapshot.fact_depths.get(fact_id, 0) < depth
    ]
    if missing_facts:
        issues.append("事实深度不足：" + ", ".join(missing_facts))
    missing_events = [
        event_id for event_id in expected.event_ids if event_id not in snapshot.event_ids
    ]
    if missing_events:
        issues.append("故事事件缺失：" + ", ".join(missing_events))
    if (
        expected.ending_route_id is not None
        and snapshot.ending_route_id != expected.ending_route_id
    ):
        issues.append(
            f"结束路线不符：{snapshot.ending_route_id or 'none'}"
            f" != {expected.ending_route_id}"
        )
    if expected.end_reason is not None and snapshot.end_reason != expected.end_reason:
        issues.append(
            f"结束原因不符：{snapshot.end_reason or 'none'} != {expected.end_reason}"
        )
    if (
        expected.minimum_interaction_tension is not None
        and snapshot.interaction_tension < expected.minimum_interaction_tension
    ):
        issues.append(
            "互动紧张度不足："
            f"{snapshot.interaction_tension} < {expected.minimum_interaction_tension}"
        )
    if (
        expected.maximum_interaction_tension is not None
        and snapshot.interaction_tension > expected.maximum_interaction_tension
    ):
        issues.append(
            "互动紧张度未回落："
            f"{snapshot.interaction_tension} > {expected.maximum_interaction_tension}"
        )
    if (
        expected.allowed_repair_stages
        and snapshot.repair_stage not in expected.allowed_repair_stages
    ):
        issues.append(
            f"修复阶段不符：{snapshot.repair_stage} 不在 "
            + ", ".join(expected.allowed_repair_stages)
        )
    return issues


def _captured_turn(payload: dict[str, object]) -> CapturedTurn | None:
    sequence = payload.get("sequence")
    speaker = payload.get("speaker")
    if not isinstance(sequence, int) or speaker not in {"worker", "client"}:
        return None
    return CapturedTurn(
        sequence=sequence,
        client_turn_id=str(payload.get("client_turn_id", "")),
        speaker=speaker,
        text=str(payload.get("text", "")),
        signals=_mapping(payload.get("signals_json")),
        audio_available=bool(payload.get("audio_available", False)),
    )


def _scenario_state(snapshot: DatabaseSnapshot) -> ScenarioState:
    return ScenarioState(
        fact_depths=snapshot.fact_depths,
        event_ids=frozenset(snapshot.event_ids),
    )


def _state_frame(
    snapshot: DatabaseSnapshot,
    card_id: str | None = None,
) -> StateFrame:
    return StateFrame(
        card_id=card_id,
        conversation_stage=snapshot.conversation_stage,
        fact_depths=snapshot.fact_depths,
        event_ids=snapshot.event_ids,
        interaction_tension=snapshot.interaction_tension,
        willingness_to_continue=snapshot.willingness_to_continue,
        interaction_impact=snapshot.interaction_impact,
        repair_stage=snapshot.repair_stage,
    )


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _fact_depths_from_turn_state(
    state: dict[str, object],
) -> dict[str, int] | None:
    raw_fact_states = state.get("fact_states")
    if not isinstance(raw_fact_states, dict):
        return None
    depths: dict[str, int] = {}
    for fact_id, raw_state in raw_fact_states.items():
        if not isinstance(fact_id, str) or not isinstance(raw_state, dict):
            continue
        depth = raw_state.get("disclosed_depth")
        if isinstance(depth, int) and not isinstance(depth, bool):
            depths[fact_id] = depth
    return depths


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _world_stage(value: object) -> WorldStage | None:
    if value not in {
        "not_contacted",
        "first_unanswered",
        "coming",
        "at_door",
        "present",
    }:
        return None
    return value


def _world_stage_mismatch(
    expected: WorldStage | None,
    snapshot: DatabaseSnapshot,
) -> list[str]:
    if expected is None or snapshot.world_stage == expected:
        return []
    return [
        "world_stage: "
        f"expected={expected}, actual={snapshot.world_stage or 'none'}"
    ]


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _simulation_failure(
    exc: Exception,
    *,
    session_id: str | None,
    operation: str,
    failure_code: str,
    client_turn_id: str | None = None,
    attempt_count: int = 1,
) -> RuntimeFailureEvidence:
    detail = _safe_exception_text(exc)
    return RuntimeFailureEvidence(
        session_id=session_id,
        client_turn_id=client_turn_id,
        component="simulation",
        phase="simulation",
        operation=operation,
        failure_code=failure_code,
        error_class=type(exc).__name__,
        attempt_count=attempt_count,
        retryable=False,
        disposition="aborted",
        details_json=safe_failure_details({"summary": detail}),
        created_at=datetime.now(UTC),
    )


def _safe_exception_text(exc: BaseException) -> str:
    detail = str(exc).strip() or type(exc).__name__
    return str(safe_failure_details({"summary": detail})["summary"])


def _configured_runtime_engine(
    case_id: str,
    *,
    character_repository: CharacterRepository | None = None,
) -> RuntimeEngine:
    package = CaseRepository().get(case_id)
    repository = character_repository or CharacterRepository()
    try:
        repository.get_for_case(package.case)
    except CharacterNotFoundError:
        return "character_prompt" if package.case.character_required else "workflow"
    return "character_prompt"


def _merge_runtime_failures(
    existing: Sequence[RuntimeFailureEvidence],
    additions: Sequence[RuntimeFailureEvidence],
) -> list[RuntimeFailureEvidence]:
    merged: list[RuntimeFailureEvidence] = []
    seen: set[str] = set()
    for failure in [*existing, *additions]:
        identity = failure.id or json.dumps(
            failure.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(failure)
    return merged


def _append_check_findings(
    lines: list[str],
    heading: str,
    checks: Sequence[CheckResult],
) -> None:
    if not checks:
        return
    lines.extend(["", heading])
    for check in checks:
        lines.append(f"- {check.check_id}：{check.detail}")
        for item in check.evidence:
            payload = json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            lines.append(f"  - 证据：`{payload}`")


def _manual_review_rows(turns: Sequence[CapturedTurn]) -> list[ManualReviewRow]:
    return [
        ManualReviewRow(
            turn_sequence=turn.sequence,
            client_turn_id=turn.client_turn_id,
            character_text=turn.text,
        )
        for turn in turns
        if turn.speaker == "client"
    ]


def _summary_markdown(results: Sequence[ScenarioRunResult]) -> str:
    lines = ["# 固定脚本黑箱测评结果", ""]
    for result in results:
        run_status = "已完成" if result.run_status == "completed" else "失败"
        checks_status = {
            "not_run": "未执行",
            "passed": "通过",
            "warning": "有告警",
            "failed": "未通过",
        }[result.checks_status]
        expectations_status = {
            "not_evaluated": "未评估",
            "passed": "通过",
            "failed": "未通过",
        }[result.expectations_status]
        lines.extend(
            [
                f"## {result.title}",
                "",
                f"- 运行状态：{run_status}",
                f"- 自动契约检查：{checks_status}",
                f"- 最终预期：{expectations_status}",
                "- 真人感审阅：待完成",
                f"- 会话：`{result.session_id}`",
                f"- 案例：`{result.case_id}`",
                f"- 场域：`{result.scene.value}`",
                "- 最终状态 "
                f"{result.final_snapshot.status}；"
                f"案例阶段 {result.final_snapshot.conversation_stage}；"
                f"结束原因 {result.final_snapshot.end_reason or '无'}；"
                f"结束路线 {result.final_snapshot.ending_route_id or '无'}",
                f"- 探针卡：{len(result.cards)}",
                f"- 模型调用记录：{len(result.model_call_metrics)}",
                "- 专业工作记录：暂不生成",
                "- 测量报告：暂不生成",
                "",
            ]
        )
        if result.manual_review:
            lines.extend(
                [
                    "",
                    "### 固定人工审阅表",
                    "",
                    "自动合同通过不能替代人工审阅。请在 result.json 中把 "
                    "pending 改为 pass 或 fail，并填写 notes。",
                    "",
                    "| 回合 | 人物事实 | 未知边界 | 回应贴合 | 媒介语言 | 故事推进 |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for row in result.manual_review:
                lines.append(
                    f"| {row.turn_sequence} | {row.character_facts} | "
                    f"{row.unknown_boundaries} | {row.response_fit} | "
                    f"{row.media_language} | {row.story_progression} |"
                )
        if result.cards:
            elapsed_by_card = [
                (card.card_id, elapsed)
                for card in result.cards
                for elapsed in card.attempt_elapsed_ms
            ]
            lines.extend(["", "### 探针耗时", ""])
            if elapsed_by_card:
                average_elapsed = round(
                    sum(elapsed for _, elapsed in elapsed_by_card)
                    / len(elapsed_by_card)
                )
                slowest_card_id, slowest_elapsed = max(
                    elapsed_by_card,
                    key=lambda item: item[1],
                )
                lines.extend(
                    [
                        f"- 平均探针耗时：{average_elapsed} ms",
                        "- 探针耗时 P50："
                        f"{_nearest_rank_percentile([item[1] for item in elapsed_by_card], 50)} ms",
                        "- 探针耗时 P90："
                        f"{_nearest_rank_percentile([item[1] for item in elapsed_by_card], 90)} ms",
                        f"- 最慢探针：{slowest_card_id} {slowest_elapsed} ms",
                    ]
                )
                if slowest_elapsed > 10_000:
                    lines.append(
                        "- 耗时提示：最慢探针超过 10 秒，请结合逐调用耗时排查"
                    )
            for card in result.cards:
                elapsed = (
                    " / ".join(f"{value} ms" for value in card.attempt_elapsed_ms)
                    if card.attempt_elapsed_ms
                    else "未调用"
                )
                retry = "，使用一次预写重试" if card.retry_used else ""
                lines.append(f"- {card.card_id}：{elapsed}{retry}；{card.status}")
        progress_lines = _probe_progress_lines(result.state_frames)
        if progress_lines:
            lines.extend(["", "### 探针案例推进", "", *progress_lines])
        if result.transcript:
            lines.extend(["", "### 对话逐字稿", ""])
            for turn in result.transcript:
                speaker = "受测者" if turn.speaker == "worker" else "来访者"
                text = " ".join(turn.text.splitlines()).strip()
                fact_annotation = _turn_fact_annotation(turn)
                lines.append(
                    f"- {turn.sequence}. {speaker}（{turn.client_turn_id}）："
                    f"{text}{fact_annotation}"
                )
        if result.final_issues:
            lines.extend(["", "未达预期："])
            lines.extend(f"- {issue}" for issue in result.final_issues)
        error_checks = [
            check
            for check in result.checks
            if not check.passed and check.severity == "error"
        ]
        warning_checks = [
            check
            for check in result.checks
            if not check.passed and check.severity == "warning"
        ]
        _append_check_findings(lines, "自动检查失败：", error_checks)
        _append_check_findings(lines, "自动检查告警：", warning_checks)
        if result.runtime_failures:
            lines.extend(["", "### 运行失败记录", ""])
            for failure in result.runtime_failures:
                lines.append(
                    "- "
                    f"{failure.component} / {failure.operation} / "
                    f"{failure.failure_code}；{failure.disposition}；"
                    f"{failure.attempt_count} 次尝试；"
                    f"可重试={'是' if failure.retryable else '否'}；"
                    f"记录={failure.id or '未落库'}"
                )
                summary = _optional_text(failure.details_json.get("summary"))
                if summary:
                    lines.append(f"  - 摘要：{summary}")
                for attempt in failure.attempts_json:
                    index = attempt.get("index", "?")
                    error_class = _optional_text(attempt.get("error_class"))
                    message = _optional_text(attempt.get("message"))
                    if message:
                        prefix = f"{error_class}：" if error_class else ""
                        lines.append(f"  - 第 {index} 次：{prefix}{message}")
                if failure.provider_request_id:
                    lines.append(
                        f"  - 供应商请求标识：{failure.provider_request_id}"
                    )
        if result.model_call_metrics:
            lines.extend(["", "### 模型调用", ""])
            failed_calls = sum(
                not metric.success for metric in result.model_call_metrics
            )
            repair_calls = sum(
                metric.call_kind == "repair"
                for metric in result.model_call_metrics
            )
            lines.append(f"- 失败调用：{failed_calls}；返修次数：{repair_calls}")
            for role in sorted(
                {metric.model_role for metric in result.model_call_metrics}
            ):
                role_metrics = [
                    metric
                    for metric in result.model_call_metrics
                    if metric.model_role == role
                ]
                prompt_tokens = sum(item.prompt_tokens for item in role_metrics)
                cached_tokens = sum(item.cached_tokens for item in role_metrics)
                ratio = (
                    cached_tokens / prompt_tokens * 100 if prompt_tokens else 0.0
                )
                lines.append(
                    f"- {role}：{len(role_metrics)} 次调用；缓存命中："
                    f"{cached_tokens}/{prompt_tokens}"
                    f"（{ratio:.1f}%）"
                )
            if result.final_snapshot.engine == "character_prompt":
                opening_metrics = [
                    metric
                    for metric in result.model_call_metrics
                    if _is_opening_metric(metric)
                ]
                realtime_metrics = [
                    metric
                    for metric in result.model_call_metrics
                    if not _is_opening_metric(metric)
                ]
                if opening_metrics:
                    opening_elapsed = " / ".join(
                        f"{metric.latency_ms} ms" for metric in opening_metrics
                    )
                    lines.append(
                        f"- 首次开场冷调用耗时：{opening_elapsed}"
                        "（单独展示，不计入后续实时门槛）"
                    )
                if realtime_metrics:
                    realtime_p90 = _nearest_rank_percentile(
                        [metric.latency_ms for metric in realtime_metrics],
                        90,
                    )
                    lines.append(f"- 后续实时调用耗时 P90：{realtime_p90} ms")
            lines.extend(
                "- "
                f"{metric.model_role} / {metric.model_name} / "
                f"{metric.cache_mode} / {metric.latency_ms} ms；"
                f"call_kind={metric.call_kind}；"
                f"success={str(metric.success).lower()}；"
                f"request_id={metric.request_id or '无'}"
                for metric in result.model_call_metrics
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _nearest_rank_percentile(values: Sequence[int], percentile: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = (len(ordered) * percentile + 99) // 100
    return ordered[max(0, rank - 1)]


def _is_opening_metric(metric: ModelCallEvidence) -> bool:
    return (metric.client_turn_id or "").startswith("opening-")


def runtime_quality_issues(
    metrics: Sequence[ModelCallEvidence],
    *,
    expected_turn_ids_by_role: dict[str, Sequence[str]],
    runtime_engine: RuntimeEngine = "workflow",
) -> list[str]:
    issues: list[str] = []
    repair_count = sum(metric.call_kind == "repair" for metric in metrics)
    if repair_count > 1:
        issues.append("模型返修超过一次")

    for role, expected_turn_ids in expected_turn_ids_by_role.items():
        expected_ids = list(dict.fromkeys(expected_turn_ids))
        expected = len(expected_ids)
        role_metrics = [metric for metric in metrics if metric.model_role == role]
        if expected == 0:
            if role_metrics:
                issues.append(
                    f"{role.capitalize()} 调用 {len(role_metrics)}，预期 0"
                )
            continue
        initial_metrics = [
            metric for metric in role_metrics if metric.call_kind == "initial"
        ]
        initial_count = len(initial_metrics)
        repair_role_count = sum(
            metric.call_kind == "repair" for metric in role_metrics
        )
        if initial_count != expected:
            issues.append(
                f"{role.capitalize()} 初次调用 {initial_count}，预期 {expected}"
                f"（另有返修 {repair_role_count}）"
            )
        counts_by_turn = {
            turn_id: sum(
                metric.client_turn_id == turn_id for metric in initial_metrics
            )
            for turn_id in expected_ids
        }
        incomplete = [
            f"{turn_id}={count}"
            for turn_id, count in counts_by_turn.items()
            if count != 1
        ]
        if incomplete:
            issues.append(
                f"{role.capitalize()} 话轮指标不完整：" + ", ".join(incomplete)
            )

    latency_metrics = [
        metric
        for metric in metrics
        if not (
            runtime_engine == "character_prompt"
            and _is_opening_metric(metric)
        )
    ]
    latencies = [metric.latency_ms for metric in latency_metrics]
    if latencies:
        if max(latencies) > 20_000:
            issues.append("模型单次调用超过 20 秒")
        if _nearest_rank_percentile(latencies, 90) > 15_000:
            issues.append("模型调用耗时 P90 超过 15 秒")
    return issues


def _probe_progress_lines(frames: Sequence[StateFrame]) -> list[str]:
    lines: list[str] = []
    previous: StateFrame | None = None
    occurrences: dict[str, int] = {}
    for frame in frames:
        if frame.card_id is None:
            previous = frame
            continue

        occurrences[frame.card_id] = occurrences.get(frame.card_id, 0) + 1
        prior_fact_depths = previous.fact_depths if previous is not None else {}
        changed_facts = [
            f"{fact_id}:{depth}"
            for fact_id, depth in sorted(frame.fact_depths.items())
            if depth > prior_fact_depths.get(fact_id, 0)
        ]
        prior_event_ids = set(previous.event_ids if previous is not None else [])
        new_event_ids: list[str] = []
        seen_event_ids: set[str] = set()
        for event_id in frame.event_ids:
            if event_id not in prior_event_ids and event_id not in seen_event_ids:
                new_event_ids.append(event_id)
                seen_event_ids.add(event_id)
        attempt = (
            f"（第 {occurrences[frame.card_id]} 次）"
            if occurrences[frame.card_id] > 1
            else ""
        )
        prior_stage = previous.conversation_stage if previous is not None else None
        stage_progress = (
            f"{prior_stage} → {frame.conversation_stage}"
            if prior_stage is not None and prior_stage != frame.conversation_stage
            else frame.conversation_stage
        )
        lines.append(
            f"- {frame.card_id}{attempt}："
            f"案例阶段 {stage_progress}；"
            f"新增/加深事实 {', '.join(changed_facts) or '无'}；"
            f"新事件 {', '.join(new_event_ids) or '无'}；"
            f"互动影响 {frame.interaction_impact or '无'}；"
            f"紧张度 {frame.interaction_tension}；"
            f"继续意愿 {frame.willingness_to_continue}；"
            f"修复阶段 {frame.repair_stage}"
        )
        previous = frame
    return lines


def _turn_fact_annotation(turn: CapturedTurn) -> str:
    facts: list[str] = []
    if turn.speaker == "worker":
        plan = _mapping(turn.signals.get("turn_plan"))
        for fact_id, depth in _mapping(plan.get("allowed_fact_depths")).items():
            if isinstance(fact_id, str) and isinstance(depth, int) and depth > 0:
                facts.append(f"{fact_id}:{depth}")
    if not facts:
        return ""
    return f"【本轮许可 {', '.join(dict.fromkeys(facts))}】"


def _decode_message(payload: str) -> dict[str, object]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise SimulationProtocolError("WebSocket JSON 不是对象")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


async def _run_from_cli(args: argparse.Namespace) -> int:
    case_id = str(getattr(args, "case_id", "crisis_student_main"))
    scene = Scene(str(getattr(args, "scene", Scene.hotline.value)))
    catalog = getattr(args, "catalog", None)
    scenarios = load_scenarios(catalog)
    selected = select_scenarios(
        scenarios,
        args.suite,
        case_id=case_id,
        scene=scene,
    )
    package = CaseRepository().get(case_id)
    if scene not in package.case.supported_scenes:
        raise ValueError(f"案例 {case_id} 不支持场域 {scene.value}")
    if args.check_only:
        print(
            f"场景检查通过：案例 {case_id}，场域 {scene.value}，"
            + ", ".join(item.scenario_id for item in selected)
        )
        return 0

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=DEFAULT_TURN_TIMEOUT_SECONDS + 10,
    ) as client:
        api = HttpxClientAdapter(client)
        environment = await read_environment(api)
        if not environment.healthy:
            print("DEMO 后端尚未就绪")
            return 1
        if not environment.configured:
            print("尚未配置 API Key，请先在 DEMO 前端的设置页完成配置。")
            return 2
        runner = SimulationRunner(
            base_url=args.base_url,
            output_root=args.output_root,
            case_id=case_id,
            scene=scene,
        )
        results = await _run_selected_scenarios(runner, selected, api)
        result_dir = runner.write_results(results)
    for result in results:
        print(f"{result.scenario_id}: {'通过' if result.passed else '未通过'}")
    print(f"结果已写入：{result_dir}")
    if any(result.run_status == "failed" for result in results):
        return 2
    return 0 if all(result.passed for result in results) else 1


async def _run_selected_scenarios(
    runner: SimulationRunner,
    scenarios: Sequence[Scenario],
    api: HttpClientLike,
) -> list[ScenarioRunResult]:
    results: list[ScenarioRunResult] = []
    repair_calls = 0
    for scenario in scenarios:
        try:
            result = await runner.run_scenario(scenario, api)
        except (
            SimulationProtocolError,
            ConnectionClosed,
            httpx.HTTPError,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
        ) as exc:
            detail = _safe_exception_text(exc)
            failure = (
                exc.failure
                if isinstance(exc, SimulationProtocolError)
                else None
            ) or _simulation_failure(
                exc,
                session_id=None,
                operation="scenario_run",
                failure_code=(
                    "simulation_protocol"
                    if isinstance(exc, SimulationProtocolError)
                    else "simulation_runtime"
                ),
            )
            result = ScenarioRunResult(
                scenario_id=scenario.scenario_id,
                title=scenario.title,
                profile=(
                    runner.profile_for_scenario(scenario)
                    if isinstance(runner, SimulationRunner)
                    else scenario.profile
                ),
                case_id=getattr(runner, "case_id", scenario.case_id),
                scene=getattr(runner, "scene", Scene.hotline),
                session_id="not-created",
                passed=False,
                run_status="failed",
                checks_status="not_run",
                expectations_status="not_evaluated",
                cards=[],
                checks=[],
                final_issues=[f"运行中断：{detail}"],
                runtime_failures=[failure],
                final_snapshot=DatabaseSnapshot(
                    status="run_failed",
                    end_reason="technical_interruption",
                    scene=getattr(runner, "scene", Scene.hotline),
                    engine=(
                        runner.runtime_engine_for_case()
                        if isinstance(runner, SimulationRunner)
                        else "workflow"
                    ),
                    ending_route_id=None,
                    interaction_tension=0,
                    repair_stage="none",
                ),
            )
        if not scenario.objective_contracts:
            repair_calls += sum(
                metric.call_kind == "repair" for metric in result.model_call_metrics
            )
        if not scenario.objective_contracts and repair_calls > 1 and result.passed:
            result = result.model_copy(
                update={
                    "passed": False,
                    "final_issues": [
                        *result.final_issues,
                        "整套黑盒测评的模型返修累计超过一次",
                    ],
                }
            )
        results.append(result)
        if not result.passed:
            break
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="固定脚本黑箱测评")
    parser.add_argument(
        "--suite",
        default="normal",
        help="固定脚本编号，或 all；可用编号由目录文件决定",
    )
    parser.add_argument(
        "--case-id",
        default="crisis_student_main",
        help="运行脚本使用的案例编号",
    )
    parser.add_argument(
        "--scene",
        choices=[scene.value for scene in Scene],
        default=Scene.hotline.value,
        help="测评场域",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        help="固定脚本目录 JSON；不传时使用项目内置目录",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        return asyncio.run(_run_from_cli(args))
    except (httpx.HTTPError, OSError, SimulationProtocolError, ValueError) as exc:
        print(f"黑箱测评未完成：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

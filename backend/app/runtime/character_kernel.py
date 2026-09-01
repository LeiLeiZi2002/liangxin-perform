from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.audio.models import AudioKind
from app.cases.loader import CaseRepository
from app.runtime.character_provider import (
    CharacterContextExhaustedError,
    CharacterDefinition,
    CharacterOutput,
    CharacterOutputValidationError,
    CharacterProvider,
    CharacterRepository,
    CharacterTranscriptTurn,
)
from app.runtime.character_world import (
    SupportWorldAction,
    SupportWorldDefinition,
    SupportWorldView,
    apply_support_world_action,
    build_support_world_view,
    load_support_world,
    materialize_support_world,
    no_external_world_view,
    store_support_world,
)
from app.runtime.domain import ActorDelivery
from app.runtime.failures import RuntimeFailureRecorder
from app.runtime.kernel import (
    ActorRuntime,
    ActorTextCallback,
    AssessmentKernel,
    AudioChunkCallback,
    DirectorRuntime,
    KernelOpeningResult,
    KernelSessionConflictError,
    KernelSessionNotFoundError,
    KernelTurnConflictError,
    KernelTurnResult,
    LiveSnapshot,
    ModelMetricRecorder,
    PersistedTurn,
    PhaseCallback,
    RuntimePhase,
    SpeechMetricsInput,
    SpeechRuntime,
    TechnicalPauseError,
)
from app.sessions.models import (
    EndReason,
    Media,
    SessionRecord,
    SessionStatus,
    TurnRecord,
    TurnSpeaker,
    utc_now,
)

CHARACTER_PROMPT_ENGINE = "character_prompt"
WORKFLOW_ENGINE = "workflow"
CHARACTER_END_ROUTE_ID = "character_prompt_end"
_PRESERVE_PENDING = object()


def runtime_engine_from_state(state_json: dict[str, object]) -> str:
    runtime = state_json.get("runtime")
    if not isinstance(runtime, dict):
        return WORKFLOW_ENGINE
    engine = runtime.get("engine")
    return (
        CHARACTER_PROMPT_ENGINE
        if engine == CHARACTER_PROMPT_ENGINE
        else WORKFLOW_ENGINE
    )


class CharacterRuntime(Protocol):
    async def respond(
        self,
        *,
        character: CharacterDefinition,
        transcript: Sequence[CharacterTranscriptTurn],
        current_worker_text: str,
        opening: bool,
        current_scene: str,
        world_reality: str,
        allowed_world_actions: Sequence[SupportWorldAction],
        session_id: str | None = None,
        client_turn_id: str | None = None,
    ) -> CharacterOutput: ...


@dataclass(frozen=True, slots=True)
class _CharacterContext:
    record: SessionRecord
    transcript: list[CharacterTranscriptTurn]


@dataclass(frozen=True, slots=True)
class CharacterPendingRetry:
    session_id: str
    client_turn_id: str
    opening: bool
    text: str
    worker_pcm: bytes
    speech_metrics: SpeechMetricsInput | None
    world_time_advance_seconds: float


class CharacterPromptKernel(AssessmentKernel):
    """单次角色文字生成主链；不读取或推进旧 Workflow 人物状态。"""

    manual_turn_completion = True

    @staticmethod
    def _failure_identity(
        phase: RuntimePhase,
        error: Exception,
    ) -> tuple[str, str, str]:
        if isinstance(error, CharacterContextExhaustedError):
            return "actor", "context_budget", "actor.context_exhausted"
        return AssessmentKernel._failure_identity(phase, error)

    def __init__(
        self,
        *,
        engine: Engine,
        characters: CharacterRepository,
        character: CharacterRuntime,
        speech: SpeechRuntime | None,
        audio_root: Path,
        failure_recorder: RuntimeFailureRecorder | None = None,
        model_call_recorder: ModelMetricRecorder | None = None,
    ) -> None:
        # 基类只承载会话、音频、失败和指标基础设施；本类覆盖全部 Director/Actor 流程。
        super().__init__(
            engine=engine,
            cases=CaseRepository(),
            director=cast(DirectorRuntime, object()),
            actor=cast(ActorRuntime, object()),
            speech=speech,
            audio_root=audio_root,
            failure_recorder=failure_recorder,
            model_call_recorder=model_call_recorder,
        )
        self._characters = characters
        self._character = character
        self._pending_outputs: dict[tuple[str, str, bool], CharacterOutput] = {}
        self._pending_retries: dict[
            tuple[str, str, bool], CharacterPendingRetry
        ] = {}

    async def process_worker_turn(
        self,
        *,
        session_id: str,
        client_turn_id: str,
        text: str,
        worker_pcm: bytes = b"",
        speech_metrics: SpeechMetricsInput | None = None,
        synthesize_audio: bool = True,
        on_phase: PhaseCallback | None = None,
        on_actor_text: ActorTextCallback | None = None,
        on_audio_chunk: AudioChunkCallback | None = None,
        capture_failure_payload: bool = False,
        world_time_advance_seconds: float = 0,
    ) -> KernelTurnResult:
        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("受测者发言不能为空")
        if world_time_advance_seconds < 0:
            raise ValueError("现实事件时间推进不能为负数")
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            output_key = (session_id, client_turn_id, False)
            if self._existing_context_exhaustion(
                session_id,
                client_turn_id,
                expected_worker_text=normalized_text,
            ):
                raise TechnicalPauseError(
                    RuntimePhase.acting,
                    can_retry=False,
                    failure_code="actor.context_exhausted",
                )
            loaded = self._load_character_context(session_id)
            existing = self._existing_character_pair(
                loaded.record,
                client_turn_id,
                expected_worker_text=normalized_text,
            )
            if existing is not None:
                self._pending_outputs.pop(output_key, None)
                self._pending_retries.pop(output_key, None)
                return existing
            if self._pending_ending_route(loaded.record.state_json) is not None:
                raise KernelSessionConflictError("人物会话已经进入结束状态")
            self._set_runtime_phase(session_id, RuntimePhase.acting)
            character_definition = self._characters.get(loaded.record.case_id)
            pending_retry = self._pending_retries.get(output_key)
            if pending_retry is not None:
                if pending_retry.text != normalized_text:
                    raise KernelTurnConflictError(
                        "请求标识对应的工作者发言不一致"
                    )
                worker_pcm = pending_retry.worker_pcm
                if speech_metrics is None:
                    speech_metrics = pending_retry.speech_metrics
                world_time_advance_seconds = (
                    pending_retry.world_time_advance_seconds
                )
            world_view = self._world_view(
                character_definition.world,
                loaded.record.state_json,
                world_time_advance_seconds=world_time_advance_seconds,
            )

            async def act() -> CharacterOutput:
                generated = await self._character.respond(
                    character=character_definition,
                    transcript=loaded.transcript,
                    current_worker_text=normalized_text,
                    opening=False,
                    current_scene=loaded.record.scene.value,
                    world_reality=world_view.reality,
                    allowed_world_actions=world_view.allowed_actions,
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                )
                self._require_allowed_action(generated, world_view.allowed_actions)
                return generated

            output = self._pending_outputs.get(output_key)
            if output is None:
                try:
                    output = await self._attempt_stage(
                        session_id,
                        RuntimePhase.acting,
                        act,
                        on_phase,
                        client_turn_id=client_turn_id,
                        failure_details=(
                            (lambda: {"worker_text": normalized_text})
                            if capture_failure_payload
                            else None
                        ),
                    )
                except TechnicalPauseError as exc:
                    if isinstance(exc.__cause__, CharacterContextExhaustedError):
                        self._commit_context_exhausted_worker(
                            loaded=loaded,
                            client_turn_id=client_turn_id,
                            worker_text=normalized_text,
                            worker_pcm=worker_pcm,
                            speech_metrics=speech_metrics,
                        )
                    raise
                self._pending_outputs[output_key] = output
                self._pending_retries[output_key] = CharacterPendingRetry(
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                    opening=False,
                    text=normalized_text,
                    worker_pcm=worker_pcm,
                    speech_metrics=speech_metrics,
                    world_time_advance_seconds=world_time_advance_seconds,
                )
            audio_chunks: tuple[bytes, ...] = ()
            if loaded.record.media is Media.voice and synthesize_audio:
                audio_chunks = await self._synthesize(
                    session_id,
                    output.spoken_text.strip(),
                    ActorDelivery(pace=output.delivery_hint.strip()),
                    on_phase,
                    on_actor_text,
                    on_audio_chunk,
                    client_turn_id=client_turn_id,
                )
            try:
                committed = self._commit_character_pair(
                    loaded=loaded,
                    client_turn_id=client_turn_id,
                    worker_text=normalized_text,
                    output=output,
                    worker_pcm=worker_pcm,
                    client_pcm=b"".join(audio_chunks),
                    speech_metrics=speech_metrics,
                    world_definition=character_definition.world,
                    world_time_advance_seconds=world_time_advance_seconds,
                )
            except Exception as exc:
                raise await self._persistence_pause(
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                    failed_phase=(
                        RuntimePhase.synthesizing
                        if loaded.record.media is Media.voice and synthesize_audio
                        else RuntimePhase.acting
                    ),
                    error=exc,
                    operation="commit_character_turn",
                ) from exc
            self._pending_outputs.pop(output_key, None)
            self._pending_retries.pop(output_key, None)
            return KernelTurnResult(
                worker=committed.worker,
                client=committed.client,
                audio_chunks=audio_chunks,
                ending_route_id=(
                    CHARACTER_END_ROUTE_ID if output.end_session else None
                ),
            )

    async def generate_opening(
        self,
        *,
        session_id: str,
        client_turn_id: str,
        synthesize_audio: bool = True,
        on_phase: PhaseCallback | None = None,
        on_actor_text: ActorTextCallback | None = None,
        on_audio_chunk: AudioChunkCallback | None = None,
        capture_failure_payload: bool = False,
    ) -> KernelOpeningResult:
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            output_key = (session_id, client_turn_id, True)
            loaded = self._load_character_context(session_id)
            existing = self._existing_character_opening(
                loaded.record,
                client_turn_id,
            )
            if existing is not None:
                self._pending_outputs.pop(output_key, None)
                self._pending_retries.pop(output_key, None)
                return existing
            if loaded.transcript:
                raise KernelSessionConflictError("会话已经开始，不再生成开场")
            self._set_runtime_phase(session_id, RuntimePhase.acting)
            character_definition = self._characters.get(loaded.record.case_id)
            world_view = self._world_view(
                character_definition.world,
                loaded.record.state_json,
                world_time_advance_seconds=0,
            )
            opening_actions = (SupportWorldAction.none,)

            async def act() -> CharacterOutput:
                generated = await self._character.respond(
                    character=character_definition,
                    transcript=loaded.transcript,
                    current_worker_text="",
                    opening=True,
                    current_scene=loaded.record.scene.value,
                    world_reality=world_view.reality,
                    allowed_world_actions=opening_actions,
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                )
                self._require_allowed_action(generated, opening_actions)
                return generated

            output = self._pending_outputs.get(output_key)
            if output is None:
                output = await self._attempt_stage(
                    session_id,
                    RuntimePhase.acting,
                    act,
                    on_phase,
                    client_turn_id=client_turn_id,
                    failure_details=(
                        (lambda: {"opening": True})
                        if capture_failure_payload
                        else None
                    ),
                )
                self._pending_outputs[output_key] = output
                self._pending_retries[output_key] = CharacterPendingRetry(
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                    opening=True,
                    text="",
                    worker_pcm=b"",
                    speech_metrics=None,
                    world_time_advance_seconds=0,
                )
            audio_chunks: tuple[bytes, ...] = ()
            if loaded.record.media is Media.voice and synthesize_audio:
                audio_chunks = await self._synthesize(
                    session_id,
                    output.spoken_text.strip(),
                    ActorDelivery(pace=output.delivery_hint.strip()),
                    on_phase,
                    on_actor_text,
                    on_audio_chunk,
                    client_turn_id=client_turn_id,
                )
            try:
                client = self._commit_character_opening(
                    loaded=loaded,
                    client_turn_id=client_turn_id,
                    output=output,
                    client_pcm=b"".join(audio_chunks),
                    world_definition=character_definition.world,
                )
            except Exception as exc:
                raise await self._persistence_pause(
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                    failed_phase=(
                        RuntimePhase.synthesizing
                        if loaded.record.media is Media.voice and synthesize_audio
                        else RuntimePhase.acting
                    ),
                    error=exc,
                    operation="commit_character_opening",
                ) from exc
            self._pending_outputs.pop(output_key, None)
            self._pending_retries.pop(output_key, None)
            return KernelOpeningResult(
                client=client,
                audio_chunks=audio_chunks,
                ending_route_id=(
                    CHARACTER_END_ROUTE_ID if output.end_session else None
                ),
            )

    def pending_retry(self, session_id: str) -> CharacterPendingRetry | None:
        matches = [
            pending
            for (pending_session_id, _, _), pending in self._pending_retries.items()
            if pending_session_id == session_id
        ]
        if len(matches) > 1:
            raise KernelSessionConflictError("同一会话存在多个待重试角色话轮")
        return matches[0] if matches else None

    def snapshot(self, session_id: str) -> LiveSnapshot:
        with Session(self._engine) as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                raise KernelSessionNotFoundError(session_id)
            if record.status is not SessionStatus.active:
                raise KernelSessionConflictError("会话已结束")
            self._require_character_engine(record)
            turns = self._all_turns(db, session_id)
            return LiveSnapshot(
                session_id=session_id,
                media=record.media,
                phase=self._phase_from_payload(record.state_json),
                transcript=[self._persisted_turn(turn) for turn in turns],
                opening_delay_seconds=0.0 if not turns else None,
                pending_ending_route_id=self._pending_ending_route(
                    record.state_json
                ),
                technical_retry_allowed=(
                    self._technical_retry_allowed_from_payload(record.state_json)
                ),
            )

    def _load_character_context(self, session_id: str) -> _CharacterContext:
        with Session(self._engine) as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                raise KernelSessionNotFoundError(session_id)
            if record.status is not SessionStatus.active:
                raise KernelSessionConflictError("会话已结束，不能继续")
            self._require_character_engine(record)
            turns = self._all_turns(db, session_id)
            record_copy = SessionRecord.model_validate(record.model_dump())
        return _CharacterContext(
            record=record_copy,
            transcript=[
                CharacterTranscriptTurn(
                    speaker=turn.speaker.value,
                    text=turn.text,
                )
                for turn in turns
            ],
        )

    @staticmethod
    def _require_character_engine(record: SessionRecord) -> None:
        if runtime_engine_from_state(record.state_json) != CHARACTER_PROMPT_ENGINE:
            raise KernelSessionConflictError("会话不属于轻量角色内核")

    def _commit_character_pair(
        self,
        *,
        loaded: _CharacterContext,
        client_turn_id: str,
        worker_text: str,
        output: CharacterOutput,
        worker_pcm: bytes,
        client_pcm: bytes,
        speech_metrics: SpeechMetricsInput | None,
        world_definition: SupportWorldDefinition | None,
        world_time_advance_seconds: float,
    ) -> KernelTurnResult:
        worker_id = uuid4().hex
        client_id = uuid4().hex
        written_paths: list[Path] = []
        try:
            worker_audio = self._write_wav(
                loaded.record.id,
                worker_id,
                worker_pcm,
                sample_rate=16000,
            )
            client_audio = self._write_wav(
                loaded.record.id,
                client_id,
                client_pcm,
                sample_rate=24000,
            )
            written_paths.extend(
                path for path in (worker_audio, client_audio) if path is not None
            )
            with Session(self._engine) as db:
                record = db.get(SessionRecord, loaded.record.id)
                if record is None:
                    raise KernelSessionNotFoundError(loaded.record.id)
                existing = self._query_client_turns(
                    db,
                    record.id,
                    client_turn_id,
                )
                if existing:
                    return self._pair_from_character_records(
                        existing,
                        replayed=True,
                        expected_worker_text=worker_text,
                    )
                action_request = SupportWorldAction(output.action_request)
                if world_definition is None:
                    allowed_actions = no_external_world_view().allowed_actions
                    next_state_json = record.state_json
                    client_signals: dict[str, object] = {
                        "runtime_engine": CHARACTER_PROMPT_ENGINE,
                        "delivery_hint": output.delivery_hint.strip(),
                        "end_session": output.end_session,
                        "action_request": action_request.value,
                    }
                else:
                    world_now = utc_now() + timedelta(
                        seconds=world_time_advance_seconds
                    )
                    world_before = materialize_support_world(
                        load_support_world(record.state_json),
                        now=world_now,
                    )
                    allowed_actions = build_support_world_view(
                        world_definition,
                        world_before,
                    ).allowed_actions
                if action_request not in allowed_actions:
                    raise CharacterOutputValidationError(
                        f"当前现实状态不允许行动 {action_request.value}"
                    )
                if world_definition is not None:
                    world_after = apply_support_world_action(
                        world_definition,
                        world_before,
                        action_request,
                        now=world_now,
                    )
                    client_signals = {
                        "runtime_engine": CHARACTER_PROMPT_ENGINE,
                        "delivery_hint": output.delivery_hint.strip(),
                        "end_session": output.end_session,
                        "action_request": action_request.value,
                        "world_stage_before": world_before.stage.value,
                        "world_stage_after": world_after.stage.value,
                    }
                    next_state_json = store_support_world(
                        record.state_json,
                        world_after,
                    )
                sequence = self._next_sequence(db, record.id)
                turn_state = self._lightweight_turn_state()
                worker = TurnRecord(
                    id=worker_id,
                    session_id=record.id,
                    client_turn_id=client_turn_id,
                    sequence=sequence,
                    speaker=TurnSpeaker.worker,
                    text=worker_text,
                    audio_path=self._relative_audio_path(worker_audio),
                    provider="asr",
                    signals_json={"runtime_engine": CHARACTER_PROMPT_ENGINE},
                    state_before_json=turn_state,
                    state_after_json=turn_state,
                )
                client = TurnRecord(
                    id=client_id,
                    session_id=record.id,
                    client_turn_id=client_turn_id,
                    sequence=sequence + 1,
                    speaker=TurnSpeaker.client,
                    text=output.spoken_text.strip(),
                    audio_path=self._relative_audio_path(client_audio),
                    provider=CHARACTER_PROMPT_ENGINE,
                    signals_json=client_signals,
                    state_before_json=turn_state,
                    state_after_json=turn_state,
                    used_fact_ids=[],
                )
                record.state_json = self._state_with_phase(
                    next_state_json,
                    RuntimePhase.listening,
                    pending_ending_route_id=(
                        CHARACTER_END_ROUTE_ID if output.end_session else None
                    ),
                )
                record.updated_at = utc_now()
                db.add(worker)
                db.add(client)
                db.add(record)
                self._add_audio_record(
                    db,
                    record.id,
                    worker_audio,
                    AudioKind.worker_turn,
                    "asr",
                )
                self._add_audio_record(
                    db,
                    record.id,
                    client_audio,
                    AudioKind.client_turn,
                    self._tts_provider_name(),
                )
                if record.media is Media.voice and speech_metrics is not None:
                    db.add(
                        self._speech_metric(
                            record.id,
                            worker_id,
                            worker_text,
                            speech_metrics,
                        )
                    )
                db.commit()
                db.refresh(worker)
                db.refresh(client)
                return KernelTurnResult(
                    worker=self._persisted_turn(worker),
                    client=self._persisted_turn(client),
                    audio_chunks=(),
                    ending_route_id=(
                        CHARACTER_END_ROUTE_ID if output.end_session else None
                    ),
                )
        except Exception:
            for path in written_paths:
                path.unlink(missing_ok=True)
            raise

    def _commit_context_exhausted_worker(
        self,
        *,
        loaded: _CharacterContext,
        client_turn_id: str,
        worker_text: str,
        worker_pcm: bytes,
        speech_metrics: SpeechMetricsInput | None,
    ) -> None:
        worker_id = uuid4().hex
        worker_audio = self._write_wav(
            loaded.record.id,
            worker_id,
            worker_pcm,
            sample_rate=16000,
        )
        try:
            with Session(self._engine) as db:
                record = db.get(SessionRecord, loaded.record.id)
                if record is None:
                    raise KernelSessionNotFoundError(loaded.record.id)
                existing = self._query_client_turns(
                    db,
                    record.id,
                    client_turn_id,
                )
                if existing:
                    if (
                        len(existing) != 1
                        or existing[0].speaker is not TurnSpeaker.worker
                        or existing[0].text != worker_text
                    ):
                        raise KernelTurnConflictError(
                            "请求标识对应的工作者发言不一致"
                        )
                    return
                now = utc_now()
                turn_state = self._lightweight_turn_state()
                worker = TurnRecord(
                    id=worker_id,
                    session_id=record.id,
                    client_turn_id=client_turn_id,
                    sequence=self._next_sequence(db, record.id),
                    speaker=TurnSpeaker.worker,
                    text=worker_text,
                    audio_path=self._relative_audio_path(worker_audio),
                    provider="asr",
                    signals_json={
                        "runtime_engine": CHARACTER_PROMPT_ENGINE,
                        "technical_interruption": "actor.context_exhausted",
                    },
                    state_before_json=turn_state,
                    state_after_json=turn_state,
                )
                record.status = SessionStatus.ended
                record.end_reason = EndReason.technical_interruption
                record.ended_at = now
                record.state_json = self._state_with_phase(
                    record.state_json,
                    RuntimePhase.ended,
                    technical_retry_allowed=False,
                    pending_ending_route_id=None,
                )
                record.updated_at = now
                db.add(worker)
                db.add(record)
                self._add_audio_record(
                    db,
                    record.id,
                    worker_audio,
                    AudioKind.worker_turn,
                    "asr",
                )
                if record.media is Media.voice and speech_metrics is not None:
                    db.add(
                        self._speech_metric(
                            record.id,
                            worker_id,
                            worker_text,
                            speech_metrics,
                        )
                    )
                db.commit()
        except Exception:
            if worker_audio is not None:
                worker_audio.unlink(missing_ok=True)
            raise

    def _commit_character_opening(
        self,
        *,
        loaded: _CharacterContext,
        client_turn_id: str,
        output: CharacterOutput,
        client_pcm: bytes,
        world_definition: SupportWorldDefinition | None,
    ) -> PersistedTurn:
        client_id = uuid4().hex
        audio_path = self._write_wav(
            loaded.record.id,
            client_id,
            client_pcm,
            sample_rate=24000,
        )
        try:
            with Session(self._engine) as db:
                record = db.get(SessionRecord, loaded.record.id)
                if record is None:
                    raise KernelSessionNotFoundError(loaded.record.id)
                existing = self._query_client_turns(
                    db,
                    record.id,
                    client_turn_id,
                )
                if existing:
                    if (
                        len(existing) != 1
                        or existing[0].speaker is not TurnSpeaker.client
                    ):
                        raise KernelSessionConflictError(
                            "请求标识已被工作者话轮使用"
                    )
                    return self._persisted_turn(existing[0])
                turn_state = self._lightweight_turn_state()
                signals: dict[str, object] = {
                    "runtime_engine": CHARACTER_PROMPT_ENGINE,
                    "delivery_hint": output.delivery_hint.strip(),
                    "end_session": output.end_session,
                    "ending_route_id": (
                        CHARACTER_END_ROUTE_ID if output.end_session else None
                    ),
                    "action_request": SupportWorldAction.none.value,
                }
                if world_definition is None:
                    next_state_json = record.state_json
                else:
                    world = load_support_world(record.state_json)
                    signals.update(
                        {
                            "world_stage_before": world.stage.value,
                            "world_stage_after": world.stage.value,
                        }
                    )
                    next_state_json = store_support_world(
                        record.state_json,
                        world,
                    )
                turn = TurnRecord(
                    id=client_id,
                    session_id=record.id,
                    client_turn_id=client_turn_id,
                    sequence=self._next_sequence(db, record.id),
                    speaker=TurnSpeaker.client,
                    text=output.spoken_text.strip(),
                    audio_path=self._relative_audio_path(audio_path),
                    provider=CHARACTER_PROMPT_ENGINE,
                    signals_json=signals,
                    state_before_json=turn_state,
                    state_after_json=turn_state,
                    used_fact_ids=[],
                )
                record.state_json = self._state_with_phase(
                    next_state_json,
                    RuntimePhase.listening,
                    pending_ending_route_id=(
                        CHARACTER_END_ROUTE_ID if output.end_session else None
                    ),
                )
                record.updated_at = utc_now()
                db.add(turn)
                db.add(record)
                self._add_audio_record(
                    db,
                    record.id,
                    audio_path,
                    AudioKind.client_turn,
                    self._tts_provider_name(),
                )
                db.commit()
                db.refresh(turn)
                return self._persisted_turn(turn)
        except Exception:
            if audio_path is not None:
                audio_path.unlink(missing_ok=True)
            raise

    def _existing_character_pair(
        self,
        record: SessionRecord,
        client_turn_id: str,
        *,
        expected_worker_text: str,
    ) -> KernelTurnResult | None:
        with Session(self._engine) as db:
            turns = self._query_client_turns(db, record.id, client_turn_id)
        if not turns:
            return None
        return self._pair_from_character_records(
            turns,
            replayed=True,
            expected_worker_text=expected_worker_text,
        )

    def _existing_character_opening(
        self,
        record: SessionRecord,
        client_turn_id: str,
    ) -> KernelOpeningResult | None:
        with Session(self._engine) as db:
            turns = self._query_client_turns(db, record.id, client_turn_id)
        if not turns:
            return None
        if len(turns) != 1 or turns[0].speaker is not TurnSpeaker.client:
            raise KernelSessionConflictError("请求标识已被工作者话轮使用")
        return KernelOpeningResult(
            client=self._persisted_turn(turns[0]),
            audio_chunks=(),
            replayed=True,
            ending_route_id=(
                str(turns[0].signals_json["ending_route_id"])
                if isinstance(turns[0].signals_json.get("ending_route_id"), str)
                and turns[0].signals_json["ending_route_id"]
                else (
                    CHARACTER_END_ROUTE_ID
                    if turns[0].signals_json.get("end_session") is True
                    else None
                )
            ),
        )

    def _existing_context_exhaustion(
        self,
        session_id: str,
        client_turn_id: str,
        *,
        expected_worker_text: str,
    ) -> bool:
        with Session(self._engine) as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                return False
            turns = self._query_client_turns(db, session_id, client_turn_id)
        if not turns:
            return False
        if not (
            len(turns) == 1
            and turns[0].speaker is TurnSpeaker.worker
            and turns[0].signals_json.get("technical_interruption")
            == "actor.context_exhausted"
        ):
            return False
        if turns[0].text != expected_worker_text:
            raise KernelTurnConflictError("请求标识对应的工作者发言不一致")
        return True

    @staticmethod
    def _pair_from_character_records(
        turns: list[TurnRecord],
        *,
        replayed: bool,
        expected_worker_text: str | None = None,
    ) -> KernelTurnResult:
        worker = next(
            (turn for turn in turns if turn.speaker is TurnSpeaker.worker),
            None,
        )
        client = next(
            (turn for turn in turns if turn.speaker is TurnSpeaker.client),
            None,
        )
        if worker is None or client is None:
            raise KernelSessionConflictError("请求标识已用于来访者开场")
        if expected_worker_text is not None and worker.text != expected_worker_text:
            raise KernelTurnConflictError("请求标识对应的工作者发言不一致")
        end_session = client.signals_json.get("end_session") is True
        return KernelTurnResult(
            worker=AssessmentKernel._persisted_turn(worker),
            client=AssessmentKernel._persisted_turn(client),
            audio_chunks=(),
            replayed=replayed,
            ending_route_id=(CHARACTER_END_ROUTE_ID if end_session else None),
        )

    def _set_runtime_phase(
        self,
        session_id: str,
        phase: RuntimePhase,
        *,
        technical_retry_allowed: bool = False,
    ) -> None:
        with Session(self._engine) as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                raise KernelSessionNotFoundError(session_id)
            if record.status is SessionStatus.ended:
                return
            self._require_character_engine(record)
            record.state_json = self._state_with_phase(
                record.state_json,
                phase,
                technical_retry_allowed=technical_retry_allowed,
            )
            record.updated_at = utc_now()
            db.add(record)
            db.commit()

    @staticmethod
    def _state_with_phase(
        state_json: dict[str, object],
        phase: RuntimePhase,
        *,
        technical_retry_allowed: bool = False,
        pending_ending_route_id: object = _PRESERVE_PENDING,
    ) -> dict[str, object]:
        payload = dict(state_json)
        current_runtime = payload.get("runtime")
        runtime = dict(current_runtime) if isinstance(current_runtime, dict) else {}
        runtime.update(
            {
                "engine": CHARACTER_PROMPT_ENGINE,
                "phase": phase.value,
                "technical_retry_allowed": technical_retry_allowed,
            }
        )
        if pending_ending_route_id is not _PRESERVE_PENDING:
            if isinstance(pending_ending_route_id, str) and pending_ending_route_id:
                runtime["pending_ending_route_id"] = pending_ending_route_id
            else:
                runtime.pop("pending_ending_route_id", None)
        payload["runtime"] = runtime
        return payload

    @staticmethod
    def _pending_ending_route(state_json: dict[str, object]) -> str | None:
        runtime = state_json.get("runtime")
        if not isinstance(runtime, dict):
            return None
        value = runtime.get("pending_ending_route_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _world_view(
        definition: SupportWorldDefinition | None,
        state_json: dict[str, object],
        *,
        world_time_advance_seconds: float,
    ) -> SupportWorldView:
        if definition is None:
            return no_external_world_view()
        world = materialize_support_world(
            load_support_world(state_json),
            now=utc_now() + timedelta(seconds=world_time_advance_seconds),
        )
        return build_support_world_view(definition, world)

    @staticmethod
    def _require_allowed_action(
        output: CharacterOutput,
        allowed_actions: Sequence[SupportWorldAction],
    ) -> None:
        try:
            action_request = SupportWorldAction(output.action_request)
        except (TypeError, ValueError) as exc:
            raise CharacterOutputValidationError(
                "来访者返回了未知的现实行动"
            ) from exc
        if action_request not in allowed_actions:
            raise CharacterOutputValidationError(
                f"当前现实状态不允许行动 {action_request.value}"
            )

    @staticmethod
    def _lightweight_turn_state() -> dict[str, object]:
        return {"runtime": {"engine": CHARACTER_PROMPT_ENGINE}}

    def _tts_provider_name(self) -> str:
        return self._speech.tts_model_name if self._speech is not None else "tts"


def build_character_prompt_kernel(
    *,
    engine: Engine,
    character_provider: CharacterProvider,
    speech: SpeechRuntime | None,
    audio_root: Path,
    failure_recorder: RuntimeFailureRecorder | None = None,
    model_call_recorder: ModelMetricRecorder | None = None,
) -> CharacterPromptKernel:
    return CharacterPromptKernel(
        engine=engine,
        characters=CharacterRepository(),
        character=character_provider,
        speech=speech,
        audio_root=audio_root,
        failure_recorder=failure_recorder,
        model_call_recorder=model_call_recorder,
    )

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.audio.models import AudioKind, AudioRecord, SpeechMetricRecord
from app.cases.domain import CasePackage
from app.cases.loader import CaseRepository
from app.runtime.domain import (
    ActorDelivery,
    ActorOutput,
    ActorOutputValidationError,
    ActorState,
    ActorStateValidationError,
    ActorView,
    DialogueTurn,
    DirectorDecision,
    FactProposalValidationError,
    TurnPlan,
    WorkflowDecisionError,
    commit_turn_plan,
    compile_actor_view,
    compile_speech_delivery,
    opening_turn_plan,
    resolve_turn_plan,
)
from app.runtime.failures import (
    FailureAttempt,
    FailureDisposition,
    RuntimeFailure,
    RuntimeFailureRecorder,
    exception_failure_attempts,
    exception_failure_details,
    failure_attempt_from_exception,
)
from app.runtime.metrics import ModelCallMetric
from app.runtime.models import (
    CacheMode,
    ModelCallKind,
    ModelRole,
    RuntimeFailureRecord,
)
from app.runtime.providers import (
    NonRetryableRuntimeModelError,
    RepairableModelOutputError,
)
from app.sessions.models import (
    EndReason,
    Media,
    Scene,
    SessionRecord,
    SessionStatus,
    TurnRecord,
    TurnSpeaker,
    utc_now,
)
from app.sessions.service import mark_session_ended

logger = logging.getLogger(__name__)


class RuntimePhase(StrEnum):
    listening = "listening"
    directing = "directing"
    acting = "acting"
    synthesizing = "synthesizing"
    playing = "playing"
    technical_paused = "technical_paused"
    ended = "ended"


class KernelSessionNotFoundError(LookupError):
    pass


class KernelSessionConflictError(RuntimeError):
    pass


class KernelTurnConflictError(KernelSessionConflictError):
    """同一来访者发言标识被用于不同的工作者正文。"""


class TechnicalPauseError(RuntimeError):
    def __init__(
        self,
        failed_phase: RuntimePhase,
        *,
        can_retry: bool = True,
        failure_id: str | None = None,
        failure_code: str | None = None,
        failure_record: RuntimeFailureRecord | None = None,
    ) -> None:
        self.failed_phase = failed_phase
        self.can_retry = can_retry
        self.failure_id = failure_id
        self.failure_code = failure_code
        self.failure_record = failure_record
        super().__init__("来访者的信号不太稳定")


class _PartialSpeechFailure(RuntimeError):
    def __init__(self, audio_chunk_count: int, audio_byte_count: int) -> None:
        self.audio_chunk_count = audio_chunk_count
        self.audio_byte_count = audio_byte_count
        super().__init__("语音播放前已收到部分音频，不能自动重试")


class DirectorRuntime(Protocol):
    async def decide(
        self,
        *,
        package: CasePackage,
        scene: Scene,
        state: ActorState,
        history: Sequence[DialogueTurn],
        current_worker_text: str,
        session_id: str,
        client_turn_id: str,
        feedback: str | None = None,
    ) -> DirectorDecision: ...


class ActorRuntime(Protocol):
    async def respond(
        self,
        view: ActorView,
        *,
        session_id: str,
        client_turn_id: str,
    ) -> ActorOutput: ...


class SpeechRuntime(Protocol):
    @property
    def tts_model_name(self) -> str: ...

    def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
    ) -> AsyncIterator[bytes]: ...


class ModelMetricRecorder(Protocol):
    def record(self, metric: ModelCallMetric) -> None: ...


PhaseCallback = Callable[[RuntimePhase], Awaitable[None]]
ActorTextCallback = Callable[[str], object]
AudioChunkCallback = Callable[[bytes], object]


@dataclass(frozen=True, slots=True)
class SpeechMetricsInput:
    first_response_ms: int = 0
    speech_duration_ms: int = 0
    pause_durations_ms: tuple[int, ...] = ()
    supplement_count: int = 0
    overlap_duration_ms: int = 0
    excluded_technical_ms: int = 0
    asr_sentences: tuple[dict[str, object], ...] = ()


class PersistedTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    sequence: int
    speaker: TurnSpeaker
    text: str
    client_turn_id: str


@dataclass(frozen=True, slots=True)
class KernelTurnResult:
    worker: PersistedTurn
    client: PersistedTurn
    audio_chunks: tuple[bytes, ...]
    replayed: bool = False
    ending_route_id: str | None = None


@dataclass(frozen=True, slots=True)
class KernelOpeningResult:
    client: PersistedTurn
    audio_chunks: tuple[bytes, ...]
    replayed: bool = False
    ending_route_id: str | None = None


class LiveSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    media: Media
    phase: RuntimePhase
    transcript: list[PersistedTurn]
    opening_delay_seconds: float | None = None
    pending_ending_route_id: str | None = None
    technical_retry_allowed: bool = False


ResultT = TypeVar("ResultT")


class AssessmentKernel:
    """按固定次序运行 Director、Actor 和语音。

    模型调用时不持有数据库事务；只有整轮成功后才会一次提交。
    """

    def __init__(
        self,
        *,
        engine: Engine,
        cases: CaseRepository,
        director: DirectorRuntime,
        actor: ActorRuntime,
        speech: SpeechRuntime | None,
        audio_root: Path,
        failure_recorder: RuntimeFailureRecorder | None = None,
        model_call_recorder: ModelMetricRecorder | None = None,
    ) -> None:
        self._engine = engine
        self._cases = cases
        self._director = director
        self._actor = actor
        self._speech = speech
        self._audio_root = audio_root.resolve()
        self._failure_recorder = failure_recorder or RuntimeFailureRecorder(engine)
        self._model_call_recorder = model_call_recorder
        self._locks: dict[str, asyncio.Lock] = {}

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
    ) -> KernelTurnResult:
        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("受测者发言不能为空")
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            loaded = await self._load_context_or_pause(
                session_id=session_id,
                client_turn_id=client_turn_id,
                failed_phase=RuntimePhase.directing,
                pending_text=normalized_text,
            )
            existing = self._existing_pair(
                loaded.record,
                client_turn_id,
                expected_worker_text=normalized_text,
            )
            if existing is not None:
                return existing
            accepted_route_id = loaded.state.ending_state.accepted_route_id
            if accepted_route_id is not None:
                raise KernelSessionConflictError(
                    f"人物会话已经进入结束状态：{accepted_route_id}"
                )
            self._set_runtime_phase(session_id, RuntimePhase.directing)
            worker_id = uuid4().hex
            history = [*loaded.history, DialogueTurn(
                turn_id=worker_id,
                role="worker",
                text=normalized_text,
            )]

            director_feedback: str | None = None
            director_attempt_payloads: list[dict[str, object]] = []

            async def decide() -> tuple[DirectorDecision, TurnPlan]:
                nonlocal director_feedback
                try:
                    decision = await self._director.decide(
                        package=loaded.package,
                        scene=loaded.record.scene,
                        state=loaded.state,
                        history=history,
                        current_worker_text=normalized_text,
                        session_id=session_id,
                        client_turn_id=client_turn_id,
                        feedback=director_feedback,
                    )
                except RepairableModelOutputError:
                    director_feedback = (
                        "上次没有返回可读取的 Director JSON，"
                        "请严格按约定结构重写完整决策。"
                    )
                    raise
                if capture_failure_payload:
                    director_attempt_payloads.append(
                        {"candidate": decision.model_dump(mode="json")}
                    )
                plan = resolve_turn_plan(
                    loaded.package,
                    loaded.record.scene,
                    loaded.state,
                    decision,
                    history,
                )
                return decision, plan

            decision, plan = await self._attempt_stage(
                session_id,
                RuntimePhase.directing,
                decide,
                on_phase,
                client_turn_id=client_turn_id,
                failure_details=(
                    lambda: {
                        "worker_text": normalized_text,
                        "attempt_payloads": director_attempt_payloads,
                    }
                    if capture_failure_payload
                    else None
                ),
            )
            actor_view = compile_actor_view(
                package=loaded.package,
                scene=loaded.record.scene,
                state=loaded.state,
                history=history,
                current_worker_text=normalized_text,
                plan=plan,
            )

            async def act() -> ActorOutput:
                return await self._actor.respond(
                    actor_view,
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                )

            actor_output = await self._attempt_stage(
                session_id,
                RuntimePhase.acting,
                act,
                on_phase,
                client_turn_id=client_turn_id,
            )
            actor_output = actor_output.model_copy(
                update={"spoken_text": self._tts_text(actor_output.spoken_text)}
            )
            audio_chunks: tuple[bytes, ...] = ()
            if loaded.record.media is Media.voice and synthesize_audio:
                audio_chunks = await self._synthesize(
                    session_id,
                    actor_output.spoken_text,
                    compile_speech_delivery(loaded.package, plan),
                    on_phase,
                    on_actor_text,
                    on_audio_chunk,
                    client_turn_id=client_turn_id,
                )
            try:
                result = self._commit_pair(
                    loaded=loaded,
                    worker_id=worker_id,
                    client_turn_id=client_turn_id,
                    worker_text=normalized_text,
                    actor_output=actor_output,
                    decision=decision,
                    plan=plan,
                    final_state=commit_turn_plan(loaded.package, loaded.state, plan),
                    worker_pcm=worker_pcm,
                    client_pcm=b"".join(audio_chunks),
                    speech_metrics=speech_metrics,
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
                    operation="commit",
                ) from exc
            return KernelTurnResult(
                worker=result.worker,
                client=result.client,
                audio_chunks=audio_chunks,
                ending_route_id=(
                    plan.legal_ending.route_id
                    if plan.legal_ending is not None
                    and plan.legal_ending.ends_session
                    else None
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
            loaded = await self._load_context_or_pause(
                session_id=session_id,
                client_turn_id=client_turn_id,
                failed_phase=RuntimePhase.acting,
            )
            existing = self._existing_opening(loaded.record, client_turn_id)
            if existing is not None:
                return existing
            if loaded.history:
                raise KernelSessionConflictError("会话已经开始，不再生成开场")
            plan = opening_turn_plan(loaded.state)
            view = compile_actor_view(
                package=loaded.package,
                scene=loaded.record.scene,
                state=loaded.state,
                history=loaded.history,
                current_worker_text="",
                plan=plan,
            )

            async def act() -> ActorOutput:
                return await self._actor.respond(
                    view,
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                )

            actor_output = await self._attempt_stage(
                session_id,
                RuntimePhase.acting,
                act,
                on_phase,
                client_turn_id=client_turn_id,
                failure_details=(
                    lambda: {"opening": True}
                    if capture_failure_payload
                    else None
                ),
            )
            actor_output = actor_output.model_copy(
                update={"spoken_text": self._tts_text(actor_output.spoken_text)}
            )
            audio_chunks: tuple[bytes, ...] = ()
            if loaded.record.media is Media.voice and synthesize_audio:
                audio_chunks = await self._synthesize(
                    session_id,
                    actor_output.spoken_text,
                    compile_speech_delivery(loaded.package, plan),
                    on_phase,
                    on_actor_text,
                    on_audio_chunk,
                    client_turn_id=client_turn_id,
                )
            try:
                client = self._commit_opening(
                    loaded=loaded,
                    client_turn_id=client_turn_id,
                    actor_output=actor_output,
                    final_state=commit_turn_plan(loaded.package, loaded.state, plan),
                    client_pcm=b"".join(audio_chunks),
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
                    operation="commit_opening",
                ) from exc
            return KernelOpeningResult(client=client, audio_chunks=audio_chunks)

    def snapshot(self, session_id: str) -> LiveSnapshot:
        with Session(self._engine) as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                raise KernelSessionNotFoundError(session_id)
            if record.status is not SessionStatus.active:
                raise KernelSessionConflictError("会话已结束")
            turns = self._all_turns(db, session_id)
            phase = self._phase_from_payload(record.state_json)
            package = self._cases.get(record.case_id)
            state = self._actor_state_from_payload(package, record.state_json)
            opening_delay_seconds: float | None = None
            if not turns:
                opening = package.actor.opening
                opening_delay_seconds = float(
                    opening.silence_seconds if opening.worker_starts else 0
                )
            return LiveSnapshot(
                session_id=session_id,
                media=record.media,
                phase=phase,
                transcript=[self._persisted_turn(turn) for turn in turns],
                opening_delay_seconds=opening_delay_seconds,
                pending_ending_route_id=state.ending_state.accepted_route_id,
                technical_retry_allowed=self._technical_retry_allowed_from_payload(
                    record.state_json
                ),
            )

    def resume_listening(self, session_id: str) -> None:
        self._set_runtime_phase(session_id, RuntimePhase.listening)

    def pause_technical(self, session_id: str, *, can_retry: bool) -> None:
        self._set_runtime_phase(
            session_id,
            RuntimePhase.technical_paused,
            technical_retry_allowed=can_retry,
        )

    def end_session(
        self,
        session_id: str,
        reason: EndReason = EndReason.user_ended,
    ) -> None:
        with Session(self._engine) as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                raise KernelSessionNotFoundError(session_id)
            if mark_session_ended(record, reason):
                db.add(record)
                db.commit()

    async def _synthesize(
        self,
        session_id: str,
        text: str,
        delivery: ActorDelivery,
        on_phase: PhaseCallback | None,
        on_actor_text: ActorTextCallback | None,
        on_audio_chunk: AudioChunkCallback | None,
        *,
        client_turn_id: str,
    ) -> tuple[bytes, ...]:
        attempt_number = 0

        async def synthesize() -> tuple[bytes, ...]:
            nonlocal attempt_number
            attempt_number += 1
            started = time.perf_counter()
            if self._speech is None:
                raise RuntimeError("当前语音服务未配置")
            if on_actor_text is not None:
                await self._invoke_callback(on_actor_text, text)
            collected: list[bytes] = []
            try:
                async for chunk in self._speech.synthesize(
                    text,
                    instruction=self._speech_instruction(delivery),
                ):
                    if not chunk:
                        continue
                    collected.append(chunk)
                    if on_audio_chunk is not None:
                        await self._invoke_callback(on_audio_chunk, chunk)
            except Exception as exc:
                await self._record_tts_call(
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                    model_name=self._speech.tts_model_name,
                    attempt_number=attempt_number,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                )
                if collected:
                    raise _PartialSpeechFailure(
                        len(collected),
                        sum(len(chunk) for chunk in collected),
                    ) from exc
                raise
            chunks = tuple(collected)
            if not chunks:
                error = RuntimeError("语音服务未返回音频")
                await self._record_tts_call(
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                    model_name=self._speech.tts_model_name,
                    attempt_number=attempt_number,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    success=False,
                )
                raise error
            await self._record_tts_call(
                session_id=session_id,
                client_turn_id=client_turn_id,
                model_name=self._speech.tts_model_name,
                attempt_number=attempt_number,
                latency_ms=int((time.perf_counter() - started) * 1000),
                success=True,
            )
            return chunks

        return await self._attempt_stage(
            session_id,
            RuntimePhase.synthesizing,
            synthesize,
            on_phase,
            client_turn_id=client_turn_id,
        )

    async def _record_tts_call(
        self,
        *,
        session_id: str,
        client_turn_id: str,
        model_name: str,
        attempt_number: int,
        latency_ms: int,
        success: bool,
    ) -> None:
        if self._model_call_recorder is None:
            return
        metric = ModelCallMetric(
            session_id=session_id,
            client_turn_id=client_turn_id,
            model_role=ModelRole.tts,
            model_name=model_name,
            call_kind=(
                ModelCallKind.initial
                if attempt_number == 1
                else ModelCallKind.repair
            ),
            cache_mode=CacheMode.none,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cached_tokens=0,
            cache_creation_input_tokens=0,
            latency_ms=max(0, latency_ms),
            success=success,
            request_id=None,
        )
        try:
            await asyncio.to_thread(self._model_call_recorder.record, metric)
        except Exception:
            logger.warning("语音合成技术指标写入失败", exc_info=True)

    @staticmethod
    def _speech_instruction(delivery: ActorDelivery) -> str:
        parts = [
            delivery.pace.strip(),
            delivery.volume.strip(),
            *[item.strip() for item in delivery.tone],
            *[item.strip() for item in delivery.pauses],
            *[item.strip() for item in delivery.vocal_texture],
        ]
        return "；".join(dict.fromkeys(item for item in parts if item))

    @staticmethod
    def _tts_text(text: str) -> str:
        cleaned = re.sub(
            r"(?:（[^（）\r\n]*）|\([^()\r\n]*\)|【[^【】\r\n]*】|\[[^\[\]\r\n]*\])",
            "",
            text,
        )
        return re.sub(r"[ \t]+", " ", cleaned).strip()

    @staticmethod
    async def _invoke_callback(
        callback: Callable[[ResultT], object],
        value: ResultT,
    ) -> None:
        result = callback(value)
        if inspect.isawaitable(result):
            await result

    async def _attempt_stage(
        self,
        session_id: str,
        phase: RuntimePhase,
        operation: Callable[[], Awaitable[ResultT]],
        on_phase: PhaseCallback | None,
        *,
        client_turn_id: str | None = None,
        failure_details: Callable[[], Mapping[str, object] | None] | None = None,
    ) -> ResultT:
        if on_phase is not None:
            await on_phase(phase)
        attempts: list[FailureAttempt] = []
        first_identity: tuple[str, str, str] | None = None
        for attempt in range(2):
            try:
                result = await operation()
            except Exception as exc:
                component, operation_name, failure_code = self._failure_identity(
                    phase,
                    exc,
                )
                if first_identity is None:
                    first_identity = (component, operation_name, failure_code)
                attempt_details = exception_failure_details(exc)
                if isinstance(exc, _PartialSpeechFailure):
                    attempt_details = {
                        **attempt_details,
                        "partial_audio": True,
                        "audio_chunk_count": exc.audio_chunk_count,
                        "audio_byte_count": exc.audio_byte_count,
                    }
                nested_attempts = exception_failure_attempts(exc)
                if nested_attempts:
                    attempts.extend(
                        replace(item, index=len(attempts) + offset)
                        for offset, item in enumerate(nested_attempts, start=1)
                    )
                else:
                    attempts.append(
                        failure_attempt_from_exception(
                            len(attempts) + 1,
                            exc,
                            call_kind=(
                                "initial" if attempt == 0 else "repair"
                            ),
                            details=attempt_details,
                        )
                    )
                actor_content_failure = (
                    phase is RuntimePhase.acting
                    and isinstance(exc, ActorOutputValidationError)
                )
                local_validation_failure = isinstance(
                    exc,
                    (
                        FactProposalValidationError,
                        WorkflowDecisionError,
                        ActorStateValidationError,
                    ),
                )
                if (
                    isinstance(exc, _PartialSpeechFailure)
                    or isinstance(exc, NonRetryableRuntimeModelError)
                    or actor_content_failure
                    or local_validation_failure
                    or attempt == 1
                ):
                    can_retry = bool(
                        actor_content_failure
                        and getattr(exc, "allow_user_retry", False)
                    ) or not isinstance(
                        exc,
                        (
                            ActorOutputValidationError,
                            FactProposalValidationError,
                            NonRetryableRuntimeModelError,
                            RepairableModelOutputError,
                            WorkflowDecisionError,
                            ActorStateValidationError,
                            _PartialSpeechFailure,
                        ),
                    )
                    details = self._failure_details(failure_details)
                    if isinstance(exc, _PartialSpeechFailure):
                        details.update(
                            {
                                "partial_audio": True,
                                "audio_chunk_count": exc.audio_chunk_count,
                                "audio_byte_count": exc.audio_byte_count,
                            }
                        )
                    failure_record = await self._record_failure(
                        RuntimeFailure(
                            session_id=session_id,
                            client_turn_id=client_turn_id,
                            component=component,
                            phase=phase.value,
                            operation=operation_name,
                            failure_code=failure_code,
                            retryable=can_retry,
                            disposition=FailureDisposition.technical_pause,
                            attempts=tuple(attempts),
                            details=details,
                        )
                    )
                    self._set_runtime_phase(
                        session_id,
                        RuntimePhase.technical_paused,
                        technical_retry_allowed=can_retry,
                    )
                    raise TechnicalPauseError(
                        phase,
                        can_retry=can_retry,
                        failure_id=(failure_record.id if failure_record else None),
                        failure_code=failure_code,
                        failure_record=failure_record,
                    ) from exc
            else:
                if attempts and first_identity is not None:
                    component, operation_name, failure_code = first_identity
                    await self._record_failure(
                        RuntimeFailure(
                            session_id=session_id,
                            client_turn_id=client_turn_id,
                            component=component,
                            phase=phase.value,
                            operation=operation_name,
                            failure_code=failure_code,
                            retryable=True,
                            disposition=FailureDisposition.recovered,
                            attempts=tuple(attempts),
                            details=self._failure_details(failure_details),
                        )
                    )
                return result
        raise AssertionError("重试流程未正常结束")

    @staticmethod
    def _failure_details(
        factory: Callable[[], Mapping[str, object] | None] | None,
    ) -> dict[str, object]:
        if factory is None:
            return {}
        details = factory()
        return dict(details) if details is not None else {}

    @staticmethod
    def _failure_identity(
        phase: RuntimePhase,
        error: Exception,
    ) -> tuple[str, str, str]:
        if phase is RuntimePhase.directing:
            if isinstance(error, WorkflowDecisionError):
                return (
                    "director",
                    "workflow_validation",
                    "director.workflow_validation",
                )
            if isinstance(error, FactProposalValidationError):
                return (
                    "director",
                    "workflow_validation",
                    "director.fact_validation",
                )
            if isinstance(error, ActorStateValidationError):
                return (
                    "director",
                    "state_validation",
                    "director.state_validation",
                )
            if isinstance(error, NonRetryableRuntimeModelError):
                return (
                    "director",
                    "provider_call",
                    "director.provider_non_retryable",
                )
            if isinstance(error, RepairableModelOutputError):
                return (
                    "director",
                    "output_parse",
                    "director.output_invalid",
                )
            return "director", "provider_call", "director.provider_failure"
        if phase is RuntimePhase.acting:
            if isinstance(error, ActorOutputValidationError):
                return (
                    "actor",
                    "output_validation",
                    "actor.output_validation",
                )
            if isinstance(error, NonRetryableRuntimeModelError):
                return (
                    "actor",
                    "provider_call",
                    "actor.provider_non_retryable",
                )
            return "actor", "provider_call", "actor.provider_failure"
        if isinstance(error, _PartialSpeechFailure):
            return "tts", "synthesis", "tts.partial_audio"
        if isinstance(error, NonRetryableRuntimeModelError):
            return "tts", "synthesis", "tts.provider_non_retryable"
        if str(error) == "语音服务未返回音频":
            return "tts", "synthesis", "tts.no_audio"
        return "tts", "synthesis", "tts.provider_failure"

    async def _record_failure(
        self,
        failure: RuntimeFailure,
    ) -> RuntimeFailureRecord | None:
        try:
            return await asyncio.to_thread(self._failure_recorder.record, failure)
        except Exception:
            logger.warning("运行失败记录写入失败", exc_info=True)
            return None

    async def _persistence_pause(
        self,
        *,
        session_id: str,
        client_turn_id: str,
        failed_phase: RuntimePhase,
        error: Exception,
        operation: str,
    ) -> TechnicalPauseError:
        record = await self._record_failure(
            RuntimeFailure(
                session_id=session_id,
                client_turn_id=client_turn_id,
                component="persistence",
                phase="persisting",
                operation=operation,
                failure_code="persistence.commit",
                retryable=False,
                disposition=FailureDisposition.technical_pause,
                attempts=(failure_attempt_from_exception(1, error),),
            )
        )
        try:
            self._set_runtime_phase(
                session_id,
                RuntimePhase.technical_paused,
                technical_retry_allowed=False,
            )
        except Exception:
            logger.warning("持久化失败后无法更新会话阶段", exc_info=True)
        return TechnicalPauseError(
            failed_phase,
            can_retry=False,
            failure_id=record.id if record else None,
            failure_code="persistence.commit",
            failure_record=record,
        )

    async def _load_context_or_pause(
        self,
        *,
        session_id: str,
        client_turn_id: str,
        failed_phase: RuntimePhase,
        pending_text: str | None = None,
    ) -> _LoadedContext:
        try:
            return self._load_context(session_id, pending_text=pending_text)
        except ValidationError as exc:
            failure_record = await self._record_failure(
                RuntimeFailure(
                    session_id=session_id,
                    client_turn_id=client_turn_id,
                    component="workflow",
                    phase=failed_phase.value,
                    operation="state_validation",
                    failure_code="runtime.actor_state_invalid",
                    retryable=False,
                    disposition=FailureDisposition.technical_pause,
                    attempts=(failure_attempt_from_exception(1, exc),),
                )
            )
            self._mark_invalid_state_pause(session_id)
            raise TechnicalPauseError(
                failed_phase,
                can_retry=False,
                failure_id=(failure_record.id if failure_record else None),
                failure_code="runtime.actor_state_invalid",
                failure_record=failure_record,
            ) from exc

    def _mark_invalid_state_pause(self, session_id: str) -> None:
        with Session(self._engine) as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                raise KernelSessionNotFoundError(session_id)
            payload = dict(record.state_json)
            payload["runtime"] = {
                "phase": RuntimePhase.technical_paused.value,
                "technical_retry_allowed": False,
            }
            record.state_json = payload
            record.updated_at = utc_now()
            db.add(record)
            db.commit()

    def _load_context(
        self,
        session_id: str,
        *,
        pending_text: str | None = None,
    ) -> _LoadedContext:
        del pending_text
        with Session(self._engine) as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                raise KernelSessionNotFoundError(session_id)
            if record.status is not SessionStatus.active:
                raise KernelSessionConflictError("会话已结束，不能继续")
            package = self._cases.get(record.case_id)
            turns = self._all_turns(db, session_id)
            state = self._actor_state_from_payload(package, record.state_json)
            record_copy = SessionRecord.model_validate(record.model_dump())
        return _LoadedContext(
            record=record_copy,
            package=package,
            state=state,
            history=[
                DialogueTurn(
                    turn_id=turn.id,
                    role=turn.speaker.value,
                    text=turn.text,
                )
                for turn in turns
            ],
        )

    def _commit_pair(
        self,
        *,
        loaded: _LoadedContext,
        worker_id: str,
        client_turn_id: str,
        worker_text: str,
        actor_output: ActorOutput,
        decision: DirectorDecision,
        plan: TurnPlan,
        final_state: ActorState,
        worker_pcm: bytes,
        client_pcm: bytes,
        speech_metrics: SpeechMetricsInput | None,
    ) -> KernelTurnResult:
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
            written_paths.extend(path for path in (worker_audio, client_audio) if path)
            with Session(self._engine) as db:
                record = db.get(SessionRecord, loaded.record.id)
                if record is None:
                    raise KernelSessionNotFoundError(loaded.record.id)
                existing = self._query_client_turns(db, record.id, client_turn_id)
                if existing:
                    replay = self._pair_from_records(
                        existing,
                        replayed=True,
                        expected_worker_text=worker_text,
                    )
                    return replay
                start = self._next_sequence(db, record.id)
                before_json = self._turn_state(loaded.state)
                after_json = self._turn_state(final_state)
                worker_turn = TurnRecord(
                    id=worker_id,
                    session_id=record.id,
                    client_turn_id=client_turn_id,
                    sequence=start,
                    speaker=TurnSpeaker.worker,
                    text=worker_text,
                    audio_path=self._relative_audio_path(worker_audio),
                    provider="qwen3.7-plus",
                    signals_json={
                        "director_decision": decision.model_dump(mode="json"),
                        "turn_plan": plan.model_dump(mode="json"),
                    },
                    state_before_json=before_json,
                    state_after_json=before_json,
                )
                client_turn = TurnRecord(
                    id=client_id,
                    session_id=record.id,
                    client_turn_id=client_turn_id,
                    sequence=start + 1,
                    speaker=TurnSpeaker.client,
                    text=actor_output.spoken_text.strip(),
                    audio_path=self._relative_audio_path(client_audio),
                    provider="qwen-plus-character",
                    state_before_json=before_json,
                    state_after_json=after_json,
                    used_fact_ids=list(plan.allowed_fact_depths),
                    signals_json={},
                )
                record.state_json = self._session_state(final_state, RuntimePhase.listening)
                record.updated_at = utc_now()
                db.add(worker_turn)
                db.add(client_turn)
                db.add(record)
                self._add_audio_record(
                    db,
                    record.id,
                    worker_audio,
                    AudioKind.worker_turn,
                    "qwen-audio-3.0-asr-flash-streaming",
                )
                self._add_audio_record(
                    db,
                    record.id,
                    client_audio,
                    AudioKind.client_turn,
                    "qwen-audio-3.0-tts-plus",
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
                db.refresh(worker_turn)
                db.refresh(client_turn)
                return KernelTurnResult(
                    worker=self._persisted_turn(worker_turn),
                    client=self._persisted_turn(client_turn),
                    audio_chunks=(),
                )
        except Exception:
            for path in written_paths:
                path.unlink(missing_ok=True)
            raise

    def _commit_opening(
        self,
        *,
        loaded: _LoadedContext,
        client_turn_id: str,
        actor_output: ActorOutput,
        final_state: ActorState,
        client_pcm: bytes,
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
                existing = self._query_client_turns(db, record.id, client_turn_id)
                if existing:
                    return self._persisted_turn(existing[0])
                turn = TurnRecord(
                    id=client_id,
                    session_id=record.id,
                    client_turn_id=client_turn_id,
                    sequence=self._next_sequence(db, record.id),
                    speaker=TurnSpeaker.client,
                    text=actor_output.spoken_text.strip(),
                    audio_path=self._relative_audio_path(audio_path),
                    provider="qwen-plus-character",
                    signals_json={},
                    state_before_json=self._turn_state(loaded.state),
                    state_after_json=self._turn_state(final_state),
                    used_fact_ids=[],
                )
                record.state_json = self._session_state(final_state, RuntimePhase.listening)
                record.updated_at = utc_now()
                db.add(turn)
                db.add(record)
                self._add_audio_record(
                    db,
                    record.id,
                    audio_path,
                    AudioKind.client_turn,
                    "qwen-audio-3.0-tts-plus",
                )
                db.commit()
                db.refresh(turn)
                return self._persisted_turn(turn)
        except Exception:
            if audio_path is not None:
                audio_path.unlink(missing_ok=True)
            raise

    def _existing_pair(
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
            return self._pair_from_records(
                turns,
                replayed=True,
                expected_worker_text=expected_worker_text,
            )

    def _existing_opening(
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
            )

    @staticmethod
    def _pair_from_records(
        turns: list[TurnRecord],
        *,
        replayed: bool,
        expected_worker_text: str | None = None,
    ) -> KernelTurnResult:
        worker = next((turn for turn in turns if turn.speaker is TurnSpeaker.worker), None)
        client = next((turn for turn in turns if turn.speaker is TurnSpeaker.client), None)
        if worker is None or client is None:
            raise KernelSessionConflictError("请求标识已用于来访者开场")
        if expected_worker_text is not None and worker.text != expected_worker_text:
            raise KernelTurnConflictError("请求标识对应的工作者发言不一致")
        return KernelTurnResult(
            worker=AssessmentKernel._persisted_turn(worker),
            client=AssessmentKernel._persisted_turn(client),
            audio_chunks=(),
            replayed=replayed,
            ending_route_id=AssessmentKernel._accepted_ending_route(
                client.state_after_json
            ),
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
            package = self._cases.get(record.case_id)
            state = self._actor_state_from_payload(package, record.state_json)
            record.state_json = self._session_state(
                state,
                phase,
                technical_retry_allowed=technical_retry_allowed,
            )
            record.updated_at = utc_now()
            db.add(record)
            db.commit()

    @staticmethod
    def _session_state(
        state: ActorState,
        phase: RuntimePhase,
        *,
        technical_retry_allowed: bool = False,
    ) -> dict[str, object]:
        return {
            "actor_state": state.model_dump(mode="json"),
            "runtime": {
                "phase": phase.value,
                "technical_retry_allowed": technical_retry_allowed,
            },
        }

    @staticmethod
    def _turn_state(state: ActorState) -> dict[str, object]:
        payload = state.model_dump(mode="json")
        payload["disclosed_fact_ids"] = sorted(
            fact_id
            for fact_id, fact_state in state.fact_states.items()
            if fact_state.disclosed_depth > 0
        )
        return payload

    @staticmethod
    def _actor_state_from_payload(
        package: CasePackage,
        payload: dict[str, object],
    ) -> ActorState:
        del package
        candidate = payload.get("actor_state", payload)
        return ActorState.model_validate(candidate)

    @staticmethod
    def _accepted_ending_route(payload: dict[str, object]) -> str | None:
        ending_state = payload.get("ending_state")
        if not isinstance(ending_state, dict):
            return None
        route_id = ending_state.get("accepted_route_id")
        return route_id if isinstance(route_id, str) and route_id else None

    @staticmethod
    def _phase_from_payload(payload: dict[str, object]) -> RuntimePhase:
        runtime = payload.get("runtime")
        if isinstance(runtime, dict):
            try:
                return RuntimePhase(str(runtime.get("phase", "listening")))
            except ValueError:
                pass
        return RuntimePhase.listening

    @staticmethod
    def _technical_retry_allowed_from_payload(payload: dict[str, object]) -> bool:
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            return False
        return runtime.get("technical_retry_allowed") is True

    @staticmethod
    def _all_turns(db: Session, session_id: str) -> list[TurnRecord]:
        return list(
            db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == session_id)
                .order_by(col(TurnRecord.sequence))
            ).all()
        )

    @staticmethod
    def _query_client_turns(
        db: Session,
        session_id: str,
        client_turn_id: str,
    ) -> list[TurnRecord]:
        return list(
            db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == session_id)
                .where(TurnRecord.client_turn_id == client_turn_id)
                .order_by(col(TurnRecord.sequence))
            ).all()
        )

    @staticmethod
    def _next_sequence(db: Session, session_id: str) -> int:
        last = db.exec(
            select(TurnRecord)
            .where(TurnRecord.session_id == session_id)
            .order_by(col(TurnRecord.sequence).desc())
            .limit(1)
        ).first()
        return 1 if last is None else last.sequence + 1

    @staticmethod
    def _persisted_turn(turn: TurnRecord) -> PersistedTurn:
        return PersistedTurn(
            id=turn.id,
            sequence=turn.sequence,
            speaker=turn.speaker,
            text=turn.text,
            client_turn_id=turn.client_turn_id,
        )

    def _write_wav(
        self,
        session_id: str,
        turn_id: str,
        pcm: bytes,
        *,
        sample_rate: int,
    ) -> Path | None:
        if not pcm:
            return None
        safe_session_id = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
        safe_turn_id = re.sub(r"[^A-Za-z0-9_-]", "_", turn_id)
        directory = (self._audio_root / safe_session_id).resolve()
        if directory.parent != self._audio_root:
            raise ValueError("音频保存路径无效")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{safe_turn_id}.wav"
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
        return path

    def _relative_audio_path(self, path: Path | None) -> str | None:
        if path is None:
            return None
        return path.relative_to(self._audio_root).as_posix()

    @staticmethod
    def _add_audio_record(
        db: Session,
        session_id: str,
        path: Path | None,
        kind: AudioKind,
        provider: str,
    ) -> None:
        if path is None:
            return
        db.add(
            AudioRecord(
                id=uuid4().hex,
                session_id=session_id,
                kind=kind,
                storage_name=path.name,
                mime_type="audio/wav",
                provider=provider,
                size_bytes=path.stat().st_size,
            )
        )

    @staticmethod
    def _speech_metric(
        session_id: str,
        turn_id: str,
        text: str,
        metric: SpeechMetricsInput,
    ) -> SpeechMetricRecord:
        spoken_characters = len("".join(text.split()))
        speech_rate = (
            spoken_characters * 1000 / metric.speech_duration_ms
            if metric.speech_duration_ms
            else 0.0
        )
        return SpeechMetricRecord(
            session_id=session_id,
            turn_id=turn_id,
            first_response_ms=max(0, metric.first_response_ms),
            speech_duration_ms=max(0, metric.speech_duration_ms),
            pause_durations_ms=[max(0, value) for value in metric.pause_durations_ms],
            supplement_count=max(0, metric.supplement_count),
            speech_rate=speech_rate,
            overlap_duration_ms=max(0, metric.overlap_duration_ms),
            excluded_technical_ms=max(0, metric.excluded_technical_ms),
            asr_sentences_json=list(metric.asr_sentences),
        )


@dataclass(frozen=True, slots=True)
class _LoadedContext:
    record: SessionRecord
    package: CasePackage
    state: ActorState
    history: list[DialogueTurn]

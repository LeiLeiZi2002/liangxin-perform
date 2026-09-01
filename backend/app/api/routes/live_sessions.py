import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlmodel import Session

from app import database
from app.cases.loader import CaseRepository
from app.runtime.character_kernel import (
    CHARACTER_PROMPT_ENGINE,
    CharacterPromptKernel,
    build_character_prompt_kernel,
    runtime_engine_from_state,
)
from app.runtime.character_provider import CharacterProvider
from app.runtime.failures import (
    FailureDisposition,
    RuntimeFailure,
    RuntimeFailureRecorder,
    failure_attempt_from_exception,
)
from app.runtime.kernel import (
    AssessmentKernel,
    KernelOpeningResult,
    KernelSessionConflictError,
    KernelSessionNotFoundError,
    KernelTurnConflictError,
    KernelTurnResult,
    RuntimePhase,
    SpeechMetricsInput,
    TechnicalPauseError,
)
from app.runtime.metrics import ModelCallRecorder
from app.runtime.models import RuntimeFailureRecord
from app.runtime.providers import (
    ActorProvider,
    AliyunSpeechProvider,
    ASRSentence,
    DirectorProvider,
    RuntimeSpeechError,
)
from app.runtime.turn_boundary import BoundaryState, TurnBoundary
from app.runtime_config import RuntimeCredentialStore, runtime_credential_store
from app.sessions.models import EndReason, Media, Scene, SessionRecord

router = APIRouter(tags=["live-sessions"])
BOUNDARY_POLL_SECONDS = 0.1
MANUAL_ASR_DRAIN_SECONDS = 1.0
REDO_ASR_RESET_SECONDS = 5.0
MAX_CLIENT_CONFIRMED_SILENCE_MS = 1000
REPEAT_REQUIRED_MESSAGE = "刚才那句话没有完整送达，请重新说一遍"
IN_FLIGHT_PHASES = frozenset(
    {
        RuntimePhase.directing,
        RuntimePhase.acting,
        RuntimePhase.synthesizing,
    }
)
CONTENT_SIMULATION_HEADER = "content"
DIAGNOSTIC_SIMULATION_HEADERS = frozenset({"content", "voice"})
NORMAL_WEBSOCKET_CLOSE_CODES = frozenset({1000, 1001})
CLIENT_FAILURE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
logger = logging.getLogger(__name__)


class ASRStream(Protocol):
    async def send_audio(self, pcm_chunk: bytes) -> None: ...

    async def receive_sentence(self) -> ASRSentence | None: ...

    async def finish(self) -> None: ...

    async def close(self) -> None: ...


class LiveSpeechProvider(Protocol):
    async def open_asr(self) -> ASRStream: ...


class LiveFailureRecorder(Protocol):
    def record(self, failure: RuntimeFailure) -> RuntimeFailureRecord: ...


def get_live_credential_store() -> RuntimeCredentialStore:
    return runtime_credential_store


@lru_cache(maxsize=1)
def get_assessment_kernel() -> AssessmentKernel:
    store = runtime_credential_store
    recorder = ModelCallRecorder(database.engine)
    failure_recorder = RuntimeFailureRecorder(database.engine)
    return AssessmentKernel(
        engine=database.engine,
        cases=CaseRepository(),
        director=DirectorProvider(
            store,
            recorder=recorder,
            failure_recorder=failure_recorder,
        ),
        actor=ActorProvider(
            store,
            recorder=recorder,
            failure_recorder=failure_recorder,
        ),
        speech=AliyunSpeechProvider(store),
        audio_root=Path("data/audio"),
        failure_recorder=failure_recorder,
        model_call_recorder=recorder,
    )


@lru_cache(maxsize=1)
def get_character_prompt_kernel() -> CharacterPromptKernel:
    store = runtime_credential_store
    recorder = ModelCallRecorder(database.engine)
    failure_recorder = RuntimeFailureRecorder(database.engine)
    return build_character_prompt_kernel(
        engine=database.engine,
        character_provider=CharacterProvider(
            store,
            recorder=recorder,
            failure_recorder=failure_recorder,
        ),
        speech=AliyunSpeechProvider(store),
        audio_root=Path("data/audio"),
        failure_recorder=failure_recorder,
        model_call_recorder=recorder,
    )


def get_live_speech_provider() -> LiveSpeechProvider:
    return AliyunSpeechProvider(runtime_credential_store)


LiveKernel = AssessmentKernel | CharacterPromptKernel
WorkflowKernelDep = Annotated[AssessmentKernel, Depends(get_assessment_kernel)]
CharacterKernelDep = Annotated[
    CharacterPromptKernel,
    Depends(get_character_prompt_kernel),
]
CredentialStoreDep = Annotated[RuntimeCredentialStore, Depends(get_live_credential_store)]
SpeechProviderDep = Annotated[LiveSpeechProvider, Depends(get_live_speech_provider)]


def select_live_kernel(
    session_id: str,
    *,
    workflow_kernel: LiveKernel,
    character_kernel: LiveKernel,
) -> LiveKernel:
    with Session(database.engine) as db:
        record = db.get(SessionRecord, session_id)
    if (
        record is not None
        and record.scene is Scene.hotline
        and runtime_engine_from_state(record.state_json) != CHARACTER_PROMPT_ENGINE
    ):
        raise KernelSessionConflictError("本次会谈使用的是旧配置，请返回并重新开始")
    if (
        record is not None
        and runtime_engine_from_state(record.state_json) == CHARACTER_PROMPT_ENGINE
    ):
        return character_kernel
    return workflow_kernel


@dataclass(frozen=True, slots=True)
class _PendingGeneration:
    text: str
    client_turn_id: str
    worker_pcm: bytes
    metrics: SpeechMetricsInput | None
    world_time_advance_seconds: float = 0
    sentence_ids: frozenset[tuple[int, int]] = frozenset()
    pcm_length: int = 0


class _LiveConnection:
    def __init__(
        self,
        *,
        websocket: WebSocket,
        session_id: str,
        kernel: LiveKernel,
        speech_provider: LiveSpeechProvider,
        media: Media,
        initial_phase: RuntimePhase,
        opening_delay_seconds: float | None = None,
        content_simulation: bool = False,
        diagnostic_simulation: bool = False,
        technical_retry_allowed: bool = False,
        failure_recorder: LiveFailureRecorder | None = None,
    ) -> None:
        self.websocket = websocket
        self.session_id = session_id
        self.kernel = kernel
        self.manual_turn_completion = bool(
            getattr(kernel, "manual_turn_completion", False)
        )
        self.speech_provider = speech_provider
        self.media = media
        self.phase = initial_phase
        self.boundary: TurnBoundary | None = None
        self.prior_boundaries: list[TurnBoundary] = []
        self.asr_sentences: dict[tuple[int, int], ASRSentence] = {}
        self._discarded_asr_sentences: list[dict[str, object]] = []
        self._consumed_asr_sentence_keys: set[tuple[int, int]] = set()
        self.worker_pcm = bytearray()
        self._active_input_pcm_start_byte = 0
        self.asr_stream: ASRStream | None = None
        self.asr_task: asyncio.Task[None] | None = None
        self.opening_task: asyncio.Task[None] | None = None
        self.opening_client_turn_id: str | None = None
        self.boundary_task: asyncio.Task[None] | None = None
        self.generation_task: asyncio.Task[None] | None = None
        self.retry_payload: _PendingGeneration | None = None
        self.technical_pause_started_ms: int | None = None
        self.technical_retry_allowed = technical_retry_allowed
        self.excluded_technical_ms = 0
        self.overlap_started_ms: int | None = None
        self.overlap_duration_ms = 0
        self._listening_started_ms = self._now_ms()
        self._asr_reconnects = 0
        self._asr_generation = 0
        self._asr_lock = asyncio.Lock()
        self._asr_input_suspended = initial_phase is not RuntimePhase.listening
        self._started = False
        self.opening_delay_seconds = opening_delay_seconds
        self._opening_pending = (
            opening_delay_seconds is not None
            and initial_phase is not RuntimePhase.technical_paused
            and initial_phase not in IN_FLIGHT_PHASES
        )
        self._repeat_required_after_resume = (
            initial_phase is RuntimePhase.technical_paused
            or initial_phase in IN_FLIGHT_PHASES
        )
        self.content_simulation = content_simulation
        self.diagnostic_simulation = diagnostic_simulation
        self.failure_recorder = failure_recorder
        self._pending_actor_text = ""
        self._streamed_text = False
        self._streamed_audio = False
        self._generation_committed = False
        self._pending_natural_close = False

    async def run(self) -> None:
        try:
            while True:
                message = await self.websocket.receive()
                message_type = message.get("type")
                if message_type == "websocket.disconnect":
                    await self._record_disconnect(message.get("code"))
                    return
                if message.get("bytes") is not None:
                    if self.media is Media.text:
                        await self._record_protocol_failure(
                            operation="audio_frame",
                            failure_code="websocket.invalid_event",
                            message="文字会话收到了音频帧",
                        )
                        continue
                    await self._handle_audio(message["bytes"])
                    continue
                text = message.get("text")
                if text is None:
                    await self._record_protocol_failure(
                        operation="frame",
                        failure_code="websocket.invalid_event",
                        message="WebSocket 消息既不包含文字也不包含音频",
                    )
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError as exc:
                    await self._record_protocol_failure(
                        operation="decode_json",
                        failure_code="websocket.invalid_json",
                        message="客户端消息不是有效 JSON",
                        error=exc,
                    )
                    await self.websocket.send_json(
                        {"type": "input.error", "message": "这次输入没有被正常接收，请重新操作"}
                    )
                    continue
                if not isinstance(event, dict):
                    await self._record_protocol_failure(
                        operation="decode_event",
                        failure_code="websocket.invalid_event",
                        message="客户端消息不是对象",
                    )
                    continue
                await self._handle_control(event)
        except WebSocketDisconnect as exc:
            await self._record_disconnect(exc.code)
        except Exception as exc:
            await self._record_runtime_failure(
                component="websocket",
                phase=self.phase,
                operation="connection_loop",
                failure_code="websocket.connection_loop",
                errors=(exc,),
                retryable=True,
                disposition=FailureDisposition.connection_close,
            )
            raise
        finally:
            await self._cleanup()

    async def _handle_control(self, event: dict[str, object]) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "session.start":
            await self._start_session()
        elif event_type == "vad.speech_started":
            await self._speech_started(self._event_ms(event.get("at_ms")))
        elif event_type == "vad.speech_stopped":
            await self._speech_stopped(
                self._event_ms(event.get("at_ms")),
                confirmed_silence_ms=self._confirmed_silence_ms(
                    event.get("confirmed_silence_ms")
                ),
            )
        elif event_type == "turn.manual_complete":
            await self._manual_complete(self._event_ms(event.get("at_ms")))
        elif event_type == "turn.redo_input":
            await self._redo_voice_input()
        elif event_type == "text.turn":
            await self._text_turn(event)
        elif event_type == "technical.retry":
            await self._technical_retry()
        elif event_type == "playback.ended":
            await self._playback_ended()
        elif event_type == "client.failure":
            await self._client_failure(event)
        elif event_type == "session.end":
            await self._end()
        else:
            await self._record_protocol_failure(
                operation="control_event" if event_type else "missing_type",
                failure_code="websocket.unknown_event",
                message="客户端发送了不支持的事件",
            )

    async def _start_session(self) -> None:
        if self._started or self.phase is RuntimePhase.technical_paused:
            return
        self._started = True
        self._asr_input_suspended = False
        self._listening_started_ms = self._now_ms()
        self.boundary = TurnBoundary(listening_started_ms=self._listening_started_ms)
        await self._set_phase(RuntimePhase.listening)
        if self._repeat_required_after_resume:
            self._repeat_required_after_resume = False
            await self.websocket.send_json(
                {"type": "input.error", "message": REPEAT_REQUIRED_MESSAGE}
            )
        if self._opening_pending and self.opening_delay_seconds is not None:
            await self._cancel_task(self.opening_task)
            self.opening_client_turn_id = f"opening-{uuid4().hex}"
            self.opening_task = asyncio.create_task(self._opening_after_delay())

    async def _opening_after_delay(self, *, wait_for_delay: bool = True) -> None:
        try:
            delay = self.opening_delay_seconds
            if delay is None:
                return
            if wait_for_delay:
                await asyncio.sleep(delay)
            client_turn_id = self.opening_client_turn_id
            if client_turn_id is None:
                return
            await self._retire_asr(operation="close_for_opening")
            if not (
                self._streamed_text
                and self._pending_actor_text
                and not self._streamed_audio
            ):
                self._reset_stream_state()
            result = await self.kernel.generate_opening(
                session_id=self.session_id,
                client_turn_id=client_turn_id,
                synthesize_audio=not self.content_simulation,
                capture_failure_payload=self.diagnostic_simulation,
                on_phase=self._set_phase,
                on_actor_text=self._actor_text_ready,
                on_audio_chunk=self._stream_audio_chunk,
            )
            await self._play_opening(result)
            self._opening_pending = False
            self.opening_client_turn_id = None
        except asyncio.CancelledError:
            if not self._streamed_audio:
                self.opening_client_turn_id = None
            raise
        except TechnicalPauseError as exc:
            if (
                isinstance(self.kernel, CharacterPromptKernel)
                and exc.failed_phase is RuntimePhase.synthesizing
            ):
                await self._send_pending_actor_text()
            if self._streamed_audio:
                self.opening_client_turn_id = None
                await self._technical_pause(
                    exc.failed_phase,
                    can_retry=False,
                    failure_record=getattr(exc, "failure_record", None),
                    failure_id=getattr(exc, "failure_id", None),
                    failure_code=getattr(exc, "failure_code", None),
                )
            else:
                if not exc.can_retry:
                    self.opening_client_turn_id = None
                await self._technical_pause(
                    exc.failed_phase,
                    can_retry=exc.can_retry,
                    failure_record=getattr(exc, "failure_record", None),
                    failure_id=getattr(exc, "failure_id", None),
                    failure_code=getattr(exc, "failure_code", None),
                )
        finally:
            if self.opening_task is asyncio.current_task():
                self.opening_task = None

    async def _speech_started(self, at_ms: int) -> None:
        if self.phase is RuntimePhase.technical_paused:
            return
        if self.manual_turn_completion:
            if (
                self.opening_task is not None
                and self.phase is RuntimePhase.listening
                and not self._streamed_audio
            ):
                self._opening_pending = False
                await self._cancel_task(self.opening_task)
                self.opening_task = None
                self.opening_client_turn_id = None
                self._asr_input_suspended = False
            if self.boundary is None or self.boundary.state is BoundaryState.complete:
                self.boundary = TurnBoundary(
                    listening_started_ms=self._listening_started_ms
                )
            self.boundary.speech_started(at_ms=at_ms)
            if self.phase is RuntimePhase.playing and self.overlap_started_ms is None:
                self.overlap_started_ms = at_ms
            await self._cancel_task(self.boundary_task)
            self.boundary_task = None
            return
        if not self._streamed_audio and self.phase is not RuntimePhase.playing:
            self._opening_pending = False
            await self._cancel_task(self.opening_task)
            self.opening_task = None
            self._asr_input_suspended = False
        if self.phase in {
            RuntimePhase.directing,
            RuntimePhase.acting,
            RuntimePhase.synthesizing,
        }:
            await self._cancel_external_generation()
            self.retry_payload = None
            self.kernel.resume_listening(self.session_id)
            if self.boundary is not None:
                self.prior_boundaries.append(self.boundary)
            self.boundary = TurnBoundary(listening_started_ms=self._listening_started_ms)
        if self.boundary is None or self.boundary.state is BoundaryState.complete:
            self.boundary = TurnBoundary(listening_started_ms=self._listening_started_ms)
        self.boundary.speech_started(at_ms=at_ms)
        if self.phase is RuntimePhase.playing and self.overlap_started_ms is None:
            self.overlap_started_ms = at_ms
        if self.phase is not RuntimePhase.playing:
            await self._set_phase(RuntimePhase.listening)
        await self._cancel_task(self.boundary_task)
        self.boundary_task = None

    async def _speech_stopped(
        self,
        at_ms: int,
        *,
        confirmed_silence_ms: int = 0,
    ) -> None:
        if self.phase is RuntimePhase.technical_paused:
            return
        if self.boundary is None:
            return
        self.boundary.speech_stopped(
            at_ms=at_ms,
            confirmed_silence_ms=confirmed_silence_ms,
        )
        if self.overlap_started_ms is not None:
            speech_ended_ms = at_ms - confirmed_silence_ms
            self.overlap_duration_ms += max(
                0,
                speech_ended_ms - self.overlap_started_ms,
            )
            self.overlap_started_ms = None
        await self._cancel_task(self.boundary_task)
        self.boundary_task = None
        if not self.manual_turn_completion:
            self.boundary_task = asyncio.create_task(self._wait_for_boundary(at_ms))

    async def _wait_for_boundary(self, stopped_at_ms: int) -> None:
        started = time.monotonic()
        while self.boundary is not None:
            await asyncio.sleep(BOUNDARY_POLL_SECONDS)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if self.boundary.advance(at_ms=stopped_at_ms + elapsed_ms) is BoundaryState.complete:
                await self._submit_voice_turn()
                return

    async def _manual_complete(self, at_ms: int) -> None:
        if self.phase is RuntimePhase.technical_paused:
            return
        if self.manual_turn_completion and not await self._drain_manual_asr():
            return
        if not self._assembled_transcript().strip():
            self._asr_input_suspended = False
            await self.websocket.send_json(
                {
                    "type": "input.error",
                    "message": "还没有听清这句话，请再说一遍后提交",
                }
            )
            return
        if self.boundary is None:
            self.boundary = TurnBoundary(
                listening_started_ms=self._listening_started_ms
            )
        if self.manual_turn_completion:
            self.boundary.manual_complete(at_ms=at_ms)
            await self._cancel_task(self.boundary_task)
            self.boundary_task = None
            await self._submit_voice_turn()
            return
        if self.boundary.manual_complete(at_ms=at_ms) is BoundaryState.complete:
            await self._cancel_task(self.boundary_task)
            self.boundary_task = None
            await self._submit_voice_turn()

    async def _redo_voice_input(self) -> None:
        if (
            self.media is not Media.voice
            or not self.manual_turn_completion
            or self.phase is not RuntimePhase.listening
            or self.generation_task is not None
        ):
            await self.websocket.send_json(
                {
                    "type": "input.error",
                    "message": "当前不能重新录入，请等来访者回应后再试",
                }
            )
            return

        await self._cancel_task(self.boundary_task)
        self.boundary_task = None
        self._discarded_asr_sentences.extend(
            self._asr_metric_items(discarded_by_worker=True)
        )
        self.asr_sentences.clear()
        self._active_input_pcm_start_byte = len(self.worker_pcm)
        self.prior_boundaries.clear()
        self._listening_started_ms = self._now_ms()
        self.boundary = TurnBoundary(listening_started_ms=self._listening_started_ms)
        self.overlap_started_ms = None
        self.overlap_duration_ms = 0
        self.excluded_technical_ms = 0

        generation = self._asr_generation
        try:
            if self.asr_stream is not None:
                reset_succeeded = await asyncio.wait_for(
                    self._replace_asr(
                        expected_generation=generation,
                        failure_reconnect=False,
                    ),
                    timeout=REDO_ASR_RESET_SECONDS,
                )
            else:
                reset_succeeded = True
        except TimeoutError:
            error = RuntimeSpeechError("重新建立语音识别连接超时")
            record = await self._record_runtime_failure(
                component="asr",
                phase=RuntimePhase.listening,
                operation="redo_input",
                failure_code="asr.redo_input_timeout",
                errors=(error,),
                retryable=True,
                disposition=FailureDisposition.technical_pause,
            )
            self._repeat_required_after_resume = True
            await self._technical_pause(
                RuntimePhase.listening,
                failure_record=record,
            )
            return
        if not reset_succeeded:
            self._repeat_required_after_resume = True
            return

        await self.websocket.send_json(
            {
                "type": "input.reset",
                "message": "已清空，请重新说这一句",
            }
        )

    async def _drain_manual_asr(self) -> bool:
        if self.asr_stream is None:
            return True

        deadline = asyncio.get_running_loop().time() + MANUAL_ASR_DRAIN_SECONDS
        while (stream := self.asr_stream) is not None:
            task = self.asr_task
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(stream.finish(), timeout=remaining)
            except TimeoutError:
                break
            except RuntimeSpeechError as exc:
                await self._record_runtime_failure(
                    component="asr",
                    phase=RuntimePhase.listening,
                    operation="finish_manual_turn",
                    failure_code="asr.finish",
                    errors=(exc,),
                    retryable=True,
                    disposition=FailureDisposition.recovered,
                )

            remaining = deadline - asyncio.get_running_loop().time()
            if task is not None and not task.done() and remaining > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                except TimeoutError:
                    break
            if self.asr_stream is stream:
                break

        await self._retire_asr(operation="close_completed_stream")
        return True

    async def _submit_voice_turn(self) -> None:
        text = self._assembled_transcript().strip()
        if (
            not text
            or self.generation_task is not None
            or self.phase in {RuntimePhase.playing, RuntimePhase.technical_paused}
        ):
            return
        sentence_ids = frozenset(self.asr_sentences)
        pcm_length = len(self.worker_pcm)
        payload = _PendingGeneration(
            text=text,
            client_turn_id=f"voice-{uuid4().hex}",
            worker_pcm=bytes(self.worker_pcm[:pcm_length]),
            metrics=self._speech_metrics(),
            sentence_ids=sentence_ids,
            pcm_length=pcm_length,
        )
        self.generation_task = asyncio.create_task(self._run_generation(payload))

    async def _text_turn(self, event: dict[str, object]) -> None:
        if self.phase is RuntimePhase.technical_paused:
            await self.websocket.send_json(
                {"type": "phase", "phase": self.phase.value}
            )
            return
        text = str(event.get("text", "")).strip()
        client_turn_id = str(event.get("client_turn_id", "")).strip()
        if not text or not client_turn_id:
            await self._record_protocol_failure(
                operation="text.turn",
                failure_code="websocket.invalid_event",
                message="文字发言缺少内容或话轮标识",
            )
            await self.websocket.send_json(
                {"type": "input.error", "message": "请输入要发送的内容"}
            )
            return
        if self.generation_task is not None and not self.generation_task.done():
            await self._record_protocol_failure(
                operation="text.turn_in_progress",
                failure_code="websocket.invalid_event",
                message="上一轮来访者回应尚未完成",
            )
            await self.websocket.send_json(
                {"type": "input.error", "message": "请等来访者回应后再继续"}
            )
            return
        await self._cancel_task(self.opening_task)
        self.opening_task = None
        if self.phase is not RuntimePhase.playing:
            await self._cancel_task(self.generation_task)
        payload = _PendingGeneration(
            text=text,
            client_turn_id=client_turn_id,
            worker_pcm=b"",
            metrics=None,
            world_time_advance_seconds=self._world_time_advance_seconds(event),
        )
        self.generation_task = asyncio.create_task(self._run_generation(payload))

    def _world_time_advance_seconds(self, event: dict[str, object]) -> int:
        if not self.diagnostic_simulation:
            return 0
        value = event.get("world_time_advance_seconds", 0)
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value if 0 <= value <= 3600 else 0

    async def _run_generation(self, payload: _PendingGeneration) -> None:
        try:
            retrying_after_text_only_failure = (
                self.retry_payload is not None
                and self.retry_payload.client_turn_id == payload.client_turn_id
                and self._streamed_text
                and bool(self._pending_actor_text)
                and not self._streamed_audio
            )
            if not retrying_after_text_only_failure:
                self._reset_stream_state()
            self._generation_committed = False
            if isinstance(self.kernel, CharacterPromptKernel):
                result = await self.kernel.process_worker_turn(
                    session_id=self.session_id,
                    client_turn_id=payload.client_turn_id,
                    text=payload.text,
                    worker_pcm=payload.worker_pcm,
                    speech_metrics=payload.metrics,
                    world_time_advance_seconds=payload.world_time_advance_seconds,
                    synthesize_audio=not self.content_simulation,
                    capture_failure_payload=self.diagnostic_simulation,
                    on_phase=self._set_phase,
                    on_actor_text=(
                        self._actor_text_ready if self.media is Media.voice else None
                    ),
                    on_audio_chunk=(
                        self._stream_audio_chunk if self.media is Media.voice else None
                    ),
                )
            else:
                result = await self.kernel.process_worker_turn(
                    session_id=self.session_id,
                    client_turn_id=payload.client_turn_id,
                    text=payload.text,
                    worker_pcm=payload.worker_pcm,
                    speech_metrics=payload.metrics,
                    synthesize_audio=not self.content_simulation,
                    capture_failure_payload=self.diagnostic_simulation,
                    on_phase=self._set_phase,
                    on_actor_text=(
                        self._actor_text_ready if self.media is Media.voice else None
                    ),
                    on_audio_chunk=(
                        self._stream_audio_chunk if self.media is Media.voice else None
                    ),
                )
            self._generation_committed = True
            self.retry_payload = None
            self._consume_committed_audio(payload)
            await self._play_turn(result)
        except asyncio.CancelledError:
            raise
        except KernelTurnConflictError:
            self.kernel.resume_listening(self.session_id)
            await self.websocket.send_json(
                {
                    "type": "input.error",
                    "message": "这次发言标识已被使用，请重新发送",
                }
            )
            await self._set_phase(RuntimePhase.listening)
        except TechnicalPauseError as exc:
            if (
                isinstance(self.kernel, CharacterPromptKernel)
                and exc.failed_phase is RuntimePhase.synthesizing
            ):
                await self._send_pending_actor_text()
            if self._streamed_audio:
                self.retry_payload = None
                await self._technical_pause(
                    exc.failed_phase,
                    can_retry=False,
                    failure_record=getattr(exc, "failure_record", None),
                    failure_id=getattr(exc, "failure_id", None),
                    failure_code=getattr(exc, "failure_code", None),
                )
            else:
                self.retry_payload = payload if exc.can_retry else None
                await self._technical_pause(
                    exc.failed_phase,
                    can_retry=exc.can_retry,
                    failure_record=getattr(exc, "failure_record", None),
                    failure_id=getattr(exc, "failure_id", None),
                    failure_code=getattr(exc, "failure_code", None),
                )
        finally:
            if self.generation_task is asyncio.current_task():
                self.generation_task = None
            self._generation_committed = False

    async def _play_turn(self, result: KernelTurnResult) -> None:
        self._pending_natural_close = result.ending_route_id is not None
        if not self._streamed_text:
            if self.media is Media.voice:
                await self._set_phase(RuntimePhase.playing)
            await self.websocket.send_json(
                {
                    "type": "visitor.text",
                    "text": result.client.text,
                    "turn": result.client.model_dump(mode="json"),
                }
            )
        if not self._streamed_audio:
            for chunk in result.audio_chunks:
                await self.websocket.send_bytes(chunk)
        await self.websocket.send_json(
            {
                "type": "turn.committed",
                "client_turn_id": result.client.client_turn_id,
                "worker": result.worker.model_dump(mode="json"),
                "client": result.client.model_dump(mode="json"),
            }
        )
        if self.media is Media.text:
            if self._pending_natural_close:
                await self._end(EndReason.natural_closure)
            else:
                await self._set_phase(RuntimePhase.listening)

    async def _play_opening(self, result: KernelOpeningResult) -> None:
        self._pending_natural_close = result.ending_route_id is not None
        if not self._streamed_text:
            if self.media is Media.voice:
                await self._set_phase(RuntimePhase.playing)
            await self.websocket.send_json(
                {
                    "type": "visitor.text",
                    "text": result.client.text,
                    "turn": result.client.model_dump(mode="json"),
                }
            )
        if not self._streamed_audio:
            for chunk in result.audio_chunks:
                await self.websocket.send_bytes(chunk)
        await self.websocket.send_json(
            {
                "type": "turn.committed",
                "client_turn_id": result.client.client_turn_id,
                "client": result.client.model_dump(mode="json"),
            }
        )
        if self.media is Media.text:
            if self._pending_natural_close:
                await self._end(EndReason.natural_closure)
            else:
                await self._set_phase(RuntimePhase.listening)

    async def _handle_audio(self, pcm: bytes) -> None:
        if (
            self.media is not Media.voice
            or self.content_simulation
            or not pcm
            or self.phase is RuntimePhase.technical_paused
            or self._asr_input_suspended
        ):
            return
        self.worker_pcm.extend(pcm)
        if self.asr_stream is None and not await self._open_asr():
            return
        assert self.asr_stream is not None
        stream = self.asr_stream
        generation = self._asr_generation
        try:
            await stream.send_audio(pcm)
            self._asr_reconnects = 0
        except RuntimeSpeechError as first_error:
            if not await self._reconnect_asr(expected_generation=generation):
                await self._record_runtime_failure(
                    component="asr",
                    phase=RuntimePhase.listening,
                    operation="send_audio",
                    failure_code="asr.send_audio",
                    errors=(first_error,),
                    retryable=False,
                    disposition=FailureDisposition.aborted,
                )
                return
            assert self.asr_stream is not None
            try:
                await self.asr_stream.send_audio(pcm)
            except RuntimeSpeechError as second_error:
                record = await self._record_runtime_failure(
                    component="asr",
                    phase=RuntimePhase.listening,
                    operation="send_audio",
                    failure_code="asr.send_audio",
                    errors=(first_error, second_error),
                    retryable=True,
                    disposition=FailureDisposition.technical_pause,
                )
                await self._technical_pause(
                    RuntimePhase.listening,
                    failure_record=record,
                )
            else:
                self._asr_reconnects = 0
                await self._record_runtime_failure(
                    component="asr",
                    phase=RuntimePhase.listening,
                    operation="send_audio",
                    failure_code="asr.send_audio",
                    errors=(first_error,),
                    retryable=True,
                    disposition=FailureDisposition.recovered,
                )

    async def _open_asr(self) -> bool:
        errors: list[Exception] = []
        async with self._asr_lock:
            if self.asr_stream is not None:
                return True
            for attempt in range(2):
                try:
                    stream = await self.speech_provider.open_asr()
                    self._install_asr(stream)
                    if errors:
                        await self._record_runtime_failure(
                            component="asr",
                            phase=RuntimePhase.listening,
                            operation="open",
                            failure_code="asr.open",
                            errors=tuple(errors),
                            retryable=True,
                            disposition=FailureDisposition.recovered,
                        )
                    return True
                except RuntimeSpeechError as exc:
                    errors.append(exc)
                    if attempt == 1:
                        break
        record = await self._record_runtime_failure(
            component="asr",
            phase=RuntimePhase.listening,
            operation="open",
            failure_code="asr.open",
            errors=tuple(errors),
            retryable=True,
            disposition=FailureDisposition.technical_pause,
        )
        await self._technical_pause(
            RuntimePhase.listening,
            failure_record=record,
        )
        return False

    async def _reconnect_asr(self, *, expected_generation: int | None = None) -> bool:
        return await self._replace_asr(
            expected_generation=(
                self._asr_generation
                if expected_generation is None
                else expected_generation
            ),
            failure_reconnect=True,
        )

    async def _retire_asr(self, *, operation: str) -> None:
        async with self._asr_lock:
            self._asr_input_suspended = True
            old_stream = self.asr_stream
            old_task = self.asr_task
            if old_stream is None and old_task is None:
                return
            self.asr_stream = None
            self.asr_task = None
            self._asr_generation += 1
            self._asr_reconnects = 0

        caller_task = asyncio.current_task()
        receive_task = (
            old_task if old_task is not None and old_task is not caller_task else None
        )
        if receive_task is not None and not receive_task.done():
            receive_task.cancel()

        async def finish_retirement() -> None:
            if old_stream is not None:
                try:
                    await old_stream.close()
                except Exception as exc:
                    await self._record_runtime_failure(
                        component="asr",
                        phase=self.phase,
                        operation=operation,
                        failure_code="asr.close",
                        errors=(exc,),
                        retryable=True,
                        disposition=FailureDisposition.recovered,
                    )
            if receive_task is not None:
                with suppress(asyncio.CancelledError):
                    await receive_task

        cleanup_task = asyncio.create_task(finish_retirement())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise

    async def _replace_asr(
        self,
        *,
        expected_generation: int,
        failure_reconnect: bool,
    ) -> bool:
        failed = False
        errors: list[Exception] = []
        operation = "reconnect" if failure_reconnect else "rotate"
        async with self._asr_lock:
            if expected_generation != self._asr_generation:
                return self.asr_stream is not None
            if failure_reconnect and self._asr_reconnects >= 1:
                errors.append(RuntimeSpeechError("ASR 重连次数已用尽"))
                failed = True
            else:
                if failure_reconnect:
                    self._asr_reconnects += 1
                else:
                    self._asr_reconnects = 0
                old_stream = self.asr_stream
                old_task = self.asr_task
                self.asr_stream = None
                self.asr_task = None
                self._asr_generation += 1
                if old_stream is not None:
                    try:
                        await old_stream.close()
                    except Exception as exc:
                        await self._record_runtime_failure(
                            component="asr",
                            phase=RuntimePhase.listening,
                            operation="close_replaced_stream",
                            failure_code="asr.close",
                            errors=(exc,),
                            retryable=True,
                            disposition=FailureDisposition.recovered,
                        )
                if old_task is not asyncio.current_task():
                    await self._cancel_task(old_task)
                attempts = 1 if failure_reconnect else 2
                for _ in range(attempts):
                    try:
                        stream = await self.speech_provider.open_asr()
                        self._install_asr(stream)
                        if errors:
                            await self._record_runtime_failure(
                                component="asr",
                                phase=RuntimePhase.listening,
                                operation=operation,
                                failure_code=f"asr.{operation}",
                                errors=tuple(errors),
                                retryable=True,
                                disposition=FailureDisposition.recovered,
                            )
                        return True
                    except RuntimeSpeechError as exc:
                        errors.append(exc)
                        continue
                failed = True
        if failed:
            record = await self._record_runtime_failure(
                component="asr",
                phase=RuntimePhase.listening,
                operation=operation,
                failure_code=f"asr.{operation}",
                errors=tuple(errors),
                retryable=True,
                disposition=FailureDisposition.technical_pause,
            )
            await self._technical_pause(
                RuntimePhase.listening,
                failure_record=record,
            )
        return False

    def _install_asr(self, stream: ASRStream) -> None:
        self._asr_generation += 1
        generation = self._asr_generation
        self.asr_stream = stream
        self.asr_task = asyncio.create_task(self._receive_asr(stream, generation))

    async def _receive_asr(self, stream: ASRStream, generation: int) -> None:
        while generation == self._asr_generation and self.asr_stream is stream:
            try:
                sentence = await stream.receive_sentence()
            except RuntimeSpeechError as exc:
                if (
                    generation != self._asr_generation
                    or self.asr_stream is not stream
                ):
                    return
                recovered = await self._reconnect_asr(
                    expected_generation=generation
                )
                await self._record_runtime_failure(
                    component="asr",
                    phase=RuntimePhase.listening,
                    operation="receive",
                    failure_code="asr.receive",
                    errors=(exc,),
                    retryable=True,
                    disposition=(
                        FailureDisposition.recovered
                        if recovered
                        else FailureDisposition.aborted
                    ),
                )
                return
            if sentence is None:
                return
            if generation != self._asr_generation or self.asr_stream is not stream:
                return
            self._asr_reconnects = 0
            sentence_key = (generation, sentence.sentence_id)
            if sentence_key in self._consumed_asr_sentence_keys:
                continue
            self.asr_sentences[sentence_key] = sentence
            if self.boundary is not None:
                self.boundary.observe_asr(sentence)
            await self.websocket.send_json(
                {
                    "type": "asr.final" if sentence.sentence_end else "asr.partial",
                    "text": sentence.text,
                    "transcript": self._assembled_transcript(),
                    "sentence_id": sentence.sentence_id,
                    "begin_time_ms": sentence.begin_time_ms,
                    "end_time_ms": sentence.end_time_ms,
                }
            )
            if (
                self.boundary is not None
                and self.boundary.state is BoundaryState.complete
            ):
                await self._submit_voice_turn()

    async def _client_failure(self, event: dict[str, object]) -> None:
        stage = event.get("stage")
        code = event.get("code")
        if (
            stage not in {"capture", "playback"}
            or not isinstance(code, str)
            or CLIENT_FAILURE_CODE.fullmatch(code) is None
        ):
            await self._record_protocol_failure(
                operation="client.failure",
                failure_code="websocket.invalid_event",
                message="客户端音频故障事件缺少有效阶段或故障码",
            )
            await self.websocket.send_json(
                {"type": "input.error", "message": "这次音频状态没有被正常接收"}
            )
            return
        failed_phase = (
            RuntimePhase.playing
            if stage == "playback"
            else RuntimePhase.listening
        )
        record = await self._record_runtime_failure(
            component="browser_audio",
            phase=failed_phase,
            operation=stage,
            failure_code=f"client.{stage}.{code}",
            errors=(RuntimeError(f"浏览器上报{stage}故障：{code}"),),
            retryable=True,
            disposition=FailureDisposition.technical_pause,
            details={"stage": stage, "client_code": code},
        )
        await self._technical_pause(
            failed_phase,
            can_retry=True,
            failure_record=record,
        )

    async def _record_protocol_failure(
        self,
        *,
        operation: str,
        failure_code: str,
        message: str,
        error: Exception | None = None,
    ) -> RuntimeFailureRecord | None:
        return await self._record_runtime_failure(
            component="websocket",
            phase=self.phase,
            operation=operation,
            failure_code=failure_code,
            errors=(error or ValueError(message),),
            retryable=True,
            disposition=FailureDisposition.recovered,
        )

    async def _record_disconnect(self, value: object) -> None:
        code = value if isinstance(value, int) else None
        if code in NORMAL_WEBSOCKET_CLOSE_CODES:
            return
        await self._record_runtime_failure(
            component="websocket",
            phase=self.phase,
            operation="disconnect",
            failure_code="websocket.abnormal_close",
            errors=(WebSocketDisconnect(code=code or 1006),),
            retryable=True,
            disposition=FailureDisposition.connection_close,
            details={"close_code": code},
        )

    async def _record_runtime_failure(
        self,
        *,
        component: str,
        phase: RuntimePhase,
        operation: str,
        failure_code: str,
        errors: tuple[Exception, ...],
        retryable: bool,
        disposition: FailureDisposition,
        client_turn_id: str | None = None,
        details: dict[str, object] | None = None,
    ) -> RuntimeFailureRecord | None:
        if self.failure_recorder is None or not errors:
            return None
        failure = RuntimeFailure(
            session_id=self.session_id,
            client_turn_id=client_turn_id,
            component=component,
            phase=phase.value,
            operation=operation,
            failure_code=failure_code,
            retryable=retryable,
            disposition=disposition,
            attempts=tuple(
                failure_attempt_from_exception(index, error)
                for index, error in enumerate(errors, start=1)
            ),
            details=details or {},
        )
        try:
            return await asyncio.to_thread(self.failure_recorder.record, failure)
        except Exception:
            logger.exception(
                "运行失败记录写入失败 session_id=%s failure_code=%s",
                self.session_id,
                failure_code,
            )
            return None

    async def _technical_retry(self) -> None:
        if not self.technical_retry_allowed:
            await self.websocket.send_json(
                {"type": "phase", "phase": self.phase.value}
            )
            return
        self.technical_retry_allowed = False
        self._started = True
        if self.technical_pause_started_ms is not None:
            self.excluded_technical_ms += max(
                0,
                self._now_ms() - self.technical_pause_started_ms,
            )
        self.technical_pause_started_ms = None
        self.kernel.resume_listening(self.session_id)
        await self._set_phase(RuntimePhase.listening)
        if (
            self.retry_payload is None
            and self.opening_client_turn_id is None
            and isinstance(self.kernel, CharacterPromptKernel)
        ):
            pending = self.kernel.pending_retry(self.session_id)
            if pending is not None:
                self._repeat_required_after_resume = False
                if pending.opening:
                    self.opening_client_turn_id = pending.client_turn_id
                else:
                    self.retry_payload = _PendingGeneration(
                        text=pending.text,
                        client_turn_id=pending.client_turn_id,
                        worker_pcm=pending.worker_pcm,
                        metrics=pending.speech_metrics,
                        world_time_advance_seconds=(
                            pending.world_time_advance_seconds
                        ),
                    )
        if self.retry_payload is not None:
            payload = self.retry_payload
            self._repeat_required_after_resume = False
            if payload.metrics is not None:
                payload = replace(
                    payload,
                    metrics=replace(
                        payload.metrics,
                        excluded_technical_ms=(
                            payload.metrics.excluded_technical_ms
                            + self.excluded_technical_ms
                        ),
                    ),
                )
            self.generation_task = asyncio.create_task(self._run_generation(payload))
            return
        if self.opening_client_turn_id is not None:
            self.opening_task = asyncio.create_task(
                self._opening_after_delay(wait_for_delay=False)
            )
            return
        if self._opening_pending and self.opening_delay_seconds is not None:
            self.opening_client_turn_id = f"opening-{uuid4().hex}"
            self.opening_task = asyncio.create_task(
                self._opening_after_delay(wait_for_delay=False)
            )
            return
        self._asr_input_suspended = False
        if self._repeat_required_after_resume:
            self._repeat_required_after_resume = False
            await self.websocket.send_json(
                {"type": "input.error", "message": REPEAT_REQUIRED_MESSAGE}
            )

    async def _technical_pause(
        self,
        failed_phase: RuntimePhase,
        *,
        can_retry: bool = True,
        failure_record: RuntimeFailureRecord | None = None,
        failure_id: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        if await self._cancel_external_generation():
            self._repeat_required_after_resume = True
        self._asr_input_suspended = True
        self.phase = RuntimePhase.technical_paused
        self.technical_retry_allowed = can_retry
        self.kernel.pause_technical(self.session_id, can_retry=can_retry)
        await self._cancel_task(self.boundary_task)
        self.boundary_task = None
        if self.asr_task is not asyncio.current_task():
            await self._cancel_task(self.asr_task)
        self.asr_task = None
        self._asr_generation += 1
        if self.asr_stream is not None:
            try:
                await self.asr_stream.close()
            except Exception as exc:
                await self._record_runtime_failure(
                    component="asr",
                    phase=failed_phase,
                    operation="close_for_pause",
                    failure_code="asr.close",
                    errors=(exc,),
                    retryable=can_retry,
                    disposition=FailureDisposition.aborted,
                )
        self.asr_stream = None
        if self.technical_pause_started_ms is None:
            self.technical_pause_started_ms = self._now_ms()
        event: dict[str, object] = {
            "type": "technical.pause",
            "phase": RuntimePhase.technical_paused.value,
            "failed_phase": failed_phase.value,
            "message": "来访者的信号不太稳定",
            "can_retry": can_retry,
        }
        summary = self._public_failure_summary(
            failure_record=failure_record,
            failure_id=failure_id,
            failure_code=failure_code,
            failed_phase=failed_phase,
            can_retry=can_retry,
        )
        if summary is not None:
            event["failure_id"] = summary["id"]
            event["failure_code"] = summary["failure_code"]
            event["failure"] = summary
        await self.websocket.send_json(event)
        if not can_retry:
            await self._end(EndReason.technical_interruption)

    def _public_failure_summary(
        self,
        *,
        failure_record: RuntimeFailureRecord | None,
        failure_id: str | None,
        failure_code: str | None,
        failed_phase: RuntimePhase,
        can_retry: bool,
    ) -> dict[str, object] | None:
        if failure_record is not None:
            return {
                "id": failure_record.id,
                "failure_code": failure_record.failure_code,
                "session_id": failure_record.session_id,
                "client_turn_id": failure_record.client_turn_id,
                "component": failure_record.component,
                "phase": failure_record.phase,
                "operation": failure_record.operation,
                "error_class": failure_record.error_class,
                "attempt_count": failure_record.attempt_count,
                "retryable": failure_record.retryable,
                "disposition": failure_record.disposition,
                "provider_status_code": failure_record.provider_status_code,
                "provider_request_id": failure_record.provider_request_id,
                "attempts_json": (
                    failure_record.attempts_json
                    if self.diagnostic_simulation
                    else []
                ),
                "details_json": (
                    failure_record.details_json
                    if self.diagnostic_simulation
                    else {}
                ),
            }
        if failure_id and failure_code:
            return {
                "id": failure_id,
                "session_id": self.session_id,
                "client_turn_id": None,
                "component": failure_code.partition(".")[0] or "runtime",
                "phase": failed_phase.value,
                "operation": failure_code.rpartition(".")[2] or "unknown",
                "failure_code": failure_code,
                "error_class": "TechnicalPauseError",
                "attempt_count": 1,
                "retryable": can_retry,
                "disposition": FailureDisposition.technical_pause.value,
                "provider_status_code": None,
                "provider_request_id": None,
                "attempts_json": [],
                "details_json": {},
            }
        return None

    async def _playback_ended(self) -> None:
        if self.phase is not RuntimePhase.playing:
            return
        if self._pending_natural_close:
            await self._end(EndReason.natural_closure)
            return
        self._asr_input_suspended = False
        await self._set_phase(RuntimePhase.listening)
        if self.boundary is not None and self.boundary.state is BoundaryState.complete:
            await self._submit_voice_turn()

    async def _actor_text_ready(self, text: str) -> None:
        self._pending_actor_text = text

    async def _send_pending_actor_text(self) -> None:
        if not self._pending_actor_text:
            return
        if self._streamed_text:
            return
        await self._set_phase(RuntimePhase.playing)
        await self.websocket.send_json(
            {
                "type": "visitor.text",
                "text": self._pending_actor_text,
                "provisional": True,
            }
        )
        self._streamed_text = True

    async def _stream_audio_chunk(self, chunk: bytes) -> None:
        if not self._streamed_text:
            await self._send_pending_actor_text()
        await self.websocket.send_bytes(chunk)
        self._streamed_audio = True

    def _reset_stream_state(self) -> None:
        self._pending_actor_text = ""
        self._streamed_text = False
        self._streamed_audio = False

    async def _end(self, reason: EndReason | None = None) -> None:
        if self.phase is RuntimePhase.ended:
            return
        if self.opening_task is not asyncio.current_task():
            await self._cancel_task(self.opening_task)
            self.opening_task = None
        await self._cancel_external_generation()
        if reason is None:
            reason = (
                EndReason.technical_interruption
                if self.phase is RuntimePhase.technical_paused
                else EndReason.user_ended
            )
        self.kernel.end_session(self.session_id, reason)
        self.phase = RuntimePhase.ended
        await self.websocket.send_json(
            {"type": "session.ended", "reason": reason.value}
        )
        await self.websocket.close(code=1000)

    async def _set_phase(self, phase: RuntimePhase) -> None:
        self.phase = phase
        await self.websocket.send_json({"type": "phase", "phase": phase.value})

    def _assembled_transcript(self) -> str:
        return "".join(
            self.asr_sentences[sentence_key].text
            for sentence_key in sorted(self.asr_sentences)
        )

    def _speech_metrics(self) -> SpeechMetricsInput:
        boundaries = [*self.prior_boundaries]
        if self.boundary is not None:
            boundaries.append(self.boundary)
        first_response = next(
            (
                boundary.first_response_ms
                for boundary in boundaries
                if boundary.first_response_ms is not None
            ),
            0,
        )
        return SpeechMetricsInput(
            first_response_ms=first_response or 0,
            speech_duration_ms=sum(item.speech_duration_ms for item in boundaries),
            pause_durations_ms=tuple(
                pause
                for item in boundaries
                for pause in item.pause_durations_ms
            ),
            supplement_count=sum(item.supplement_count for item in boundaries),
            overlap_duration_ms=self.overlap_duration_ms,
            excluded_technical_ms=self.excluded_technical_ms,
            asr_sentences=tuple(
                [
                    *self._discarded_asr_sentences,
                    *self._asr_metric_items(discarded_by_worker=False),
                ]
            ),
        )

    def _asr_metric_items(
        self,
        *,
        discarded_by_worker: bool,
    ) -> list[dict[str, object]]:
        return [
            {
                "generation": generation,
                "sentence_id": sentence.sentence_id,
                "text": sentence.text,
                "begin_time_ms": sentence.begin_time_ms,
                "end_time_ms": sentence.end_time_ms,
                "sentence_end": sentence.sentence_end,
                "discarded_by_worker": discarded_by_worker,
                "pcm_start_byte": self._active_input_pcm_start_byte,
                "pcm_end_byte": len(self.worker_pcm),
            }
            for (generation, _sentence_id), sentence in sorted(
                self.asr_sentences.items()
            )
        ]

    def _consume_committed_audio(self, payload: _PendingGeneration) -> None:
        if payload.pcm_length:
            del self.worker_pcm[: payload.pcm_length]
        self._consumed_asr_sentence_keys.update(payload.sentence_ids)
        for sentence_key in payload.sentence_ids:
            self.asr_sentences.pop(sentence_key, None)
        self._discarded_asr_sentences.clear()
        self._active_input_pcm_start_byte = 0
        self.prior_boundaries.clear()
        self._listening_started_ms = self._now_ms()
        self.boundary = TurnBoundary(listening_started_ms=self._listening_started_ms)
        self.overlap_duration_ms = 0
        self.excluded_technical_ms = 0

    async def _cleanup(self) -> None:
        for task in (
            self.opening_task,
            self.boundary_task,
            self.generation_task,
            self.asr_task,
        ):
            await self._cancel_task(task)
        if self.asr_stream is not None:
            try:
                await self.asr_stream.close()
            except Exception as exc:
                await self._record_runtime_failure(
                    component="asr",
                    phase=self.phase,
                    operation="close_on_disconnect",
                    failure_code="asr.close",
                    errors=(exc,),
                    retryable=False,
                    disposition=FailureDisposition.connection_close,
                )
        if self._pending_natural_close and self.phase is not RuntimePhase.ended:
            self.kernel.end_session(self.session_id, EndReason.natural_closure)
            self.phase = RuntimePhase.ended

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _cancel_external_generation(self) -> bool:
        task = self.generation_task
        if task is None or task is asyncio.current_task() or task.done():
            return False
        uncommitted = not self._generation_committed
        await self._cancel_task(task)
        if self.generation_task is task:
            self.generation_task = None
        return uncommitted

    @staticmethod
    def _now_ms() -> int:
        return int(time.monotonic() * 1000)

    def _event_ms(self, value: object) -> int:
        # Browser performance.now() and server monotonic() have different origins.
        # Arrival time keeps every process metric on the server's monotonic clock.
        del value
        return self._now_ms()

    @staticmethod
    def _confirmed_silence_ms(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return min(max(0, value), MAX_CLIENT_CONFIRMED_SILENCE_MS)


@router.websocket("/live-sessions/{session_id}")
async def live_session(
    websocket: WebSocket,
    session_id: str,
    workflow_kernel: WorkflowKernelDep,
    character_kernel: CharacterKernelDep,
    store: CredentialStoreDep,
    speech_provider: SpeechProviderDep,
) -> None:
    await websocket.accept()
    try:
        kernel = select_live_kernel(
            session_id,
            workflow_kernel=workflow_kernel,
            character_kernel=character_kernel,
        )
    except KernelSessionConflictError as exc:
        await websocket.send_json({"type": "session.error", "message": str(exc)})
        await websocket.close(code=4409)
        return
    simulation_mode = websocket.headers.get("x-assessment-simulation", "")
    content_simulation = simulation_mode == CONTENT_SIMULATION_HEADER
    diagnostic_simulation = simulation_mode in DIAGNOSTIC_SIMULATION_HEADERS
    if not store.credentials().api_key.strip():
        await websocket.send_json(
            {
                "type": "technical.pause",
                "phase": RuntimePhase.technical_paused.value,
                "message": "请先在设置页配置阿里云百炼 API Key",
                "can_retry": False,
            }
        )
        await websocket.close(code=4403)
        return
    try:
        snapshot = kernel.snapshot(session_id)
    except KernelSessionNotFoundError:
        await websocket.send_json({"type": "session.error", "message": "会话不存在"})
        await websocket.close(code=4404)
        return
    except KernelSessionConflictError:
        await websocket.send_json({"type": "session.error", "message": "会话已结束"})
        await websocket.close(code=4409)
        return
    await websocket.send_json(
        {
            "type": "snapshot",
            "session_id": snapshot.session_id,
            "media": snapshot.media.value,
            "phase": (
                RuntimePhase.ended.value
                if snapshot.pending_ending_route_id is not None
                else snapshot.phase.value
            ),
            "transcript": [
                turn.model_dump(mode="json") for turn in snapshot.transcript
            ],
            "opening_delay_seconds": snapshot.opening_delay_seconds,
            "pending_ending_route_id": snapshot.pending_ending_route_id,
            "can_retry": snapshot.technical_retry_allowed,
            "can_redo_input": (
                snapshot.media is Media.voice
                and bool(getattr(kernel, "manual_turn_completion", False))
            ),
        }
    )
    if snapshot.pending_ending_route_id is not None:
        kernel.end_session(session_id, EndReason.natural_closure)
        await websocket.send_json(
            {
                "type": "session.ended",
                "reason": EndReason.natural_closure.value,
            }
        )
        await websocket.close(code=1000)
        return
    connection = _LiveConnection(
        websocket=websocket,
        session_id=session_id,
        kernel=kernel,
        speech_provider=speech_provider,
        media=snapshot.media,
        initial_phase=snapshot.phase,
        opening_delay_seconds=snapshot.opening_delay_seconds,
        content_simulation=content_simulation,
        diagnostic_simulation=diagnostic_simulation,
        technical_retry_allowed=snapshot.technical_retry_allowed,
        failure_recorder=RuntimeFailureRecorder(database.engine),
    )
    await connection.run()

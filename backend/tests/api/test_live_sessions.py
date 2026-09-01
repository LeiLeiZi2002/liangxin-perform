import asyncio
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.api.routes.live_sessions import (
    get_assessment_kernel,
    get_character_prompt_kernel,
)
from app.main import app
from app.runtime.failures import FailureDisposition, RuntimeFailure
from app.runtime.kernel import (
    KernelOpeningResult,
    KernelTurnConflictError,
    KernelTurnResult,
    LiveSnapshot,
    PersistedTurn,
    RuntimePhase,
    TechnicalPauseError,
)
from app.runtime.metrics import ModelCallRecorder
from app.runtime.models import RuntimeFailureRecord
from app.runtime.providers import ASRSentence, RuntimeSpeechError
from app.runtime.turn_boundary import BoundaryState, TurnBoundary
from app.runtime_config import RuntimeCredentialStore
from app.sessions.models import EndReason, Media, TurnSpeaker


def test_assessment_kernel_injects_shared_model_call_recorder(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database as database

    monkeypatch.setattr(database, "engine", test_engine)
    clear = getattr(get_assessment_kernel, "cache_clear", None)
    if clear is not None:
        clear()
    try:
        kernel = get_assessment_kernel()

        director_recorder = kernel._director._recorder
        actor_recorder = kernel._actor._recorder
        assert isinstance(director_recorder, ModelCallRecorder)
        assert actor_recorder is director_recorder
        assert kernel._model_call_recorder is director_recorder
        assert kernel._director._failure_recorder is kernel._failure_recorder
        assert kernel._actor._failure_recorder is kernel._failure_recorder
    finally:
        if clear is not None:
            clear()


def test_live_kernel_factories_share_process_instances(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database as database

    monkeypatch.setattr(database, "engine", test_engine)
    factories = (get_assessment_kernel, get_character_prompt_kernel)
    for factory in factories:
        clear = getattr(factory, "cache_clear", None)
        if clear is not None:
            clear()
    try:
        assert get_assessment_kernel() is get_assessment_kernel()
        assert get_character_prompt_kernel() is get_character_prompt_kernel()
    finally:
        for factory in factories:
            clear = getattr(factory, "cache_clear", None)
            if clear is not None:
                clear()


def test_live_kernel_selection_uses_persisted_engine_and_legacy_default(
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlmodel import Session, SQLModel

    import app.database as database
    from app.api.routes.live_sessions import select_live_kernel
    from app.sessions.models import (
        CaseType,
        ModelMode,
        Scene,
        SessionMode,
        SessionRecord,
    )

    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(database, "engine", test_engine)
    with Session(test_engine) as db:
        db.add_all(
            [
                SessionRecord(
                    id="new-character-session",
                    mode=SessionMode.assessment,
                    scene=Scene.hotline,
                    case_type=CaseType.main,
                    case_id="crisis_student_main",
                    media=Media.voice,
                    model_mode=ModelMode.live,
                    state_json={
                        "runtime": {
                            "engine": "character_prompt",
                            "phase": "listening",
                        }
                    },
                ),
                SessionRecord(
                    id="legacy-workflow-session",
                    mode=SessionMode.assessment,
                    scene=Scene.hotline,
                    case_type=CaseType.main,
                    case_id="crisis_student_main",
                    media=Media.voice,
                    model_mode=ModelMode.live,
                    state_json={"runtime": {"phase": "listening"}},
                ),
            ]
        )
        db.commit()

    workflow = object()
    character = object()
    assert (
        select_live_kernel(
            "new-character-session",
            workflow_kernel=workflow,
            character_kernel=character,
        )
        is character
    )
    assert (
        select_live_kernel(
            "legacy-workflow-session",
            workflow_kernel=workflow,
            character_kernel=character,
        )
        is workflow
    )


class FakeKernel:
    def __init__(
        self,
        *,
        media: Media,
        fail: bool = False,
        has_transcript: bool = True,
        ending_route_id: str | None = None,
        opening_ending_route_id: str | None = None,
        opening_delay_seconds: float | None = None,
        pending_ending_route_id: str | None = None,
        phase: RuntimePhase = RuntimePhase.listening,
        technical_retry_allowed: bool = False,
    ) -> None:
        self.media = media
        self.fail = fail
        self.has_transcript = has_transcript
        self.ending_route_id = ending_route_id
        self.opening_ending_route_id = opening_ending_route_id
        self.opening_delay_seconds = (
            None if has_transcript else opening_delay_seconds
        )
        self.pending_ending_route_id = pending_ending_route_id
        self.phase = phase
        self.technical_retry_allowed = technical_retry_allowed
        self.opening_calls = 0
        self.turn_calls: list[tuple[str, str]] = []
        self.turn_synthesize_audio: list[bool] = []
        self.opening_synthesize_audio: list[bool] = []
        self.turn_capture_failure_payload: list[bool] = []
        self.turn_world_time_advances: list[int] = []
        self.turn_worker_pcm: list[bytes] = []
        self.turn_speech_metrics: list[object] = []
        self.opening_capture_failure_payload: list[bool] = []
        self.ended_with: EndReason | None = None

    def snapshot(self, session_id: str) -> LiveSnapshot:
        return LiveSnapshot(
            session_id=session_id,
            media=self.media,
            phase=self.phase,
            opening_delay_seconds=self.opening_delay_seconds,
            pending_ending_route_id=self.pending_ending_route_id,
            technical_retry_allowed=self.technical_retry_allowed,
            transcript=(
                [
                    PersistedTurn(
                        id="old-worker",
                        sequence=1,
                        speaker=TurnSpeaker.worker,
                        text="你好，这里是心理援助热线。",
                        client_turn_id="old-1",
                    )
                ]
                if self.has_transcript
                else []
            ),
        )

    async def process_worker_turn(
        self,
        *,
        session_id: str,
        client_turn_id: str,
        text: str,
        worker_pcm: bytes = b"",
        speech_metrics: object = None,
        synthesize_audio: bool = True,
        capture_failure_payload: bool = False,
        world_time_advance_seconds: int = 0,
        on_phase: Callable[[RuntimePhase], Awaitable[None]] | None = None,
        on_actor_text: Callable[[str], object] | None = None,
        on_audio_chunk: Callable[[bytes], object] | None = None,
    ) -> KernelTurnResult:
        del session_id
        self.turn_calls.append((client_turn_id, text))
        self.turn_worker_pcm.append(worker_pcm)
        self.turn_speech_metrics.append(speech_metrics)
        self.turn_synthesize_audio.append(synthesize_audio)
        self.turn_capture_failure_payload.append(capture_failure_payload)
        self.turn_world_time_advances.append(world_time_advance_seconds)
        if on_phase is not None:
            await on_phase(RuntimePhase.directing)
            await on_phase(RuntimePhase.acting)
            if self.media is Media.voice and synthesize_audio:
                await on_phase(RuntimePhase.synthesizing)
        if self.fail:
            raise TechnicalPauseError(RuntimePhase.acting)
        if self.media is Media.voice and synthesize_audio and on_actor_text is not None:
            value = on_actor_text("嗯……我在。")
            if asyncio.iscoroutine(value):
                await value
        if self.media is Media.voice and synthesize_audio and on_audio_chunk is not None:
            for chunk in (b"first-pcm", b"second-pcm"):
                value = on_audio_chunk(chunk)
                if asyncio.iscoroutine(value):
                    await value
        return KernelTurnResult(
            worker=PersistedTurn(
                id="worker-new",
                sequence=2,
                speaker=TurnSpeaker.worker,
                text=text,
                client_turn_id=client_turn_id,
            ),
            client=PersistedTurn(
                id="client-new",
                sequence=3,
                speaker=TurnSpeaker.client,
                text="嗯……我在。",
                client_turn_id=client_turn_id,
            ),
            audio_chunks=(
                (b"first-pcm", b"second-pcm")
                if self.media is Media.voice and synthesize_audio
                else ()
            ),
            ending_route_id=self.ending_route_id,
        )

    async def generate_opening(
        self,
        *,
        session_id: str,
        client_turn_id: str,
        synthesize_audio: bool = True,
        capture_failure_payload: bool = False,
        on_phase: Callable[[RuntimePhase], Awaitable[None]] | None = None,
        on_actor_text: Callable[[str], object] | None = None,
        on_audio_chunk: Callable[[bytes], object] | None = None,
    ) -> KernelOpeningResult:
        del session_id
        self.opening_calls += 1
        self.opening_synthesize_audio.append(synthesize_audio)
        self.opening_capture_failure_payload.append(capture_failure_payload)
        if on_phase is not None:
            await on_phase(RuntimePhase.acting)
        del on_actor_text, on_audio_chunk
        return KernelOpeningResult(
            client=PersistedTurn(
                id="opening",
                sequence=2,
                speaker=TurnSpeaker.client,
                text="喂……你好，能听见吗？",
                client_turn_id=client_turn_id,
            ),
            audio_chunks=(
                (b"opening-pcm",)
                if self.media is Media.voice and synthesize_audio
                else ()
            ),
            ending_route_id=self.opening_ending_route_id,
        )

    def resume_listening(self, session_id: str) -> None:
        del session_id

    def pause_technical(self, session_id: str, *, can_retry: bool) -> None:
        del session_id, can_retry

    def end_session(self, session_id: str, reason: EndReason) -> None:
        del session_id
        self.ended_with = reason


def configured_store() -> RuntimeCredentialStore:
    store = RuntimeCredentialStore()
    store.update(api_key="test-live-session-key")
    return store


class FakeASRStream:
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.delivered = False
        self.closed = False
        self.sent_audio: list[bytes] = []

    async def send_audio(self, pcm_chunk: bytes) -> None:
        if pcm_chunk:
            self.sent_audio.append(pcm_chunk)
            self.ready.set()

    async def receive_sentence(self) -> ASRSentence | None:
        if self.delivered:
            await asyncio.Event().wait()
        await self.ready.wait()
        self.delivered = True
        return ASRSentence(
            text="你现在是一个人吗？",
            sentence_id=1,
            begin_time_ms=0,
            end_time_ms=900,
            sentence_begin=False,
            sentence_end=True,
            words=(),
        )

    async def finish(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FakeLiveSpeechProvider:
    def __init__(self) -> None:
        self.stream = FakeASRStream()
        self.calls = 0

    async def open_asr(self) -> FakeASRStream:
        self.calls += 1
        return self.stream


class FakeSocket:
    def __init__(self) -> None:
        self.json_messages: list[dict[str, object]] = []
        self.binary_messages: list[bytes] = []
        self.closed = False

    async def send_json(self, message: dict[str, object]) -> None:
        self.json_messages.append(message)

    async def send_bytes(self, chunk: bytes) -> None:
        self.binary_messages.append(chunk)

    async def close(self, code: int = 1000) -> None:
        del code
        self.closed = True


class ScriptedSocket(FakeSocket):
    def __init__(self, messages: list[dict[str, object]]) -> None:
        super().__init__()
        self.messages = list(messages)

    async def receive(self) -> dict[str, object]:
        return self.messages.pop(0)


class FailingReceiveSocket(FakeSocket):
    async def receive(self) -> dict[str, object]:
        raise OSError("WebSocket 连接突然中断")


class FakeFailureRecorder:
    def __init__(self) -> None:
        self.failures: list[RuntimeFailure] = []

    def record(self, failure: RuntimeFailure) -> object:
        self.failures.append(failure)
        last = failure.attempts[-1]
        return SimpleNamespace(
            id=f"failure-{len(self.failures)}",
            session_id=failure.session_id,
            client_turn_id=failure.client_turn_id,
            failure_code=failure.failure_code,
            component=failure.component,
            phase=failure.phase,
            operation=failure.operation,
            attempt_count=len(failure.attempts),
            retryable=failure.retryable,
            disposition=failure.disposition.value,
            error_class=last.error_class,
            provider_status_code=last.provider_status_code,
            provider_request_id=last.provider_request_id,
            attempts_json=[
                {
                    "index": item.index,
                    "error_class": item.error_class,
                    "message": item.message,
                    "call_kind": item.call_kind,
                    "provider_status_code": item.provider_status_code,
                    "provider_request_id": item.provider_request_id,
                    "details": dict(item.details),
                }
                for item in failure.attempts
            ],
            details_json=dict(failure.details),
        )


class FailingOpenSpeechProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def open_asr(self) -> FakeASRStream:
        self.calls += 1
        raise RuntimeSpeechError(f"ASR 连接失败 {self.calls}")


class QueueASRStream:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.queue: asyncio.Queue[ASRSentence | None] = asyncio.Queue()
        self.fail_send = fail_send
        self.receive_calls = 0
        self.closed = False

    async def send_audio(self, pcm_chunk: bytes) -> None:
        del pcm_chunk
        if self.fail_send:
            raise RuntimeSpeechError("连接中断")

    async def receive_sentence(self) -> ASRSentence | None:
        self.receive_calls += 1
        return await self.queue.get()

    async def finish(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class CompletingASRStream(QueueASRStream):
    async def finish(self) -> None:
        await self.queue.put(None)


class FinalizingASRStream(QueueASRStream):
    def __init__(self, tail: ASRSentence) -> None:
        super().__init__()
        self.tail = tail
        self.finished = False

    async def finish(self) -> None:
        self.finished = True
        await self.queue.put(self.tail)
        await self.queue.put(None)


class DelayedFinalizingASRStream(QueueASRStream):
    def __init__(self, tail: ASRSentence, *, delay_seconds: float) -> None:
        super().__init__()
        self.tail = tail
        self.delay_seconds = delay_seconds
        self.finished = False
        self.publish_task: asyncio.Task[None] | None = None

    async def finish(self) -> None:
        self.finished = True

        async def publish_tail() -> None:
            await asyncio.sleep(self.delay_seconds)
            await self.queue.put(self.tail)
            await self.queue.put(None)

        self.publish_task = asyncio.create_task(publish_tail())

    async def close(self) -> None:
        await super().close()
        if self.publish_task is not None and not self.publish_task.done():
            self.publish_task.cancel()


class FailingReceiveASRStream(QueueASRStream):
    async def receive_sentence(self) -> ASRSentence | None:
        self.receive_calls += 1
        raise RuntimeSpeechError("ASR 识别连接中断")


class CloseWakesFailingReceiveASRStream(QueueASRStream):
    def __init__(self) -> None:
        super().__init__()
        self.receive_started = asyncio.Event()
        self.close_started = asyncio.Event()

    async def receive_sentence(self) -> ASRSentence | None:
        self.receive_calls += 1
        self.receive_started.set()
        await self.close_started.wait()
        raise RuntimeSpeechError("ASR 流已主动关闭")

    async def close(self) -> None:
        self.closed = True
        self.close_started.set()
        await asyncio.sleep(0.02)


class FinishWakesFailingReceiveASRStream(QueueASRStream):
    def __init__(self) -> None:
        super().__init__()
        self.finish_started = asyncio.Event()

    async def receive_sentence(self) -> ASRSentence | None:
        self.receive_calls += 1
        await self.finish_started.wait()
        raise RuntimeSpeechError("ASR 收尾时连接中断")

    async def finish(self) -> None:
        self.finish_started.set()


class BlockingCloseASRStream(QueueASRStream):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_cancelled = False

    async def close(self) -> None:
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.closed = True


class SequenceSpeechProvider:
    def __init__(self, streams: list[QueueASRStream]) -> None:
        self.streams = streams
        self.calls = 0

    async def open_asr(self) -> QueueASRStream:
        stream = self.streams[self.calls]
        self.calls += 1
        return stream


class FailAfterFirstOpenSpeechProvider:
    def __init__(self, stream: QueueASRStream) -> None:
        self.stream = stream
        self.calls = 0

    async def open_asr(self) -> QueueASRStream:
        self.calls += 1
        if self.calls == 1:
            return self.stream
        raise RuntimeSpeechError("重开识别连接失败")


class BlockingAfterFirstOpenSpeechProvider:
    def __init__(self, stream: QueueASRStream) -> None:
        self.stream = stream
        self.calls = 0
        self.blocked = asyncio.Event()

    async def open_asr(self) -> QueueASRStream:
        self.calls += 1
        if self.calls == 1:
            return self.stream
        await self.blocked.wait()
        raise AssertionError("阻塞的 ASR 重开不应自行结束")


class PartialAudioFailureKernel(FakeKernel):
    async def process_worker_turn(self, **kwargs: object) -> KernelTurnResult:
        on_actor_text = kwargs.get("on_actor_text")
        on_audio_chunk = kwargs.get("on_audio_chunk")
        if callable(on_actor_text):
            value = on_actor_text("嗯……我还在。")
            if asyncio.iscoroutine(value):
                await value
        if callable(on_audio_chunk):
            value = on_audio_chunk(b"audible-part")
            if asyncio.iscoroutine(value):
                await value
        raise TechnicalPauseError(RuntimePhase.synthesizing)


class TextOnlyTTSFailureKernel(FakeKernel):
    async def process_worker_turn(self, **kwargs: object) -> KernelTurnResult:
        on_actor_text = kwargs.get("on_actor_text")
        if callable(on_actor_text):
            value = on_actor_text("我已经想好要怎么说了，只是声音暂时送不出来。")
            if asyncio.iscoroutine(value):
                await value
        raise TechnicalPauseError(RuntimePhase.synthesizing, can_retry=True)


class BlockingTurnKernel(FakeKernel):
    def __init__(self) -> None:
        super().__init__(media=Media.text)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def process_worker_turn(self, **kwargs: object) -> KernelTurnResult:
        client_turn_id = str(kwargs["client_turn_id"])
        text = str(kwargs["text"])
        self.turn_calls.append((client_turn_id, text))
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return KernelTurnResult(
            worker=PersistedTurn(
                id="blocking-worker",
                sequence=2,
                speaker=TurnSpeaker.worker,
                text=text,
                client_turn_id=client_turn_id,
            ),
            client=PersistedTurn(
                id="blocking-client",
                sequence=3,
                speaker=TurnSpeaker.client,
                text="嗯，你说。",
                client_turn_id=client_turn_id,
            ),
            audio_chunks=(),
        )


class RecordedTechnicalPauseKernel(FakeKernel):
    async def process_worker_turn(self, **kwargs: object) -> KernelTurnResult:
        del kwargs
        record = RuntimeFailureRecord(
            id="failure-from-kernel",
            session_id="kernel-recorded-pause",
            client_turn_id="kernel-failed-turn",
            component="actor",
            phase="acting",
            operation="output_validation",
            failure_code="actor.output_validation",
            error_class="ActorOutputValidationError",
            attempt_count=2,
            retryable=False,
            disposition="technical_pause",
            attempts_json=[
                {
                    "index": 1,
                    "error_class": "ActorOutputValidationError",
                    "message": "回答中出现了未开放信息",
                }
            ],
            details_json={"diagnostic": "safe"},
        )
        raise TechnicalPauseError(
            RuntimePhase.acting,
            can_retry=False,
            failure_id=record.id,
            failure_code=record.failure_code,
            failure_record=record,
        )


class RetryableRecordedTechnicalPauseKernel(FakeKernel):
    async def process_worker_turn(self, **kwargs: object) -> KernelTurnResult:
        client_turn_id = str(kwargs["client_turn_id"])
        record = RuntimeFailureRecord(
            id="retryable-character-failure",
            session_id="retryable-character-pause",
            client_turn_id=client_turn_id,
            component="actor",
            phase="acting",
            operation="output_validation",
            failure_code="actor.output_validation",
            error_class="CharacterOutputValidationError",
            attempt_count=2,
            retryable=True,
            disposition="technical_pause",
            attempts_json=[],
            details_json={},
        )
        raise TechnicalPauseError(
            RuntimePhase.acting,
            can_retry=True,
            failure_id=record.id,
            failure_code=record.failure_code,
            failure_record=record,
        )


class StreamingOpeningKernel(FakeKernel):
    def __init__(self) -> None:
        super().__init__(
            media=Media.voice,
            has_transcript=False,
            opening_delay_seconds=0,
        )
        self.first_chunk_sent = asyncio.Event()
        self.release_finish = asyncio.Event()

    async def generate_opening(self, **kwargs: object) -> KernelOpeningResult:
        self.opening_calls += 1
        client_turn_id = str(kwargs["client_turn_id"])
        on_actor_text = kwargs.get("on_actor_text")
        on_audio_chunk = kwargs.get("on_audio_chunk")
        if callable(on_actor_text):
            value = on_actor_text("喂……你好，能听见吗？")
            if asyncio.iscoroutine(value):
                await value
        if callable(on_audio_chunk):
            value = on_audio_chunk(b"opening-first")
            if asyncio.iscoroutine(value):
                await value
        self.first_chunk_sent.set()
        await self.release_finish.wait()
        if callable(on_audio_chunk):
            value = on_audio_chunk(b"opening-second")
            if asyncio.iscoroutine(value):
                await value
        return KernelOpeningResult(
            client=PersistedTurn(
                id="opening-streamed",
                sequence=1,
                speaker=TurnSpeaker.client,
                text="喂……你好，能听见吗？",
                client_turn_id=client_turn_id,
            ),
            audio_chunks=(b"opening-first", b"opening-second"),
        )


class RetriableOpeningKernel(FakeKernel):
    def __init__(self, *, partial_audio: bool = False) -> None:
        super().__init__(
            media=Media.voice if partial_audio else Media.text,
            has_transcript=False,
            opening_delay_seconds=0,
        )
        self.partial_audio = partial_audio
        self.opening_client_turn_ids: list[str] = []

    async def generate_opening(self, **kwargs: object) -> KernelOpeningResult:
        self.opening_calls += 1
        client_turn_id = str(kwargs["client_turn_id"])
        self.opening_client_turn_ids.append(client_turn_id)
        if self.opening_calls == 1:
            if self.partial_audio:
                on_actor_text = kwargs.get("on_actor_text")
                on_audio_chunk = kwargs.get("on_audio_chunk")
                if callable(on_actor_text):
                    value = on_actor_text("喂……")
                    if asyncio.iscoroutine(value):
                        await value
                if callable(on_audio_chunk):
                    value = on_audio_chunk(b"opening-audible")
                    if asyncio.iscoroutine(value):
                        await value
            raise TechnicalPauseError(RuntimePhase.synthesizing)
        return KernelOpeningResult(
            client=PersistedTurn(
                id="opening-retried",
                sequence=1,
                speaker=TurnSpeaker.client,
                text="喂……你好，能听见吗？",
                client_turn_id=client_turn_id,
            ),
            audio_chunks=(),
        )


class RetriableTurnKernel(FakeKernel):
    def __init__(self) -> None:
        super().__init__(
            media=Media.voice,
            has_transcript=False,
            opening_delay_seconds=0,
        )
        self.fail_next_turn = True

    async def process_worker_turn(self, **kwargs: object) -> KernelTurnResult:
        if self.fail_next_turn:
            self.fail_next_turn = False
            self.turn_calls.append(
                (str(kwargs["client_turn_id"]), str(kwargs["text"]))
            )
            raise TechnicalPauseError(RuntimePhase.acting)
        return await super().process_worker_turn(**kwargs)


def asr_sentence(text: str, *, sentence_id: int = 1) -> ASRSentence:
    return ASRSentence(
        text=text,
        sentence_id=sentence_id,
        begin_time_ms=0,
        end_time_ms=800,
        sentence_begin=False,
        sentence_end=True,
        words=(),
    )


def override_live_dependencies(
    kernel: FakeKernel,
    speech: FakeLiveSpeechProvider | None = None,
) -> None:
    from app.api.routes.live_sessions import (
        get_assessment_kernel,
        get_character_prompt_kernel,
        get_live_credential_store,
        get_live_speech_provider,
    )

    app.dependency_overrides[get_assessment_kernel] = lambda: kernel
    app.dependency_overrides[get_character_prompt_kernel] = lambda: kernel
    app.dependency_overrides[get_live_credential_store] = configured_store
    if speech is not None:
        app.dependency_overrides[get_live_speech_provider] = lambda: speech


def receive_until(websocket: object, expected_type: str, limit: int = 10) -> dict[str, object]:
    for _ in range(limit):
        message = websocket.receive_json()  # type: ignore[attr-defined]
        if message["type"] == expected_type:
            return message
    raise AssertionError(f"未收到 {expected_type}")


def test_reconnect_sends_complete_persisted_snapshot(client: TestClient) -> None:
    kernel = FakeKernel(media=Media.text)
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-1") as websocket:
        snapshot = websocket.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["phase"] == "listening"
    assert snapshot["transcript"][0]["text"] == "你好，这里是心理援助热线。"


def test_reconnect_snapshot_exposes_persisted_retry_permission(
    client: TestClient,
) -> None:
    kernel = FakeKernel(
        media=Media.text,
        has_transcript=False,
        opening_delay_seconds=0,
        phase=RuntimePhase.technical_paused,
        technical_retry_allowed=True,
    )
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-retryable") as websocket:
        snapshot = websocket.receive_json()

    assert snapshot["phase"] == RuntimePhase.technical_paused.value
    assert snapshot["can_retry"] is True


def test_voice_snapshot_exposes_manual_redo_capability(client: TestClient) -> None:
    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-redo-capability") as websocket:
        snapshot = websocket.receive_json()

    assert snapshot["can_redo_input"] is True


def test_reconnect_finishes_persisted_pending_natural_closure(
    client: TestClient,
) -> None:
    kernel = FakeKernel(
        media=Media.voice,
        pending_ending_route_id="worker_close",
    )
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-pending-close") as websocket:
        snapshot = websocket.receive_json()
        ended = websocket.receive_json()

    assert snapshot["pending_ending_route_id"] == "worker_close"
    assert ended == {"type": "session.ended", "reason": "natural_closure"}
    assert kernel.ended_with is EndReason.natural_closure


def test_reconnect_with_transcript_does_not_schedule_another_opening(
    client: TestClient,
) -> None:
    kernel = FakeKernel(media=Media.text)
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-existing") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "session.start"})
        receive_until(websocket, "phase")
        time.sleep(0.05)

    assert kernel.opening_calls == 0


def test_text_turn_runs_phases_and_never_sends_binary(client: TestClient) -> None:
    kernel = FakeKernel(media=Media.text)
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-text") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "session.start"})
        receive_until(websocket, "phase")
        websocket.send_json(
            {"type": "text.turn", "text": "你现在方便聊吗？", "client_turn_id": "text-1"}
        )
        visitor = receive_until(websocket, "visitor.text")
        committed = receive_until(websocket, "turn.committed")

    assert visitor["text"] == "嗯……我在。"
    assert committed["client_turn_id"] == "text-1"
    assert kernel.turn_calls == [("text-1", "你现在方便聊吗？")]


def test_text_turn_natural_closure_ends_after_committed_reply(client: TestClient) -> None:
    kernel = FakeKernel(media=Media.text, ending_route_id="worker_close")
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-text-close") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"type": "text.turn", "text": "那我们先到这里。", "client_turn_id": "text-close"}
        )
        receive_until(websocket, "turn.committed")
        ended = receive_until(websocket, "session.ended")

    assert ended["reason"] == "natural_closure"
    assert kernel.ended_with is EndReason.natural_closure


def test_voice_sends_visitor_text_before_ordered_pcm(client: TestClient) -> None:
    kernel = FakeKernel(media=Media.voice)
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-voice") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"type": "text.turn", "text": "你还在吗？", "client_turn_id": "voice-1"}
        )
        receive_until(websocket, "visitor.text")
        assert websocket.receive_bytes() == b"first-pcm"
        assert websocket.receive_bytes() == b"second-pcm"
        committed = websocket.receive_json()
        websocket.send_json({"type": "playback.ended"})
        listening = receive_until(websocket, "phase")

    assert committed["type"] == "turn.committed"
    assert listening["phase"] == "listening"


def test_voice_natural_closure_waits_until_playback_finishes(client: TestClient) -> None:
    kernel = FakeKernel(media=Media.voice, ending_route_id="worker_close")
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-voice-close") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"type": "text.turn", "text": "朋友已经到了吗？", "client_turn_id": "voice-close"}
        )
        receive_until(websocket, "visitor.text")
        assert websocket.receive_bytes() == b"first-pcm"
        assert websocket.receive_bytes() == b"second-pcm"
        assert websocket.receive_json()["type"] == "turn.committed"
        websocket.send_json({"type": "playback.ended"})
        ended = receive_until(websocket, "session.ended")

    assert ended["reason"] == "natural_closure"
    assert kernel.ended_with is EndReason.natural_closure


@pytest.mark.asyncio
async def test_text_opening_natural_closure_ends_after_opening_is_delivered() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.text,
        has_transcript=False,
        opening_ending_route_id="character_prompt_end",
    )
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="text-opening-close",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=object(),  # type: ignore[arg-type]
        media=Media.text,
        initial_phase=RuntimePhase.acting,
    )
    result = await kernel.generate_opening(
        session_id="text-opening-close",
        client_turn_id="opening-text-close",
        synthesize_audio=False,
    )

    await connection._play_opening(result)

    assert kernel.ended_with is EndReason.natural_closure
    assert socket.json_messages[-1] == {
        "type": "session.ended",
        "reason": EndReason.natural_closure.value,
    }


@pytest.mark.asyncio
async def test_voice_opening_natural_closure_waits_for_playback_end() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.voice,
        has_transcript=False,
        opening_ending_route_id="character_prompt_end",
    )
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="voice-opening-close",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=object(),  # type: ignore[arg-type]
        media=Media.voice,
        initial_phase=RuntimePhase.acting,
    )
    result = await kernel.generate_opening(
        session_id="voice-opening-close",
        client_turn_id="opening-voice-close",
    )
    await connection._actor_text_ready(result.client.text)
    await connection._stream_audio_chunk(b"opening-streamed-pcm")

    await connection._play_opening(result)

    assert connection.phase is RuntimePhase.playing
    assert kernel.ended_with is None
    await connection._playback_ended()
    assert kernel.ended_with is EndReason.natural_closure
    assert socket.json_messages[-1] == {
        "type": "session.ended",
        "reason": EndReason.natural_closure.value,
    }


def test_content_simulation_header_skips_asr_and_tts_but_keeps_voice_playback(
    client: TestClient,
) -> None:
    kernel = FakeKernel(media=Media.voice, ending_route_id="worker_close")
    speech = FakeLiveSpeechProvider()
    override_live_dependencies(kernel, speech)

    with client.websocket_connect(
        "/api/live-sessions/session-content-simulation",
        headers={"X-Assessment-Simulation": "content"},
    ) as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "session.start"})
        receive_until(websocket, "phase")
        websocket.send_json(
            {
                "type": "text.turn",
                "text": "那我们先到这里。",
                "client_turn_id": "content-close",
            }
        )
        messages: list[dict[str, object]] = []
        while not any(item["type"] == "turn.committed" for item in messages):
            messages.append(websocket.receive_json())
        websocket.send_json({"type": "playback.ended"})
        ended = receive_until(websocket, "session.ended")

    assert speech.calls == 0
    assert kernel.turn_synthesize_audio == [False]
    assert kernel.turn_capture_failure_payload == [True]
    assert any(
        item.get("type") == "phase" and item.get("phase") == "playing"
        for item in messages
    )
    assert ended["reason"] == "natural_closure"
    assert kernel.ended_with is EndReason.natural_closure


@pytest.mark.parametrize("simulation_mode", ["content", "voice"])
def test_diagnostic_simulation_forwards_world_time_advance(
    client: TestClient,
    simulation_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import live_sessions

    kernel = FakeKernel(media=Media.text)
    override_live_dependencies(kernel)
    monkeypatch.setattr(live_sessions, "CharacterPromptKernel", FakeKernel)

    with client.websocket_connect(
        "/api/live-sessions/session-world-time-simulation",
        headers={"X-Assessment-Simulation": simulation_mode},
    ) as websocket:
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "text.turn",
                "text": "再听一下门外的动静。",
                "client_turn_id": "timed-turn",
                "world_time_advance_seconds": 960,
            }
        )
        receive_until(websocket, "turn.committed")

    assert kernel.turn_world_time_advances == [960]


def test_formal_connection_ignores_world_time_advance(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import live_sessions

    kernel = FakeKernel(media=Media.text)
    override_live_dependencies(kernel)
    monkeypatch.setattr(live_sessions, "CharacterPromptKernel", FakeKernel)

    with client.websocket_connect(
        "/api/live-sessions/session-world-time-formal",
    ) as websocket:
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "text.turn",
                "text": "再听一下门外的动静。",
                "client_turn_id": "formal-turn",
                "world_time_advance_seconds": 960,
            }
        )
        receive_until(websocket, "turn.committed")

    assert kernel.turn_world_time_advances == [0]


def test_only_exact_content_simulation_header_changes_voice_behavior(
    client: TestClient,
) -> None:
    kernel = FakeKernel(media=Media.voice)
    speech = FakeLiveSpeechProvider()
    override_live_dependencies(kernel, speech)

    with client.websocket_connect(
        "/api/live-sessions/session-wrong-simulation-header",
        headers={"X-Assessment-Simulation": "Content"},
    ) as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "session.start"})
        receive_until(websocket, "phase")
        websocket.send_json(
            {
                "type": "text.turn",
                "text": "你还在吗？",
                "client_turn_id": "wrong-header",
            }
        )
        receive_until(websocket, "visitor.text")
        assert websocket.receive_bytes() == b"first-pcm"
        assert websocket.receive_bytes() == b"second-pcm"

    assert speech.calls == 0
    assert kernel.turn_synthesize_audio == [True]


def test_content_simulation_opening_uses_actor_without_asr_or_tts(
    client: TestClient,
) -> None:
    kernel = FakeKernel(
        media=Media.voice,
        has_transcript=False,
        opening_delay_seconds=0,
    )
    speech = FakeLiveSpeechProvider()
    override_live_dependencies(kernel, speech)

    with client.websocket_connect(
        "/api/live-sessions/session-content-opening",
        headers={"X-Assessment-Simulation": "content"},
    ) as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "session.start"})
        receive_until(websocket, "visitor.text")
        receive_until(websocket, "turn.committed")
        websocket.send_json({"type": "playback.ended"})
        listening = receive_until(websocket, "phase")

    assert speech.calls == 0
    assert kernel.opening_synthesize_audio == [False]
    assert kernel.opening_capture_failure_payload == [True]
    assert listening["phase"] == "listening"


def test_content_simulation_never_opens_asr_for_pcm_or_retry(
    client: TestClient,
) -> None:
    kernel = FakeKernel(media=Media.voice)
    speech = FakeLiveSpeechProvider()
    override_live_dependencies(kernel, speech)

    with client.websocket_connect(
        "/api/live-sessions/session-content-no-asr",
        headers={"X-Assessment-Simulation": "content"},
    ) as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "session.start"})
        receive_until(websocket, "phase")
        websocket.send_bytes(b"pcm-that-must-be-ignored")
        websocket.send_json({"type": "technical.retry"})
        receive_until(websocket, "phase")

    assert speech.calls == 0


def test_five_second_opening_is_cancelled_when_worker_speaks_first(
    client: TestClient,
) -> None:
    kernel = FakeKernel(
        media=Media.text,
        has_transcript=False,
        opening_delay_seconds=0.05,
    )
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-opening") as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "session.start"})
        receive_until(websocket, "phase")
        websocket.send_json(
            {"type": "text.turn", "text": "你好。", "client_turn_id": "worker-first"}
        )
        receive_until(websocket, "turn.committed")
        time.sleep(0.08)

    assert kernel.opening_calls == 0


async def test_connection_schedules_immediate_opening_from_snapshot_policy() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.text, has_transcript=False)
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="immediate-opening",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0,
    )

    await connection._start_session()
    await asyncio.wait_for(_wait_for(lambda: kernel.opening_calls == 1), timeout=1)
    await connection._cleanup()


async def test_worker_speech_does_not_cancel_opening_after_audio_started() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = StreamingOpeningKernel()
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="overlap-opening",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0,
    )

    await connection._start_session()
    await asyncio.wait_for(kernel.first_chunk_sent.wait(), timeout=1)
    opening_task = connection.opening_task
    await connection._speech_started(100)

    assert opening_task is not None
    assert opening_task.cancelled() is False
    assert connection.overlap_started_ms == 100

    kernel.release_finish.set()
    await asyncio.wait_for(opening_task, timeout=1)
    assert any(item["type"] == "turn.committed" for item in socket.json_messages)
    await connection._cleanup()


async def test_technical_retry_restarts_failed_opening_without_delay() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = RetriableOpeningKernel()
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="retry-opening",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0,
    )

    await connection._start_session()
    await asyncio.wait_for(
        _wait_for(
            lambda: any(
                item["type"] == "technical.pause" for item in socket.json_messages
            )
        ),
        timeout=1,
    )
    await connection._technical_retry()
    await asyncio.wait_for(
        _wait_for(
            lambda: any(
                item["type"] == "turn.committed" for item in socket.json_messages
            )
        ),
        timeout=1,
    )

    assert kernel.opening_calls == 2
    assert len(set(kernel.opening_client_turn_ids)) == 1
    await connection._cleanup()


async def test_technical_retry_after_opening_retries_original_turn_before_opening() -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    kernel = RetriableTurnKernel()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="retry-normal-turn",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0,
    )

    await connection._start_session()
    await asyncio.wait_for(_wait_for(lambda: kernel.opening_calls == 1), timeout=1)
    await asyncio.wait_for(_wait_for(lambda: connection.opening_task is None), timeout=1)
    await connection._playback_ended()
    payload = _PendingGeneration(
        text="你现在身边有人吗？",
        client_turn_id="voice-original",
        worker_pcm=b"worker-audio",
        metrics=None,
    )

    await connection._run_generation(payload)
    assert connection.retry_payload == payload
    connection._repeat_required_after_resume = True
    await connection._technical_retry()
    await asyncio.wait_for(_wait_for(lambda: len(kernel.turn_calls) == 2), timeout=1)

    assert kernel.opening_calls == 1
    assert connection._repeat_required_after_resume is False
    assert kernel.turn_calls == [
        ("voice-original", "你现在身边有人吗？"),
        ("voice-original", "你现在身边有人吗？"),
    ]
    await connection._cleanup()


async def test_worker_speaks_first_then_turn_retry_never_restarts_opening() -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    kernel = RetriableTurnKernel()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="worker-first-retry",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=60,
    )

    await connection._start_session()
    await connection._speech_started(100)
    payload = _PendingGeneration(
        text="你好，我在听。",
        client_turn_id="worker-first",
        worker_pcm=b"worker-audio",
        metrics=None,
    )

    await connection._run_generation(payload)
    await connection._technical_retry()
    await asyncio.wait_for(_wait_for(lambda: len(kernel.turn_calls) == 2), timeout=1)

    assert kernel.opening_calls == 0
    assert {client_turn_id for client_turn_id, _ in kernel.turn_calls} == {"worker-first"}
    await connection._cleanup()


async def test_non_retryable_pause_ends_session_and_rejects_manual_retry() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = RetriableOpeningKernel()
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="non-retryable-opening",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0,
    )
    connection.opening_client_turn_id = "opening-stays-failed"

    await connection._technical_pause(RuntimePhase.acting, can_retry=False)
    await connection._technical_retry()
    await asyncio.sleep(0)

    assert kernel.opening_calls == 0
    assert connection.phase is RuntimePhase.ended
    assert kernel.ended_with is EndReason.technical_interruption
    assert socket.closed is True
    await connection._cleanup()


async def test_technical_pause_rejects_text_turn_until_resumed() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.text)
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="paused-text-turn",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.technical_paused,
        technical_retry_allowed=True,
    )
    event = {
        "text": "你还在吗？",
        "client_turn_id": "paused-turn",
    }

    await connection._text_turn(event)

    assert kernel.turn_calls == []
    assert socket.json_messages[-1] == {
        "type": "phase",
        "phase": RuntimePhase.technical_paused.value,
    }

    await connection._technical_retry()
    await connection._text_turn(event)
    await asyncio.wait_for(_wait_for(lambda: bool(kernel.turn_calls)), timeout=1)

    assert kernel.turn_calls == [("paused-turn", "你还在吗？")]
    await connection._cleanup()


async def test_reconnected_non_retryable_pause_cannot_restart_opening() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.text,
        has_transcript=False,
        opening_delay_seconds=0,
    )
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="reconnected-non-retryable-opening",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.technical_paused,
        opening_delay_seconds=0,
        technical_retry_allowed=False,
    )

    await connection._start_session()
    await connection._technical_retry()
    await asyncio.sleep(0)

    assert kernel.opening_calls == 0
    assert connection.phase is RuntimePhase.technical_paused
    await connection._cleanup()


async def test_reconnected_retryable_pause_without_payload_requires_worker_to_repeat() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.text,
        has_transcript=False,
        opening_delay_seconds=0,
    )
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="reconnected-retryable-opening",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.technical_paused,
        opening_delay_seconds=0,
        technical_retry_allowed=True,
    )

    await connection._technical_retry()
    repeat = next(
        (item for item in socket.json_messages if item["type"] == "input.error"),
        None,
    )

    assert repeat is not None
    assert repeat["message"] == "刚才那句话没有完整送达，请重新说一遍"
    assert kernel.opening_calls == 0
    assert connection.opening_task is None
    assert connection.technical_retry_allowed is False
    await connection._cleanup()


async def test_reconnect_during_generation_requires_worker_to_repeat_after_start() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="reconnect-inflight",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.acting,
    )

    await connection._start_session()

    repeat = next(
        (item for item in socket.json_messages if item["type"] == "input.error"),
        None,
    )
    assert repeat is not None
    assert repeat["message"] == "刚才那句话没有完整送达，请重新说一遍"
    await connection._cleanup()


async def test_partial_opening_audio_failure_cannot_be_retried() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = RetriableOpeningKernel(partial_audio=True)
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="partial-opening",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0,
    )

    await connection._start_session()
    await asyncio.wait_for(_wait_for(lambda: socket.closed), timeout=1)

    pause = next(item for item in socket.json_messages if item["type"] == "technical.pause")
    assert pause["can_retry"] is False
    assert kernel.ended_with is EndReason.technical_interruption
    assert connection.opening_client_turn_id is None
    await connection._cleanup()


async def test_cleanup_finishes_pending_natural_closure() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="cleanup-pending-close",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.playing,
    )
    connection._pending_natural_close = True

    await connection._cleanup()

    assert kernel.ended_with is EndReason.natural_closure


def test_second_runtime_failure_enters_contextual_technical_pause(
    client: TestClient,
) -> None:
    kernel = FakeKernel(media=Media.text, fail=True)
    override_live_dependencies(kernel)

    with client.websocket_connect("/api/live-sessions/session-fail") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"type": "text.turn", "text": "能听见吗？", "client_turn_id": "failed-1"}
        )
        paused = receive_until(websocket, "technical.pause")
        websocket.send_json({"type": "session.end"})
        receive_until(websocket, "session.ended")

    assert paused["message"] == "来访者的信号不太稳定"
    assert kernel.ended_with is EndReason.technical_interruption


def test_missing_api_key_is_explained_without_exposing_system_detail(
    client: TestClient,
) -> None:
    from app.api.routes.live_sessions import (
        get_assessment_kernel,
        get_live_credential_store,
    )

    kernel = FakeKernel(media=Media.text)
    app.dependency_overrides[get_assessment_kernel] = lambda: kernel
    app.dependency_overrides[get_live_credential_store] = RuntimeCredentialStore

    with client.websocket_connect("/api/live-sessions/session-no-key") as websocket:
        paused = websocket.receive_json()

    assert paused == {
        "type": "technical.pause",
        "phase": "technical_paused",
        "message": "请先在设置页配置阿里云百炼 API Key",
        "can_retry": False,
    }


def test_technical_interruption_is_a_persistable_end_reason() -> None:
    assert EndReason.technical_interruption.value == "technical_interruption"


async def test_late_asr_final_submits_a_boundary_that_is_already_complete() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    speech = FakeLiveSpeechProvider()
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="late-asr",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection.boundary.speech_started(at_ms=0)
    connection.boundary.speech_stopped(at_ms=900)
    connection.boundary.manual_complete(at_ms=1000)
    connection._install_asr(speech.stream)

    await connection._handle_audio(b"\x00\x00" * 320)
    await asyncio.wait_for(_wait_for(lambda: bool(kernel.turn_calls)), timeout=1)
    await connection._cleanup()

    assert kernel.turn_calls[0][1] == "你现在是一个人吗？"


async def test_repeated_session_start_does_not_open_duplicate_asr_or_opening() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    speech = FakeLiveSpeechProvider()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="repeat-start",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )

    await connection._start_session()
    first_opening = connection.opening_task
    await connection._start_session()
    await connection._cleanup()

    assert speech.calls == 0
    assert connection.opening_task is first_opening


async def test_cold_voice_opening_defers_asr_until_first_worker_pcm() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.voice,
        has_transcript=False,
        opening_delay_seconds=0,
    )
    speech = FakeLiveSpeechProvider()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="cold-opening-lazy-asr",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0,
    )

    await connection._start_session()
    opening_task = connection.opening_task
    assert opening_task is not None
    await opening_task

    assert connection.phase is RuntimePhase.playing
    assert speech.calls == 0
    assert connection.asr_stream is None

    await connection._playback_ended()
    assert speech.calls == 0

    await connection._handle_audio(b"first-worker-pcm")

    assert speech.calls == 1
    assert connection.asr_stream is speech.stream
    assert speech.stream.sent_audio == [b"first-worker-pcm"]
    await connection._cleanup()


async def test_automatic_opening_retires_asr_started_during_wait() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.voice,
        has_transcript=False,
        opening_delay_seconds=0.02,
    )
    waiting_stream = QueueASRStream()
    speech = SequenceSpeechProvider([waiting_stream])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="opening-retires-waiting-asr",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0.02,
    )

    await connection._start_session()
    await connection._handle_audio(b"waiting-room-pcm")
    opening_task = connection.opening_task
    assert opening_task is not None
    assert speech.calls == 1
    assert connection.asr_stream is waiting_stream

    await opening_task

    assert kernel.opening_calls == 1
    assert waiting_stream.closed is True
    assert connection.asr_stream is None
    assert connection.asr_task is None
    assert connection.phase is RuntimePhase.playing
    await connection._cleanup()


async def test_manual_worker_speaking_during_wait_cancels_unheard_opening() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.voice,
        has_transcript=False,
        opening_delay_seconds=60,
    )
    kernel.manual_turn_completion = True
    stream = QueueASRStream()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="manual-worker-first",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=SequenceSpeechProvider([stream]),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=60,
    )

    await connection._start_session()
    opening_task = connection.opening_task
    assert opening_task is not None
    await connection._handle_audio(b"worker-first-pcm")

    await connection._speech_started(100)

    assert opening_task.cancelled() is True
    assert connection.opening_task is None
    assert connection.opening_client_turn_id is None
    assert connection._opening_pending is False
    assert connection.asr_stream is stream
    assert connection.worker_pcm == b"worker-first-pcm"
    assert kernel.opening_calls == 0
    await connection._cleanup()


async def test_cancelled_opening_still_finishes_asr_retirement() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.voice,
        has_transcript=False,
        opening_delay_seconds=0,
    )
    kernel.manual_turn_completion = True
    stream = BlockingCloseASRStream()
    resumed_stream = QueueASRStream()
    speech = SequenceSpeechProvider([resumed_stream])
    recorder = FakeFailureRecorder()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="cancel-safe-opening-retirement",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )
    connection._install_asr(stream)
    receive_task = connection.asr_task
    assert receive_task is not None
    connection.opening_client_turn_id = "opening-cancel-safe"
    connection.opening_task = asyncio.create_task(
        connection._opening_after_delay(wait_for_delay=False)
    )
    opening_task = connection.opening_task
    await asyncio.wait_for(stream.close_started.wait(), timeout=1)

    speech_started_task = asyncio.create_task(connection._speech_started(100))
    await asyncio.sleep(0)
    stream.release_close.set()
    await asyncio.wait_for(speech_started_task, timeout=1)
    asr_before_resume = connection.asr_stream
    asr_task_before_resume = connection.asr_task
    await connection._handle_audio(b"worker-pcm-after-cancel")

    stream_was_closed = stream.closed
    close_was_cancelled = stream.close_cancelled
    receive_was_done = receive_task.done()
    if not receive_was_done:
        await connection._cancel_task(receive_task)
    if not stream_was_closed:
        await stream.close()

    assert opening_task.cancelled() is True
    assert close_was_cancelled is False
    assert stream_was_closed is True
    assert receive_was_done is True
    assert asr_before_resume is None
    assert asr_task_before_resume is None
    assert not any(item.failure_code == "asr.receive" for item in recorder.failures)
    assert kernel.opening_calls == 0
    assert speech.calls == 1
    assert connection.asr_stream is resumed_stream
    assert connection.worker_pcm == b"worker-pcm-after-cancel"
    await connection._cleanup()


async def test_audio_during_opening_retirement_cannot_reopen_asr() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.voice,
        has_transcript=False,
        opening_delay_seconds=0,
    )
    kernel.manual_turn_completion = True
    old_stream = BlockingCloseASRStream()
    reopened_stream = QueueASRStream()
    speech = SequenceSpeechProvider([reopened_stream])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="opening-retirement-blocks-reopen",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        opening_delay_seconds=0,
    )
    connection._install_asr(old_stream)
    connection.opening_client_turn_id = "opening-block-reopen"
    connection.opening_task = asyncio.create_task(
        connection._opening_after_delay(wait_for_delay=False)
    )
    opening_task = connection.opening_task
    await asyncio.wait_for(old_stream.close_started.wait(), timeout=1)

    await connection._handle_audio(b"pcm-during-retirement")

    assert speech.calls == 0
    assert connection.asr_stream is None
    assert connection.worker_pcm == b""

    old_stream.release_close.set()
    await asyncio.wait_for(opening_task, timeout=1)

    assert connection.phase is RuntimePhase.playing
    assert connection.asr_stream is None
    assert speech.calls == 0
    await connection._cleanup()


def test_browser_event_timestamp_is_mapped_to_server_monotonic_clock(
    monkeypatch: object,
) -> None:
    from app.api.routes import live_sessions

    monkeypatch.setattr(live_sessions._LiveConnection, "_now_ms", staticmethod(lambda: 9000))  # type: ignore[attr-defined]
    connection = live_sessions._LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="clock-origin",
        kernel=FakeKernel(media=Media.text),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
    )

    assert connection._event_ms(120) == 9000


@pytest.mark.parametrize(
    ("value", "expected"),
    [(450, 450), (10_000, 1000), (-50, 0), (True, 0), ("450", 0)],
)
def test_confirmed_silence_protocol_has_a_small_trusted_range(
    value: object,
    expected: int,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection

    assert _LiveConnection._confirmed_silence_ms(value) == expected


async def test_vad_stop_protocol_counts_confirmed_silence_from_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import live_sessions

    monkeypatch.setattr(
        live_sessions._LiveConnection,
        "_now_ms",
        staticmethod(lambda: 10_000),
    )
    connection = live_sessions._LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="vad-confirmed-silence",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection.boundary.speech_started(at_ms=9000)

    await connection._handle_control(
        {
            "type": "vad.speech_stopped",
            "at_ms": 820,
            "confirmed_silence_ms": 450,
        }
    )

    assert connection.boundary.speech_duration_ms == 550
    assert connection.boundary.advance(at_ms=12_349) is BoundaryState.candidate_pause
    assert connection.boundary.advance(at_ms=12_350) is BoundaryState.complete
    await connection._cleanup()


async def test_character_manual_complete_submits_asr_without_vad_event() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-manual-without-vad",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection.asr_sentences[(1, 1)] = asr_sentence("我已经说完了")

    await connection._handle_control(
        {"type": "turn.manual_complete", "at_ms": 1000}
    )
    await asyncio.wait_for(_wait_for(lambda: bool(kernel.turn_calls)), timeout=1)

    assert kernel.turn_calls[0][1] == "我已经说完了"
    assert not any(item["type"] == "input.error" for item in socket.json_messages)
    await connection._cleanup()


async def test_character_manual_complete_drains_asr_tail_before_snapshot() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    old_stream = FinalizingASRStream(asr_sentence("，还有最后半句", sentence_id=2))
    rotated_stream = QueueASRStream()
    speech = SequenceSpeechProvider([rotated_stream])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="character-manual-asr-tail",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection._install_asr(old_stream)
    await old_stream.queue.put(asr_sentence("前半句", sentence_id=1))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "前半句"),
        timeout=1,
    )

    await connection._handle_control(
        {"type": "turn.manual_complete", "at_ms": 1000}
    )
    await asyncio.wait_for(_wait_for(lambda: bool(kernel.turn_calls)), timeout=1)
    await asyncio.wait_for(
        _wait_for(lambda: connection.generation_task is None), timeout=1
    )

    assert old_stream.finished is True
    assert old_stream.closed is True
    assert speech.calls == 0
    assert kernel.turn_calls[0][1] == "前半句，还有最后半句"
    assert connection._assembled_transcript() == ""
    assert connection.phase is RuntimePhase.playing
    assert connection.asr_stream is None
    assert connection.asr_task is None

    await connection._playback_ended()
    await connection._handle_audio(b"next-turn-pcm")

    assert speech.calls == 1
    assert connection.asr_stream is rotated_stream
    await connection._cleanup()


async def test_manual_drain_finishes_reconnected_stream_before_snapshot() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    interrupted_stream = FinishWakesFailingReceiveASRStream()
    reconnected_stream = FinalizingASRStream(
        asr_sentence("，重连后的尾句", sentence_id=2)
    )
    speech = SequenceSpeechProvider([reconnected_stream])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="manual-drain-reconnected-stream",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection.asr_sentences[(1, 1)] = asr_sentence("前半句", sentence_id=1)
    connection._install_asr(interrupted_stream)

    await connection._manual_complete(1000)
    await asyncio.wait_for(_wait_for(lambda: bool(kernel.turn_calls)), timeout=1)

    assert speech.calls == 1
    assert reconnected_stream.finished is True
    assert reconnected_stream.closed is True
    assert kernel.turn_calls[0][1] == "前半句，重连后的尾句"
    assert connection.asr_stream is None
    assert connection.asr_task is None
    await connection._cleanup()


async def test_character_redo_input_replaces_active_asr_without_losing_raw_audio() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    old_stream = QueueASRStream()
    fresh_stream = QueueASRStream()
    speech = SequenceSpeechProvider([old_stream, fresh_stream])
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-redo-input",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    await connection._start_session()
    await connection._handle_audio(b"old-pcm")
    await old_stream.queue.put(asr_sentence("关系的，没有没有关系的。"))
    await asyncio.wait_for(
        _wait_for(
            lambda: connection._assembled_transcript()
            == "关系的，没有没有关系的。"
        ),
        timeout=1,
    )

    await connection._handle_control({"type": "turn.redo_input"})

    assert old_stream.closed is True
    assert speech.calls == 2
    assert connection._assembled_transcript() == ""
    assert bytes(connection.worker_pcm) == b"old-pcm"
    assert socket.json_messages[-1] == {
        "type": "input.reset",
        "message": "已清空，请重新说这一句",
    }

    await connection._handle_audio(b"fresh-pcm")
    await fresh_stream.queue.put(asr_sentence("没关系的。"))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "没关系的。"),
        timeout=1,
    )
    metric_items = connection._speech_metrics().asr_sentences
    assert [item["text"] for item in metric_items] == [
        "关系的，没有没有关系的。",
        "没关系的。",
    ]
    assert [item["discarded_by_worker"] for item in metric_items] == [True, False]
    assert [
        (item["pcm_start_byte"], item["pcm_end_byte"])
        for item in metric_items
    ] == [(0, len(b"old-pcm")), (len(b"old-pcm"), len(b"old-pcmfresh-pcm"))]

    await connection._submit_voice_turn()
    await asyncio.wait_for(_wait_for(lambda: bool(kernel.turn_calls)), timeout=1)

    assert kernel.turn_calls[0][1] == "没关系的。"
    assert kernel.turn_worker_pcm == [b"old-pcmfresh-pcm"]
    await connection._cleanup()


async def test_character_redo_without_active_asr_defers_open_until_next_pcm() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    speech = FakeLiveSpeechProvider()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="character-redo-lazy-asr",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )

    await connection._redo_voice_input()

    assert speech.calls == 0
    assert connection.asr_stream is None
    assert connection.asr_task is None

    await connection._handle_audio(b"first-pcm-after-redo")

    assert speech.calls == 1
    assert speech.stream.sent_audio == [b"first-pcm-after-redo"]
    await connection._cleanup()


async def test_character_multiple_redos_keep_auditable_pcm_ranges_contiguous() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    first_stream = QueueASRStream()
    second_stream = QueueASRStream()
    final_stream = QueueASRStream()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="character-multiple-redos",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=SequenceSpeechProvider(
            [first_stream, second_stream, final_stream]
        ),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    await connection._start_session()
    await connection._handle_audio(b"a")
    await first_stream.queue.put(asr_sentence("第一次"))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "第一次"), timeout=1
    )
    await connection._redo_voice_input()

    await connection._handle_audio(b"b")
    await second_stream.queue.put(asr_sentence("第二次"))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "第二次"), timeout=1
    )
    await connection._redo_voice_input()

    await connection._handle_audio(b"c")
    await final_stream.queue.put(asr_sentence("最终句"))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "最终句"), timeout=1
    )

    metrics = connection._speech_metrics().asr_sentences
    assert [item["text"] for item in metrics] == ["第一次", "第二次", "最终句"]
    assert [item["discarded_by_worker"] for item in metrics] == [True, True, False]
    assert [
        (item["pcm_start_byte"], item["pcm_end_byte"])
        for item in metrics
    ] == [(0, 1), (1, 2), (2, 3)]
    await connection._cleanup()


async def test_character_redo_input_rejects_invalid_state_with_input_error() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-redo-invalid-state",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )

    await connection._handle_control({"type": "turn.redo_input"})

    assert socket.json_messages[-1]["type"] == "input.error"
    assert isinstance(socket.json_messages[-1]["message"], str)
    await connection._cleanup()


class RotateFailureThenRecoverySpeechProvider:
    def __init__(self, old_stream: QueueASRStream, fresh_stream: QueueASRStream) -> None:
        self.old_stream = old_stream
        self.fresh_stream = fresh_stream
        self.calls = 0

    async def open_asr(self) -> QueueASRStream:
        self.calls += 1
        if self.calls == 1:
            return self.old_stream
        if self.calls in {2, 3}:
            raise RuntimeSpeechError("ASR 轮换失败")
        return self.fresh_stream


async def test_character_redo_rotate_failure_discards_old_input_before_retry() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    old_stream = QueueASRStream()
    fresh_stream = QueueASRStream()
    speech = RotateFailureThenRecoverySpeechProvider(old_stream, fresh_stream)
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-redo-rotate-failure",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,  # type: ignore[arg-type]
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    await connection._start_session()
    await connection._handle_audio(b"old-pcm")
    await old_stream.queue.put(asr_sentence("旧句"))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "旧句"), timeout=1
    )

    await connection._handle_control({"type": "turn.redo_input"})

    assert connection.phase is RuntimePhase.technical_paused
    assert connection._assembled_transcript() == ""
    assert connection._speech_metrics().asr_sentences[0]["discarded_by_worker"] is True
    assert connection._repeat_required_after_resume is True

    await connection._technical_retry()

    assert connection.phase is RuntimePhase.listening
    assert connection._assembled_transcript() == ""
    assert any(
        item == {
            "type": "input.error",
            "message": "刚才那句话没有完整送达，请重新说一遍",
        }
        for item in socket.json_messages
    )
    await connection._cleanup()


async def test_character_redo_input_never_revives_discarded_text_when_asr_reopen_fails() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    old_stream = QueueASRStream()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="character-redo-open-failure",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FailAfterFirstOpenSpeechProvider(old_stream),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    await connection._start_session()
    await connection._handle_audio(b"old-pcm")
    await old_stream.queue.put(asr_sentence("这句识别错了"))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "这句识别错了"),
        timeout=1,
    )

    await connection._handle_control({"type": "turn.redo_input"})

    assert connection._assembled_transcript() == ""
    assert connection._discarded_asr_sentences[0]["text"] == "这句识别错了"
    assert connection.phase is RuntimePhase.technical_paused
    await connection._cleanup()


async def test_character_redo_asr_reopen_timeout_enters_existing_technical_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.routes.live_sessions as live_sessions

    monkeypatch.setattr(live_sessions, "REDO_ASR_RESET_SECONDS", 0.01)
    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    old_stream = QueueASRStream()
    socket = FakeSocket()
    recorder = FakeFailureRecorder()
    connection = live_sessions._LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-redo-open-timeout",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=BlockingAfterFirstOpenSpeechProvider(old_stream),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )
    await connection._start_session()
    await connection._handle_audio(b"old-pcm")
    await old_stream.queue.put(asr_sentence("这句识别错了"))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "这句识别错了"),
        timeout=1,
    )

    await asyncio.wait_for(connection._redo_voice_input(), timeout=0.2)

    assert connection.phase is RuntimePhase.technical_paused
    assert connection._assembled_transcript() == ""
    assert connection._repeat_required_after_resume is True
    assert recorder.failures[-1].failure_code == "asr.redo_input_timeout"
    assert socket.json_messages[-1]["type"] == "technical.pause"
    await connection._cleanup()


async def test_character_redo_input_rejection_returns_a_frontend_release_event() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-redo-rejected",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.acting,
    )

    await connection._handle_control({"type": "turn.redo_input"})

    assert socket.json_messages[-1] == {
        "type": "input.error",
        "message": "当前不能重新录入，请等来访者回应后再试",
    }
    await connection._cleanup()


async def test_character_manual_complete_waits_for_a_slightly_late_asr_tail() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    old_stream = DelayedFinalizingASRStream(
        asr_sentence("，还有最后半句", sentence_id=2),
        delay_seconds=0.45,
    )
    rotated_stream = QueueASRStream()
    speech = SequenceSpeechProvider([rotated_stream])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="character-manual-late-asr-tail",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection._install_asr(old_stream)
    await old_stream.queue.put(asr_sentence("前半句", sentence_id=1))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "前半句"),
        timeout=1,
    )

    await connection._handle_control(
        {"type": "turn.manual_complete", "at_ms": 1000}
    )
    await asyncio.wait_for(_wait_for(lambda: bool(kernel.turn_calls)), timeout=2)

    assert old_stream.finished is True
    assert old_stream.closed is True
    assert speech.calls == 0
    assert kernel.turn_calls[0][1] == "前半句，还有最后半句"
    assert connection.asr_stream is None
    assert connection.asr_task is None
    await connection._cleanup()


async def test_character_vad_stop_only_updates_metrics_without_auto_submission() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="character-vad-metrics-only",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection.asr_sentences[(1, 1)] = asr_sentence("这句话还不能自动提交")

    await connection._speech_started(100)
    await connection._speech_stopped(900, confirmed_silence_ms=100)
    await asyncio.sleep(0)

    assert connection.boundary.speech_duration_ms == 700
    assert connection.boundary_task is None
    assert kernel.turn_calls == []
    await connection._cleanup()


async def test_character_vad_start_cancels_unheard_pending_opening() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="character-vad-opening",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection.opening_task = asyncio.create_task(asyncio.Event().wait())
    opening_task = connection.opening_task

    await connection._speech_started(100)

    assert connection.opening_task is None
    assert opening_task.cancelled() is True
    assert connection.phase is RuntimePhase.listening
    await connection._cleanup()


async def test_character_vad_start_does_not_cancel_generation_in_progress() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="character-vad-generation",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.acting,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection.generation_task = asyncio.create_task(asyncio.Event().wait())
    generation_task = connection.generation_task

    await connection._speech_started(100)

    assert connection.generation_task is generation_task
    assert generation_task.done() is False
    assert connection.phase is RuntimePhase.acting
    await connection._cleanup()


async def test_empty_manual_complete_returns_input_error_instead_of_silent_drop() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-empty-manual",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)

    await connection._handle_control(
        {"type": "turn.manual_complete", "at_ms": 1000}
    )

    assert socket.json_messages[-1] == {
        "type": "input.error",
        "message": "还没有听清这句话，请再说一遍后提交",
    }
    assert kernel.turn_calls == []
    await connection._cleanup()


async def test_workflow_vad_stop_still_starts_automatic_boundary_wait() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="workflow-auto-boundary",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    await connection._speech_started(100)
    await connection._speech_stopped(900)

    assert connection.boundary_task is not None
    await connection._cleanup()


async def test_worker_audio_during_playback_is_ignored_until_playback_ended() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(media=Media.voice)
    speech = FakeLiveSpeechProvider()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="during-playback",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.playing,
    )
    connection.boundary = TurnBoundary(listening_started_ms=0)
    connection.boundary.speech_started(at_ms=0)
    connection.boundary.speech_stopped(at_ms=900)
    connection.boundary.manual_complete(at_ms=1000)
    connection._install_asr(speech.stream)

    await connection._handle_audio(b"\x00\x00" * 320)
    await asyncio.sleep(0.05)
    calls_during_playback = list(kernel.turn_calls)
    if calls_during_playback:
        await connection._cleanup()
    assert calls_during_playback == []
    assert connection.worker_pcm == b""
    assert speech.stream.sent_audio == []

    await connection._playback_ended()
    await connection._handle_audio(b"\x00\x00" * 320)
    await asyncio.wait_for(_wait_for(lambda: bool(kernel.turn_calls)), timeout=1)
    await connection._cleanup()


async def test_technical_pause_ignores_new_vad_and_pcm() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    speech = FakeLiveSpeechProvider()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="paused-input",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.technical_paused,
    )

    await connection._speech_started(100)
    await connection._handle_audio(b"new-audio")

    assert connection.boundary is None
    assert connection.worker_pcm == b""
    assert speech.calls == 0


async def test_asr_send_reconnect_starts_exactly_one_new_receive_loop() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    old_stream = QueueASRStream()
    new_stream = QueueASRStream()
    speech = SequenceSpeechProvider([old_stream, new_stream])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="asr-handoff",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    await connection._start_session()
    await connection._handle_audio(b"warmup")
    await asyncio.wait_for(_wait_for(lambda: old_stream.receive_calls == 1), timeout=1)
    old_stream.fail_send = True

    await connection._handle_audio(b"reconnect")
    await asyncio.sleep(0.05)
    new_receive_calls = new_stream.receive_calls
    await connection._cleanup()

    assert old_stream.closed is True
    assert new_receive_calls == 1


async def test_asr_reconnect_keeps_same_sentence_ids_in_order() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    first = QueueASRStream()
    second = QueueASRStream()
    speech = SequenceSpeechProvider([first, second])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="asr-id-reset",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    await connection._start_session()
    await connection._handle_audio(b"pcm")
    await first.queue.put(asr_sentence("第一句"))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "第一句"),
        timeout=1,
    )

    assert await connection._reconnect_asr() is True
    await second.queue.put(asr_sentence("第二句"))
    await asyncio.sleep(0.05)
    transcript = connection._assembled_transcript()
    await connection._cleanup()
    assert transcript == "第一句第二句"


async def test_committed_turn_ignores_late_update_for_consumed_asr_sentence() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    stream = QueueASRStream()
    rotated_stream = QueueASRStream()
    speech = SequenceSpeechProvider([stream, rotated_stream])
    kernel = FakeKernel(media=Media.voice)
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="asr-late-final",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    await connection._start_session()
    await connection._handle_audio(b"pcm")
    assert connection.boundary is not None
    connection.boundary.speech_started(at_ms=0)
    connection.boundary.speech_stopped(at_ms=800)
    connection.boundary.manual_complete(at_ms=900)
    await stream.queue.put(asr_sentence("我说完了", sentence_id=1))
    await asyncio.wait_for(_wait_for(lambda: bool(kernel.turn_calls)), timeout=1)
    await asyncio.wait_for(
        _wait_for(lambda: connection.generation_task is None), timeout=1
    )
    await stream.queue.put(asr_sentence("迟到的同句更新", sentence_id=1))
    await rotated_stream.queue.put(asr_sentence("迟到的同句更新", sentence_id=1))
    await asyncio.sleep(0.05)

    transcript = connection._assembled_transcript()
    open_calls = speech.calls
    await connection._cleanup()

    assert open_calls == 1
    assert transcript == ""


async def test_manual_worker_turns_open_one_asr_stream_per_listening_segment() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    first = CompletingASRStream()
    second = CompletingASRStream()
    speech = SequenceSpeechProvider([first, second])
    kernel = FakeKernel(media=Media.voice)
    kernel.manual_turn_completion = True
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="asr-two-turns",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    await connection._start_session()
    await connection._handle_audio(b"first-turn-pcm")
    first_asr_task = connection.asr_task
    assert first_asr_task is not None

    await first.queue.put(asr_sentence("第一轮", sentence_id=1))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "第一轮"),
        timeout=1,
    )
    await connection._manual_complete(900)
    await asyncio.wait_for(_wait_for(lambda: len(kernel.turn_calls) == 1), timeout=1)
    await asyncio.wait_for(
        _wait_for(lambda: connection.generation_task is None), timeout=1
    )
    assert first.closed is True
    assert connection.asr_stream is None
    assert connection.asr_task is None
    assert connection.phase is RuntimePhase.playing
    await connection._playback_ended()

    await connection._handle_audio(b"second-turn-pcm")
    second_asr_task = connection.asr_task
    assert second_asr_task is not None
    await second.queue.put(asr_sentence("第二轮", sentence_id=2))
    await asyncio.wait_for(
        _wait_for(lambda: connection._assembled_transcript() == "第二轮"),
        timeout=1,
    )
    await connection._manual_complete(1900)
    await asyncio.wait_for(_wait_for(lambda: len(kernel.turn_calls) == 2), timeout=1)
    await asyncio.wait_for(
        _wait_for(lambda: connection.generation_task is None), timeout=1
    )

    texts = [text for _, text in kernel.turn_calls]
    open_calls = speech.calls
    second_stream_retired = connection.asr_stream is None
    await connection._cleanup()

    assert open_calls == 2
    assert second_asr_task is not first_asr_task
    assert first.closed is True
    assert second.closed is True
    assert second_stream_retired is True
    assert texts == ["第一轮", "第二轮"]


async def test_partial_tts_failure_ends_as_technical_interruption_without_retry() -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    kernel = PartialAudioFailureKernel(media=Media.voice)
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="partial-tts",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )

    await connection._run_generation(
        _PendingGeneration(
            text="你还在吗？",
            client_turn_id="partial-tts-turn",
            worker_pcm=b"",
            metrics=None,
        )
    )

    pause = next(item for item in socket.json_messages if item["type"] == "technical.pause")
    assert pause["can_retry"] is False
    assert connection.retry_payload is None
    assert kernel.ended_with is EndReason.technical_interruption
    assert socket.closed is True


async def test_workflow_tts_failure_does_not_publish_uncommitted_actor_text() -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="tts-text-preserved",
        kernel=TextOnlyTTSFailureKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    payload = _PendingGeneration(
        text="请保留我这一轮。",
        client_turn_id="tts-text-preserved-turn",
        worker_pcm=b"",
        metrics=None,
    )

    await connection._run_generation(payload)

    pause = next(
        item for item in socket.json_messages if item["type"] == "technical.pause"
    )
    assert not any(item["type"] == "visitor.text" for item in socket.json_messages)
    assert pause["message"] == "来访者的信号不太稳定"
    assert pause["can_retry"] is True
    assert connection.retry_payload is not None
    assert connection.retry_payload.text == "请保留我这一轮。"
    assert socket.closed is False


async def test_client_audio_failure_is_recorded_and_enters_contextual_pause() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="client-audio-failure",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        diagnostic_simulation=True,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )

    await connection._handle_control(
        {
            "type": "client.failure",
            "stage": "capture",
            "code": "permission_denied",
        }
    )

    assert len(recorder.failures) == 1
    failure = recorder.failures[0]
    assert failure.component == "browser_audio"
    assert failure.operation == "capture"
    assert failure.failure_code == "client.capture.permission_denied"
    assert failure.disposition is FailureDisposition.technical_pause
    pause = socket.json_messages[-1]
    assert pause["message"] == "来访者的信号不太稳定"
    assert pause["failure_id"] == "failure-1"
    assert pause["failure_code"] == "client.capture.permission_denied"
    assert pause["failure"] == {
        "id": "failure-1",
        "failure_code": "client.capture.permission_denied",
        "session_id": "client-audio-failure",
        "client_turn_id": None,
        "component": "browser_audio",
        "phase": "listening",
        "operation": "capture",
        "error_class": "RuntimeError",
        "attempt_count": 1,
        "retryable": True,
        "disposition": "technical_pause",
        "provider_status_code": None,
        "provider_request_id": None,
        "attempts_json": [
            {
                "index": 1,
                "error_class": "RuntimeError",
                "message": "浏览器上报capture故障：permission_denied",
                "call_kind": None,
                "provider_status_code": None,
                "provider_request_id": None,
                "details": {"exception_chain": [
                    {
                        "error_class": "RuntimeError",
                        "message": "浏览器上报capture故障：permission_denied",
                        "status_code": None,
                        "request_id": None,
                    }
                ]},
            }
        ],
        "details_json": {
            "stage": "capture",
            "client_code": "permission_denied",
        },
    }
    await connection._cleanup()


async def test_formal_connection_omits_internal_failure_diagnostics() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="formal-client-audio-failure",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )

    await connection._handle_control(
        {
            "type": "client.failure",
            "stage": "playback",
            "code": "playback_failed",
        }
    )

    pause = socket.json_messages[-1]
    failure = pause["failure"]
    assert isinstance(failure, dict)
    assert failure["attempts_json"] == []
    assert failure["details_json"] == {}
    assert recorder.failures[0].details == {
        "stage": "playback",
        "client_code": "playback_failed",
    }
    await connection._cleanup()


async def test_invalid_json_and_unknown_events_are_recorded_without_ending_session() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    socket = ScriptedSocket(
        [
            {"type": "websocket.receive", "text": "{"},
            {"type": "websocket.receive", "text": '{"type":"not.supported"}'},
            {"type": "websocket.disconnect", "code": 1000},
        ]
    )
    kernel = FakeKernel(media=Media.text)
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="invalid-events",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )

    await connection.run()

    assert [item.failure_code for item in recorder.failures] == [
        "websocket.invalid_json",
        "websocket.unknown_event",
    ]
    assert all(
        item.disposition is FailureDisposition.recovered
        for item in recorder.failures
    )
    assert kernel.ended_with is None


async def test_invalid_client_failure_event_is_recorded_as_protocol_failure() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="invalid-client-failure",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )

    await connection._handle_control(
        {"type": "client.failure", "stage": "camera", "code": "offline"}
    )

    assert recorder.failures[0].failure_code == "websocket.invalid_event"
    assert recorder.failures[0].operation == "client.failure"
    assert connection.phase is RuntimePhase.listening
    await connection._cleanup()


async def test_second_text_turn_does_not_cancel_the_turn_already_in_progress() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    kernel = BlockingTurnKernel()
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="text-turn-in-progress",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )

    await connection._text_turn(
        {"type": "text.turn", "text": "第一句", "client_turn_id": "first"}
    )
    await asyncio.wait_for(kernel.started.wait(), timeout=1)
    await connection._text_turn(
        {"type": "text.turn", "text": "第二句", "client_turn_id": "second"}
    )

    assert kernel.cancelled is False
    assert kernel.turn_calls == [("first", "第一句")]
    assert recorder.failures[0].failure_code == "websocket.invalid_event"
    assert recorder.failures[0].operation == "text.turn_in_progress"
    assert socket.json_messages[-1] == {
        "type": "input.error",
        "message": "请等来访者回应后再继续",
    }

    kernel.release.set()
    assert connection.generation_task is not None
    await connection.generation_task
    await connection._cleanup()


async def test_turn_id_conflict_is_consumed_and_restores_listening() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    class TurnConflictKernel(FakeKernel):
        def __init__(self) -> None:
            super().__init__(media=Media.text)
            self.resume_calls = 0

        async def process_worker_turn(self, **kwargs: object) -> KernelTurnResult:
            del kwargs
            raise KernelTurnConflictError(
                "请求标识对应的工作者发言不一致"
            )

        def resume_listening(self, session_id: str) -> None:
            assert session_id == "live-turn-conflict"
            self.resume_calls += 1

    kernel = TurnConflictKernel()
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="live-turn-conflict",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
    )

    await connection._text_turn(
        {
            "type": "text.turn",
            "client_turn_id": "already-used",
            "text": "这次是不同的正文。",
        }
    )
    generation_task = connection.generation_task
    assert generation_task is not None
    await generation_task

    assert kernel.resume_calls == 1
    assert connection.phase is RuntimePhase.listening
    assert socket.json_messages == [
        {
            "type": "input.error",
            "message": "这次发言标识已被使用，请重新发送",
        },
        {"type": "phase", "phase": RuntimePhase.listening.value},
    ]
    assert connection.generation_task is None
    await connection._cleanup()


async def test_asr_open_failure_keeps_both_attempts_in_one_record() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    socket = FakeSocket()
    speech = FailingOpenSpeechProvider()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="asr-open-failure",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )

    assert await connection._open_asr() is False

    assert speech.calls == 2
    failure = recorder.failures[0]
    assert failure.failure_code == "asr.open"
    assert len(failure.attempts) == 2
    assert failure.disposition is FailureDisposition.technical_pause
    assert socket.json_messages[-1]["failure_id"] == "failure-1"
    await connection._cleanup()


async def test_technical_retry_returns_to_listening_without_preopening_asr() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = FakeKernel(
        media=Media.voice,
        has_transcript=False,
        opening_delay_seconds=0,
    )
    socket = FakeSocket()
    speech = FakeLiveSpeechProvider()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="asr-retry-stays-paused",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.technical_paused,
        opening_delay_seconds=0,
        technical_retry_allowed=True,
    )

    await connection._technical_retry()

    assert speech.calls == 0
    assert kernel.opening_calls == 0
    assert connection.phase is RuntimePhase.listening
    assert connection.asr_stream is None

    await connection._handle_audio(b"pcm-after-retry")

    assert speech.calls == 1
    assert speech.stream.sent_audio == [b"pcm-after-retry"]
    await connection._cleanup()


async def test_asr_send_failure_is_recorded_after_successful_reconnect() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    first = QueueASRStream(fail_send=True)
    second = QueueASRStream()
    speech = SequenceSpeechProvider([first, second])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="asr-send-recovered",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )
    await connection._start_session()

    await connection._handle_audio(b"pcm")

    send_failure = next(
        item for item in recorder.failures if item.failure_code == "asr.send_audio"
    )
    assert send_failure.disposition is FailureDisposition.recovered
    assert send_failure.retryable is True
    assert speech.calls == 2
    await connection._cleanup()


async def test_asr_receive_failure_is_recorded_after_successful_reconnect() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    first = FailingReceiveASRStream()
    second = QueueASRStream()
    speech = SequenceSpeechProvider([first, second])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="asr-receive-recovered",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )

    await connection._start_session()
    await connection._handle_audio(b"pcm")
    await asyncio.wait_for(
        _wait_for(
            lambda: any(
                item.failure_code == "asr.receive" for item in recorder.failures
            )
        ),
        timeout=1,
    )

    receive_failure = next(
        item for item in recorder.failures if item.failure_code == "asr.receive"
    )
    assert receive_failure.disposition is FailureDisposition.recovered
    assert speech.calls == 2
    await connection._cleanup()


async def test_retired_asr_receive_error_is_not_recorded_as_runtime_failure() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    stream = CloseWakesFailingReceiveASRStream()
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="asr-intentional-retirement",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=SequenceSpeechProvider([]),
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )
    connection._install_asr(stream)
    await asyncio.wait_for(stream.receive_started.wait(), timeout=1)

    await connection._retire_asr(operation="close_for_test")

    assert stream.closed is True
    assert connection.asr_stream is None
    assert connection.asr_task is None
    assert recorder.failures == []
    await connection._cleanup()


async def test_replaced_asr_ignores_receive_error_from_closed_old_stream() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    old_stream = CloseWakesFailingReceiveASRStream()
    new_stream = QueueASRStream()
    speech = SequenceSpeechProvider([new_stream])
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="asr-replacement-closes-old-stream",
        kernel=FakeKernel(media=Media.voice),  # type: ignore[arg-type]
        speech_provider=speech,
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )
    connection._install_asr(old_stream)
    generation = connection._asr_generation
    await asyncio.wait_for(old_stream.receive_started.wait(), timeout=1)

    replaced = await connection._replace_asr(
        expected_generation=generation,
        failure_reconnect=False,
    )

    assert replaced is True
    assert old_stream.closed is True
    assert connection.asr_stream is new_stream
    assert not any(item.failure_code == "asr.receive" for item in recorder.failures)
    await connection._cleanup()


async def test_only_abnormal_websocket_disconnect_is_recorded() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    abnormal_recorder = FakeFailureRecorder()
    abnormal = _LiveConnection(
        websocket=ScriptedSocket(
            [{"type": "websocket.disconnect", "code": 1006}]
        ),  # type: ignore[arg-type]
        session_id="abnormal-disconnect",
        kernel=FakeKernel(media=Media.text),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        failure_recorder=abnormal_recorder,  # type: ignore[arg-type]
    )
    await abnormal.run()

    normal_recorder = FakeFailureRecorder()
    normal = _LiveConnection(
        websocket=ScriptedSocket(
            [{"type": "websocket.disconnect", "code": 1000}]
        ),  # type: ignore[arg-type]
        session_id="normal-disconnect",
        kernel=FakeKernel(media=Media.text),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        failure_recorder=normal_recorder,  # type: ignore[arg-type]
    )
    await normal.run()

    assert abnormal_recorder.failures[0].failure_code == "websocket.abnormal_close"
    assert abnormal_recorder.failures[0].disposition is FailureDisposition.connection_close
    assert normal_recorder.failures == []


async def test_connection_loop_exception_is_recorded_before_propagating() -> None:
    from app.api.routes.live_sessions import _LiveConnection

    recorder = FakeFailureRecorder()
    connection = _LiveConnection(
        websocket=FailingReceiveSocket(),  # type: ignore[arg-type]
        session_id="connection-loop-failure",
        kernel=FakeKernel(media=Media.text),  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        failure_recorder=recorder,  # type: ignore[arg-type]
    )

    with pytest.raises(OSError, match="WebSocket 连接突然中断"):
        await connection.run()

    failure = recorder.failures[0]
    assert failure.failure_code == "websocket.connection_loop"
    assert failure.operation == "connection_loop"
    assert failure.disposition is FailureDisposition.connection_close


async def test_kernel_failure_summary_is_forwarded_and_non_retryable_pause_ends() -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    kernel = RecordedTechnicalPauseKernel(media=Media.text)
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="kernel-recorded-pause",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        diagnostic_simulation=True,
    )

    await connection._run_generation(
        _PendingGeneration(
            text="你现在在哪里？",
            client_turn_id="kernel-failed-turn",
            worker_pcm=b"",
            metrics=None,
        )
    )

    pause = next(item for item in socket.json_messages if item["type"] == "technical.pause")
    assert pause["failure_id"] == "failure-from-kernel"
    assert pause["failure_code"] == "actor.output_validation"
    failure = pause["failure"]
    assert isinstance(failure, dict)
    assert failure["id"] == "failure-from-kernel"
    assert failure["failure_code"] == "actor.output_validation"
    assert failure["component"] == "actor"
    assert failure["operation"] == "output_validation"
    assert failure["error_class"] == "ActorOutputValidationError"
    assert failure["attempt_count"] == 2
    assert failure["attempts_json"][0]["message"] == "回答中出现了未开放信息"
    assert failure["details_json"] == {"diagnostic": "safe"}
    assert kernel.ended_with is EndReason.technical_interruption
    assert connection.phase is RuntimePhase.ended
    assert socket.closed is True


async def test_character_contract_failure_keeps_current_turn_retryable() -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    kernel = RetryableRecordedTechnicalPauseKernel(media=Media.text)
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="retryable-character-pause",
        kernel=kernel,  # type: ignore[arg-type]
        speech_provider=FakeLiveSpeechProvider(),
        media=Media.text,
        initial_phase=RuntimePhase.listening,
        diagnostic_simulation=True,
    )
    payload = _PendingGeneration(
        text="我刚才说的是今晚先不要对质。",
        client_turn_id="retryable-character-turn",
        worker_pcm=b"",
        metrics=None,
    )

    await connection._run_generation(payload)

    pause = next(item for item in socket.json_messages if item["type"] == "technical.pause")
    assert pause["message"] == "来访者的信号不太稳定"
    assert pause["can_retry"] is True
    assert pause["failure_code"] == "actor.output_validation"
    assert connection.retry_payload == payload
    assert connection.phase is RuntimePhase.technical_paused
    assert kernel.ended_with is None
    assert socket.closed is False


def test_voice_simulation_enables_diagnostics_without_disabling_audio(
    client: TestClient,
) -> None:
    kernel = FakeKernel(media=Media.voice)
    speech = FakeLiveSpeechProvider()
    override_live_dependencies(kernel, speech)

    with client.websocket_connect(
        "/api/live-sessions/session-voice-diagnostic",
        headers={"X-Assessment-Simulation": "voice"},
    ) as websocket:
        websocket.receive_json()
        websocket.send_json({"type": "session.start"})
        receive_until(websocket, "phase")
        websocket.send_json(
            {
                "type": "text.turn",
                "text": "你现在在哪里？",
                "client_turn_id": "voice-diagnostic-turn",
            }
        )
        receive_until(websocket, "visitor.text")
        assert websocket.receive_bytes() == b"first-pcm"
        assert websocket.receive_bytes() == b"second-pcm"

    assert speech.calls == 0
    assert kernel.turn_synthesize_audio == [True]
    assert kernel.turn_capture_failure_payload == [True]


async def _wait_for(predicate: Callable[[], bool]) -> None:
    while not predicate():
        await asyncio.sleep(0.01)

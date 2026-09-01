from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from app.audio.models import SpeechMetricRecord
from app.runtime.character_provider import CharacterOutput
from app.runtime.kernel import RuntimePhase, SpeechMetricsInput, TechnicalPauseError
from app.runtime.models import RuntimeFailureRecord
from app.sessions.models import (
    CaseType,
    EndReason,
    Media,
    ModelMode,
    Scene,
    SessionMode,
    SessionRecord,
    SessionStatus,
    TurnRecord,
    TurnSpeaker,
)


class FakeCharacter:
    def __init__(
        self,
        *,
        spoken_text: str = "我最怕她进门以后什么都明白了。",
        end_session: bool = False,
        action_requests: Sequence[str] = ("none",),
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.spoken_text = spoken_text
        self.end_session = end_session
        self.action_requests = list(action_requests)

    async def respond(self, **kwargs: object):
        self.calls.append(kwargs)
        action_request = (
            self.action_requests.pop(0) if self.action_requests else "none"
        )
        return CharacterOutput(
            spoken_text=self.spoken_text,
            delivery_hint="声音偏低，语速稍慢，句间短暂停顿",
            end_session=self.end_session,
            action_request=action_request,
        )


class InvalidCharacter:
    def __init__(self) -> None:
        self.calls = 0

    async def respond(self, **kwargs: object):
        from app.runtime.character_provider import CharacterOutputValidationError

        del kwargs
        self.calls += 1
        raise CharacterOutputValidationError(
            "来访者对话模型返修后仍未返回可安全朗读的台词"
        )


class ExhaustedCharacter:
    def __init__(self) -> None:
        self.calls = 0

    async def respond(self, **kwargs: object):
        from app.runtime.character_provider import CharacterContextExhaustedError

        del kwargs
        self.calls += 1
        raise CharacterContextExhaustedError("完整原文已无法容纳本轮回复")


class FakeSpeech:
    tts_model_name = "fake-tts"

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.instructions: list[str] = []

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
    ) -> AsyncIterator[bytes]:
        self.texts.append(text)
        self.instructions.append(instruction)
        yield b"character-pcm"


def test_context_budget_closure_keeps_forced_end_signal() -> None:
    output = CharacterOutput(
        spoken_text="好，我知道了。",
        end_session=False,
        action_request="none",
    ).with_forced_context_closure()

    assert output.context_closure_forced is True
    assert output.end_session is True


class RecoveringSpeech(FakeSpeech):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
    ) -> AsyncIterator[bytes]:
        self.texts.append(text)
        self.instructions.append(instruction)
        if self.fail:
            raise RuntimeError("tts unavailable")
        yield b"recovered-character-pcm"


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


class NoopASRStream:
    async def close(self) -> None:
        return None


def _world_definition():
    from app.runtime.character_world import SupportWorldDefinition

    return SupportWorldDefinition(
        support_name="唐婷",
        arrival_after_seconds=780,
        not_contacted_reality="热线接通后还没有联系唐婷。",
        first_unanswered_reality="第一次联系唐婷没有接通。",
        coming_reality="唐婷已经答应赶来，但还没到门口。",
        at_door_reality="唐婷已经到门外，尚未进屋。",
        present_reality="唐婷已经进屋，沈雯不再独处。",
    )


class FakeCharacterRepository:
    def get(self, case_id: str) -> object:
        assert case_id == "crisis_student_main"
        return SimpleNamespace(world=_world_definition())


class FakeNoWorldCharacterRepository:
    def get(self, case_id: str) -> object:
        assert case_id == "boundary_referral_short"
        return SimpleNamespace(world=None)


def _kernel(
    test_engine: Engine,
    tmp_path: Path,
    character: FakeCharacter,
    speech: FakeSpeech,
    *,
    with_transcript: bool = True,
):
    from app.runtime.character_kernel import CharacterPromptKernel

    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="character-session",
                mode=SessionMode.assessment,
                scene=Scene.hotline,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.voice,
                model_mode=ModelMode.live,
                state_json={
                    "actor_state": {"legacy": "must-stay-untouched"},
                    "runtime": {
                        "engine": "character_prompt",
                        "phase": RuntimePhase.listening.value,
                    },
                },
            )
        )
        if with_transcript:
            db.add_all(
                [
                    TurnRecord(
                        id="opening-client",
                        session_id="character-session",
                        client_turn_id="opening-1",
                        sequence=1,
                        speaker=TurnSpeaker.client,
                        text="喂……你好。我妈明早就到了。",
                    ),
                    TurnRecord(
                        id="prior-worker",
                        session_id="character-session",
                        client_turn_id="pair-1",
                        sequence=2,
                        speaker=TurnSpeaker.worker,
                        text="你愿意告诉我发生了什么吗？",
                    ),
                    TurnRecord(
                        id="prior-client",
                        session_id="character-session",
                        client_turn_id="pair-1",
                        sequence=3,
                        speaker=TurnSpeaker.client,
                        text="我失业四十一天了，一直瞒着她。",
                    ),
                ]
            )
        db.commit()
    return CharacterPromptKernel(
        engine=test_engine,
        characters=FakeCharacterRepository(),
        character=character,
        speech=speech,
        audio_root=tmp_path,
    )


def _no_world_kernel(
    test_engine: Engine,
    tmp_path: Path,
    character: FakeCharacter,
    *,
    scene: Scene = Scene.online,
):
    from app.runtime.character_kernel import CharacterPromptKernel

    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="short-character-session",
                mode=SessionMode.assessment,
                scene=scene,
                case_type=CaseType.short,
                case_id="boundary_referral_short",
                media=Media.text if scene is Scene.online else Media.voice,
                model_mode=ModelMode.live,
                state_json={
                    "runtime": {
                        "engine": "character_prompt",
                        "phase": RuntimePhase.listening.value,
                    },
                },
            )
        )
        db.commit()
    return CharacterPromptKernel(
        engine=test_engine,
        characters=FakeNoWorldCharacterRepository(),
        character=character,
        speech=None,
        audio_root=tmp_path,
    )


def _marriage_online_kernel(
    test_engine: Engine,
    tmp_path: Path,
    character: FakeCharacter,
):
    from app.runtime.character_kernel import CharacterPromptKernel
    from app.runtime.character_provider import CharacterRepository

    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="marriage-character-session",
                mode=SessionMode.assessment,
                scene=Scene.online,
                case_type=CaseType.main,
                case_id="marriage_boundary_main",
                media=Media.text,
                model_mode=ModelMode.live,
                state_json={
                    "actor_state": {"compatibility_actor": "must-not-be-read"},
                    "runtime": {
                        "engine": "character_prompt",
                        "phase": RuntimePhase.listening.value,
                    },
                },
            )
        )
        db.add_all(
            [
                TurnRecord(
                    id="marriage-opening",
                    session_id="marriage-character-session",
                    client_turn_id="marriage-opening-turn",
                    sequence=1,
                    speaker=TurnSpeaker.client,
                    text="你好，我想问个事。\n\n这些聊天以后谁能看到？",
                ),
                TurnRecord(
                    id="marriage-prior-worker",
                    session_id="marriage-character-session",
                    client_turn_id="marriage-pair-1",
                    sequence=2,
                    speaker=TurnSpeaker.worker,
                    text="聊天记录会按平台规则保存，不会替你联系丈夫。",
                ),
                TurnRecord(
                    id="marriage-prior-client",
                    session_id="marriage-character-session",
                    client_turn_id="marriage-pair-1",
                    sequence=3,
                    speaker=TurnSpeaker.client,
                    text="那我想先说说今晚怎么办。",
                ),
            ]
        )
        db.commit()
    return CharacterPromptKernel(
        engine=test_engine,
        characters=CharacterRepository(),
        character=character,
        speech=None,
        audio_root=tmp_path,
    )


def test_character_kernel_end_session_persists_terminal_runtime_state(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    kernel = _kernel(test_engine, tmp_path, FakeCharacter(), FakeSpeech())
    with Session(test_engine) as db:
        record = db.get(SessionRecord, "character-session")
        assert record is not None
        record.state_json = {
            **record.state_json,
            "runtime": {
                **record.state_json["runtime"],
                "phase": RuntimePhase.technical_paused.value,
                "technical_retry_allowed": True,
                "pending_ending_route_id": "character_prompt_end",
            },
        }
        db.add(record)
        db.commit()

    kernel.end_session("character-session", EndReason.user_ended)
    kernel.end_session("character-session", EndReason.technical_interruption)

    with Session(test_engine) as db:
        record = db.get(SessionRecord, "character-session")
    assert record is not None
    assert record.status is SessionStatus.ended
    assert record.end_reason is EndReason.user_ended
    assert record.state_json["actor_state"] == {
        "legacy": "must-stay-untouched"
    }
    assert record.state_json["runtime"] == {
        "engine": "character_prompt",
        "phase": RuntimePhase.ended.value,
        "technical_retry_allowed": False,
    }


@pytest.mark.asyncio
async def test_character_kernel_uses_full_transcript_once_and_tts_only_spoken_text(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    character = FakeCharacter()
    speech = FakeSpeech()
    kernel = _kernel(test_engine, tmp_path, character, speech)

    result = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-2",
        text="你最怕母亲进门后发生什么？",
    )

    assert len(character.calls) == 1
    call = character.calls[0]
    transcript = call["transcript"]
    assert isinstance(transcript, Sequence)
    assert [(turn.speaker, turn.text) for turn in transcript] == [
        ("client", "喂……你好。我妈明早就到了。"),
        ("worker", "你愿意告诉我发生了什么吗？"),
        ("client", "我失业四十一天了，一直瞒着她。"),
    ]
    assert call["current_worker_text"] == "你最怕母亲进门后发生什么？"
    assert speech.texts == ["我最怕她进门以后什么都明白了。"]
    assert speech.instructions == ["声音偏低，语速稍慢，句间短暂停顿"]
    assert result.audio_chunks == (b"character-pcm",)

    with Session(test_engine) as db:
        turns = list(
            db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == "character-session")
                .order_by(TurnRecord.sequence)
            ).all()
        )
        record = db.get(SessionRecord, "character-session")
    assert [turn.text for turn in turns[-2:]] == [
        "你最怕母亲进门后发生什么？",
        "我最怕她进门以后什么都明白了。",
    ]
    assert record is not None
    assert record.state_json["actor_state"] == {"legacy": "must-stay-untouched"}
    assert record.state_json["runtime"]["engine"] == "character_prompt"
    assert record.state_json["runtime"]["phase"] == "listening"


@pytest.mark.asyncio
async def test_marriage_online_turn_keeps_full_transcript_and_one_multiline_client_turn(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    spoken_text = "我最怕他回来以后又说我没完没了。\n\n可我今晚也不想马上吵。"
    character = FakeCharacter(spoken_text=spoken_text)
    kernel = _marriage_online_kernel(test_engine, tmp_path, character)

    result = await kernel.process_worker_turn(
        session_id="marriage-character-session",
        client_turn_id="marriage-pair-2",
        text="你希望今晚先避免什么？",
        synthesize_audio=False,
    )

    assert len(character.calls) == 1
    call = character.calls[0]
    transcript = call["transcript"]
    assert isinstance(transcript, Sequence)
    assert [(turn.speaker, turn.text) for turn in transcript] == [
        ("client", "你好，我想问个事。\n\n这些聊天以后谁能看到？"),
        ("worker", "聊天记录会按平台规则保存，不会替你联系丈夫。"),
        ("client", "那我想先说说今晚怎么办。"),
    ]
    assert call["current_worker_text"] == "你希望今晚先避免什么？"
    assert tuple(call["allowed_world_actions"]) == ("none",)
    assert result.client.text == spoken_text

    with Session(test_engine) as db:
        current_turns = list(
            db.exec(
                select(TurnRecord)
                .where(
                    TurnRecord.session_id == "marriage-character-session",
                    TurnRecord.client_turn_id == "marriage-pair-2",
                )
                .order_by(TurnRecord.sequence)
            ).all()
        )
        record = db.get(SessionRecord, "marriage-character-session")

    assert [turn.speaker for turn in current_turns] == [
        TurnSpeaker.worker,
        TurnSpeaker.client,
    ]
    assert [turn.text for turn in current_turns] == [
        "你希望今晚先避免什么？",
        spoken_text,
    ]
    assert current_turns[-1].signals_json["action_request"] == "none"
    assert record is not None
    assert record.state_json["actor_state"] == {
        "compatibility_actor": "must-not-be-read"
    }


@pytest.mark.asyncio
async def test_character_contract_failure_is_recorded_and_allows_retrying_current_turn(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    invalid_character = InvalidCharacter()
    kernel = _marriage_online_kernel(
        test_engine,
        tmp_path,
        invalid_character,  # type: ignore[arg-type]
    )

    with pytest.raises(TechnicalPauseError) as raised:
        await kernel.process_worker_turn(
            session_id="marriage-character-session",
            client_turn_id="marriage-invalid-output",
            text="我刚才这句话请保留，恢复后重试这一轮。",
            synthesize_audio=False,
        )

    assert invalid_character.calls == 1
    assert raised.value.failed_phase is RuntimePhase.acting
    assert raised.value.can_retry is True
    with Session(test_engine) as db:
        record = db.get(SessionRecord, "marriage-character-session")
        failure = db.exec(
            select(RuntimeFailureRecord).where(
                RuntimeFailureRecord.client_turn_id == "marriage-invalid-output"
            )
        ).one()
    assert record is not None
    assert record.state_json["runtime"]["phase"] == "technical_paused"
    assert record.state_json["runtime"]["technical_retry_allowed"] is True
    assert failure.failure_code == "actor.output_validation"
    assert failure.retryable is True
    assert failure.attempt_count == 1


@pytest.mark.asyncio
async def test_replayed_character_turn_does_not_call_model_or_tts_again(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    character = FakeCharacter()
    speech = FakeSpeech()
    kernel = _kernel(test_engine, tmp_path, character, speech)

    first = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-idempotent",
        text="你现在最担心什么？",
    )
    replay = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-idempotent",
        text="你现在最担心什么？",
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.worker.text == "你现在最担心什么？"
    assert len(character.calls) == 1
    assert speech.texts == ["我最怕她进门以后什么都明白了。"]


@pytest.mark.asyncio
async def test_replayed_character_turn_rejects_different_worker_text(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.kernel import KernelSessionConflictError

    character = FakeCharacter()
    speech = FakeSpeech()
    kernel = _kernel(test_engine, tmp_path, character, speech)
    await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-conflict",
        text="你现在最担心什么？",
    )

    with pytest.raises(KernelSessionConflictError, match="工作者发言不一致"):
        await kernel.process_worker_turn(
            session_id="character-session",
            client_turn_id="pair-conflict",
            text="这次使用了不同的工作者发言。",
        )

    assert len(character.calls) == 1
    assert speech.texts == ["我最怕她进门以后什么都明白了。"]


@pytest.mark.asyncio
async def test_live_character_turn_id_conflict_returns_to_listening(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection

    character = FakeCharacter()
    speech = FakeSpeech()
    kernel = _kernel(test_engine, tmp_path, character, speech)
    await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-live-conflict",
        text="你现在最担心什么？",
    )
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-session",
        kernel=kernel,
        speech_provider=object(),  # type: ignore[arg-type]
        media=Media.text,
        initial_phase=RuntimePhase.listening,
    )

    await connection._text_turn(
        {
            "type": "text.turn",
            "client_turn_id": "pair-live-conflict",
            "text": "这次正文不同，不能静默回放。",
        }
    )
    generation_task = connection.generation_task
    assert generation_task is not None
    await generation_task

    assert connection.phase is RuntimePhase.listening
    assert socket.json_messages[-2:] == [
        {
            "type": "input.error",
            "message": "这次发言标识已被使用，请重新发送",
        },
        {"type": "phase", "phase": RuntimePhase.listening.value},
    ]
    assert not any(
        message["type"] in {"turn.committed", "technical.pause"}
        for message in socket.json_messages
    )
    assert connection.generation_task is None
    assert len(character.calls) == 1
    await connection._cleanup()


@pytest.mark.asyncio
async def test_live_context_exhaustion_persists_worker_verbatim_then_ends_technically(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    kernel = _no_world_kernel(
        test_engine,
        tmp_path,
        ExhaustedCharacter(),  # type: ignore[arg-type]
    )
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="short-character-session",
        kernel=kernel,
        speech_provider=object(),  # type: ignore[arg-type]
        media=Media.text,
        initial_phase=RuntimePhase.listening,
    )
    worker_text = "当前工作者完整原话：我们先确认你现在是否安全。"

    await connection._run_generation(
        _PendingGeneration(
            text=worker_text,
            client_turn_id="context-exhausted-turn",
            worker_pcm=b"",
            metrics=None,
        )
    )

    with Session(test_engine) as db:
        record = db.get(SessionRecord, "short-character-session")
        turns = list(
            db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == "short-character-session")
                .order_by(TurnRecord.sequence)
            ).all()
        )
        failure = db.exec(
            select(RuntimeFailureRecord).where(
                RuntimeFailureRecord.session_id == "short-character-session"
            )
        ).one()

    assert record is not None
    assert record.status is SessionStatus.ended
    assert record.end_reason is EndReason.technical_interruption
    assert "actor_state" not in record.state_json
    assert record.state_json["runtime"]["phase"] == RuntimePhase.ended.value
    assert record.state_json["runtime"]["technical_retry_allowed"] is False
    assert [(turn.speaker, turn.text) for turn in turns] == [
        (TurnSpeaker.worker, worker_text)
    ]
    assert turns[0].client_turn_id == "context-exhausted-turn"
    assert turns[0].signals_json["technical_interruption"] == (
        "actor.context_exhausted"
    )
    assert failure.failure_code == "actor.context_exhausted"
    assert failure.retryable is False
    assert socket.closed is True
    assert socket.json_messages[-1] == {
        "type": "session.ended",
        "reason": "technical_interruption",
    }


@pytest.mark.asyncio
async def test_context_exhaustion_retry_is_idempotent_and_conflicting_text_is_rejected(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.kernel import KernelTurnConflictError

    character = ExhaustedCharacter()
    kernel = _no_world_kernel(test_engine, tmp_path, character)  # type: ignore[arg-type]
    request = {
        "session_id": "short-character-session",
        "client_turn_id": "context-exhausted-idempotent",
        "text": "这句完整原话只能保存一次。",
        "synthesize_audio": False,
    }

    with pytest.raises(TechnicalPauseError) as first:
        await kernel.process_worker_turn(**request)
    with pytest.raises(TechnicalPauseError) as replay:
        await kernel.process_worker_turn(**request)
    with pytest.raises(KernelTurnConflictError):
        await kernel.process_worker_turn(
            **{**request, "text": "相同标识下的另一段文本。"}
        )

    with Session(test_engine) as db:
        record = db.get(SessionRecord, "short-character-session")
        turns = list(
            db.exec(
                select(TurnRecord).where(
                    TurnRecord.client_turn_id == "context-exhausted-idempotent"
                )
            ).all()
        )
    assert first.value.failure_code == "actor.context_exhausted"
    assert replay.value.failure_code == "actor.context_exhausted"
    assert character.calls == 1
    assert len(turns) == 1
    assert record is not None
    assert record.status is SessionStatus.ended
    assert record.state_json["runtime"] == {
        "engine": "character_prompt",
        "phase": RuntimePhase.ended.value,
        "technical_retry_allowed": False,
    }


@pytest.mark.asyncio
async def test_live_character_ended_session_conflict_is_not_downgraded(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration
    from app.runtime.kernel import KernelSessionConflictError

    kernel = _kernel(test_engine, tmp_path, FakeCharacter(), FakeSpeech())
    kernel.end_session("character-session")
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-session",
        kernel=kernel,
        speech_provider=object(),  # type: ignore[arg-type]
        media=Media.text,
        initial_phase=RuntimePhase.listening,
    )

    with pytest.raises(KernelSessionConflictError, match="会话已结束"):
        await connection._run_generation(
            _PendingGeneration(
                text="会话结束后不能继续。",
                client_turn_id="pair-after-ended",
                worker_pcm=b"",
                metrics=None,
            )
        )

    assert not any(
        message["type"] in {"input.error", "technical.pause", "turn.committed"}
        for message in socket.json_messages
    )
    assert connection.phase is RuntimePhase.listening
    await connection._cleanup()


@pytest.mark.asyncio
async def test_tts_retry_reuses_generated_character_output(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    character = FakeCharacter()
    speech = RecoveringSpeech()
    kernel = _kernel(test_engine, tmp_path, character, speech)
    metrics = SpeechMetricsInput(
        speech_duration_ms=900,
        asr_sentences=({"text": "你最怕什么？"},),
    )

    with pytest.raises(TechnicalPauseError) as failure:
        await kernel.process_worker_turn(
            session_id="character-session",
            client_turn_id="pair-tts-retry",
            text="你最怕什么？",
            worker_pcm=b"worker-pcm",
            speech_metrics=metrics,
            world_time_advance_seconds=17,
        )
    assert failure.value.failed_phase is RuntimePhase.synthesizing
    assert len(character.calls) == 1
    pending = kernel.pending_retry("character-session")
    assert pending is not None
    assert pending.client_turn_id == "pair-tts-retry"
    assert pending.opening is False
    assert pending.text == "你最怕什么？"
    assert pending.worker_pcm == b"worker-pcm"
    assert pending.speech_metrics == metrics
    assert pending.world_time_advance_seconds == 17

    speech.fail = False
    kernel.resume_listening("character-session")
    retry_metrics = SpeechMetricsInput(
        speech_duration_ms=900,
        excluded_technical_ms=321,
        asr_sentences=({"text": "你最怕什么？"},),
    )
    result = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-tts-retry",
        text="你最怕什么？",
        speech_metrics=retry_metrics,
    )

    with Session(test_engine) as db:
        persisted_metric = db.exec(
            select(SpeechMetricRecord).where(
                SpeechMetricRecord.session_id == "character-session"
            )
        ).one()
    assert len(character.calls) == 1
    assert result.client.text == "我最怕她进门以后什么都明白了。"
    assert result.audio_chunks == (b"recovered-character-pcm",)
    assert persisted_metric.excluded_technical_ms == 321
    assert kernel._pending_outputs == {}
    assert kernel.pending_retry("character-session") is None


@pytest.mark.asyncio
async def test_reconnected_character_turn_recovers_pending_payload_without_model_recall(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    character = FakeCharacter()
    speech = RecoveringSpeech()
    kernel = _kernel(test_engine, tmp_path, character, speech)
    first_socket = FakeSocket()
    first_connection = _LiveConnection(
        websocket=first_socket,  # type: ignore[arg-type]
        session_id="character-session",
        kernel=kernel,
        speech_provider=object(),  # type: ignore[arg-type]
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
    )
    await first_connection._run_generation(
        _PendingGeneration(
            text="请把这轮完整恢复。",
            client_turn_id="pair-reconnect-tts",
            worker_pcm=b"reconnect-worker-pcm",
            metrics=None,
        )
    )
    assert [
        message["text"]
        for message in first_socket.json_messages
        if message["type"] == "visitor.text"
    ] == ["我最怕她进门以后什么都明白了。"]
    assert first_connection.retry_payload is not None

    speech.fail = False
    socket = FakeSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="character-session",
        kernel=kernel,
        speech_provider=object(),  # type: ignore[arg-type]
        media=Media.voice,
        initial_phase=RuntimePhase.technical_paused,
        technical_retry_allowed=True,
    )
    connection.asr_stream = NoopASRStream()  # type: ignore[assignment]

    await connection._technical_retry()
    generation_task = connection.generation_task
    assert generation_task is not None
    await generation_task

    assert len(character.calls) == 1
    assert socket.binary_messages == [b"recovered-character-pcm"]
    assert [
        message["text"]
        for message in socket.json_messages
        if message["type"] == "visitor.text"
    ] == ["我最怕她进门以后什么都明白了。"]
    assert kernel.pending_retry("character-session") is None
    with Session(test_engine) as db:
        turns = list(
            db.exec(
                select(TurnRecord).where(
                    TurnRecord.client_turn_id == "pair-reconnect-tts"
                )
            ).all()
        )
    assert [turn.text for turn in turns] == [
        "请把这轮完整恢复。",
        "我最怕她进门以后什么都明白了。",
    ]
    await first_connection._cleanup()
    await connection._cleanup()


@pytest.mark.asyncio
async def test_reconnected_character_opening_recovers_original_opening_id(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection

    character = FakeCharacter(spoken_text="喂……你好。")
    speech = RecoveringSpeech()
    kernel = _kernel(
        test_engine,
        tmp_path,
        character,
        speech,
        with_transcript=False,
    )
    with pytest.raises(TechnicalPauseError):
        await kernel.generate_opening(
            session_id="character-session",
            client_turn_id="opening-reconnect-tts",
        )
    pending = kernel.pending_retry("character-session")
    assert pending is not None
    assert pending.opening is True
    assert pending.client_turn_id == "opening-reconnect-tts"

    speech.fail = False
    connection = _LiveConnection(
        websocket=FakeSocket(),  # type: ignore[arg-type]
        session_id="character-session",
        kernel=kernel,
        speech_provider=object(),  # type: ignore[arg-type]
        media=Media.voice,
        initial_phase=RuntimePhase.technical_paused,
        opening_delay_seconds=0,
        technical_retry_allowed=True,
    )
    connection.asr_stream = NoopASRStream()  # type: ignore[assignment]

    await connection._technical_retry()
    opening_task = connection.opening_task
    assert opening_task is not None
    assert connection.opening_client_turn_id == "opening-reconnect-tts"
    await opening_task

    assert len(character.calls) == 1
    assert kernel.pending_retry("character-session") is None
    with Session(test_engine) as db:
        opening = db.exec(
            select(TurnRecord).where(
                TurnRecord.client_turn_id == "opening-reconnect-tts"
            )
        ).one()
    assert opening.speaker is TurnSpeaker.client
    assert opening.text == "喂……你好。"
    await connection._cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spoken_text",
    (
        "我觉得今晚聊到这里差不多了，我先去卧室待着，看看能不能静下来。",
        "我觉得可以先结束这次聊天了。谢谢你的倾听，我会按照咱们说的先回卧室待一会儿。",
    ),
)
async def test_structured_character_end_sets_result_signal_and_pending_route(
    test_engine: Engine,
    tmp_path: Path,
    spoken_text: str,
) -> None:
    from app.runtime.character_kernel import CHARACTER_END_ROUTE_ID

    character = FakeCharacter(
        spoken_text=spoken_text,
        end_session=True,
    )
    kernel = _kernel(test_engine, tmp_path, character, FakeSpeech())

    result = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-natural-end",
        text="那我们今天先聊到这里，可以吗？",
    )

    with Session(test_engine) as db:
        client_turn = db.exec(
            select(TurnRecord).where(
                TurnRecord.session_id == "character-session",
                TurnRecord.client_turn_id == "pair-natural-end",
                TurnRecord.speaker == TurnSpeaker.client,
            )
        ).one()
        record = db.get(SessionRecord, "character-session")

    assert result.ending_route_id == CHARACTER_END_ROUTE_ID
    assert client_turn.signals_json["end_session"] is True
    assert record is not None
    assert (
        record.state_json["runtime"]["pending_ending_route_id"]
        == CHARACTER_END_ROUTE_ID
    )


@pytest.mark.asyncio
async def test_character_opening_end_signal_is_committed_and_restored_on_replay(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.character_kernel import CHARACTER_END_ROUTE_ID

    character = FakeCharacter(spoken_text="好，那我先挂了。", end_session=True)
    kernel = _kernel(
        test_engine,
        tmp_path,
        character,
        FakeSpeech(),
        with_transcript=False,
    )

    first = await kernel.generate_opening(
        session_id="character-session",
        client_turn_id="opening-natural-end",
        synthesize_audio=False,
    )
    replay = await kernel.generate_opening(
        session_id="character-session",
        client_turn_id="opening-natural-end",
        synthesize_audio=False,
    )

    with Session(test_engine) as db:
        turn = db.exec(
            select(TurnRecord).where(
                TurnRecord.client_turn_id == "opening-natural-end"
            )
        ).one()
        record = db.get(SessionRecord, "character-session")

    assert first.ending_route_id == CHARACTER_END_ROUTE_ID
    assert replay.ending_route_id == CHARACTER_END_ROUTE_ID
    assert replay.replayed is True
    assert turn.signals_json["end_session"] is True
    assert turn.signals_json["ending_route_id"] == CHARACTER_END_ROUTE_ID
    assert record is not None
    assert (
        record.state_json["runtime"]["pending_ending_route_id"]
        == CHARACTER_END_ROUTE_ID
    )


@pytest.mark.asyncio
async def test_character_opening_uses_structured_end_signal_without_text_inference(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.character_kernel import CHARACTER_END_ROUTE_ID

    character = FakeCharacter(
        spoken_text="喂……你好，我想跟你说点事。",
        end_session=True,
    )
    kernel = _kernel(
        test_engine,
        tmp_path,
        character,
        FakeSpeech(),
        with_transcript=False,
    )

    result = await kernel.generate_opening(
        session_id="character-session",
        client_turn_id="opening-false-end",
        synthesize_audio=False,
    )

    with Session(test_engine) as db:
        turn = db.exec(
            select(TurnRecord).where(
                TurnRecord.client_turn_id == "opening-false-end"
            )
        ).one()
        record = db.get(SessionRecord, "character-session")

    assert result.ending_route_id == CHARACTER_END_ROUTE_ID
    assert turn.signals_json["end_session"] is True
    assert record is not None
    assert (
        record.state_json["runtime"]["pending_ending_route_id"]
        == CHARACTER_END_ROUTE_ID
    )


@pytest.mark.asyncio
async def test_relationship_rupture_worded_as_unable_to_continue_can_end(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.character_kernel import CHARACTER_END_ROUTE_ID

    character = FakeCharacter(
        spoken_text="你根本没听我说，我很难再继续了。",
        end_session=True,
    )
    kernel = _kernel(test_engine, tmp_path, character, FakeSpeech())

    result = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-rupture-end",
        text="你这样占着线也解决不了问题，我要结束了。",
    )

    assert result.ending_route_id == CHARACTER_END_ROUTE_ID


@pytest.mark.asyncio
async def test_relationship_rupture_worded_as_the_call_being_pointless_can_end(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.character_kernel import CHARACTER_END_ROUTE_ID

    character = FakeCharacter(
        spoken_text="你连听我把话说完的耐心都没有，这通电话没意思了。",
        end_session=True,
    )
    kernel = _kernel(test_engine, tmp_path, character, FakeSpeech())

    result = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-pointless-end",
        text="你这样占着线也解决不了问题，我要结束了。",
    )

    assert result.ending_route_id == CHARACTER_END_ROUTE_ID


@pytest.mark.asyncio
async def test_model_true_end_signal_ends_without_spoken_text_inference(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.character_kernel import CHARACTER_END_ROUTE_ID

    character = FakeCharacter(
        spoken_text="我想先联系姐姐，让她陪我。",
        end_session=True,
    )
    kernel = _kernel(test_engine, tmp_path, character, FakeSpeech())

    result = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-silent-end",
        text="那你准备先做哪一步？",
    )

    with Session(test_engine) as db:
        client_turn = db.exec(
            select(TurnRecord).where(
                TurnRecord.session_id == "character-session",
                TurnRecord.client_turn_id == "pair-silent-end",
                TurnRecord.speaker == TurnSpeaker.client,
            )
        ).one()
        record = db.get(SessionRecord, "character-session")

    assert result.ending_route_id == CHARACTER_END_ROUTE_ID
    assert client_turn.signals_json["end_session"] is True
    assert record is not None
    assert (
        record.state_json["runtime"]["pending_ending_route_id"]
        == CHARACTER_END_ROUTE_ID
    )


@pytest.mark.asyncio
async def test_same_spoken_text_does_not_end_without_model_end_signal(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    character = FakeCharacter(
        spoken_text="我想先联系姐姐，让她陪我。",
        end_session=False,
    )
    kernel = _kernel(test_engine, tmp_path, character, FakeSpeech())

    result = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="pair-not-an-end",
        text="那我们继续聊聊你今晚怎么安排，好吗？",
    )

    with Session(test_engine) as db:
        client_turn = db.exec(
            select(TurnRecord).where(
                TurnRecord.session_id == "character-session",
                TurnRecord.client_turn_id == "pair-not-an-end",
                TurnRecord.speaker == TurnSpeaker.client,
            )
        ).one()
        record = db.get(SessionRecord, "character-session")

    assert result.client.text == "我想先联系姐姐，让她陪我。"
    assert result.ending_route_id is None
    assert client_turn.signals_json["end_session"] is False
    assert record is not None
    assert "pending_ending_route_id" not in record.state_json["runtime"]


@pytest.mark.asyncio
async def test_no_world_opening_and_turn_do_not_create_world_state_or_stage_signals(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    character = FakeCharacter(spoken_text="我想先把这个请求说清楚。")
    kernel = _no_world_kernel(test_engine, tmp_path, character)

    opening = await kernel.generate_opening(
        session_id="short-character-session",
        client_turn_id="short-opening",
        synthesize_audio=False,
    )
    result = await kernel.process_worker_turn(
        session_id="short-character-session",
        client_turn_id="short-turn-1",
        text="你刚才说希望一直找同一个人，是吗？",
        synthesize_audio=False,
    )

    with Session(test_engine) as db:
        record = db.get(SessionRecord, "short-character-session")
        persisted = list(
            db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == "short-character-session")
                .order_by(TurnRecord.sequence)
            ).all()
        )

    assert opening.client.text
    assert result.client.text
    assert record is not None
    assert "world" not in record.state_json
    assert len(character.calls) == 2
    for call in character.calls:
        assert call["current_scene"] == "online"
        assert tuple(call["allowed_world_actions"]) == ("none",)
    for turn in persisted:
        assert "world_stage_before" not in turn.signals_json
        assert "world_stage_after" not in turn.signals_json


@pytest.mark.asyncio
async def test_no_world_four_turn_chain_passes_full_transcript_and_scene_once_per_turn(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    character = FakeCharacter(spoken_text="我听见了，我再说具体一点。")
    kernel = _no_world_kernel(
        test_engine,
        tmp_path,
        character,
        scene=Scene.institution,
    )
    await kernel.generate_opening(
        session_id="short-character-session",
        client_turn_id="short-chain-opening",
        synthesize_audio=False,
    )
    worker_lines = [
        "发作的时候具体是什么样？",
        "我们不能建立私人联系。",
        "这是职业道德要求。",
        "我可以帮你整理转介信息，也约一次过渡会谈。",
    ]

    for index, worker_text in enumerate(worker_lines, start=1):
        calls_before = len(character.calls)
        await kernel.process_worker_turn(
            session_id="short-character-session",
            client_turn_id=f"short-chain-{index}",
            text=worker_text,
            synthesize_audio=False,
        )
        assert len(character.calls) == calls_before + 1

    assert len(character.calls) == 5
    for index, call in enumerate(character.calls[1:], start=1):
        transcript = call["transcript"]
        assert isinstance(transcript, Sequence)
        assert call["current_worker_text"] == worker_lines[index - 1]
        assert call["current_scene"] == "institution"
        assert tuple(call["allowed_world_actions"]) == ("none",)
        assert len(transcript) == 1 + (index - 1) * 2
        assert transcript[0].speaker == "client"
        if index > 1:
            assert transcript[-2].speaker == "worker"
            assert transcript[-2].text == worker_lines[index - 2]
            assert transcript[-1].speaker == "client"


@pytest.mark.asyncio
async def test_no_world_character_rejects_non_none_action_before_commit(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    character = FakeCharacter(action_requests=("send_first_support_message",))
    kernel = _no_world_kernel(test_engine, tmp_path, character)

    with pytest.raises(TechnicalPauseError):
        await kernel.process_worker_turn(
            session_id="short-character-session",
            client_turn_id="short-illegal-action",
            text="你现在就联系我。",
            synthesize_audio=False,
        )

    with Session(test_engine) as db:
        record = db.get(SessionRecord, "short-character-session")
        turns = list(
            db.exec(
                select(TurnRecord).where(
                    TurnRecord.session_id == "short-character-session"
                )
            ).all()
        )
    assert record is not None
    assert "world" not in record.state_json
    assert turns == []


@pytest.mark.asyncio
async def test_support_world_progresses_in_order_and_is_injected_into_actor(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.character_world import SupportWorldAction

    character = FakeCharacter(
        action_requests=(
            "send_first_support_message",
            "send_urgent_support_message",
            "none",
            "let_support_in",
        )
    )
    kernel = _kernel(test_engine, tmp_path, character, FakeSpeech())

    await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="support-first",
        text="你愿意先联系唐婷吗？",
        synthesize_audio=False,
    )
    with Session(test_engine) as db:
        record = db.get(SessionRecord, "character-session")
    assert record is not None
    assert record.state_json["world"]["stage"] == "first_unanswered"
    assert record.state_json["world"]["arrival_due_at"] is None

    await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="support-second",
        text="把你现在不能独处说清楚，再联系她一次。",
        synthesize_audio=False,
    )
    with Session(test_engine) as db:
        record = db.get(SessionRecord, "character-session")
    assert record is not None
    assert record.state_json["world"]["stage"] == "coming"
    assert record.state_json["world"]["arrival_due_at"] is not None

    await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="support-arrived",
        text="现在门外有动静吗？",
        synthesize_audio=False,
        world_time_advance_seconds=781,
    )
    await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="support-present",
        text="确认是唐婷以后再开门。",
        synthesize_audio=False,
        world_time_advance_seconds=781,
    )

    with Session(test_engine) as db:
        record = db.get(SessionRecord, "character-session")
        client_turns = list(
            db.exec(
                select(TurnRecord)
                .where(
                    TurnRecord.session_id == "character-session",
                    TurnRecord.speaker == TurnSpeaker.client,
                )
                .order_by(TurnRecord.sequence)
            ).all()
        )
    assert record is not None
    assert record.state_json["world"] == {
        "kind": "support_arrival",
        "stage": "present",
        "arrival_due_at": None,
    }
    assert len(character.calls) == 4
    assert character.calls[0]["world_reality"] == "热线接通后还没有联系唐婷。"
    assert character.calls[1]["world_reality"] == "第一次联系唐婷没有接通。"
    assert character.calls[2]["world_reality"] == "唐婷已经到门外，尚未进屋。"
    assert tuple(character.calls[0]["allowed_world_actions"]) == (
        SupportWorldAction.none,
        SupportWorldAction.send_first_support_message,
    )
    assert tuple(character.calls[1]["allowed_world_actions"]) == (
        SupportWorldAction.none,
        SupportWorldAction.send_urgent_support_message,
    )
    assert tuple(character.calls[2]["allowed_world_actions"]) == (
        SupportWorldAction.none,
        SupportWorldAction.let_support_in,
    )
    support_turns = [
        turn
        for turn in client_turns
        if turn.client_turn_id.startswith("support-")
    ]
    assert [turn.signals_json["action_request"] for turn in support_turns] == [
        "send_first_support_message",
        "send_urgent_support_message",
        "none",
        "let_support_in",
    ]
    assert support_turns[-1].signals_json["world_stage_before"] == "at_door"
    assert support_turns[-1].signals_json["world_stage_after"] == "present"


@pytest.mark.asyncio
async def test_support_action_replay_does_not_advance_world_twice(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    character = FakeCharacter(action_requests=("send_first_support_message",))
    kernel = _kernel(test_engine, tmp_path, character, FakeSpeech())

    first = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="support-replay",
        text="联系唐婷试试。",
        synthesize_audio=False,
    )
    replay = await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="support-replay",
        text="联系唐婷试试。",
        synthesize_audio=False,
        world_time_advance_seconds=1000,
    )

    with Session(test_engine) as db:
        record = db.get(SessionRecord, "character-session")
    assert first.replayed is False
    assert replay.replayed is True
    assert len(character.calls) == 1
    assert record is not None
    assert record.state_json["world"]["stage"] == "first_unanswered"
    assert record.state_json["world"]["arrival_due_at"] is None


@pytest.mark.asyncio
async def test_tts_retry_commits_support_action_exactly_once(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.character_world import load_support_world

    character = FakeCharacter(action_requests=("send_first_support_message",))
    speech = RecoveringSpeech()
    kernel = _kernel(test_engine, tmp_path, character, speech)

    with pytest.raises(TechnicalPauseError):
        await kernel.process_worker_turn(
            session_id="character-session",
            client_turn_id="support-tts-retry",
            text="联系唐婷试试。",
        )
    with Session(test_engine) as db:
        failed_record = db.get(SessionRecord, "character-session")
    assert failed_record is not None
    assert load_support_world(failed_record.state_json).stage.value == "not_contacted"

    speech.fail = False
    kernel.resume_listening("character-session")
    await kernel.process_worker_turn(
        session_id="character-session",
        client_turn_id="support-tts-retry",
        text="联系唐婷试试。",
    )

    with Session(test_engine) as db:
        record = db.get(SessionRecord, "character-session")
    assert len(character.calls) == 1
    assert record is not None
    assert record.state_json["world"]["stage"] == "first_unanswered"


@pytest.mark.asyncio
async def test_opening_rejects_world_action_and_keeps_initial_world(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.runtime.character_world import load_support_world

    character = FakeCharacter(action_requests=("send_first_support_message",))
    kernel = _kernel(
        test_engine,
        tmp_path,
        character,
        FakeSpeech(),
        with_transcript=False,
    )

    with pytest.raises(TechnicalPauseError):
        await kernel.generate_opening(
            session_id="character-session",
            client_turn_id="opening-illegal-action",
            synthesize_audio=False,
        )

    with Session(test_engine) as db:
        record = db.get(SessionRecord, "character-session")
        turns = list(
            db.exec(
                select(TurnRecord).where(
                    TurnRecord.session_id == "character-session"
                )
            ).all()
        )
    assert record is not None
    assert load_support_world(record.state_json).stage.value == "not_contacted"
    assert turns == []

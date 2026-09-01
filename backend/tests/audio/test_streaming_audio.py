import json
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from app.audio.models import AudioKind, SpeechMetricRecord
from app.runtime.failures import failure_attempt_from_exception
from app.runtime.providers import AliyunSpeechProvider, RuntimeSpeechError
from app.runtime_config import RuntimeCredentialStore
from app.sessions.models import (
    CaseType,
    Media,
    ModelMode,
    Scene,
    SessionMode,
    SessionRecord,
    TurnRecord,
    TurnSpeaker,
)


def event(name: str, *, payload: dict[str, object] | None = None) -> str:
    return json.dumps(
        {
            "header": {"event": name, "task_id": "server-task"},
            "payload": payload or {},
        }
    )


def task_failed_event(
    *,
    task_id: str,
    error_code: str,
    error_message: str,
    request_id: str | None = None,
) -> str:
    header = {
        "event": "task-failed",
        "task_id": task_id,
        "error_code": error_code,
        "error_message": error_message,
    }
    if request_id is not None:
        header["request_id"] = request_id
    return json.dumps({"header": header, "payload": {}})


def failure_evidence(error: Exception) -> tuple[str | None, dict[str, object]]:
    attempt = failure_attempt_from_exception(1, error)
    chain = attempt.details["exception_chain"]
    assert isinstance(chain, list)
    head = chain[0]
    assert isinstance(head, dict)
    details = head["details"]
    assert isinstance(details, dict)
    return attempt.provider_request_id, details


class FakeWebSocket:
    def __init__(self, incoming: list[str | bytes]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[str | bytes] = []
        self.closed = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if not self.incoming:
            raise AssertionError("测试消息已经用完")
        return self.incoming.popleft()

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, socket: FakeWebSocket) -> None:
        self.socket = socket
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeWebSocket:
        self.calls.append((url, headers))
        return self.socket


class FailingStreamSocket(FakeWebSocket):
    def __init__(self, *, fail_send_after_start: bool = False, fail_receive: bool = False) -> None:
        super().__init__([event("task-started")])
        self.fail_send_after_start = fail_send_after_start
        self.fail_receive = fail_receive

    async def send(self, message: str | bytes) -> None:
        if self.fail_send_after_start and self.sent:
            raise OSError("socket contained test-secret-for-unit")
        await super().send(message)

    async def recv(self) -> str | bytes:
        if self.fail_receive and not self.incoming:
            raise OSError("socket contained test-secret-for-unit")
        return await super().recv()


def credential_store(*, workspace_id: str | None = None) -> RuntimeCredentialStore:
    store = RuntimeCredentialStore()
    store.update(api_key="test-speech-provider-key", workspace_id=workspace_id)
    return store


def decode_sent(socket: FakeWebSocket, index: int) -> dict[str, Any]:
    message = socket.sent[index]
    assert isinstance(message, str)
    return json.loads(message)


async def test_asr_uses_official_inference_protocol_and_waits_for_task_started() -> None:
    socket = FakeWebSocket(
        [
            event("task-started"),
            event(
                "result-generated",
                payload={
                    "output": {
                        "sentence": {
                            "sentence_id": 1,
                            "text": "我再想想",
                            "begin_time": 120,
                            "end_time": 980,
                            "sentence_begin": False,
                            "sentence_end": True,
                            "words": [
                                {
                                    "text": "我",
                                    "begin_time": 120,
                                    "end_time": 250,
                                    "punctuation": "",
                                }
                            ],
                        }
                    }
                },
            ),
            event("task-finished"),
        ]
    )
    connector = FakeConnector(socket)
    provider = AliyunSpeechProvider(
        credential_store(workspace_id="workspace-a"), connector=connector
    )

    stream = await provider.open_asr()

    assert len(socket.sent) == 1
    assert connector.calls == [
        (
            "wss://workspace-a.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference",
            {
                "Authorization": "Bearer test-speech-provider-key",
                "X-DashScope-DataInspection": "enable",
            },
        )
    ]
    run_task = decode_sent(socket, 0)
    assert run_task["header"]["action"] == "run-task"
    assert run_task["header"]["streaming"] == "duplex"
    assert run_task["payload"] == {
        "task_group": "audio",
        "task": "asr",
        "function": "recognition",
        "model": "qwen-audio-3.0-asr-flash-streaming",
        "parameters": {
            "format": "pcm",
            "sample_rate": 16000,
            "heartbeat": True,
        },
        "input": {},
    }

    await stream.send_audio(b"pcm-frame")
    result = await stream.receive_sentence()
    await stream.finish()
    finished = await stream.receive_sentence()
    await stream.close()

    assert socket.sent[1] == b"pcm-frame"
    assert result is not None
    assert result.text == "我再想想"
    assert result.sentence_end is True
    assert result.begin_time_ms == 120
    assert result.words[0].text == "我"
    assert decode_sent(socket, 2)["header"]["action"] == "finish-task"
    assert decode_sent(socket, 2)["payload"] == {"input": {}}
    assert finished is None
    assert socket.closed is True


async def test_asr_task_failure_raises_a_safe_chinese_error() -> None:
    failure = task_failed_event(
        task_id="asr-open-task",
        request_id="asr-open-request",
        error_code="InvalidParameter",
        error_message="Authorization: Bearer test-speech-provider-key",
    )
    socket = FakeWebSocket([failure])
    provider = AliyunSpeechProvider(credential_store(), connector=FakeConnector(socket))

    with pytest.raises(RuntimeSpeechError, match="实时语音识别任务失败") as caught:
        await provider.open_asr()

    request_id, details = failure_evidence(caught.value)
    assert request_id == "asr-open-request"
    assert details == {
        "event": "task-failed",
        "task_id": "asr-open-task",
        "request_id": "asr-open-request",
        "error_code": "InvalidParameter",
        "error_message": "Authorization: Bearer [REDACTED]",
    }
    assert "sk-secret" not in str(caught.value)
    assert "sk-secret" not in json.dumps(details)
    assert socket.closed is True


@pytest.mark.parametrize("operation", ["send_audio", "finish"])
async def test_asr_send_failures_are_wrapped_and_close_the_socket(operation: str) -> None:
    socket = FailingStreamSocket(fail_send_after_start=True)
    provider = AliyunSpeechProvider(credential_store(), connector=FakeConnector(socket))
    stream = await provider.open_asr()

    with pytest.raises(RuntimeSpeechError, match="实时语音识别连接中断") as caught:
        if operation == "send_audio":
            await stream.send_audio(b"pcm-frame")
        else:
            await stream.finish()

    assert "sk-secret" not in str(caught.value)
    assert socket.closed is True


async def test_asr_receive_failure_is_wrapped_and_closes_the_socket() -> None:
    socket = FailingStreamSocket(fail_receive=True)
    provider = AliyunSpeechProvider(credential_store(), connector=FakeConnector(socket))
    stream = await provider.open_asr()

    with pytest.raises(RuntimeSpeechError, match="实时语音识别连接中断") as caught:
        await stream.receive_sentence()

    assert "sk-secret" not in str(caught.value)
    assert socket.closed is True


async def test_asr_task_failure_after_start_closes_the_socket() -> None:
    failure = task_failed_event(
        task_id="asr-receive-task",
        error_code="CLIENT_ERROR",
        error_message="request timeout after 23 seconds.",
    )
    socket = FakeWebSocket([event("task-started"), failure])
    provider = AliyunSpeechProvider(credential_store(), connector=FakeConnector(socket))
    stream = await provider.open_asr()

    with pytest.raises(RuntimeSpeechError, match="实时语音识别任务失败") as caught:
        await stream.receive_sentence()

    request_id, details = failure_evidence(caught.value)
    assert request_id == "asr-receive-task"
    assert details == {
        "event": "task-failed",
        "task_id": "asr-receive-task",
        "error_code": "CLIENT_ERROR",
        "error_message": "request timeout after 23 seconds.",
    }
    assert "sk-secret" not in str(caught.value)
    assert socket.closed is True


async def collect_audio(chunks: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in chunks]


async def test_tts_streams_binary_audio_after_official_sentence_event() -> None:
    socket = FakeWebSocket(
        [
            event("task-started"),
            event(
                "result-generated",
                payload={
                    "output": {
                        "type": "sentence-synthesis",
                        "sentence": {"index": 0, "words": []},
                    }
                },
            ),
            b"pcm-one",
            event("task-finished"),
        ]
    )
    connector = FakeConnector(socket)
    provider = AliyunSpeechProvider(credential_store(), connector=connector)

    chunks = await collect_audio(
        provider.synthesize(
            "嗯……你先别挂。",
            instruction="声音压低一些，语速自然，像刚哭过但仍在努力说清楚。",
        )
    )

    assert chunks == [b"pcm-one"]
    assert connector.calls[0][0] == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    run_task = decode_sent(socket, 0)
    assert run_task["payload"] == {
        "task_group": "audio",
        "task": "tts",
        "function": "SpeechSynthesizer",
        "model": "qwen-audio-3.0-tts-plus",
        "parameters": {
            "text_type": "PlainText",
            "voice": "longanlingxin",
            "format": "pcm",
            "sample_rate": 24000,
            "instruction": "声音压低一些，语速自然，像刚哭过但仍在努力说清楚。",
        },
        "input": {},
    }
    assert decode_sent(socket, 1)["header"]["action"] == "continue-task"
    assert decode_sent(socket, 1)["payload"] == {"input": {"text": "嗯……你先别挂。"}}
    assert decode_sent(socket, 2)["header"]["action"] == "finish-task"
    assert socket.closed is True


async def test_tts_task_failure_raises_a_safe_chinese_error() -> None:
    socket = FakeWebSocket(
        [
            event("task-started"),
            task_failed_event(
                task_id="tts-task",
                request_id="tts-request",
                error_code="InvalidParameter",
                error_message="DASHSCOPE_API_KEY=test-speech-provider-key",
            ),
        ]
    )
    provider = AliyunSpeechProvider(credential_store(), connector=FakeConnector(socket))

    with pytest.raises(RuntimeSpeechError, match="语音合成任务失败") as caught:
        await collect_audio(provider.synthesize("你好", instruction="自然说话"))

    request_id, details = failure_evidence(caught.value)
    assert request_id == "tts-request"
    assert details == {
        "event": "task-failed",
        "task_id": "tts-task",
        "request_id": "tts-request",
        "error_code": "InvalidParameter",
        "error_message": "DASHSCOPE_API_KEY=[REDACTED]",
    }
    assert "sk-secret" not in str(caught.value)
    assert "sk-secret" not in json.dumps(details)
    assert socket.closed is True


def test_speech_metric_record_keeps_raw_timing_evidence(test_engine: Engine) -> None:
    SQLModel.metadata.create_all(test_engine)
    session_record = SessionRecord(
        id="session-1",
        mode=SessionMode.assessment,
        scene=Scene.hotline,
        case_type=CaseType.main,
        case_id="case-1",
        media=Media.voice,
        model_mode=ModelMode.live,
    )
    turn = TurnRecord(
        id="turn-1",
        session_id=session_record.id,
        client_turn_id="client-turn-1",
        sequence=1,
        speaker=TurnSpeaker.worker,
        text="我……我想先确认一下，你现在在哪里？",
    )
    metric = SpeechMetricRecord(
        session_id=session_record.id,
        turn_id=turn.id,
        first_response_ms=1450,
        speech_duration_ms=4300,
        pause_durations_ms=[680, 1300],
        supplement_count=1,
        speech_rate=3.7,
        overlap_duration_ms=240,
        excluded_technical_ms=800,
        asr_sentences_json=[
            {"text": "我想先确认一下", "begin_time_ms": 0, "end_time_ms": 1800}
        ],
    )

    with Session(test_engine) as db:
        db.add(session_record)
        db.add(turn)
        db.add(metric)
        db.commit()
        db.refresh(metric)

    assert metric.pause_durations_ms == [680, 1300]
    assert metric.asr_sentences_json[0]["end_time_ms"] == 1800
    assert AudioKind.worker_turn.value == "worker_turn"
    assert AudioKind.client_turn.value == "client_turn"

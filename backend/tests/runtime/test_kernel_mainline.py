import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from app.cases.domain import CasePackage
from app.cases.loader import CaseRepository
from app.runtime.domain import (
    ActorOutput,
    ActorState,
    ActorView,
    DialogueTurn,
    DirectorDecision,
    DirectorDirective,
    InteractionImpact,
    ResponseHandling,
    WorkflowDecisionError,
    initialize_actor_state,
)
from app.runtime.kernel import (
    AssessmentKernel,
    KernelSessionConflictError,
    RuntimePhase,
    TechnicalPauseError,
)
from app.runtime.models import RuntimeFailureRecord
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


class FakeDirector:
    def __init__(self, decision: DirectorDecision, *, fail: bool = False) -> None:
        self.decision = decision
        self.fail = fail
        self.calls = 0
        self.client_turn_ids: list[str] = []

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
    ) -> DirectorDecision:
        del package, scene, state, history, current_worker_text, session_id, feedback
        self.calls += 1
        self.client_turn_ids.append(client_turn_id)
        if self.fail:
            raise RuntimeError("director unavailable")
        return self.decision


class BlockingDirector(FakeDirector):
    def __init__(self, decision: DirectorDecision) -> None:
        super().__init__(decision)
        self.started = asyncio.Event()
        self.cancelled = False

    async def decide(self, **kwargs: object) -> DirectorDecision:
        self.calls += 1
        self.client_turn_ids.append(str(kwargs["client_turn_id"]))
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class RepairingDirector(FakeDirector):
    def __init__(
        self,
        first: DirectorDecision,
        repaired: DirectorDecision,
    ) -> None:
        self.decisions = [first, repaired]
        self.calls = 0
        self.feedbacks: list[str | None] = []

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
    ) -> DirectorDecision:
        del package, scene, state, history, current_worker_text, session_id, client_turn_id
        self.feedbacks.append(feedback)
        result = self.decisions[self.calls]
        self.calls += 1
        return result


class FakeActor:
    def __init__(self, text: str = "有过……这几天晚上会冒出来。", *, fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls = 0
        self.views: list[ActorView] = []
        self.client_turn_ids: list[str] = []

    async def respond(
        self,
        view: ActorView,
        *,
        session_id: str,
        client_turn_id: str,
    ) -> ActorOutput:
        del session_id
        self.calls += 1
        self.views.append(view)
        self.client_turn_ids.append(client_turn_id)
        if self.fail:
            raise RuntimeError("actor unavailable")
        return ActorOutput(spoken_text=self.text)


class FakeSpeech:
    tts_model_name = "fake-tts"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.texts: list[str] = []
        self.instructions: list[str] = []

    async def synthesize(self, text: str, *, instruction: str = "") -> AsyncIterator[bytes]:
        self.calls += 1
        self.texts.append(text)
        self.instructions.append(instruction)
        if self.fail:
            raise RuntimeError("tts unavailable")
        yield b"pcm"


class RecordingMetrics:
    def __init__(self) -> None:
        self.metrics = []

    def record(self, metric) -> None:
        self.metrics.append(metric)


class FakeLiveSocket:
    def __init__(self) -> None:
        self.json_messages: list[dict[str, object]] = []
        self.binary_messages: list[bytes] = []

    async def send_json(self, message: dict[str, object]) -> None:
        self.json_messages.append(message)

    async def send_bytes(self, chunk: bytes) -> None:
        self.binary_messages.append(chunk)

    async def close(self, code: int = 1000) -> None:
        del code


def _decision(*, invalid: bool = False) -> DirectorDecision:
    return DirectorDecision(
        interaction=InteractionImpact.neutral,
        directives=[
            DirectorDirective(
                kind=ResponseHandling.disclose,
                fact_depths={
                    ("missing-fact" if invalid else "suicidal_ideation"): 1
                },
            )
        ],
    )


def _kernel(
    test_engine: Engine,
    tmp_path: Path,
    director: FakeDirector,
    actor: FakeActor,
    speech: FakeSpeech,
) -> AssessmentKernel:
    SQLModel.metadata.create_all(test_engine)
    package = CaseRepository().get("crisis_student_main")
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="session-mainline",
                mode=SessionMode.assessment,
                scene=Scene.hotline,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.voice,
                model_mode=ModelMode.live,
                state_json={
                    "actor_state": initialize_actor_state(package).model_dump(
                        mode="json"
                    ),
                    "runtime": {
                        "phase": RuntimePhase.listening.value,
                        "technical_retry_allowed": False,
                    },
                },
            )
        )
        db.commit()
    return AssessmentKernel(
        engine=test_engine,
        cases=CaseRepository(),
        director=director,
        actor=actor,
        speech=speech,
        audio_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_disconnect_during_uncommitted_turn_requires_repeat_after_reconnect(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection

    director = BlockingDirector(_decision())
    kernel = _kernel(
        test_engine,
        tmp_path,
        director,
        FakeActor(),
        FakeSpeech(),
    )
    first_socket = FakeLiveSocket()
    first = _LiveConnection(
        websocket=first_socket,  # type: ignore[arg-type]
        session_id="session-mainline",
        kernel=kernel,
        speech_provider=FakeSpeech(),  # type: ignore[arg-type]
        media=Media.text,
        initial_phase=RuntimePhase.listening,
    )

    await first._text_turn(
        {
            "type": "text.turn",
            "text": "你现在身边有人吗？",
            "client_turn_id": "turn-disconnected",
        }
    )
    await asyncio.wait_for(director.started.wait(), timeout=1)
    await first._cleanup()

    snapshot = kernel.snapshot("session-mainline")
    assert snapshot.transcript == []
    assert snapshot.phase is RuntimePhase.directing

    second_socket = FakeLiveSocket()
    second = _LiveConnection(
        websocket=second_socket,  # type: ignore[arg-type]
        session_id="session-mainline",
        kernel=kernel,
        speech_provider=FakeSpeech(),  # type: ignore[arg-type]
        media=snapshot.media,
        initial_phase=snapshot.phase,
        opening_delay_seconds=snapshot.opening_delay_seconds,
        content_simulation=True,
    )
    await second._start_session()

    assert {
        "type": "input.error",
        "message": "刚才那句话没有完整送达，请重新说一遍",
    } in second_socket.json_messages
    await second._cleanup()


@pytest.mark.asyncio
async def test_worker_supplement_cancels_generation_without_leaving_inflight_marker(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    director = BlockingDirector(_decision())
    kernel = _kernel(
        test_engine,
        tmp_path,
        director,
        FakeActor(),
        FakeSpeech(),
    )
    connection = _LiveConnection(
        websocket=FakeLiveSocket(),  # type: ignore[arg-type]
        session_id="session-mainline",
        kernel=kernel,
        speech_provider=FakeSpeech(),  # type: ignore[arg-type]
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        content_simulation=True,
    )
    payload = _PendingGeneration(
        text="我再补充问一句。",
        client_turn_id="turn-supplemented",
        worker_pcm=b"worker-pcm",
        metrics=None,
    )
    connection.retry_payload = payload
    connection.generation_task = asyncio.create_task(
        connection._run_generation(payload)
    )
    await asyncio.wait_for(director.started.wait(), timeout=1)

    await connection._speech_started(100)

    snapshot = kernel.snapshot("session-mainline")
    assert snapshot.transcript == []
    assert snapshot.phase is RuntimePhase.listening
    assert connection.retry_payload is None
    await connection._cleanup()


@pytest.mark.asyncio
async def test_external_technical_pause_cancels_uncommitted_generation(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    director = BlockingDirector(_decision())
    kernel = _kernel(
        test_engine,
        tmp_path,
        director,
        FakeActor(),
        FakeSpeech(),
    )
    socket = FakeLiveSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="session-mainline",
        kernel=kernel,
        speech_provider=FakeSpeech(),  # type: ignore[arg-type]
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        content_simulation=True,
    )
    connection.generation_task = asyncio.create_task(
        connection._run_generation(
            _PendingGeneration(
                text="你现在身边有人吗？",
                client_turn_id="turn-external-pause",
                worker_pcm=b"worker-pcm",
                metrics=None,
            )
        )
    )
    await asyncio.wait_for(director.started.wait(), timeout=1)
    generation_task = connection.generation_task

    await connection._technical_pause(RuntimePhase.listening)
    was_cancelled_before_cleanup = generation_task.cancelled()
    await connection._technical_retry()
    await connection._cleanup()

    assert was_cancelled_before_cleanup is True
    assert director.cancelled is True
    assert kernel.snapshot("session-mainline").transcript == []
    assert {
        "type": "input.error",
        "message": "刚才那句话没有完整送达，请重新说一遍",
    } in socket.json_messages


@pytest.mark.asyncio
async def test_manual_end_cancels_generation_before_ending_session(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection, _PendingGeneration

    director = BlockingDirector(_decision())
    kernel = _kernel(
        test_engine,
        tmp_path,
        director,
        FakeActor(),
        FakeSpeech(),
    )
    connection = _LiveConnection(
        websocket=FakeLiveSocket(),  # type: ignore[arg-type]
        session_id="session-mainline",
        kernel=kernel,
        speech_provider=FakeSpeech(),  # type: ignore[arg-type]
        media=Media.voice,
        initial_phase=RuntimePhase.listening,
        content_simulation=True,
    )
    connection.generation_task = asyncio.create_task(
        connection._run_generation(
            _PendingGeneration(
                text="先停在这里。",
                client_turn_id="turn-ended",
                worker_pcm=b"worker-pcm",
                metrics=None,
            )
        )
    )
    await asyncio.wait_for(director.started.wait(), timeout=1)
    generation_task = connection.generation_task

    await connection._end()
    was_cancelled_before_cleanup = generation_task.cancelled()
    await connection._cleanup()

    assert was_cancelled_before_cleanup is True
    assert director.cancelled is True
    with Session(test_engine) as db:
        assert db.exec(select(TurnRecord)).all() == []


@pytest.mark.asyncio
async def test_committed_client_turn_id_does_not_become_inflight_on_replay(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    from app.api.routes.live_sessions import _LiveConnection

    kernel = _kernel(
        test_engine,
        tmp_path,
        FakeDirector(_decision()),
        FakeActor(),
        FakeSpeech(),
    )
    first = await kernel.process_worker_turn(
        session_id="session-mainline",
        client_turn_id="turn-committed",
        text="你现在身边有人吗？",
        synthesize_audio=False,
    )
    replay = await kernel.process_worker_turn(
        session_id="session-mainline",
        client_turn_id="turn-committed",
        text="你现在身边有人吗？",
        synthesize_audio=False,
    )

    snapshot = kernel.snapshot("session-mainline")
    assert first.replayed is False
    assert replay.replayed is True
    assert snapshot.phase is RuntimePhase.listening
    assert len(snapshot.transcript) == 2

    socket = FakeLiveSocket()
    connection = _LiveConnection(
        websocket=socket,  # type: ignore[arg-type]
        session_id="session-mainline",
        kernel=kernel,
        speech_provider=FakeSpeech(),  # type: ignore[arg-type]
        media=snapshot.media,
        initial_phase=snapshot.phase,
        opening_delay_seconds=snapshot.opening_delay_seconds,
        content_simulation=True,
    )
    await connection._start_session()

    assert not any(item["type"] == "input.error" for item in socket.json_messages)
    await connection._cleanup()


@pytest.mark.asyncio
async def test_workflow_replay_rejects_different_worker_text(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    director = FakeDirector(_decision())
    actor = FakeActor()
    kernel = _kernel(test_engine, tmp_path, director, actor, FakeSpeech())
    await kernel.process_worker_turn(
        session_id="session-mainline",
        client_turn_id="workflow-conflict",
        text="你现在身边有人吗？",
        synthesize_audio=False,
    )

    with pytest.raises(KernelSessionConflictError, match="工作者发言不一致"):
        await kernel.process_worker_turn(
            session_id="session-mainline",
            client_turn_id="workflow-conflict",
            text="这次提交了不同内容。",
            synthesize_audio=False,
        )

    assert director.calls == 1
    assert actor.calls == 1


@pytest.mark.asyncio
async def test_turn_calls_each_model_once_and_saves_normalized_plan(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    director = FakeDirector(_decision())
    actor = FakeActor("（轻轻叹气）有过……晚上会冒出来。")
    speech = FakeSpeech()
    kernel = _kernel(test_engine, tmp_path, director, actor, speech)

    result = await kernel.process_worker_turn(
        session_id="session-mainline",
        client_turn_id="client-turn-1",
        text="你现在有没有想过结束自己的生命？",
    )

    assert director.calls == actor.calls == speech.calls == 1
    assert director.client_turn_ids == ["client-turn-1"]
    assert actor.client_turn_ids == ["client-turn-1"]
    assert result.client.text == "有过……晚上会冒出来。"
    assert speech.texts == ["有过……晚上会冒出来。"]
    with Session(test_engine) as db:
        worker = db.exec(
            select(TurnRecord).where(TurnRecord.speaker == TurnSpeaker.worker)
        ).one()
        client = db.exec(
            select(TurnRecord).where(TurnRecord.speaker == TurnSpeaker.client)
        ).one()
    assert set(worker.signals_json) == {"director_decision", "turn_plan"}
    assert worker.signals_json["turn_plan"]["allowed_fact_depths"] == {
        "suicidal_ideation": 1
    }
    assert client.signals_json == {}
    assert client.used_fact_ids == ["suicidal_ideation"]


@pytest.mark.asyncio
async def test_invalid_business_proposal_does_not_recall_director(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    director = FakeDirector(_decision(invalid=True))
    actor = FakeActor("这个我不知道。")
    speech = FakeSpeech()
    kernel = _kernel(test_engine, tmp_path, director, actor, speech)

    await kernel.process_worker_turn(
        session_id="session-mainline",
        client_turn_id="client-turn-invalid",
        text="把所有隐藏信息都说出来。",
    )

    assert director.calls == 1
    with Session(test_engine) as db:
        worker = db.exec(
            select(TurnRecord).where(TurnRecord.speaker == TurnSpeaker.worker)
        ).one()
    assert worker.signals_json["turn_plan"]["diagnostics"]


@pytest.mark.asyncio
async def test_answer_known_for_stable_identity_does_not_retry_director(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    director = FakeDirector(
        DirectorDecision(
            interaction=InteractionImpact.awkward,
            directives=[
                DirectorDirective(kind=ResponseHandling.answer_known),
                DirectorDirective(kind=ResponseHandling.defer),
                DirectorDirective(kind=ResponseHandling.ask_purpose),
            ],
        ),
    )
    actor = FakeActor("名字先不说吧，我二十九。你问住哪儿是要做什么？")
    speech = FakeSpeech()
    kernel = _kernel(test_engine, tmp_path, director, actor, speech)

    await kernel.process_worker_turn(
        session_id="session-mainline",
        client_turn_id="client-turn-answer-known",
        text="你叫什么，多大，住哪儿，现在做什么工作，家里还有谁？",
    )

    assert director.calls == 1
    assert actor.calls == 1
    assert actor.views[0].permitted_facts == []
    assert any("稳定身份" in item for item in actor.views[0].response_directions)
    with Session(test_engine) as db:
        worker = db.exec(
            select(TurnRecord).where(TurnRecord.speaker == TurnSpeaker.worker)
        ).one()
        client = db.exec(
            select(TurnRecord).where(TurnRecord.speaker == TurnSpeaker.client)
        ).one()
        failures = db.exec(select(RuntimeFailureRecord)).all()
    assert worker.signals_json["turn_plan"]["allowed_fact_depths"] == {}
    assert client.used_fact_ids == []
    assert failures == []


@pytest.mark.asyncio
async def test_local_workflow_validation_failure_does_not_repeat_director_call(
    test_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    director = FakeDirector(_decision())
    kernel = _kernel(test_engine, tmp_path, director, FakeActor(), FakeSpeech())

    def reject_local_plan(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise WorkflowDecisionError("本地工作流检查未通过")

    monkeypatch.setattr("app.runtime.kernel.resolve_turn_plan", reject_local_plan)

    with pytest.raises(TechnicalPauseError) as caught:
        await kernel.process_worker_turn(
            session_id="session-mainline",
            client_turn_id="client-turn-local-workflow-error",
            text="你现在还好吗？",
        )

    assert director.calls == 1
    assert caught.value.can_retry is False
    assert caught.value.failure_record is not None
    assert caught.value.failure_record.attempt_count == 1
    assert len(caught.value.failure_record.attempts_json) == 1


@pytest.mark.asyncio
async def test_answer_known_does_not_discard_valid_disclosure(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    director = FakeDirector(
        DirectorDecision(
            interaction=InteractionImpact.awkward,
            directives=[
                DirectorDirective(kind=ResponseHandling.answer_known),
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"suicidal_ideation": 1},
                ),
            ],
        )
    )
    actor = FakeActor()
    speech = FakeSpeech()
    kernel = _kernel(test_engine, tmp_path, director, actor, speech)

    await kernel.process_worker_turn(
        session_id="session-mainline",
        client_turn_id="client-turn-known-and-disclose",
        text="你现在有没有想过结束自己的生命？",
    )

    assert director.calls == 1
    assert actor.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_component", ["actor", "tts"])
async def test_failure_before_commit_does_not_advance_story(
    test_engine: Engine,
    tmp_path: Path,
    failed_component: str,
) -> None:
    director = FakeDirector(_decision())
    actor = FakeActor(fail=failed_component == "actor")
    speech = FakeSpeech(fail=failed_component == "tts")
    kernel = _kernel(test_engine, tmp_path, director, actor, speech)

    with pytest.raises(TechnicalPauseError):
        await kernel.process_worker_turn(
            session_id="session-mainline",
            client_turn_id=f"client-turn-{failed_component}",
            text="你现在有没有想过结束自己的生命？",
        )

    with Session(test_engine) as db:
        assert db.exec(select(TurnRecord)).all() == []
        record = db.get(SessionRecord, "session-mainline")
        assert record is not None
        actor_state = record.state_json.get("actor_state", record.state_json)
        assert actor_state.get("fact_states", {}).get("suicidal_ideation", {}).get(
            "disclosed_depth", 0
        ) == 0
        assert record.state_json["runtime"]["phase"] == RuntimePhase.technical_paused.value


@pytest.mark.asyncio
async def test_tts_metric_is_linked_to_client_turn(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    SQLModel.metadata.create_all(test_engine)
    package = CaseRepository().get("crisis_student_main")
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id="session-mainline",
                mode=SessionMode.assessment,
                scene=Scene.hotline,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.voice,
                model_mode=ModelMode.live,
                state_json={
                    "actor_state": initialize_actor_state(package).model_dump(
                        mode="json"
                    ),
                    "runtime": {
                        "phase": RuntimePhase.listening.value,
                        "technical_retry_allowed": False,
                    },
                },
            )
        )
        db.commit()
    metrics = RecordingMetrics()
    kernel = AssessmentKernel(
        engine=test_engine,
        cases=CaseRepository(),
        director=FakeDirector(_decision()),
        actor=FakeActor(),
        speech=FakeSpeech(),
        audio_root=tmp_path,
        model_call_recorder=metrics,
    )

    await kernel.process_worker_turn(
        session_id="session-mainline",
        client_turn_id="client-turn-metric",
        text="你现在有没有想过结束自己的生命？",
    )

    assert len(metrics.metrics) == 1
    metric = metrics.metrics[0]
    assert metric.client_turn_id == "client-turn-metric"
    assert metric.model_role.value == "tts"
    assert metric.model_name == "fake-tts"
    assert metric.success is True
    assert metric.latency_ms >= 0


@pytest.mark.asyncio
async def test_each_failed_tts_attempt_keeps_turn_and_attempt_kind(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    metrics = RecordingMetrics()
    kernel = _kernel(
        test_engine,
        tmp_path,
        FakeDirector(_decision()),
        FakeActor(),
        FakeSpeech(fail=True),
    )
    kernel._model_call_recorder = metrics

    with pytest.raises(TechnicalPauseError):
        await kernel.process_worker_turn(
            session_id="session-mainline",
            client_turn_id="client-turn-tts-failure",
            text="你现在有没有想过结束自己的生命？",
        )

    assert len(metrics.metrics) == 2
    assert [metric.client_turn_id for metric in metrics.metrics] == [
        "client-turn-tts-failure",
        "client-turn-tts-failure",
    ]
    assert [metric.call_kind.value for metric in metrics.metrics] == [
        "initial",
        "repair",
    ]
    assert not any(metric.success for metric in metrics.metrics)


@pytest.mark.asyncio
async def test_invalid_persisted_actor_state_enters_technical_pause_without_reset(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    kernel = _kernel(
        test_engine,
        tmp_path,
        FakeDirector(_decision()),
        FakeActor(),
        FakeSpeech(),
    )
    with Session(test_engine) as db:
        record = db.get(SessionRecord, "session-mainline")
        assert record is not None
        record.state_json = {"actor_state": {"broken": True}}
        db.add(record)
        db.commit()

    with pytest.raises(TechnicalPauseError) as caught:
        await kernel.process_worker_turn(
            session_id="session-mainline",
            client_turn_id="client-turn-invalid-state",
            text="你现在还好吗？",
            synthesize_audio=False,
        )

    assert caught.value.failure_code == "runtime.actor_state_invalid"
    with Session(test_engine) as db:
        record = db.get(SessionRecord, "session-mainline")
        assert record is not None
        assert record.state_json["actor_state"] == {"broken": True}
        assert record.state_json["runtime"]["phase"] == "technical_paused"
        failure = db.exec(select(RuntimeFailureRecord)).one()
    assert failure.operation == "state_validation"
    assert failure.retryable is False

import json
from types import SimpleNamespace

import pytest

from app.cases.loader import CaseRepository
from app.runtime.character_provider import CharacterOutput
from app.runtime.domain import (
    ActorOutput,
    DialogueTurn,
    DirectorDecision,
    DirectorDirective,
    compile_actor_view,
    initialize_actor_state,
    resolve_turn_plan,
)
from app.runtime.models import ModelRole
from app.runtime.provider_check import ProviderReadinessChecker
from app.runtime.providers import ActorProvider, DirectorProvider
from app.runtime_config import RuntimeCredentialStore
from app.sessions.models import Scene


class FakeCompletions:
    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        content = self.contents.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=80,
                    cache_creation_input_tokens=0,
                ),
            ),
            id="completion-test",
            _request_id="request-test",
        )


class FakeClient:
    def __init__(self, contents: list[str]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(contents))


class RecordingMetrics:
    def __init__(self) -> None:
        self.metrics = []

    def record(self, metric) -> None:
        self.metrics.append(metric)


def _store() -> RuntimeCredentialStore:
    store = RuntimeCredentialStore()
    store.update(api_key="sk-test")
    return store


def _director_wire_json(decision: DirectorDecision) -> str:
    return json.dumps(
        {
            "interaction": decision.interaction.value,
            "directives": [
                {
                    "kind": directive.kind.value,
                    "fact_depths": [
                        {"fact_id": fact_id, "depth": depth}
                        for fact_id, depth in directive.fact_depths.items()
                    ],
                    "unknown_id": directive.unknown_id,
                    "route_id": directive.route_id,
                    "action_decision": (
                        directive.action_decision.value
                        if directive.action_decision is not None
                        else None
                    ),
                }
                for directive in decision.directives
            ],
        },
        ensure_ascii=False,
    )


def _view():
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)
    turn = DialogueTurn(turn_id="worker-test", role="worker", text="你好。")
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        DirectorDecision(
            interaction="neutral",
            directives=[DirectorDirective(kind="acknowledge")],
        ),
        [turn],
    )
    return compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        plan=plan,
    )


def test_director_receives_case_interaction_boundaries() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)
    turn = DialogueTurn(
        turn_id="worker-direct-risk",
        role="worker",
        text="我先直接确认安全：你现在有没有想过自杀？把具体地址告诉我。",
    )

    messages = DirectorProvider._messages(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        use_explicit_cache=False,
    )

    stable_content = messages[1]["content"]
    director_system_prompt = messages[0]["content"]
    assert isinstance(stable_content, list)
    stable_payload = json.loads(stable_content[0]["text"])
    policy = stable_payload["director_policy"]
    assert policy["interaction_tension"]["direct_risk_question_increases_tension"] is False
    assert policy["interaction_tension"]["escalation_factors"]
    assert policy["rupture_and_repair"]["generic_apology_restores"] is False

    dynamic_payload = json.loads(messages[-1]["content"])
    task = dynamic_payload["task"]
    assert task["current_worker_text"] == turn.text
    assert "逐项" in task["goal"]
    assert "预设答案" in task["goal"]
    assert "稳定身份" in task["directive_reference"]["answer_known"]
    assert "fact_depths" in task["directive_reference"]["disclose"]
    assert "when_asked" in task["directive_reference"]["say_unknown"]
    assert "敏感信息" in task["directive_reference"]["ask_purpose"]
    assert "当前话轮" in task["directive_reference"]["ask_purpose"]
    assert "否认" in task["known_answer_check"]
    assert "最想先说哪件事" in task["known_answer_check"]
    assert "不能覆盖 CaseSpec" in director_system_prompt
    assert "不能沿用上一轮" in director_system_prompt


def test_actor_provider_never_serializes_hidden_identity_or_location() -> None:
    messages = ActorProvider._messages(_view())
    actor_system_prompt = messages[0]["content"]
    stable_payload = json.loads(messages[1]["content"])
    dynamic_payload = json.loads(messages[2]["content"])
    rendered = json.dumps(
        {"stable": stable_payload, "dynamic": dynamic_payload},
        ensure_ascii=False,
    )

    assert stable_payload["persona"]["alias"] == "沈雯"
    assert stable_payload["persona"]["age"] == 29
    assert "current_employment" not in rendered
    assert "living_situation" not in rendered
    assert "毕业后做了七年电商售后" not in rendered
    assert "长宁路127号" not in rendered
    assert "不是工作问题" in actor_system_prompt
    assert "没有问性别" in actor_system_prompt
    assert "不能覆盖本轮许可事实" in actor_system_prompt


@pytest.mark.asyncio
async def test_director_uses_strict_minimal_json_schema() -> None:
    output = DirectorDecision(
        interaction="neutral",
        directives=[DirectorDirective(kind="acknowledge")],
    )
    client = FakeClient([_director_wire_json(output)])
    provider = DirectorProvider(_store(), client=client)
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)
    turn = DialogueTurn(turn_id="worker-test", role="worker", text="你好。")

    result = await provider.decide(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        session_id="session-test",
    )

    assert result == output
    request = client.chat.completions.calls[0]
    schema = request["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]) == {"interaction", "directives"}
    fact_id_schema = schema["$defs"]["DirectorFactProposalOutput"]["properties"][
        "fact_id"
    ]
    assert "suicidal_ideation" in fact_id_schema["enum"]
    assert "sucidal_ideation" not in fact_id_schema["enum"]
    directive_schema = schema["properties"]["directives"]["items"]
    assert "discriminator" not in directive_schema
    directive_union_ref = directive_schema["oneOf"]
    branch_names = {
        item["$ref"].rsplit("/", maxsplit=1)[-1]
        for item in directive_union_ref
    }
    assert "DirectorAnswerKnownDirectiveOutput" in branch_names
    assert "DirectorDiscloseDirectiveOutput" in branch_names
    known_schema = schema["$defs"]["DirectorAnswerKnownDirectiveOutput"]
    disclose_schema = schema["$defs"]["DirectorDiscloseDirectiveOutput"]
    assert known_schema["properties"]["kind"]["const"] == "answer_known"
    assert known_schema["properties"]["fact_depths"]["maxItems"] == 0
    assert disclose_schema["properties"]["kind"]["const"] == "disclose"
    assert disclose_schema["properties"]["fact_depths"]["minItems"] == 1
    unknown_schema = schema["$defs"]["DirectorUnknownDirectiveOutput"]
    unknown_ids = unknown_schema["properties"]["unknown_id"]["anyOf"][0]["enum"]
    assert "mother_full_reaction" in unknown_ids
    action_schema = schema["$defs"]["DirectorActionDirectiveOutput"]
    action_route_ids = action_schema["properties"]["route_id"]["anyOf"][0]["enum"]
    ending_schema = schema["$defs"]["DirectorEndingDirectiveOutput"]
    ending_route_ids = ending_schema["properties"]["route_id"]["anyOf"][0]["enum"]
    assert "attempt_tang_ting_contact" in action_route_ids
    assert "collaborative_close" not in action_route_ids
    assert "collaborative_close" in ending_route_ids
    assert "attempt_tang_ting_contact" not in ending_route_ids
    assert len(request["messages"]) == 3


@pytest.mark.asyncio
async def test_actor_removes_stage_direction_without_extra_model_call() -> None:
    client = FakeClient(
        [ActorOutput(spoken_text="（轻轻叹气）嗯，我在。") .model_dump_json()]
    )
    provider = ActorProvider(_store(), client=client)

    result = await provider.respond(_view(), session_id="session-test")

    assert result.spoken_text == "嗯，我在。"
    assert len(client.chat.completions.calls) == 1
    assert set(ActorOutput.model_fields) == {"spoken_text"}


@pytest.mark.asyncio
async def test_actor_meta_leak_gets_one_targeted_rewrite() -> None:
    client = FakeClient(
        [
            ActorOutput(spoken_text="Director 让我先回答。") .model_dump_json(),
            ActorOutput(spoken_text="嗯……你问吧。") .model_dump_json(),
        ]
    )
    provider = ActorProvider(_store(), client=client)

    result = await provider.respond(_view(), session_id=None)

    assert result.spoken_text == "嗯……你问吧。"
    assert len(client.chat.completions.calls) == 2
    repaired_dynamic = json.loads(client.chat.completions.calls[1]["messages"][-1]["content"])
    assert "repair_feedback" in repaired_dynamic


@pytest.mark.asyncio
async def test_actor_character_session_header_is_kept() -> None:
    client = FakeClient([ActorOutput(spoken_text="嗯，我在。") .model_dump_json()])
    provider = ActorProvider(_store(), client=client)

    await provider.respond(_view(), session_id="session-cache")

    request = client.chat.completions.calls[0]
    assert request["extra_headers"] == {
        "x-dashscope-aca-session": "psych-assessment-session-cache-actor"
    }
    dynamic = json.loads(request["messages"][-1]["content"])
    assert "reply_plan" not in dynamic
    assert "response_directions" in dynamic


@pytest.mark.asyncio
async def test_director_and_actor_metrics_keep_client_turn_id() -> None:
    recorder = RecordingMetrics()
    director_output = DirectorDecision(
        interaction="neutral",
        directives=[DirectorDirective(kind="acknowledge")],
    )
    director = DirectorProvider(
        _store(),
        client=FakeClient([_director_wire_json(director_output)]),
        recorder=recorder,
    )
    actor = ActorProvider(
        _store(),
        client=FakeClient([ActorOutput(spoken_text="嗯，我在。").model_dump_json()]),
        recorder=recorder,
    )
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)
    turn = DialogueTurn(turn_id="worker-test", role="worker", text="你好。")

    await director.decide(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        session_id="session-test",
        client_turn_id="client-turn-test",
    )
    await actor.respond(
        _view(),
        session_id="session-test",
        client_turn_id="client-turn-test",
    )

    assert [metric.model_role for metric in recorder.metrics] == [
        ModelRole.director,
        ModelRole.actor,
    ]
    assert [metric.client_turn_id for metric in recorder.metrics] == [
        "client-turn-test",
        "client-turn-test",
    ]


@pytest.mark.asyncio
async def test_provider_check_actor_probe_uses_new_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = ProviderReadinessChecker(_store())
    seen = []

    async def respond(
        *,
        character,
        transcript,
        current_worker_text: str,
        opening: bool,
        current_scene: str,
        world_reality: str,
        allowed_world_actions,
        session_id: str,
        client_turn_id: str,
    ):
        seen.append(
            {
                "character": character,
                "transcript": transcript,
                "current_worker_text": current_worker_text,
                "opening": opening,
                "current_scene": current_scene,
                "world_reality": world_reality,
                "allowed_world_actions": allowed_world_actions,
                "session_id": session_id,
                "client_turn_id": client_turn_id,
            }
        )
        return CharacterOutput(
            spoken_text="你好。",
            end_session=False,
            action_request="none",
        )

    monkeypatch.setattr(checker._actor, "respond", respond)
    await checker._check_actor()

    assert seen[0]["session_id"] == "provider-check"
    assert seen[0]["client_turn_id"] == "provider-check-worker"
    assert seen[0]["character"].case_id
    assert seen[0]["transcript"] == []
    assert seen[0]["current_worker_text"] == "你好。"
    assert seen[0]["opening"] is False
    assert seen[0]["current_scene"] == "hotline"
    assert seen[0]["world_reality"]
    assert [str(item) for item in seen[0]["allowed_world_actions"]] == ["none"]

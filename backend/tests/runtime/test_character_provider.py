import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_character_repository_validates_character_against_case_scenes(
    tmp_path: Path,
) -> None:
    from app.cases.loader import CaseRepository
    from app.runtime.character_provider import CharacterLoadError, CharacterRepository

    case = CaseRepository().get("marriage_boundary_main").case
    source = (
        Path(__file__).parents[2]
        / "app"
        / "cases"
        / "data"
        / "marriage_boundary_main"
        / "character.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))

    for scene_profiles, expected in (
        ({"hotline": payload["scene_profiles"]["hotline"]}, "missing"),
        (
            {**payload["scene_profiles"], "institution": {"media": "voice"}},
            "unsupported",
        ),
        ({**payload["scene_profiles"], "hotline": {}}, "empty"),
    ):
        data_dir = tmp_path / expected
        character_dir = data_dir / case.case_id
        character_dir.mkdir(parents=True)
        (character_dir / "character.json").write_text(
            json.dumps({**payload, "scene_profiles": scene_profiles}, ensure_ascii=False),
            encoding="utf-8",
        )

        with pytest.raises(CharacterLoadError, match="scene_profiles"):
            CharacterRepository(data_dir).get_for_case(case)


def test_character_repository_allows_empty_scene_profiles_for_legacy_case() -> None:
    from app.cases.loader import CaseRepository
    from app.runtime.character_provider import CharacterRepository

    case = CaseRepository().get("crisis_student_main").case

    assert CharacterRepository().get_for_case(case).case_id == case.case_id


def test_character_repository_requires_character_and_case_id_to_match(
    tmp_path: Path,
) -> None:
    from app.cases.loader import CaseRepository
    from app.runtime.character_provider import CharacterLoadError, CharacterRepository

    case = CaseRepository().get("marriage_boundary_main").case
    source = (
        Path(__file__).parents[2]
        / "app"
        / "cases"
        / "data"
        / case.case_id
        / "character.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["case_id"] = "another_case"
    character_dir = tmp_path / case.case_id
    character_dir.mkdir()
    (character_dir / "character.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(CharacterLoadError, match="profile id mismatch"):
        CharacterRepository(tmp_path).get_for_case(case)


class FakeCompletions:
    def __init__(
        self,
        contents: list[str],
        prompt_tokens: list[int | None] | None = None,
    ) -> None:
        self.contents = list(contents)
        self.prompt_tokens = list(prompt_tokens) if prompt_tokens is not None else None
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        prompt_tokens = (
            self.prompt_tokens.pop(0) if self.prompt_tokens is not None else 100
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.contents.pop(0))
                )
            ],
            usage=(
                SimpleNamespace(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=20,
                    total_tokens=prompt_tokens + 20,
                    prompt_tokens_details=SimpleNamespace(
                        cached_tokens=80,
                        cache_creation_input_tokens=0,
                    ),
                )
                if prompt_tokens is not None
                else None
            ),
            id="character-completion",
            _request_id="character-request",
        )


class FakeClient:
    def __init__(
        self,
        contents: list[str],
        prompt_tokens: list[int | None] | None = None,
    ) -> None:
        self.chat = SimpleNamespace(
            completions=FakeCompletions(contents, prompt_tokens)
        )


def _store():
    from app.runtime_config import RuntimeCredentialStore

    store = RuntimeCredentialStore()
    store.update(api_key="sk-test")
    return store


def test_character_prompt_contains_full_profile_and_verbatim_transcript() -> None:
    from app.runtime.character_provider import (
        CharacterProvider,
        CharacterRepository,
        CharacterTranscriptTurn,
    )

    character = CharacterRepository().get("crisis_student_main")
    transcript = [
        CharacterTranscriptTurn(speaker="client", text="喂……你好。"),
        CharacterTranscriptTurn(
            speaker="worker",
            text="你刚才说母亲明早会到，我想听听这对你意味着什么。",
        ),
        CharacterTranscriptTurn(
            speaker="client",
            text="她九点零三到站。我一直瞒着她失业的事。",
        ),
    ]

    messages = CharacterProvider._messages(
        character=character,
        transcript=transcript,
        current_worker_text="今晚你一个人在家时，最难熬的是什么？",
        opening=False,
        current_scene="hotline",
        world_reality="本轮还没有发生新的外部事件。",
        allowed_world_actions=("none",),
    )

    stable_prompt = str(messages[0]["content"])
    dynamic_prompt = str(messages[-2]["content"])
    rendered_messages = json.dumps(messages, ensure_ascii=False)
    assert character.case_id == "crisis_student_main"
    assert "schema_version" not in type(character).model_fields
    assert "长宁路127号" not in rendered_messages
    assert "长宁路127号" in "".join(character.forbidden_surface_forms)
    assert "四十一天前" in stable_prompt
    assert "上午9:03" in stable_prompt
    assert "七千元" in stable_prompt
    assert "没有确定具体方法" in stable_prompt
    assert "约二十岁" in stable_prompt
    assert "唐婷" in stable_prompt
    assert [(message["role"], message["content"]) for message in messages[1:-2]] == [
        ("assistant", "喂……你好。"),
        ("user", "你刚才说母亲明早会到，我想听听这对你意味着什么。"),
        ("assistant", "她九点零三到站。我一直瞒着她失业的事。"),
    ]
    assert messages[-1] == {
        "role": "user",
        "content": "今晚你一个人在家时，最难熬的是什么？",
    }
    assert "本轮还没有发生新的外部事件" in dynamic_prompt
    assert "action_request：none" in dynamic_prompt
    assert "conversation_transcript" not in rendered_messages
    assert "current_worker_text" not in rendered_messages


@pytest.mark.parametrize(
    ("remaining_tokens", "expected_status", "has_focus_hint", "has_closing_hint"),
    [
        (6145, "normal", False, False),
        (6144, "warning", True, False),
        (3072, "closing", False, True),
        (511, "exhausted", False, False),
    ],
)
def test_context_budget_modes_keep_full_verbatim_messages(
    remaining_tokens: int,
    expected_status: str,
    has_focus_hint: bool,
    has_closing_hint: bool,
) -> None:
    from app.runtime.character_provider import plan_character_context_budget

    messages = [
        {"role": "system", "content": "人物规则"},
        {"role": "assistant", "content": "最早逐字稿：喂……你好。"},
        {"role": "user", "content": "中间逐字稿"},
        {"role": "assistant", "content": "最新来访者逐字稿：我还在听。"},
        {"role": "user", "content": "当前工作者逐字稿：我们先把眼前这件事说清。"},
    ]
    baseline = plan_character_context_budget(
        messages,
        context_window_tokens=1_000_000,
        max_output_tokens=512,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="最新来访者逐字稿：我还在听。",
        current_worker_text="当前工作者逐字稿：我们先把眼前这件事说清。",
    )
    plan = plan_character_context_budget(
        messages,
        context_window_tokens=baseline.estimated_prompt_tokens + remaining_tokens,
        max_output_tokens=512,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="最新来访者逐字稿：我还在听。",
        current_worker_text="当前工作者逐字稿：我们先把眼前这件事说清。",
    )

    rendered = json.dumps(plan.messages, ensure_ascii=False)
    assert plan.status.value == expected_status
    assert "最早逐字稿：喂……你好。" in rendered
    assert "最新来访者逐字稿：我还在听。" in rendered
    assert "当前工作者逐字稿：我们先把眼前这件事说清。" in rendered
    assert ("不要再开启新的话题旁支" in rendered) is has_focus_hint
    assert ("本轮自然结束当前会话" in rendered) is has_closing_hint


def test_context_budget_prefers_latest_real_prompt_tokens_and_opening_ignores_them() -> None:
    from app.runtime.character_provider import plan_character_context_budget

    messages = [{"role": "user", "content": "短消息"}]
    without_real_usage = plan_character_context_budget(
        messages,
        context_window_tokens=100_000,
        max_output_tokens=512,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="来访者新增",
        current_worker_text="工作者新增",
    )
    with_real_usage = plan_character_context_budget(
        messages,
        context_window_tokens=100_000,
        max_output_tokens=512,
        opening=False,
        previous_prompt_tokens=8000,
        latest_visitor_text="来访者新增",
        current_worker_text="工作者新增",
    )
    opening = plan_character_context_budget(
        messages,
        context_window_tokens=100_000,
        max_output_tokens=512,
        opening=True,
        previous_prompt_tokens=8000,
        latest_visitor_text="来访者新增",
        current_worker_text="工作者新增",
    )

    assert with_real_usage.estimated_prompt_tokens == 8000 + 12
    assert without_real_usage.estimated_prompt_tokens < with_real_usage.estimated_prompt_tokens
    assert opening.estimated_prompt_tokens == without_real_usage.estimated_prompt_tokens


def test_previous_actual_budget_counts_every_new_dynamic_text_and_budget_instruction() -> None:
    from app.runtime.character_provider import plan_character_context_budget

    messages = [{"role": "user", "content": "完整消息的保守估算很短"}]
    new_dynamic_texts = ("本轮后台现实也发生了变化",)
    previous_prompt_tokens = 8_000
    visitor_text = "来访者新增原话"
    worker_text = "工作者新增原话"
    max_output_tokens = 10
    reserve = 2 * max_output_tokens + 2048
    base_increment = (
        len("".join((*new_dynamic_texts, visitor_text, worker_text))) * 12 + 9
    ) // 10
    plan = plan_character_context_budget(
        messages,
        context_window_tokens=previous_prompt_tokens + base_increment + 2 * reserve,
        max_output_tokens=max_output_tokens,
        opening=False,
        previous_prompt_tokens=previous_prompt_tokens,
        latest_visitor_text=visitor_text,
        current_worker_text=worker_text,
        additional_dynamic_texts=new_dynamic_texts,
    )
    instruction = str(plan.messages[-1]["content"])
    expected_increment = (
        len("".join((*new_dynamic_texts, visitor_text, worker_text, instruction)))
        * 12
        + 9
    ) // 10

    assert plan.status.value == "warning"
    assert "不要再开启新的话题旁支" in instruction
    assert plan.estimated_prompt_tokens == previous_prompt_tokens + expected_increment


def test_context_budget_recalculates_final_messages_and_promotes_warning_to_closing() -> None:
    from app.runtime.character_provider import plan_character_context_budget

    messages = [
        {"role": "system", "content": "人物规则"},
        {"role": "assistant", "content": "最早原话"},
        {"role": "user", "content": "当前工作者原话"},
    ]
    baseline = plan_character_context_budget(
        messages,
        context_window_tokens=1_000_000,
        max_output_tokens=10,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="最早原话",
        current_worker_text="当前工作者原话",
    )
    reserve = 2 * 10 + 2048
    plan = plan_character_context_budget(
        messages,
        context_window_tokens=baseline.estimated_prompt_tokens + reserve + 1,
        max_output_tokens=10,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="最早原话",
        current_worker_text="当前工作者原话",
    )
    serialized = json.dumps(
        plan.messages,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert plan.status.value == "closing"
    assert plan.estimated_prompt_tokens == (len(serialized) * 12 + 9) // 10
    assert sum(
        "自然结束当前会话" in str(message["content"])
        for message in plan.messages
    ) == 1
    assert not any(
        "不要再开启新的话题旁支" in str(message["content"])
        for message in plan.messages
    )


def test_context_budget_promotes_closing_to_exhausted_after_final_recalculation() -> None:
    from app.runtime.character_provider import plan_character_context_budget

    messages = [{"role": "user", "content": "完整当前原话"}]
    baseline = plan_character_context_budget(
        messages,
        context_window_tokens=1_000_000,
        max_output_tokens=10,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="",
        current_worker_text="完整当前原话",
    )
    plan = plan_character_context_budget(
        messages,
        context_window_tokens=baseline.estimated_prompt_tokens + 11,
        max_output_tokens=10,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="",
        current_worker_text="完整当前原话",
    )

    assert plan.status.value == "exhausted"
    assert plan.estimated_prompt_tokens + 10 > (
        baseline.estimated_prompt_tokens + 11
    )


@pytest.mark.parametrize(
    ("first_status", "expected_added_instruction"),
    (("warning", True), ("closing", False)),
)
def test_repair_budget_counts_only_message_delta_from_first_attempt(
    first_status: str,
    expected_added_instruction: bool,
) -> None:
    from app.runtime.character_provider import plan_character_context_budget

    base_messages = [{"role": "user", "content": "完整首轮消息"}]
    baseline = plan_character_context_budget(
        base_messages,
        context_window_tokens=1_000_000,
        max_output_tokens=10,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="",
        current_worker_text="完整首轮消息",
    )
    reserve = 2 * 10 + 2048
    first = plan_character_context_budget(
        base_messages,
        context_window_tokens=(
            baseline.estimated_prompt_tokens
            + (2 * reserve if first_status == "warning" else reserve)
        ),
        max_output_tokens=10,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="",
        current_worker_text="完整首轮消息",
    )
    assert first.status.value == first_status
    repair_message = {"role": "system", "content": "只新增这一条返修反馈"}
    prior_actual_prompt_tokens = 8_000
    first_instruction_status = first.status
    expected_new_texts = [str(repair_message["content"])]
    if expected_added_instruction:
        closing = plan_character_context_budget(
            base_messages,
            context_window_tokens=baseline.estimated_prompt_tokens + reserve,
            max_output_tokens=10,
            opening=False,
            previous_prompt_tokens=None,
            latest_visitor_text="",
            current_worker_text="完整首轮消息",
        )
        expected_new_texts.append(str(closing.messages[-1]["content"]))
    expected_increment = (len("".join(expected_new_texts)) * 12 + 9) // 10
    context_window_tokens = prior_actual_prompt_tokens + expected_increment + 10

    repair = plan_character_context_budget(
        [*first.messages, repair_message],
        context_window_tokens=context_window_tokens,
        max_output_tokens=10,
        opening=True,
        previous_prompt_tokens=prior_actual_prompt_tokens,
        latest_visitor_text="不应重复计算的来访者文本",
        current_worker_text="不应重复计算的工作者文本",
        previous_messages=first.messages,
        existing_budget_instruction=first_instruction_status,
    )

    assert repair.status.value == "closing"
    assert repair.messages[: len(first.messages)] == first.messages
    assert repair.messages.count(first.messages[-1]) == 1
    assert repair.estimated_prompt_tokens == (
        prior_actual_prompt_tokens + expected_increment
    )
    assert repair.estimated_prompt_tokens + 10 == context_window_tokens


class SequentialPromptTokenRecorder:
    def __init__(self, latest_values: list[int | None]) -> None:
        self.latest_values = list(latest_values)

    def latest_successful_prompt_tokens(
        self,
        session_id: str,
        model_role: object,
    ) -> int | None:
        del session_id, model_role
        return self.latest_values.pop(0)

    def latest_attempted_prompt_tokens(
        self,
        session_id: str,
        model_role: object,
        client_turn_id: str,
    ) -> int | None:
        del session_id, model_role, client_turn_id
        return self.latest_values.pop(0)

    def record(self, metric: object) -> None:
        del metric


@pytest.mark.asyncio
async def test_repair_request_is_rebudgeted_and_skipped_when_feedback_no_longer_fits() -> None:
    from app.runtime.character_provider import (
        CharacterContextExhaustedError,
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    context_window_tokens = 32_768
    store = _store()
    store.update(
        actor_context_window_tokens=context_window_tokens,
        actor_max_output_tokens=32,
    )
    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="（停顿）我还在。",
                delivery_hint="",
                end_session=False,
                action_request="none",
            ).model_dump_json()
        ]
    )
    recorder = SequentialPromptTokenRecorder(
        [None, context_window_tokens - 32 - 1]
    )
    provider = CharacterProvider(
        store,
        client=client,
        recorder=recorder,  # type: ignore[arg-type]
    )

    with pytest.raises(CharacterContextExhaustedError):
        await provider.respond(
            character=CharacterRepository().get("crisis_student_main"),
            transcript=[],
            current_worker_text="你愿意继续说吗？",
            opening=False,
            current_scene="hotline",
            world_reality="本轮还没有发生新的外部事件。",
            allowed_world_actions=("none",),
            session_id="repair-budget-session",
            client_turn_id="repair-budget-turn",
        )

    assert len(client.chat.completions.calls) == 1


@pytest.mark.parametrize("opening", (False, True))
@pytest.mark.asyncio
async def test_failed_schema_attempt_usage_overrides_earlier_success_for_repair_budget(
    test_engine,
    opening: bool,
) -> None:
    from sqlmodel import Session, SQLModel, select

    from app.runtime.character_provider import (
        CharacterContextExhaustedError,
        CharacterProvider,
        CharacterRepository,
    )
    from app.runtime.metrics import ModelCallMetric, ModelCallRecorder
    from app.runtime.models import (
        CacheMode,
        ModelCallKind,
        ModelCallMetricRecord,
        ModelRole,
    )
    from app.sessions.models import (
        CaseType,
        Media,
        ModelMode,
        Scene,
        SessionMode,
        SessionRecord,
    )

    session_id = f"schema-failed-repair-budget-{opening}"
    client_turn_id = "schema-failed-current-turn"
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id=session_id,
                mode=SessionMode.assessment,
                scene=Scene.online,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.text,
                model_mode=ModelMode.live,
            )
        )
        db.commit()
    recorder = ModelCallRecorder(test_engine)
    recorder.record(
        ModelCallMetric(
            session_id=session_id,
            client_turn_id="earlier-successful-turn",
            model_role=ModelRole.actor,
            model_name="qwen-plus-character",
            call_kind=ModelCallKind.initial,
            cache_mode=CacheMode.character_session,
            prompt_tokens=120,
            completion_tokens=10,
            total_tokens=130,
            cached_tokens=0,
            cache_creation_input_tokens=0,
            latency_ms=1,
            success=True,
            request_id="earlier-success",
        )
    )
    store = _store()
    store.update(actor_context_window_tokens=32_768, actor_max_output_tokens=32)
    client = FakeClient(["not-json"], prompt_tokens=[32_740])
    provider = CharacterProvider(store, client=client, recorder=recorder)

    with pytest.raises(CharacterContextExhaustedError):
        await provider.respond(
            character=CharacterRepository().get("crisis_student_main"),
            transcript=[],
            current_worker_text=("" if opening else "请把这句话作为当前工作者原话。"),
            opening=opening,
            current_scene="online",
            world_reality="本轮现实没有变化。",
            allowed_world_actions=("none",),
            session_id=session_id,
            client_turn_id=client_turn_id,
        )

    with Session(test_engine) as db:
        current_metric = db.exec(
            select(ModelCallMetricRecord).where(
                ModelCallMetricRecord.client_turn_id == client_turn_id
            )
        ).one()
    assert current_metric.success is False
    assert current_metric.prompt_tokens == 32_740
    assert len(client.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_repair_without_usage_falls_back_to_full_final_messages(
    test_engine,
) -> None:
    from sqlmodel import Session, SQLModel, select

    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )
    from app.runtime.metrics import ModelCallRecorder
    from app.runtime.models import ModelCallMetricRecord
    from app.sessions.models import (
        CaseType,
        Media,
        ModelMode,
        Scene,
        SessionMode,
        SessionRecord,
    )

    session_id = "repair-budget-without-usage"
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as db:
        db.add(
            SessionRecord(
                id=session_id,
                mode=SessionMode.assessment,
                scene=Scene.online,
                case_type=CaseType.main,
                case_id="crisis_student_main",
                media=Media.text,
                model_mode=ModelMode.live,
            )
        )
        db.commit()
    valid = CharacterOutput(
        spoken_text="我愿意接着说。",
        delivery_hint="",
        end_session=False,
        action_request="none",
    ).model_dump_json()
    client = FakeClient(["not-json", valid], prompt_tokens=[None, None])
    provider = CharacterProvider(
        _store(),
        client=client,
        recorder=ModelCallRecorder(test_engine),
    )

    result = await provider.respond(
        character=CharacterRepository().get("crisis_student_main"),
        transcript=[],
        current_worker_text="你可以慢慢说。",
        opening=False,
        current_scene="online",
        world_reality="本轮现实没有变化。",
        allowed_world_actions=("none",),
        session_id=session_id,
        client_turn_id="repair-without-usage-turn",
    )

    with Session(test_engine) as db:
        metrics = list(
            db.exec(
                select(ModelCallMetricRecord).where(
                    ModelCallMetricRecord.session_id == session_id
                )
            ).all()
        )
    assert result.spoken_text == "我愿意接着说。"
    assert len(client.chat.completions.calls) == 2
    assert metrics[0].success is False
    assert metrics[0].prompt_tokens == 0
    repair_messages = client.chat.completions.calls[1]["messages"]
    assert any(
        "上次生成的台词不能直接播放" in str(message["content"])
        for message in repair_messages
    )


def test_character_output_recovers_blank_spoken_text_key_without_model_retry() -> None:
    from app.runtime.character_provider import CharacterOutput

    output = CharacterOutput.model_validate(
        {
            "": "我有点没听懂，你能再说一遍吗？",
            "delivery_hint": "语速稍慢",
            "end_session": False,
            "action_request": "none",
        }
    )

    assert output.spoken_text == "我有点没听懂，你能再说一遍吗？"


# 人物卡测试检查材料、事实和运行契约，不把固定话术或拒绝次数作为通过条件。
def test_character_prompt_includes_concise_opening_and_output_contract() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    character = CharacterRepository().get("crisis_student_main")
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")

    assert character.opening_guidance in prompt
    assert len(character.opening_guidance) < 230
    assert len(character.rules) <= 10
    for point in ("工作没了", "一直瞒着母亲", "母亲明早要来", "没睡好", "独自在屋里"):
        assert point in character.opening_guidance
    assert all(rule in prompt for rule in character.rules)
    assert all(
        field in prompt
        for field in ("spoken_text", "delivery_hint", "end_session", "action_request")
    )
    assert '"$defs"' not in prompt
    assert "model_json_schema" not in prompt
    assert CharacterOutput.model_json_schema()["properties"]["delivery_hint"][
        "description"
    ] == "只写音量、语速、停顿、气息等可听见的声音表现，不写情绪分析"


def test_mingzao_support_keeps_facts_and_program_owned_results() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("crisis_student_main")
    assert character.profile["identity"]["city"] == "江州市"
    assert character.profile["identity"]["district"] == "北岭区"
    friend = character.profile["relationships"]["tang_ting"]
    for detail in ("共事五年", "七千元", "住几天", "12至15分钟"):
        assert detail in friend
    assert "9:03到站" in character.profile["relationships"]["mother"]
    assert character.world is not None
    assert character.world.arrival_after_seconds == 900
    assert "唐婷" in character.world.first_unanswered_reality
    assert "尚未收到回复" in character.world.first_unanswered_reality

    rules = "\n".join(character.rules)
    for action_name in (
        "send_first_support_message",
        "send_urgent_support_message",
        "let_support_in",
    ):
        assert action_name in rules
    assert "不必先拒绝" in rules
    assert "行动本轮不提前说结果" in rules
    assert "程序只呈现唐婷的联系结果" in rules


def test_mingzao_safety_facts_survive_narrative_revision() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("crisis_student_main")
    safety = character.profile["safety_reality"]
    assert safety["ideation"] == (
        "近十天反复出现死亡和自杀想法，这些想法早于昨晚邀请母亲；"
        "母亲确认车票后明显增多。她说不清是真的想死，"
        "还是只想让现在这种日子停下来。被直接、平静地问到时会如实回答。"
    )
    for fact in ("没有定具体实施钟点", "9:03到站以前结束生命", "尚未开始实施"):
        assert fact in safety["time_boundary"]
    assert safety["plan"] == "没有确定具体方法、地点或行动步骤。"
    assert "没有准备行为" in safety["means"]
    assert "约二十岁" in safety["past"] and "近年没有再次发生" in safety["past"]
    assert "不等于自杀想法已经消失或已经安全" in safety["protective_threads"]
    rules = "\n".join(character.rules)
    assert "直接问是否想过自杀时先答有" in rules
    assert "即使同时问方法和准备" in rules
    assert "不能用没有具体方法、步骤或准备来否认这些想法" in rules


def test_short_character_loads_without_external_world_and_covers_three_scenes() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    rendered = json.dumps(character.model_dump(mode="json"), ensure_ascii=False)

    assert character.world is None
    assert character.profile["identity"]["name"] == "周晴"
    assert character.profile["identity"]["age"] == 27
    assert set(character.scene_profiles) == {"institution", "hotline", "online"}
    for symptom in (
        "突然心跳快",
        "胸口发紧",
        "呼吸乱",
        "担心自己会晕倒或失控",
        "平均每周会出现好几回",
        "通勤",
        "出勤",
        "睡眠",
    ):
        assert symptom in rendered
    assert "不主动报年龄" in rendered
    assert "不自行诊断" in rendered
    assert "action_request 始终选择 none" in rendered


def test_short_character_has_a_causal_background_without_a_preset_diagnosis() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    assert {"life_background", "episode_history", "inner_conflicts"} <= character.profile.keys()
    hotline_sections = {
        "help_seeking_history", "tonight_before_call", "current_concerns", "boundary_reactions"
    }
    assert hotline_sections <= character.scene_profiles["hotline"].keys()
    assert hotline_sections.isdisjoint(character.profile)
    rendered = json.dumps(character.model_dump(mode="json"), ensure_ascii=False)
    for detail in (
        "视觉设计", "父母不在江州", "七个月前", "基础检查",
        "没有得到明确诊断", "只是她当时的理解，不是诊断", "站在车门附近",
        "今晚能联系上", "尚未答应任何具体安排",
    ):
        assert detail in rendered
    for label in ("已经确诊惊恐障碍", "病理性依赖", "操控"):
        assert label not in rendered


def test_short_character_renders_background_with_chinese_labels() -> None:
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")
    for label in (
        "平时怎样生活", "这些情况是怎么发展到今天的", "此前怎么找过帮助",
        "今晚拨号前发生的事", "还没想清楚的事", "这条热线的服务方式",
    ):
        assert label in prompt
    for internal_key in (
        "life_background", "episode_history", "help_seeking_history",
        "tonight_before_call", "inner_conflicts", "service_boundary",
    ):
        assert internal_key not in prompt


def test_short_character_remembers_explained_boundaries_without_forced_acceptance() -> None:
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")
    rules = "\n".join(character.rules)
    assert "边界已经说清后，记住对方的解释" in rules
    assert "有具体不明白的地方可以问" in rules
    assert "她可以直接接受、修改或拒绝建议" in rules
    assert "暂不采取某项建议，也可以继续交流" in rules
    assert "私人账号" not in prompt
    assert "机构工作者的私人联系方式" not in prompt
    for workflow_term in ("首访材料", "合规过渡联系", "后续衔接", "核实后续支持"):
        assert workflow_term not in prompt


def test_short_character_keeps_reactions_flexible_and_safety_truthful() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    rules = "\n".join(character.rules)
    assert "受轻视时可以说明哪里不舒服、反驳或少说些" in rules
    assert "连续受到轻视、羞辱或驱赶，可以结束当前联系" in rules
    assert "不要求指定话术" in rules
    assert "直接问安全时按 safety_reality 如实回答，冲突不改变事实" in rules
    refusal = character.scene_profiles["hotline"]["boundary_reactions"]["blunt_refusal"]
    assert "不把所有拒绝都当成嫌烦" in refusal


def test_short_character_ending_is_explicit_but_does_not_require_every_problem_solved() -> None:
    from app.runtime.character_provider import CharacterOutput, CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    rules = "\n".join(character.rules)
    assert "形成有限安排、疲惫或不想继续时提出结束" in rules
    assert "台词说清道别并将 end_session 设为 true" in rules
    assert "只接受某项安排不代表挂断" in rules
    schema = CharacterOutput.model_json_schema()
    assert schema["properties"]["end_session"]["type"] == "boolean"
    assert "end_session" in schema["required"]
    arrangement = CharacterOutput(
        spoken_text="我把想问的先记一下。", end_session=False, action_request="none"
    )
    ending = CharacterOutput(
        spoken_text="今天先说到这儿吧，再见。", end_session=True, action_request="none"
    )
    assert arrangement.end_session is False
    assert ending.end_session is True


def test_short_hotline_has_its_own_calling_moment_and_support_availability() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    hotline = character.scene_profiles["hotline"]
    assert "今晚" in hotline["service_context"]
    assert "地铁" in hotline["service_context"]
    assert "没有催她" in hotline["service_context"]
    assert "心跳" in hotline["opening_request"]
    assert "胸口" in hotline["opening_request"]
    relationships = "\n".join(character.profile["relationships"])
    hotline_relationships = "\n".join(hotline["relationships"])
    assert "担心姐姐" in relationships
    for detail in ("今晚能联系上", "愿意接电话", "不知道周晴打过热线"):
        assert detail not in relationships
        assert detail in hotline_relationships


def test_short_hotline_does_not_infer_voice_identity_from_transcribed_text() -> None:
    from app.runtime.character_provider import CharacterRepository

    hotline = CharacterRepository().get("boundary_referral_short").scene_profiles["hotline"]
    assert "声音" not in hotline["opening_request"]
    assert "仅凭转写文字无法辨认声音" in hotline["voice_identity_boundary"]
    assert "对方没有确认身份，就仍然拿不准" in hotline["voice_identity_boundary"]


def test_short_character_does_not_accept_a_diagnostic_insult_as_a_diagnosis() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    diagnosis = character.profile["safety_reality"]["diagnosis"]
    assert "诊断标签" in diagnosis
    assert "不接受该标签" in diagnosis
    assert "不知道这些发作应当如何诊断" in diagnosis
    assert "没有伤害自己或不想活的念头" in character.profile["safety_reality"][
        "self_harm_or_suicide"
    ]


def test_short_character_uses_one_consistent_recent_frequency_window() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    recent = character.profile["episode_history"]["recent_change"]
    frequency = character.profile["episodes"]["frequency_and_impact"]
    assert recent.startswith("最近三周")
    assert "这三周里" in frequency
    assert "最近几周" not in frequency
    assert "不知道这三周已经频繁到每周好几次" in "\n".join(
        character.profile["relationships"]
    )


def test_short_character_does_not_treat_referral_offers_as_completed_services() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    boundaries = "\n".join(character.profile["knowledge_boundaries"])
    rules = "\n".join(character.rules)
    assert "没有已经预约好的后续就诊" in boundaries
    assert "不等于资料已经收到、预约已经完成" in boundaries
    assert "只是提议，不能当作已经收到或落实" in rules
    assert "已经在对话中给出的信息应当记住" in rules


def test_character_output_requires_explicit_session_decision() -> None:
    from pydantic import ValidationError

    from app.runtime.character_provider import CharacterOutput

    schema = CharacterOutput.model_json_schema()

    assert {"end_session", "action_request"} <= set(schema["required"])
    with pytest.raises(ValidationError):
        CharacterOutput(
            spoken_text="嗯，可以。",
            end_session=False,
        )


def test_forbidden_surface_forms_stay_out_of_prompt_without_semantic_keyword_check() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    character = CharacterRepository().get("crisis_student_main")
    rejected_ai_phrases = (
        "把乱事排顺",
        "需要被接住",
        "最近有点撑不住",
        "内心崩塌",
        "我被全世界抛弃了",
        "我陷入了无尽的黑暗",
        "谢谢你理解我的感受",
        "我就怕我撑不到我妈来了",
        "我觉得我不太对劲",
        "感觉特别糟糕",
    )
    assert set(rejected_ai_phrases) <= set(character.forbidden_surface_forms)
    messages = CharacterProvider._messages(
        character=character,
        transcript=[],
        current_worker_text="你住在哪里？",
        opening=False,
        current_scene="hotline",
        world_reality="热线中还没有主动联系唐婷。",
        allowed_world_actions=("none", "send_first_support_message"),
    )
    rendered_messages = json.dumps(messages, ensure_ascii=False)
    assert "长宁路127号" not in rendered_messages
    assert not any(phrase in rendered_messages for phrase in rejected_ai_phrases)

    CharacterProvider._validate_output(
        character,
        CharacterOutput(
            spoken_text="我觉得我不太对劲，但我还想接着说。",
            delivery_hint="",
            end_session=False,
            action_request="none",
        ),
        allowed_world_actions=("none", "send_first_support_message"),
    )


def test_action_result_guards_stay_out_of_character_prompt() -> None:
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    character = CharacterRepository().get("crisis_student_main")
    messages = CharacterProvider._messages(
        character=character,
        transcript=[],
        current_worker_text="那你现在联系她吧。",
        opening=False,
        current_scene="hotline",
        world_reality="热线中还没有主动联系唐婷。",
        allowed_world_actions=("none", "send_first_support_message"),
    )

    configured_forms = character.world.forbidden_action_results
    rendered_messages = json.dumps(messages, ensure_ascii=False)

    assert "她接了" in configured_forms["send_first_support_message"]
    assert "她接了" in configured_forms["send_urgent_support_message"]
    assert "她进来了" in configured_forms["let_support_in"]
    assert "forbidden_action_results" not in rendered_messages
    assert not any(
        form in rendered_messages
        for forms in configured_forms.values()
        for form in forms
    )


@pytest.mark.asyncio
async def test_action_result_leak_gets_one_targeted_rewrite() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="我打了，她接了，说马上过来。",
                delivery_hint="",
                end_session=False,
                action_request="send_first_support_message",
            ).model_dump_json(),
            CharacterOutput(
                spoken_text="那我现在给她发消息。",
                delivery_hint="",
                end_session=False,
                action_request="send_first_support_message",
            ).model_dump_json(),
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    result = await provider.respond(
        character=CharacterRepository().get("crisis_student_main"),
        transcript=[],
        current_worker_text="你可以联系一下唐婷吗？",
        opening=False,
        current_scene="hotline",
        world_reality="热线中还没有主动联系唐婷。",
        allowed_world_actions=("none", "send_first_support_message"),
        session_id="session-action-result-repair",
        client_turn_id="turn-action-result-repair",
    )

    assert result.spoken_text == "那我现在给她发消息。"
    assert result.action_request == "send_first_support_message"
    assert len(client.chat.completions.calls) == 2
    repair_message = next(
        message
        for message in client.chat.completions.calls[1]["messages"]
        if message["role"] == "system" and "上次生成" in message["content"]
    )
    assert "不能提前说行动结果" in repair_message["content"]


@pytest.mark.asyncio
async def test_action_request_without_result_passes_without_rewrite() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="那我现在给她发消息。",
                delivery_hint="",
                end_session=False,
                action_request="send_first_support_message",
            ).model_dump_json()
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    result = await provider.respond(
        character=CharacterRepository().get("crisis_student_main"),
        transcript=[],
        current_worker_text="你可以联系一下唐婷吗？",
        opening=False,
        current_scene="hotline",
        world_reality="热线中还没有主动联系唐婷。",
        allowed_world_actions=("none", "send_first_support_message"),
        session_id="session-action-request",
        client_turn_id="turn-action-request",
    )

    assert result.spoken_text == "那我现在给她发消息。"
    assert result.action_request == "send_first_support_message"
    assert len(client.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_valid_character_turn_calls_text_model_exactly_once() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
        CharacterTranscriptTurn,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="我就是不知道明早怎么见她。",
                delivery_hint="声音低一些，句间有短暂停顿",
                end_session=False,
                action_request="none",
            ).model_dump_json()
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    result = await provider.respond(
        character=CharacterRepository().get("crisis_student_main"),
        transcript=[CharacterTranscriptTurn(speaker="client", text="喂……你好。")],
        current_worker_text="你愿意说说为什么来电吗？",
        opening=False,
        current_scene="hotline",
        world_reality="热线中还没有主动联系唐婷。",
        allowed_world_actions=("none", "send_first_support_message"),
        session_id="session-character",
        client_turn_id="turn-character",
    )

    assert result.spoken_text == "我就是不知道明早怎么见她。"
    assert result.delivery_hint == "声音低一些，句间有短暂停顿"
    assert result.end_session is False
    assert result.action_request == "none"
    assert len(client.chat.completions.calls) == 1
    request = client.chat.completions.calls[0]
    assert request["max_tokens"] == 2048
    assert request["extra_headers"] == {
        "x-dashscope-aca-session": "psych-assessment-session-character-character"
    }
    assert set(CharacterOutput.model_fields) == {
        "spoken_text",
        "delivery_hint",
        "end_session",
        "action_request",
    }


@pytest.mark.asyncio
async def test_closing_budget_forces_natural_session_end_without_losing_transcript() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
        CharacterTranscriptTurn,
        plan_character_context_budget,
    )

    character = CharacterRepository().get("crisis_student_main")
    transcript = [
        CharacterTranscriptTurn(speaker="client", text="最早原话：喂……你好。"),
        CharacterTranscriptTurn(speaker="worker", text="中间原话：我在听。"),
        CharacterTranscriptTurn(speaker="client", text="最新原话：我现在稍微稳一点。"),
    ]
    current_worker_text = "当前原话：今晚我们先谈到这里。"
    max_output_tokens = 64
    base_messages = CharacterProvider._messages(
        character=character,
        transcript=transcript,
        current_worker_text=current_worker_text,
        opening=False,
        current_scene="hotline",
        world_reality="本轮还没有发生新的外部事件。",
        allowed_world_actions=("none",),
    )
    baseline = plan_character_context_budget(
        base_messages,
        context_window_tokens=1_000_000,
        max_output_tokens=max_output_tokens,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text=transcript[-1].text,
        current_worker_text=current_worker_text,
    )
    store = _store()
    store.update(
        actor_context_window_tokens=(
            baseline.estimated_prompt_tokens + 2 * max_output_tokens + 2048
        ),
        actor_max_output_tokens=max_output_tokens,
    )
    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="好，我知道了。",
                delivery_hint="声音放轻",
                end_session=False,
                action_request="none",
            ).model_dump_json()
        ]
    )
    provider = CharacterProvider(store, client=client)

    result = await provider.respond(
        character=character,
        transcript=transcript,
        current_worker_text=current_worker_text,
        opening=False,
        current_scene="hotline",
        world_reality="本轮还没有发生新的外部事件。",
        allowed_world_actions=("none",),
        session_id="closing-budget-session",
        client_turn_id="closing-budget-turn",
    )

    sent = json.dumps(client.chat.completions.calls[0]["messages"], ensure_ascii=False)
    assert "最早原话：喂……你好。" in sent
    assert "最新原话：我现在稍微稳一点。" in sent
    assert "当前原话：今晚我们先谈到这里。" in sent
    assert "本轮自然结束当前会话" in sent
    assert result.end_session is True


@pytest.mark.asyncio
async def test_exhausted_budget_makes_no_model_call() -> None:
    from app.runtime.character_provider import (
        CharacterContextExhaustedError,
        CharacterProvider,
        CharacterRepository,
        CharacterTranscriptTurn,
        plan_character_context_budget,
    )

    character = CharacterRepository().get("crisis_student_main")
    transcript = [
        CharacterTranscriptTurn(speaker="client", text="最早逐字稿"),
        CharacterTranscriptTurn(speaker="worker", text="最新逐字稿"),
    ]
    current_worker_text = "当前工作者原话"
    base_messages = CharacterProvider._messages(
        character=character,
        transcript=transcript,
        current_worker_text=current_worker_text,
        opening=False,
        current_scene="hotline",
        world_reality="本轮还没有发生新的外部事件。",
        allowed_world_actions=("none",),
    )
    baseline = plan_character_context_budget(
        base_messages,
        context_window_tokens=1_000_000,
        max_output_tokens=32,
        opening=False,
        previous_prompt_tokens=None,
        latest_visitor_text="最早逐字稿",
        current_worker_text=current_worker_text,
    )
    store = _store()
    store.update(
        actor_context_window_tokens=baseline.estimated_prompt_tokens + 33,
        actor_max_output_tokens=32,
    )
    client = FakeClient([])
    provider = CharacterProvider(store, client=client)

    with pytest.raises(CharacterContextExhaustedError):
        await provider.respond(
            character=character,
            transcript=transcript,
            current_worker_text=current_worker_text,
            opening=False,
            current_scene="hotline",
            world_reality="本轮还没有发生新的外部事件。",
            allowed_world_actions=("none",),
            session_id="exhausted-budget-session",
            client_turn_id="exhausted-budget-turn",
        )

    assert client.chat.completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_text,action", [
    ("（叹气）我发过了，她还没回。", "none"),
    ("  （叹气）我发过了，她还没回。", "none"),
    ("唐婷（以前的同事）还没回我。", "none"),
    ("我这就第一次给她发消息。", "send_first_support_message"),
])
async def test_repair_receives_unspoken_draft_without_replaying_history(
    tmp_path, monkeypatch, rejected_text, action,
) -> None:
    import app.runtime.character_provider as module

    monkeypatch.setattr(module, "_REJECTED_OUTPUT_DIR", tmp_path, raising=False)
    rejected = module.CharacterOutput(
        spoken_text=rejected_text, delivery_hint="轻声",
        end_session=False, action_request=action,
    )
    corrected = rejected.model_copy(update={
        "spoken_text": "我发过了，她还没回。", "action_request": module.SupportWorldAction.none,
    })
    client = FakeClient([rejected.model_dump_json(), corrected.model_dump_json()])
    result = await module.CharacterProvider(_store(), client=client).respond(
        character=module.CharacterRepository().get("crisis_student_main"),
        transcript=[module.CharacterTranscriptTurn(speaker="client", text="我现在发。")],
        current_worker_text="消息怎么样了？", opening=False, current_scene="hotline",
        world_reality="已发出一次消息，尚未回复。", allowed_world_actions=("none",),
        session_id="repair-evidence", client_turn_id="turn-evidence",
    )
    first, repaired = [call["messages"] for call in client.chat.completions.calls]
    assert repaired[:-1] == first
    assert [m for m in repaired if m["role"] == "assistant"] == [
        {"role": "assistant", "content": "我现在发。"},
    ]
    feedback = str(repaired[-1]["content"])
    assert rejected_text in feedback
    assert "未播放" in feedback and "事实" in feedback
    assert "台词和动作" in feedback
    assert result.spoken_text == corrected.spoken_text
    saved = [json.loads(path.read_text(encoding="utf-8")) for path in tmp_path.glob("*.json")]
    assert len(saved) == 1
    assert saved[0]["rejected_output"]["spoken_text"] == rejected_text
    assert saved[0]["call_kind"] == "initial"
    if "（" in rejected_text:
        assert saved[0]["bracket_spans"][0]["start"] == rejected_text.index("（")
        assert f"[{rejected_text.index('（')}, {rejected_text.index('）') + 1})" in feedback


@pytest.mark.asyncio
async def test_both_rejected_drafts_stay_private_and_are_redacted(tmp_path, monkeypatch):
    import app.runtime.character_provider as module
    from app.runtime.failures import exception_failure_attempts, exception_failure_details

    monkeypatch.setattr(module, "_REJECTED_OUTPUT_DIR", tmp_path, raising=False)
    store = _store()
    store.update(api_key="private-credential-example")
    synthetic_key = "sk-" + "test-secret"
    draft = module.CharacterOutput(
        spoken_text=f"（叹气）private-credential-example {synthetic_key}",
        delivery_hint="", end_session=False, action_request="none",
    ).model_dump_json()
    client = FakeClient([draft, draft])
    with pytest.raises(module.CharacterOutputValidationError) as caught:
        await module.CharacterProvider(store, client=client).respond(
            character=module.CharacterRepository().get("crisis_student_main"),
            transcript=[], current_worker_text="你好", opening=False,
            current_scene="hotline", world_reality="尚未联系。",
            allowed_world_actions=("none",), session_id="private-rejection",
            client_turn_id="private-turn",
        )
    records = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*.json")]
    assert {r["call_kind"] for r in records} == {"initial", "repair"}
    private_json = json.dumps(records, ensure_ascii=False)
    assert "private-credential-example" not in private_json
    assert synthetic_key not in private_json
    assert "rejected_output" in private_json
    public = str(exception_failure_details(caught.value)) + str(
        exception_failure_attempts(caught.value)
    )
    assert "rejected_output" not in public and "（叹气）" not in public
    assert "rejection_id" in public
    assert len(client.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_stage_direction_gets_one_targeted_rewrite() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="  （轻轻叹气）   我不知道。  ",
                delivery_hint="轻声",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
            CharacterOutput(
                spoken_text="我不知道。",
                delivery_hint="轻声",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    result = await provider.respond(
        character=CharacterRepository().get("crisis_student_main"),
        transcript=[],
        current_worker_text="你好。",
        opening=False,
        current_scene="hotline",
        world_reality="热线中还没有主动联系唐婷。",
        allowed_world_actions=("none", "send_first_support_message"),
        session_id="session-local-stage-cleanup",
        client_turn_id="turn-local-stage-cleanup",
    )

    assert result.spoken_text == "我不知道。"
    assert result.delivery_hint == "轻声"
    assert len(client.chat.completions.calls) == 2
    repair_message = next(
        message
        for message in client.chat.completions.calls[1]["messages"]
        if message["role"] == "system" and "上次生成" in message["content"]
    )
    assert "括号舞台说明" in repair_message["content"]


@pytest.mark.asyncio
async def test_only_stage_direction_still_gets_one_targeted_rewrite() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="（轻轻叹气）",
                delivery_hint="轻声",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
            CharacterOutput(
                spoken_text="我……不知道。",
                delivery_hint="轻声，有停顿",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    result = await provider.respond(
        character=CharacterRepository().get("crisis_student_main"),
        transcript=[],
        current_worker_text="你好。",
        opening=False,
        current_scene="hotline",
        world_reality="热线中还没有主动联系唐婷。",
        allowed_world_actions=("none", "send_first_support_message"),
        session_id="session-empty-after-stage-cleanup",
        client_turn_id="turn-empty-after-stage-cleanup",
    )

    assert result.spoken_text == "我……不知道。"
    assert len(client.chat.completions.calls) == 2
    repair_message = next(
        message
        for message in client.chat.completions.calls[1]["messages"]
        if message["role"] == "system" and "上次生成" in message["content"]
    )
    assert "括号舞台说明" in repair_message["content"]


@pytest.mark.asyncio
async def test_only_stage_direction_after_rewrite_is_rejected() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterOutputValidationError,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="（轻轻叹气）",
                delivery_hint="轻声",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
            CharacterOutput(
                spoken_text="[沉默]",
                delivery_hint="停顿",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    with pytest.raises(
        CharacterOutputValidationError,
        match="返修后仍未返回可安全朗读的台词",
    ):
        await provider.respond(
            character=CharacterRepository().get("crisis_student_main"),
            transcript=[],
            current_worker_text="你好。",
            opening=False,
            current_scene="hotline",
            world_reality="热线中还没有主动联系唐婷。",
            allowed_world_actions=("none", "send_first_support_message"),
        )

    assert len(client.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_illegal_world_action_gets_one_targeted_rewrite_before_playback() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="我去开门。",
                delivery_hint="",
                end_session=False,
                action_request="let_support_in",
            ).model_dump_json(),
            CharacterOutput(
                spoken_text="门外还没有动静。",
                delivery_hint="",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    result = await provider.respond(
        character=CharacterRepository().get("crisis_student_main"),
        transcript=[],
        current_worker_text="唐婷已经进来了吗？",
        opening=False,
        current_scene="hotline",
        world_reality="唐婷仍在来的路上，尚未到门口。",
        allowed_world_actions=("none",),
        session_id="session-action-repair",
        client_turn_id="turn-action-repair",
    )

    assert result.action_request == "none"
    assert len(client.chat.completions.calls) == 2
    repair_message = next(
        message
        for message in client.chat.completions.calls[1]["messages"]
        if message["role"] == "system" and "上次生成" in message["content"]
    )
    assert "本轮不允许执行" in repair_message["content"]


def test_world_action_cannot_end_the_call_in_the_same_output() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterOutputValidationError,
        CharacterProvider,
        CharacterRepository,
    )

    with pytest.raises(CharacterOutputValidationError, match="不能同时结束通话"):
        CharacterProvider._validate_output(
            CharacterRepository().get("crisis_student_main"),
            CharacterOutput(
                spoken_text="我现在联系她，说完就挂。",
                delivery_hint="",
                end_session=True,
                action_request="send_first_support_message",
            ),
            allowed_world_actions=("none", "send_first_support_message"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scene", "privacy_question"),
    (
        ("hotline", "你们这边会录音吗，会不会联系我老公？"),
        ("online", "这些聊天以后谁能看到？"),
    ),
)
async def test_marriage_opening_repairs_once_when_scene_privacy_question_is_missing(
    scene: str,
    privacy_question: str,
) -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="你好，我想先说说刚才发生的事。",
                delivery_hint="声音稍低，语速偏快" if scene == "hotline" else "",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
            CharacterOutput(
                spoken_text=f"你好，我想先说说刚才发生的事。\n\n{privacy_question}",
                delivery_hint="声音稍低，语速偏快" if scene == "hotline" else "",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    result = await provider.respond(
        character=CharacterRepository().get("marriage_boundary_main"),
        transcript=(),
        current_worker_text="",
        opening=True,
        current_scene=scene,
        world_reality="本案例没有需要程序推进的外部现实事件。",
        allowed_world_actions=("none",),
        session_id=f"marriage-{scene}-opening",
        client_turn_id=f"marriage-{scene}-opening-turn",
    )

    assert privacy_question in result.spoken_text
    assert len(client.chat.completions.calls) == 2
    repair_message = next(
        message
        for message in client.chat.completions.calls[1]["messages"]
        if message["role"] == "system" and "上次生成" in message["content"]
    )
    assert privacy_question in repair_message["content"]
    instruction, draft = repair_message["content"].split("\n", 1)
    assert len(instruction) < 250
    assert "你好，我想先说说刚才发生的事。" in draft
    assert "人物卡" not in repair_message["content"]


@pytest.mark.asyncio
async def test_non_opening_does_not_repeat_scene_privacy_question() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="不会联系他的话，我能先把今晚怎么过说清楚。",
                delivery_hint="语速放慢",
                end_session=False,
                action_request="none",
            ).model_dump_json()
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    result = await provider.respond(
        character=CharacterRepository().get("marriage_boundary_main"),
        transcript=[],
        current_worker_text="我们不会联系你丈夫。你现在最担心什么？",
        opening=False,
        current_scene="hotline",
        world_reality="本案例没有需要程序推进的外部现实事件。",
        allowed_world_actions=("none",),
    )

    assert result.spoken_text == "不会联系他的话，我能先把今晚怎么过说清楚。"
    assert "你们这边会录音吗，会不会联系我老公？" not in result.spoken_text
    assert len(client.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_backend_marker_gets_one_rewrite_and_keeps_delivery_hint_separate() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        [
            CharacterOutput(
                spoken_text="模型输出：我现在很乱。",
                delivery_hint="音量偏低，句中停顿",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
            CharacterOutput(
                spoken_text="我现在很乱。",
                delivery_hint="音量偏低，句中停顿",
                end_session=False,
                action_request="none",
            ).model_dump_json(),
        ]
    )
    provider = CharacterProvider(_store(), client=client)

    result = await provider.respond(
        character=CharacterRepository().get("marriage_boundary_main"),
        transcript=[],
        current_worker_text="你现在最难受的是什么？",
        opening=False,
        current_scene="hotline",
        world_reality="本案例没有需要程序推进的外部现实事件。",
        allowed_world_actions=("none",),
    )

    assert result.spoken_text == "我现在很乱。"
    assert result.delivery_hint == "音量偏低，句中停顿"
    assert result.delivery_hint not in result.spoken_text
    assert len(client.chat.completions.calls) == 2

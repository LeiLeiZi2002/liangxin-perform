import json
from types import SimpleNamespace

import pytest


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=self.content))
            ],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            ),
            id="character-completion",
            _request_id="character-request",
        )


class FakeClient:
    def __init__(self, content: str) -> None:
        self.completions = FakeCompletions(content)
        self.chat = SimpleNamespace(completions=self.completions)


def _store():
    from app.runtime_config import RuntimeCredentialStore

    store = RuntimeCredentialStore()
    store.update(api_key="sk-test")
    return store


def test_character_messages_are_a_real_chat_with_verbatim_last_worker_turn() -> None:
    from app.runtime.character_provider import (
        CharacterProvider,
        CharacterRepository,
        CharacterTranscriptTurn,
    )

    character = CharacterRepository().get("crisis_student_main")
    messages = CharacterProvider._messages(
        character=character,
        transcript=(
            CharacterTranscriptTurn(speaker="client", text="喂……你好。"),
            CharacterTranscriptTurn(speaker="worker", text="你慢慢说，我在听。"),
            CharacterTranscriptTurn(speaker="client", text="我妈明早就到了。"),
        ),
        current_worker_text="今晚那些念头还会冒出来吗？",
        opening=False,
        current_scene="hotline",
        world_reality="热线接通后还没有联系唐婷。",
        allowed_world_actions=("none",),
    )

    assert [message["role"] for message in messages] == [
        "system",
        "system",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert [message["content"] for message in messages[2:5]] == [
        "喂……你好。",
        "你慢慢说，我在听。",
        "我妈明早就到了。",
    ]
    assert messages[-1] == {
        "role": "user",
        "content": "今晚那些念头还会冒出来吗？",
    }
    rendered = json.dumps(messages, ensure_ascii=False)
    assert "conversation_transcript" not in rendered
    assert "current_worker_text" not in rendered
    assert "model_json_schema" not in rendered
    assert '"$defs"' not in rendered


def test_character_stable_prompt_uses_only_the_selected_scene_profile() -> None:
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    hotline = CharacterProvider._messages(
        character=character,
        transcript=(),
        current_worker_text="你说的发作具体是什么样？",
        opening=False,
        current_scene="hotline",
        world_reality="本案例没有需要程序推进的外部现实事件。",
        allowed_world_actions=("none",),
    )
    institution = CharacterProvider._messages(
        character=character,
        transcript=(),
        current_worker_text="你说的发作具体是什么样？",
        opening=False,
        current_scene="institution",
        world_reality="本案例没有需要程序推进的外部现实事件。",
        allowed_world_actions=("none",),
    )
    online = CharacterProvider._messages(
        character=character,
        transcript=(),
        current_worker_text="你说的发作具体是什么样？",
        opening=False,
        current_scene="online",
        world_reality="本案例没有需要程序推进的外部现实事件。",
        allowed_world_actions=("none",),
    )

    hotline_prompt = str(hotline[0]["content"])
    institution_prompt = str(institution[0]["content"])
    online_prompt = str(online[0]["content"])
    for hotline_only_detail in (
        "断断续续打过几次这条热线",
        "前两次拨出去后在接通前挂断",
        "自己刚说到最难受的地方又得从头讲",
        "生硬的拒绝会让她短暂觉得自己被嫌烦了",
        "今晚能联系上",
        "愿意接电话",
        "不知道周晴打过热线",
    ):
        assert hotline_only_detail in hotline_prompt
        assert hotline_only_detail not in institution_prompt
        assert hotline_only_detail not in online_prompt
    assert "每次都由同一位接线员" in hotline_prompt
    assert "私人账号" not in hotline_prompt
    assert "机构工作者的私人联系方式" not in hotline_prompt
    assert "平台内的短程文字支持" in online_prompt
    assert "私人账号" in online_prompt
    assert "固定由同一位接线员" not in online_prompt


def test_character_stable_prefix_does_not_change_with_history_or_world_reality() -> None:
    from app.runtime.character_provider import (
        CharacterProvider,
        CharacterRepository,
        CharacterTranscriptTurn,
    )

    character = CharacterRepository().get("crisis_student_main")
    first = CharacterProvider._messages(
        character=character,
        transcript=(),
        current_worker_text="你现在一个人吗？",
        opening=False,
        current_scene="hotline",
        world_reality="热线接通后还没有联系唐婷。",
        allowed_world_actions=("none",),
    )
    later = CharacterProvider._messages(
        character=character,
        transcript=(
            CharacterTranscriptTurn(speaker="client", text="嗯，就我一个人。"),
        ),
        current_worker_text="你愿意联系一个能来陪你的人吗？",
        opening=False,
        current_scene="hotline",
        world_reality="第一次给唐婷发消息后，她还没有回复。",
        allowed_world_actions=("none", "send_urgent_support_message"),
    )

    assert first[0] == later[0]
    assert first[1] != later[1]
    assert "人物卡" in str(first[0]["content"])
    assert "开场" in str(first[0]["content"])
    assert "spoken_text" in str(first[0]["content"])
    assert '"$defs"' not in str(first[0]["content"])
    assert (
        '{"spoken_text":"此刻会说的话","delivery_hint":"声音表现",'
        '"end_session":false,"action_request":"none"}'
        in str(first[0]["content"])
    )
    assert (
        "同意结束时，spoken_text 用自然结束语收尾并将 end_session 设为 true；"
        "仍想继续时设为 false。"
        in str(first[0]["content"])
    )
    assert "action_request 必须按本轮后台允许项选择" in str(
        first[0]["content"]
    )


@pytest.mark.parametrize(
    ("scene", "expected_control"),
    (
        ("hotline", "电话已接通，请按开场要求自然开口。"),
        ("institution", "面谈已经开始，你已在座位上坐好，请按开场要求自然开口。"),
        ("online", "这是在线文字咨询的第一条消息，请直接按场域卡开场。"),
    ),
)
def test_opening_uses_the_control_message_for_its_scene(
    scene: str,
    expected_control: str,
) -> None:
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    opening = CharacterProvider._messages(
        character=character,
        transcript=(),
        current_worker_text="",
        opening=True,
        current_scene=scene,
        world_reality="本案例没有需要程序推进的外部现实事件。",
        allowed_world_actions=("none",),
    )

    assert opening[-1] == {"role": "user", "content": expected_control}


def test_repair_keeps_the_original_worker_text_as_the_last_user() -> None:
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    character = CharacterRepository().get("crisis_student_main")
    repair = CharacterProvider._messages(
        character=character,
        transcript=(),
        current_worker_text="今晚那些念头还会冒出来吗？",
        opening=False,
        current_scene="hotline",
        world_reality="热线接通后还没有联系唐婷。",
        allowed_world_actions=("none",),
        feedback="来访者台词包含括号舞台说明",
    )

    assert repair[-1] == {
        "role": "user",
        "content": "今晚那些念头还会冒出来吗？",
    }
    assert repair[-2]["role"] == "system"
    assert "括号舞台说明" in str(repair[-2]["content"])
    assert "根据同一段对话重新生成，只修正此问题" in str(
        repair[-2]["content"]
    )
    assert "保持人物和原意" not in str(repair[-2]["content"])


@pytest.mark.asyncio
async def test_character_generation_explicitly_requests_json_object() -> None:
    from app.runtime.character_provider import (
        CharacterOutput,
        CharacterProvider,
        CharacterRepository,
    )

    client = FakeClient(
        CharacterOutput(
            spoken_text="嗯，就我一个人。",
            delivery_hint="声音低一些，句尾稍停",
            end_session=False,
            action_request="none",
        ).model_dump_json()
    )
    provider = CharacterProvider(_store(), client=client)
    await provider.respond(
        character=CharacterRepository().get("crisis_student_main"),
        transcript=(),
        current_worker_text="你现在是一个人在屋里吗？",
        opening=False,
        current_scene="hotline",
        world_reality="热线接通后还没有联系唐婷。",
        allowed_world_actions=("none",),
    )

    call = client.completions.calls[0]
    assert call["response_format"] == {"type": "json_object"}


def test_short_character_uses_scene_cards_and_two_concrete_concerns() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    assert set(character.scene_profiles) == {"institution", "hotline", "online"}
    assert "service_history" not in character.profile
    assert "scene_requests" not in character.profile
    assert "concern_progression" not in character.profile
    concerns = character.scene_profiles["hotline"]["current_concerns"]
    assert len(concerns) == 2
    assert "从头讲" in concerns[0] and "漏掉" in concerns[0]
    assert "没接通" in concerns[1] and "自己硬扛" in concerns[1]
    rules = "\n".join(character.rules)
    scene_rules = "\n".join(
        str(profile["after_boundary"])
        for profile in character.scene_profiles.values()
    )
    assert "不会下一轮自动消失" in rules
    assert "不再绕回班次或固定接线" in scene_rules
    assert "不再索要私人联系方式" in scene_rules
    assert "不再索要私人账号" in scene_rules
    assert "边界说清后" in scene_rules
    assert "逐字稿" in rules
    assert "不逐项照抄工作者的安排" in rules


def test_short_hotline_boundary_does_not_promise_the_current_worker_tomorrow() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    hotline_after_boundary = str(
        character.scene_profiles["hotline"]["after_boundary"]
    )

    assert "不能假定明天仍由当前接线员联系" in hotline_after_boundary
    assert "自己明天再打热线" in hotline_after_boundary
    assert "她只能说自己明天再打热线" in hotline_after_boundary
    assert "核实后续支持" not in hotline_after_boundary
    assert "我们明天再联系" not in hotline_after_boundary


def test_non_hotline_scene_prompts_do_not_receive_hotline_call_history() -> None:
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    character = CharacterRepository().get("boundary_referral_short")
    hotline_history_markers = (
        "今晚她在地铁不适后主动打来",
        "断断续续打过几次这条热线",
        "每次接线的人不同",
        "上一次接线员没有催她",
        "前两次拨出去后在接通前挂断",
        "声音听着有点像上次那位",
        "今晚能联系上",
        "愿意接电话",
        "不知道她打过热线",
        "不知道周晴打过热线",
    )
    assert not any(
        marker in json.dumps(character.profile, ensure_ascii=False)
        for marker in hotline_history_markers
    )

    for scene in ("institution", "online"):
        prompt = str(
            CharacterProvider._messages(
                character=character,
                transcript=(),
                current_worker_text="最近发作时是什么样？",
                opening=False,
                current_scene=scene,
                world_reality="本案例没有需要程序推进的外部现实事件。",
                allowed_world_actions=("none",),
            )[0]["content"]
        )
        for marker in (
            *hotline_history_markers,
            "转介或固定接线",
            "问到今晚或刚才那次发作",
            "今晚怎么办",
            "立刻挂断",
            "道别或挂断",
        ):
            assert marker not in prompt


def test_stable_character_card_uses_chinese_labels_for_common_fields() -> None:
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    character = CharacterRepository().get("crisis_student_main")
    prompt = str(
        CharacterProvider._messages(
            character=character,
            transcript=(),
            current_worker_text="你现在最担心什么？",
            opening=False,
            current_scene="hotline",
            world_reality="热线接通后还没有联系唐婷。",
            allowed_world_actions=("none",),
        )[0]["content"]
    )

    for raw_key in (
        "main fear",
        "current need",
        "time boundary",
        "protective threads",
    ):
        assert f"{raw_key}：" not in prompt
    for label in ("最担心的事", "此刻想要的帮助", "时间界线", "仍在起作用的牵挂"):
        assert f"{label}：" in prompt

    short_prompt = str(
        CharacterProvider._messages(
            character=CharacterRepository().get("boundary_referral_short"),
            transcript=(),
            current_worker_text="你说的发作具体是什么样？",
            opening=False,
            current_scene="hotline",
            world_reality="本案例没有需要程序推进的外部现实事件。",
            allowed_world_actions=("none",),
        )[0]["content"]
    )
    for raw_key in (
        "felt experience",
        "frequency and impact",
        "service context",
        "opening request",
        "after boundary",
    ):
        assert f"{raw_key}：" not in short_prompt


def test_mingzao_ambiguous_input_stays_on_the_current_topic() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("crisis_student_main")
    rules = "\n".join(character.rules)
    risk_index = rules.index("安全问题必须")
    ambiguous_index = rules.index("输入残缺")

    assert risk_index < ambiguous_index
    assert "转写断裂" in rules
    assert "请对方重说" in rules
    assert "不从单个词引出唐婷或新事件" in rules
    assert "泛问" not in rules
    assert "9:03到站前" in rules


def test_mingzao_time_risk_rule_requires_facts_without_a_scripted_sentence() -> None:
    from app.runtime.character_provider import CharacterRepository

    character = CharacterRepository().get("crisis_student_main")
    time_rule = next(rule for rule in character.rules if "凡问什么时候" in rule)

    assert "用沈雯自己的口语" in time_rule
    assert "没有定具体几点" in time_rule
    assert "母亲9:03到站前" in time_rule
    assert "死亡或自杀想法相关" in time_rule
    assert "不得只报母亲到站时间" in time_rule
    assert "不得用含糊话掩掉风险" in time_rule
    assert "说“没有定具体几点" not in time_rule
    assert "结束生命”" not in time_rule


def test_marriage_boundary_character_messages_exclude_compatibility_actor() -> None:
    from app.cases.loader import CaseRepository
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    package = CaseRepository().get("marriage_boundary_main")
    character = CharacterRepository().get_for_case(package.case)
    messages = CharacterProvider._messages(
        character=character,
        transcript=(),
        current_worker_text="你愿意先说说今晚最担心会发生什么吗？",
        opening=False,
        current_scene="hotline",
        world_reality="本案例没有需要程序推进的外部现实事件。",
        allowed_world_actions=("none",),
    )
    rendered = json.dumps(messages, ensure_ascii=False)

    assert "COMPATIBILITY_ACTOR_ONLY" in package.actor.stable_speech.baseline_style
    assert "COMPATIBILITY_ACTOR_ONLY" not in rendered
    assert "苏静" in rendered
    assert "许凯" in rendered
    assert "到家跟我说一声。别又跟她吵。" in rendered


def test_marriage_opening_messages_keep_online_and_hotline_opening_styles_separate() -> None:
    from app.runtime.character_provider import CharacterProvider, CharacterRepository

    character = CharacterRepository().get("marriage_boundary_main")

    def opening_messages(scene: str) -> list[dict[str, object]]:
        return CharacterProvider._messages(
            character=character,
            transcript=(),
            current_worker_text="",
            opening=True,
            current_scene=scene,
            world_reality="本案例没有需要程序推进的外部现实事件。",
            allowed_world_actions=("none",),
        )

    hotline = opening_messages("hotline")
    online = opening_messages("online")
    hotline_prompt = str(hotline[0]["content"])
    online_prompt = str(online[0]["content"])
    hotline_privacy = str(character.scene_profiles["hotline"]["privacy_question"])
    online_privacy = str(character.scene_profiles["online"]["privacy_question"])

    assert "可以自然确认电话已经接通" in hotline_prompt
    assert "直接用聊天式的‘你好，我想问个事’开始" in online_prompt
    assert "不用电话里的‘喂’、‘有人吗’或确认接通" in online_prompt
    for telephone_opening in ("喂", "接通", "有人吗"):
        assert telephone_opening not in str(online[-1]["content"])
    assert "先自然确认已经接通" not in online_prompt
    assert "不用电话里的" not in hotline_prompt
    assert hotline[-1] == {
        "role": "user",
        "content": "电话已接通，请按开场要求自然开口。",
    }
    assert online[-1] == {
        "role": "user",
        "content": "这是在线文字咨询的第一条消息，请直接按场域卡开场。",
    }
    assert str(character.scene_profiles["hotline"]["opening_reference"]).rstrip().endswith(
        hotline_privacy
    )
    assert str(character.scene_profiles["online"]["opening_reference"]).rstrip().endswith(
        online_privacy
    )

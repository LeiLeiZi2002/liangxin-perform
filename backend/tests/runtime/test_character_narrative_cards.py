"""检查人物材料实际进入模型的内容；这些检查不代替真人感评估。"""

import json

import pytest

from app.runtime.character_provider import (
    CharacterProvider,
    CharacterRepository,
    CharacterTranscriptTurn,
)

CASE_IDS = ("crisis_student_main", "boundary_referral_short", "marriage_boundary_main")


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_shared_reply_instruction_precedes_background_and_preserves_fact_authority(case_id) -> None:
    character = CharacterRepository().get(case_id)
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")
    # 锁定共同指令确实进入输入；真人感另用真实生成的片段评估。
    assert prompt.index("【接着这次交流说话】") < prompt.index("【人物卡】")
    assert "对方刚才是在接你的哪句话" in prompt
    assert "不替对方完成咨询工作" in prompt
    assert "没有问句也可以继续正在谈的事" in prompt
    assert "已经商量好的事接着往下说" in prompt
    assert "若自己先前说错了，按人物事实自然纠正" in prompt
    assert "不固定加叹气、结巴或省略号" in prompt
    assert "声音提示跟着本轮台词" in prompt
    assert "心理独白" not in prompt


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_initial_portrait_is_not_a_permanent_state_and_history_is_verbatim(case_id: str) -> None:
    character = CharacterRepository().get(case_id)
    transcript = (
        CharacterTranscriptTurn(speaker="client", text="刚才那个办法我想试试。"),
        CharacterTranscriptTurn(speaker="worker", text="你想怎么做？"),
        CharacterTranscriptTurn(speaker="client", text="我先把要说的话写下来。"),
    )
    messages = CharacterProvider._messages(
        character=character,
        transcript=transcript,
        current_worker_text="好，我等你。",
        opening=False,
        current_scene="hotline",
        world_reality="此刻尚未发生新的外部事件。",
        allowed_world_actions=("none",),
    )

    assert "【接着这次交流说话】" in messages[0]["content"]
    assert "开场时的感受和打算可以改变" in messages[0]["content"]
    assert [message["content"] for message in messages[1:-2]] == [turn.text for turn in transcript]
    assert messages[-1] == {"role": "user", "content": "好，我等你。"}
    assert "此刻尚未发生新的外部事件" in messages[-2]["content"]


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_character_card_does_not_prescribe_a_disclosure_quota_or_repair_password(
    case_id: str,
) -> None:
    character = CharacterRepository().get(case_id)
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")
    for retired_instruction in (
        "一次只说眼下最想说的一件事",
        "不用把两层顾虑一次说完",
        "只挑一件",
        "沿着顾虑层次",
        "一次问太多时直接说跟不上",
        "所以现在错的是我，是吗？",
        "接住她怕打扰的顾虑",
        "她才实际发送",
        "随后只回应当前关系冲突",
    ):
        assert retired_instruction not in prompt


def test_mingzao_support_has_lived_history_without_an_approval_sequence() -> None:
    character = CharacterRepository().get("crisis_student_main")
    friend = character.profile["relationships"]["tang_ting"]
    assert all(detail in friend for detail in ("七千元", "住几天", "没说"))
    assert "可以主动提出" in "\n".join(character.rules)
    assert "尚不发送" not in "\n".join(character.rules)
    assert character.world is not None
    assert "尚未收到回复" in character.world.first_unanswered_reality
    assert "没有确定具体方法" in character.profile["safety_reality"]["plan"]


def test_mingzao_uncontacted_reality_does_not_repeat_before_call_history() -> None:
    character = CharacterRepository().get("crisis_student_main")

    assert character.world is not None
    assert character.world.not_contacted_reality == "这次热线通话中，沈雯尚未联系唐婷。"
    assert "两次电话" in "\n".join(character.profile["what_happened"])
    assert "两次" not in character.world.not_contacted_reality


def test_repeat_caller_history_records_what_previous_calls_did_and_did_not_help() -> None:
    character = CharacterRepository().get("boundary_referral_short")
    hotline = character.scene_profiles["hotline"]
    history = hotline["help_seeking_history"]
    assert "请过假" in history["previous_calls"]
    assert "身体缓过来" in history["trusted_call"]
    assert "病因" in history["trusted_call"]
    assert "通话记录" in hotline["tonight_before_call"]["at_home"]
    assert "接通前挂断" not in json.dumps(hotline, ensure_ascii=False)
    assert "私人" not in hotline["opening_request"]
    # 机构和在线有各自的既往服务，不能被全局的“从未接受服务”覆盖。
    assert "没有接受持续心理服务" not in json.dumps(character.profile, ensure_ascii=False)
    for scene in ("institution", "online"):
        assert character.scene_profiles[scene]["help_seeking_history"]


def test_marriage_case_distinguishes_record_source_from_interpretation() -> None:
    character = CharacterRepository().get("marriage_boundary_main")
    facts = "\n".join(character.profile["observed_facts"])
    assert "许凯解释" in facts and "姓名" in facts
    assert "他说" in facts
    assert "自己与许凯的聊天" in character.profile["current_functioning"]["checking"]
    assert "争执" in "\n".join(character.profile["own_inferences"])
    assert "猜" in "\n".join(character.profile["own_inferences"])
    assert "果果" in character.profile["support_options"]["choices"]
    assert "四十分钟" not in character.opening_guidance
    for scene in ("hotline", "online"):
        assert character.scene_profiles[scene]["privacy_question"]
    assert character.world is None


@pytest.mark.parametrize(
    "known_details",
    (
        ("今晚没有饮酒", "没有服用镇静药物或其他物质"),
        ("不能确定当时是否真的想死", "没有造成严重身体后果", "近年没有再次发生"),
        ("能辨认时间、地点和人物", "没有幻觉和明显意识异常"),
    ),
)
def test_mingzao_ordinary_safety_questions_have_known_answers_in_loaded_card(
    known_details: tuple[str, ...],
) -> None:
    character = CharacterRepository().get("crisis_student_main")
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")

    assert all(detail in prompt for detail in known_details)
    assert "上午9:03到站以前结束生命" in character.profile["safety_reality"]["time_boundary"]
    assert "没有确定具体方法、地点或行动步骤" in character.profile["safety_reality"]["plan"]


@pytest.mark.parametrize("scene", ("hotline", "online"))
def test_marriage_colleague_has_the_same_known_name_in_both_observations(scene: str) -> None:
    character = CharacterRepository().get("marriage_boundary_main")
    facts = character.profile["observed_facts"]
    prompt = CharacterProvider._stable_prompt(character, current_scene=scene)

    assert "林悦" in facts[1]
    assert "林悦" in facts[2]
    assert facts[1] in prompt and facts[2] in prompt
    assert "没有亲自去确认住址" in facts[1]
    assert "不知道许凯是否发生关系越界" in prompt


@pytest.mark.parametrize(
    ("scene", "shared_experience", "own_attempt"),
    (
        ("institution", "上一次两人谈到她中途下车后怕迟到", "她回去后试过提前出门"),
        ("online", "补上请假那天只跟同事说胃不舒服的事", "试着在手机里记下一次不适的前后经过"),
    ),
    ids=("institution", "online"),
)
def test_zhouqing_shared_experience_and_attempt_stay_in_their_service_scene(
    scene: str, shared_experience: str, own_attempt: str,
) -> None:
    character = CharacterRepository().get("boundary_referral_short")
    history = character.scene_profiles[scene]["help_seeking_history"]
    prompt = CharacterProvider._stable_prompt(character, current_scene=scene)

    assert shared_experience in history and own_attempt in history
    assert history in prompt
    assert "不适" in history
    assert character.scene_profiles["hotline"]["help_seeking_history"]["trusted_call"] not in prompt
    for other_scene in {"institution", "hotline", "online"} - {scene}:
        other_prompt = CharacterProvider._stable_prompt(character, current_scene=other_scene)
        assert shared_experience not in other_prompt and own_attempt not in other_prompt


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_natural_time_granularity_does_not_change_precise_background(case_id: str) -> None:
    character = CharacterRepository().get(case_id)
    style = "\n".join(character.profile["speech_style"])
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")

    assert "日常回顾" in style and "大概" in style
    assert "核对关键事实时仍须准确" in style
    assert all(instruction in prompt for instruction in character.profile["speech_style"])
    precise_details = {
        "crisis_student_main": ("四十一天前", "九天前", "11:18", "11:26", "9:03", "2:30"),
        "boundary_referral_short": ("七个月前", "最近三周", "两次", "一天假"),
        "marriage_boundary_main": ("结婚九年", "相识十一年", "三天前", "最近三晚", "四十分钟"),
    }
    assert all(detail in prompt for detail in precise_details[case_id])

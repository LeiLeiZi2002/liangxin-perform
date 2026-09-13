"""核对热线人物材料及场域隔离，不据文字命中判断模型像不像真人。"""

from pathlib import Path

import pytest

from app.runtime.character_provider import CharacterProvider, CharacterRepository


def test_hotline_opening_keeps_the_personal_request_without_a_fixed_order() -> None:
    character = CharacterRepository().get("boundary_referral_short")
    opening = character.scene_profiles["hotline"]["opening_request"]

    assert "再问以后能不能每次" not in opening
    assert "开场先问" not in opening
    assert "上次" in opening and "心跳" in opening and "胸口" in opening
    messages = CharacterProvider._messages(
        character=character, transcript=(), current_worker_text="", opening=True,
        current_scene="hotline", world_reality="", allowed_world_actions=("none",),
    )
    assert opening in messages[0]["content"]
    assert "每次都由同一位接线员" in messages[0]["content"]


def test_previous_call_contains_a_remembered_exchange_and_its_limit() -> None:
    character = CharacterRepository().get("boundary_referral_short")
    history = character.scene_profiles["hotline"]["help_seeking_history"]
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")

    assert "现在好多了" in history["previous_calls"]
    assert "怎么回家的" in history["trusted_call"]
    assert "下车以后" in history["trusted_call"]
    assert "病因" in history["trusted_call"]
    assert "没有承诺以后固定接听" in history["trusted_call"]
    assert history["trusted_call"] in prompt
    for scene in ("institution", "online"):
        assert history["trusted_call"] not in CharacterProvider._stable_prompt(
            character, current_scene=scene,
        )


def test_service_condition_is_not_the_callers_prior_knowledge() -> None:
    character = CharacterRepository().get("boundary_referral_short")
    hotline = character.scene_profiles["hotline"]
    boundary = hotline["service_boundary"]

    assert "服务设置" in boundary
    assert "尚未听过" in boundary
    assert "不替接线员讲规定" in boundary
    assert "无法承诺" in boundary and "私人联系方式" in boundary
    assert character.world is None


def test_hotline_leaves_room_for_talking_after_accepting_a_suggestion() -> None:
    character = CharacterRepository().get("boundary_referral_short")
    reaction = character.scene_profiles["hotline"]["boundary_reactions"]["concrete_alternative"]

    assert "可以不再追问联系安排" in reaction
    assert "仍没解决的事" in reaction
    assert reaction in CharacterProvider._stable_prompt(character, current_scene="hotline")


@pytest.mark.parametrize(
    ("section", "details"),
    (
        ("life_background", ("改稿", "印刷", "换乘", "聚餐", "后悔")),
        ("marriage_context", ("未婚", "目前没有伴侣", "没有孩子")),
        ("relationships", ("店", "衣服", "她的猜测", "尚未联系")),
    ),
    ids=("work_and_social_life", "household", "sister_relationship"),
)
def test_hotline_portrait_details_reach_only_the_selected_scene(
    section: str, details: tuple[str, ...],
) -> None:
    character = CharacterRepository().get("boundary_referral_short")
    material = character.scene_profiles["hotline"][section]
    paragraphs = material if isinstance(material, list) else [material]

    assert all(detail in "\n".join(paragraphs) for detail in details)
    for scene in ("hotline", "institution", "online"):
        messages = CharacterProvider._messages(
            character=character, transcript=(), current_worker_text="平时生活是怎样的？",
            opening=False, current_scene=scene, world_reality="",
            allowed_world_actions=("none",),
        )
        for paragraph in paragraphs:
            assert (paragraph in messages[0]["content"]) is (scene == "hotline")
        assert messages[-1]["content"] == "平时生活是怎样的？"


def test_hotline_additions_keep_existing_facts_and_have_no_new_external_actions() -> None:
    character = CharacterRepository().get("boundary_referral_short")
    hotline = character.scene_profiles["hotline"]
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")

    assert len(hotline["current_concerns"]) == 2
    assert character.profile["identity"]["age"] == 27
    assert "急性的身体反应已经基本缓解" in hotline["tonight_before_call"]["connection_state"]
    assert "正常询问" in hotline["tonight_before_call"]["connection_state"]
    assert "没有" in character.profile["safety_reality"]["self_harm_or_suicide"]
    assert "action_request 始终选择 none" in prompt
    assert character.world is None


def test_portrait_provenance_stays_in_author_documentation() -> None:
    document = (Path(__file__).resolve().parents[3] / "docs/案例人物稿.md").read_text(
        encoding="utf-8"
    )
    portrait = document.split("## 只想继续找你", 1)[1].split("## 锁屏亮了一下", 1)[0]
    character = CharacterRepository().get("boundary_referral_short")
    prompt = CharacterProvider._stable_prompt(character, current_scene="hotline")

    assert "虚构补写" in portrait and "未婚" in portrait
    assert "虚构补写" not in prompt

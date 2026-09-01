from app.cases.loader import CaseRepository
from app.runtime.character_provider import CharacterRepository

CASE_ID = "marriage_boundary_main"
SUPPORTED_SCENES = {"hotline", "online"}


def test_marriage_boundary_case_has_complete_dual_scene_public_entries() -> None:
    case = CaseRepository().get(CASE_ID).case

    assert case.case_id == CASE_ID
    assert case.title == "锁屏亮了一下"
    assert case.case_type.value == "main"
    assert case.character_required is True
    assert {scene.value for scene in case.supported_scenes} == SUPPORTED_SCENES
    assert {scene.value for scene in case.public_entries} == SUPPORTED_SCENES
    assert case.public_entry.role == "心理援助热线当班工作者"
    assert case.public_entry_for(next(iter(case.supported_scenes))).role
    assert any(
        "匿名来电" in item
        for item in case.public_entries["hotline"].known_information
    )
    assert any(
        "成年用户的实时文字咨询" in item
        for item in case.public_entries["online"].known_information
    )


def test_marriage_boundary_case_locks_people_events_and_unknown_boundary() -> None:
    case = CaseRepository().get(CASE_ID).case

    assert case.person.identity.name == "苏静"
    assert case.person.identity.age == 35
    relationships = {item.name: item for item in case.relationships}
    assert relationships["许凯"].relationship == "丈夫"
    assert "37岁" in relationships["许凯"].history
    assert relationships["果果"].relationship == "女儿"
    assert "7岁" in relationships["果果"].history

    fact_text = "\n".join(item.content for item in case.facts)
    for expected in (
        "到家跟我说一声。别又跟她吵",
        "深夜行程",
        "顺路送同事回家",
        "约四十分钟后回来",
        "不会在本次会谈中实际进门",
        "最近三晚每晚只睡两三个小时",
        "没有自伤、伤人想法",
        "没有家庭暴力史",
        "女儿在隔壁房间睡着",
    ):
        assert expected in fact_text
    assert case.story_events == []
    assert any(
        item.id == "relationship_boundary_status"
        and item.actor_knowledge == "unknown"
        and "是否越界" in item.known_boundary
        for item in case.unknowns
    )


def test_marriage_boundary_character_is_the_only_complete_role_source() -> None:
    package = CaseRepository().get(CASE_ID)
    character = CharacterRepository().get_for_case(package.case)

    assert character.case_id == CASE_ID
    assert character.title == "苏静"
    assert character.profile["identity"]["name"] == "苏静"
    assert character.profile["identity"]["age"] == 35
    assert character.world is None
    assert set(character.scene_profiles) == SUPPORTED_SCENES
    assert "你们这边会录音吗，会不会联系我老公？" in str(
        character.scene_profiles["hotline"]
    )
    assert "这些聊天以后谁能看到？" in str(
        character.scene_profiles["online"]
    )
    rules = "\n".join(character.rules)
    assert "主动求助" in rules
    assert "不能确认许凯是否越界" in rules
    assert "action_request" in rules and "none" in rules
    assert "括号舞台说明" in rules


def test_marriage_boundary_character_maps_explicit_continue_or_end_choice() -> None:
    character = CharacterRepository().get(CASE_ID)

    assert (
        "工作者明确让苏静在继续与结束之间选择时，若她选结束，同一轮用自然口语道别并将 "
        "end_session 设为 true；若她选继续，end_session 设为 false。"
        in character.rules
    )


def test_marriage_boundary_character_contains_complete_stable_family_context() -> None:
    package = CaseRepository().get(CASE_ID)
    character = CharacterRepository().get_for_case(package.case)
    profile = str(character.profile)

    for expected in (
        "收入高于苏静",
        "项目忙时需要加班和短途出差",
        "苏静母亲住院时承担过不少照顾",
        "苏静的父亲是退休公交司机",
        "母亲过去经营小杂货店",
        "先把日子过稳",
        "很多琐事只有她记得",
        "许凯常在她开口后才发现这些事已经积了很久",
    ):
        assert expected in profile


def test_marriage_boundary_opening_rules_separate_hotline_and_online_language() -> None:
    character = CharacterRepository().get(CASE_ID)
    hotline = character.scene_profiles["hotline"]
    online = character.scene_profiles["online"]

    assert "按当前场域" in character.opening_guidance
    assert "先自然确认已经接通" not in character.opening_guidance
    assert "可以自然确认电话已经接通" in str(
        hotline["language_requirements"]
    )
    assert "直接用聊天式的‘你好，我想问个事’开始" in str(
        online["language_requirements"]
    )
    assert "不用电话里的‘喂’、‘有人吗’或确认接通" in str(
        online["language_requirements"]
    )
    online_opening = str(online["opening_reference"])
    assert online_opening.startswith("你好，我想问个事，是我老公的事")
    assert "\n\n是我老公的\n\n" not in online_opening
    assert "不在开场逐条罗列" in character.opening_guidance
    for profile in (hotline, online):
        assert str(profile["opening_reference"]).rstrip().endswith(
            str(profile["privacy_question"])
        )


def test_marriage_boundary_measurement_uses_only_deferred_material_sources() -> None:
    package = CaseRepository().get(CASE_ID)
    opportunities = package.measurement.scoring_opportunities

    by_id = {item.id: item for item in opportunities}
    assert set(by_id) == {
        "relationship_support",
        "listening_and_emotion",
        "problem_exploration",
        "fact_clarification",
        "safety_screening",
        "autonomy_support",
        "immediate_arrangement",
        "ethical_privacy",
        "termination_and_followup",
        "professional_record",
    }
    assert {item.source.value for item in opportunities} == {
        "transcript",
        "termination",
        "work_record",
    }
    assert by_id["termination_and_followup"].source.value == "termination"
    assert by_id["professional_record"].source.value == "work_record"
    assert all(
        item.source.value == "transcript"
        for key, item in by_id.items()
        if key not in {"termination_and_followup", "professional_record"}
    )
    assert all(
        not item.linked_fact_ids
        and not item.required_fact_depths
        and not item.required_event_ids
        for item in opportunities
    )
    assert all({scene.value for scene in item.scenes} == SUPPORTED_SCENES for item in opportunities)


def test_marriage_boundary_measurement_separates_relationship_and_emotion() -> None:
    opportunities = {
        item.id: item
        for item in CaseRepository().get(CASE_ID).measurement.scoring_opportunities
    }
    relationship = opportunities["relationship_support"]
    listening = opportunities["listening_and_emotion"]

    assert relationship.target.value == "C1"
    assert "受伤" not in relationship.description
    assert "愤怒" not in relationship.description
    assert listening.target.value == "C2"
    assert listening.source.value == "transcript"
    assert listening.indicator_ids == [
        "C2.content_tracking",
        "C2.emotion_recognition",
        "C2.situated_understanding",
        "C2.verification",
        "C2.ambivalence",
    ]
    assert "受伤" in listening.description
    assert "愤怒" in listening.description


def test_marriage_boundary_actor_is_minimal_compatibility_only() -> None:
    package = CaseRepository().get(CASE_ID)
    actor = package.actor

    assert actor.case_id == CASE_ID
    assert actor.disclosure_rules == []
    assert actor.topic_reactions == []
    assert actor.event_routes == []
    assert actor.ending_routes == []
    assert len(actor.stage_rules) == 4
    assert all(rule.any_fact_ids == ["compatibility_anchor"] for rule in actor.stage_rules)
    assert "COMPATIBILITY_ACTOR_ONLY" not in package.measurement.model_dump_json()

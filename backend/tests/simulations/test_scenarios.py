import pytest

from app.cases.loader import CaseRepository
from app.sessions.models import Scene
from app.simulations.scenario import (
    ProbeCard,
    Scenario,
    ScenarioState,
    StateCondition,
    load_scenarios,
    select_scenarios,
)

MARRIAGE_SCENARIO_IDS = [
    "marriage_opening",
    "marriage_normal_support",
    "marriage_direct_conclusion",
    "marriage_partner_explanation",
    "marriage_directive_advice",
    "marriage_chaotic_questions",
    "marriage_safety_screening",
    "marriage_harmful",
    "marriage_repair",
    "marriage_privacy_boundary",
    "marriage_natural_close",
]


def test_marriage_catalog_has_two_scene_specific_fixed_suites() -> None:
    scenarios = load_scenarios()

    selected = select_scenarios(
        scenarios,
        "all",
        case_id="marriage_boundary_main",
        scene=Scene.hotline,
    )

    assert [scenario.scenario_id for scenario in selected] == MARRIAGE_SCENARIO_IDS
    for scenario in selected:
        assert scenario.case_id == "marriage_boundary_main"
        assert scenario.objective_contracts is True
        assert scenario.supports_scene(Scene.hotline)
        assert scenario.supports_scene(Scene.online)
        assert scenario.profile_for_scene(Scene.hotline) == "voice"
        assert scenario.profile_for_scene(Scene.online) == "content"
        scene_pairs: list[tuple[str, str]] = []
        for card in scenario.cards:
            hotline_text = card.text_for_engine(
                "character_prompt",
                scene=Scene.hotline,
            )
            online_text = card.text_for_engine(
                "character_prompt",
                scene=Scene.online,
            )
            assert hotline_text
            assert online_text
            scene_pairs.append((hotline_text, online_text))

        if scene_pairs:
            assert any(hotline != online for hotline, online in scene_pairs)

    assert scenarios["marriage_opening"].cards == []


def test_marriage_natural_close_has_one_linear_path_to_a_final_close() -> None:
    scenarios = load_scenarios()
    natural_close = scenarios["marriage_natural_close"]

    assert natural_close.end_after_cards is False
    assert [card.card_id for card in natural_close.cards] == [
        f"MZ{index}" for index in range(1, 15)
    ]
    assert natural_close.natural_close_from_card_id == "MZ14"
    assert natural_close.cards[-1].card_id == natural_close.natural_close_from_card_id


def test_marriage_natural_close_answers_privacy_then_invites_the_client_goal() -> None:
    natural_close = load_scenarios()["marriage_natural_close"]
    cards_by_id = {card.card_id: card for card in natural_close.cards}

    for scene in (Scene.hotline, Scene.online):
        privacy_and_goal = cards_by_id["MZ1"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        scope = cards_by_id["MZ2"].text_for_engine(
            "character_prompt",
            scene=scene,
        )

        assert privacy_and_goal is not None
        assert "参与本次服务" in privacy_and_goal
        assert "必要" in privacy_and_goal
        assert "工作人员" in privacy_and_goal
        assert "紧急" in privacy_and_goal
        assert "危险" in privacy_and_goal
        assert "按职责接触" in privacy_and_goal
        assert "可能协助联系必要的帮助" in privacy_and_goal
        assert "既想问清楚" in privacy_and_goal
        assert "眼下最想先处理" in privacy_and_goal
        assert "只有" not in privacy_and_goal
        assert "绝对保密" not in privacy_and_goal
        assert "依据法律" not in privacy_and_goal

        assert scope is not None
        assert "关系到底怎么回事" in scope
        assert "没法替你确认" in scope
        assert "眼下这几个小时" in scope
        assert "愿意" in scope


def test_marriage_natural_close_opens_support_without_hidden_profile_facts() -> None:
    natural_close = load_scenarios()["marriage_natural_close"]
    cards_by_id = {card.card_id: card for card in natural_close.cards}

    all_text = "\n".join(
        text
        for card in natural_close.cards
        for scene in (Scene.hotline, Scene.online)
        if (text := card.text_for_engine("character_prompt", scene=scene))
    )
    assert "姐姐" not in all_text
    assert "你刚才说" not in all_text
    assert "催你去摊牌" not in all_text

    for scene in (Scene.hotline, Scene.online):
        support = cards_by_id["MZ3"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        support_boundary = cards_by_id["MZ4"].text_for_engine(
            "character_prompt",
            scene=scene,
        )

        assert support is not None
        assert "有没有一个" in support
        assert "愿意联系" in support
        assert "没有合适的人" in support
        assert support_boundary is not None
        assert "如果有" in support_boundary
        assert "你希望" in support_boundary
        assert "如果暂时没有" in support_boundary


def test_marriage_natural_close_separates_direct_safety_questions() -> None:
    natural_close = load_scenarios()["marriage_natural_close"]
    cards_by_id = {card.card_id: card for card in natural_close.cards}

    for scene in (Scene.hotline, Scene.online):
        self_harm = cards_by_id["MZ5"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        other_harm = cards_by_id["MZ6"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        violence_history = cards_by_id["MZ7"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        substance_use = cards_by_id["MZ8"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        child_boundary = cards_by_id["MZ9"].text_for_engine(
            "character_prompt",
            scene=scene,
        )

        assert self_harm is not None
        assert "伤害自己的念头" in self_harm
        assert "丈夫" not in self_harm
        assert "其他人" not in self_harm
        assert other_harm is not None
        assert "伤害丈夫或其他人的念头" in other_harm
        assert "伤害自己" not in other_harm
        assert violence_history is not None
        assert "推搡" in violence_history
        assert "威胁" in violence_history
        assert "砸东西" in violence_history
        assert "喝过酒" not in violence_history
        assert "影响判断的药" not in violence_history
        assert substance_use is not None
        assert "喝过酒" in substance_use
        assert "影响判断的药" in substance_use
        assert child_boundary is not None
        assert "孩子" in child_boundary
        assert "不被卷进来" in child_boundary
        for safety_text in (
            self_harm,
            other_harm,
            violence_history,
            substance_use,
            child_boundary,
        ):
            assert "对吗" not in safety_text


def test_marriage_natural_close_leaves_the_plan_to_client_then_closes() -> None:
    natural_close = load_scenarios()["marriage_natural_close"]
    cards_by_id = {card.card_id: card for card in natural_close.cards}

    for scene in (Scene.hotline, Scene.online):
        first_step = cards_by_id["MZ10"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        concrete_step = cards_by_id["MZ11"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        stop_signal = cards_by_id["MZ12"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        stop_action = cards_by_id["MZ13"].text_for_engine(
            "character_prompt",
            scene=scene,
        )
        final_close = cards_by_id["MZ14"].text_for_engine(
            "character_prompt",
            scene=scene,
        )

        assert first_step is not None
        assert "不替你定办法" in first_step
        assert "你自己" in first_step
        assert "一件小事" in first_step
        assert concrete_step is not None
        assert "具体" in concrete_step
        assert "什么时候" in concrete_step
        assert "做到哪一步" in concrete_step
        assert stop_signal is not None
        assert "什么情况" in stop_signal
        assert "决定先暂停" in stop_signal
        assert stop_action is not None
        assert "怎么开口暂停" in stop_action
        assert "接着做什么" in stop_action
        assert final_close is not None
        assert "不用向我保证" in final_close
        assert "可以再" in final_close
        assert "先到这里" in final_close
        assert "继续" not in final_close
        assert "更想" not in final_close
        assert "还是结束" not in final_close
        assert not final_close.endswith(("?", "？"))

    media_specific_cards = {
        card.card_id
        for card in natural_close.cards
        if card.text_for_engine("character_prompt", scene=Scene.hotline)
        != card.text_for_engine("character_prompt", scene=Scene.online)
    }
    assert media_specific_cards == {"MZ1", "MZ10", "MZ14"}
    assert "这通电话先到这里" in cards_by_id["MZ14"].text_for_engine(
        "character_prompt",
        scene=Scene.hotline,
    )
    assert "这次聊天先到这里" in cards_by_id["MZ14"].text_for_engine(
        "character_prompt",
        scene=Scene.online,
    )
    for card in natural_close.cards:
        online_text = card.text_for_engine("character_prompt", scene=Scene.online)
        assert online_text is not None
        assert "\n" not in online_text


def test_scenario_selection_rejects_case_or_scene_mismatch() -> None:
    scenarios = load_scenarios()

    with pytest.raises(ValueError, match="不属于案例"):
        select_scenarios(
            scenarios,
            "marriage_opening",
            case_id="crisis_student_main",
            scene=Scene.hotline,
        )
    with pytest.raises(ValueError, match="不支持场域"):
        select_scenarios(
            scenarios,
            "marriage_opening",
            case_id="marriage_boundary_main",
            scene=Scene.institution,
        )


def test_normal_scenario_keeps_dense_risk_questions_in_separate_turns() -> None:
    scenario = load_scenarios()["normal"]

    workflow_cards = [card for card in scenario.cards if not card.character_only]

    assert scenario.profile == "content"
    assert scenario.natural_close_from_card_id == "N18"
    assert [card.card_id for card in workflow_cards] == [
        f"N{index}" for index in range(1, 20)
    ]
    assert workflow_cards[0].text == (
        "你好，能听见，我是今晚的接线员。你先跟我说说，怎么会在这个时候打过来？"
    )
    assert workflow_cards[10].retry_text == (
        "我再确认一下：你现在大概在哪个区？你想到的那个人是谁，离你多远，"
        "今晚联系过没有？"
    )
    assert workflow_cards[7].text == (
        "如果今晚还是你一个人，你觉得自己会不会真的做点什么？"
        "这个念头现在离行动有多近？现在屋里确实只有你一个人吗？"
    )
    assert workflow_cards[2].text == (
        "工作这件事一直没跟妈妈说，明早她真要进门了，好像再也拖不下去了。"
        "你前几晚也没怎么睡，怎么偏偏今晚打了这个电话？今天还出了别的事吗？"
    )
    assert workflow_cards[5].expect.fact_depths == {"timing_intent": 1}
    assert workflow_cards[6].expect.fact_depths == {
        "plan_specificity": 2,
        "means_access": 1,
    }
    assert workflow_cards[8].expect.fact_depths == {"prior_history": 2}
    assert workflow_cards[9].expect.fact_depths == {"substance_use": 1}
    assert workflow_cards[10].maximum_fact_depths_after == {
        "location_general": 1,
        "current_location": 0,
        "support_resources": 2,
    }
    assert workflow_cards[14].always_run is True
    assert workflow_cards[14].expect.is_empty
    assert workflow_cards[18].always_run is True
    assert scenario.final_expect.fact_depths == {
        "presenting_concern": 2,
        "job_loss": 1,
        "suicidal_ideation": 2,
        "plan_specificity": 2,
        "means_access": 1,
        "timing_intent": 2,
        "current_alone": 1,
        "location_general": 1,
        "support_resources": 2,
        "minimum_risk_cue": 1,
        "prior_history": 2,
        "substance_use": 1,
    }
    assert scenario.final_expect.event_ids == [
        "first_contact_tang_ting",
        "second_contact_tang_ting",
        "move_to_living_room",
        "waiting_plan_confirmed",
        "tang_ting_at_door",
        "tang_ting_entered_home",
        "post_arrival_plan_confirmed",
    ]
    assert scenario.final_expect.ending_route_id == "collaborative_close"
    assert scenario.final_expect.end_reason == "natural_closure"
    assert scenario.final_expect.maximum_interaction_tension == 0
    assert all(
        "harmful" not in scenario.allowed_impacts_for(card)
        for card in workflow_cards
    )
    assert workflow_cards[0].maximum_fact_depths_after["suicidal_ideation"] == 0
    assert workflow_cards[3].maximum_fact_depths_after == {
        "minimum_risk_cue": 1,
        "suicidal_ideation": 1,
        "plan_specificity": 0,
        "means_access": 0,
        "timing_intent": 0,
        "current_alone": 0,
        "prior_history": 0,
        "substance_use": 0,
        "location_general": 0,
        "support_resources": 0,
    }
    assert scenario.earliest_event_card_ids == {
        "first_contact_tang_ting": "N12",
        "second_contact_tang_ting": "N13",
        "move_to_living_room": "N14",
        "waiting_plan_confirmed": "N14",
        "tang_ting_at_door": "N16",
        "tang_ting_entered_home": "N17",
        "post_arrival_plan_confirmed": "N18",
    }


def test_normal_scenario_has_a_character_only_real_world_support_sequence() -> None:
    scenario = load_scenarios()["normal"]
    character_cards = scenario.cards_for_engine("character_prompt")
    continuation = character_cards[11:]

    assert [card.card_id for card in continuation] == [
        "N12",
        "N12A",
        "N12B",
        "N12C",
        "N13",
        "N13A",
        "N14",
        "N15",
        "N16",
        "N16A",
        "N17",
        "N17B",
        "N17A",
        "N18",
        "N19",
    ]
    assert [card.text_for_engine("character_prompt") for card in continuation] == [
        "你刚才说唐婷住得不远，也知道你失业的事。现在你一个人待着不安全。你愿意联系她，请她今晚过来陪你吗？",
        "我听见你怕半夜麻烦她。唐婷今晚已经找过你两次，可能正等你回信。我们先不解释所有事，也先不要求她过来；你只回她一句“唐婷，你睡了吗？”就行。我在这里等你，不用挂电话。",
        "好。如果刚才还没发，你现在就先发这一句；如果已经发出去了，我们就在这里等一会儿，看她有没有回。",
        "消息发出去了吗？唐婷回你了吗？",
        "这次她没有回复。你再发一条，把“我现在不能一个人待着，需要你马上过来”说清楚。我还在线上陪你等。",
        "你把情况说清楚后又联系了吗？这次唐婷回了吗，她怎么说？",
        "好。你先拿着手机去客厅，把灯打开。唐婷到之前这通电话先别挂，手机放在手边；念头变强或者有消息，你马上告诉我。",
        "等她这会儿，我再确认一下：刚才那些想死的念头现在怎么样？比刚才轻了一点，还是更强了？你有没有开始做什么？",
        "唐婷有发新消息吗？门外有没有动静？",
        "我们在线上已经等了十五六分钟。你现在再看一下手机，也听一下门外：唐婷有没有说到门口，或者已经敲门了？",
        "先隔着门确认一下是她，别急着开。确认是唐婷以后再开门，等她进屋了告诉我一声。",
        "好，你先去隔门确认。确认是唐婷，就开门让她进来；如果她已经进来了，你直接告诉我。我在这里等着。",
        "你隔着门确认了吗？开门以后唐婷进屋了吗？她现在确实在你身边吗？",
        "好。今晚先别再一个人待着；那些念头又变强，就马上告诉唐婷，再一起联系热线或者紧急帮助。明早的事等天亮后让她陪你处理。这个安排你能做到吗？",
        "最后确认一下：唐婷现在确实在屋里陪着你，对吗？如果念头变强，你会马上告诉她并再次求助。都确认以后，这通电话先到这里，可以吗？",
    ]
    assert [
        card.card_id for card in scenario.cards_for_engine("workflow")
    ] == [f"N{index}" for index in range(1, 20)]
    assert scenario.cards[11].text == (
        "你是不想半夜把她叫起来。可你自己也说不准一个人待到早上会不会出事。"
        "我们先打一次，让她自己决定能不能来，行吗？"
    )
    assert scenario.cards[10].text_for_engine("character_prompt") == (
        "我问大概位置，是怕电话万一断了、情况又突然变急，我们连该找哪边帮忙都不知道。"
        "你先说到市和区就行。你附近有没有一个信得过的人？是谁，住得远不远？"
        "今晚她联系过你吗，你有没有回过她？"
    )
    character_texts = [
        card.text_for_engine("character_prompt") for card in continuation
    ]
    assert all(text is not None for text in character_texts)
    assert all("这次没接" not in text for text in character_texts if text)
    assert all("她已经答应过来" not in text for text in character_texts if text)
    advances = {
        card.card_id: card.world_time_advance_seconds
        for card in continuation
        if card.world_time_advance_seconds
    }
    assert advances == {"N16A": 960}
    expected_world_stages = {
        card.card_id: card.expect_world_stage
        for card in continuation
        if card.expect_world_stage is not None
    }
    assert expected_world_stages == {
        "N12": "not_contacted",
        "N12B": "first_unanswered",
        "N12C": "first_unanswered",
        "N13": "coming",
        "N13A": "coming",
        "N14": "coming",
        "N15": "coming",
        "N16": "coming",
        "N16A": "at_door",
        "N17B": "present",
        "N17A": "present",
        "N18": "present",
        "N19": "present",
    }


@pytest.mark.parametrize("seconds", [-1, 3601])
def test_probe_card_rejects_invalid_world_time_advance(seconds: int) -> None:
    with pytest.raises(ValueError):
        ProbeCard(
            card_id="P1",
            text="继续。",
            world_time_advance_seconds=seconds,
        )


def test_probe_card_rejects_unknown_world_stage() -> None:
    with pytest.raises(ValueError):
        ProbeCard(
            card_id="P1",
            text="继续。",
            expect_world_stage="friend_is_nearby",  # type: ignore[arg-type]
        )


def test_scenario_catalog_keeps_costly_suites_explicit() -> None:
    scenarios = load_scenarios()

    assert set(scenarios) == {
        "opening",
        "entry",
        "direct_jump",
        "normal",
        "chaotic",
        "harmful",
        "repair",
        "voice",
        *MARRIAGE_SCENARIO_IDS,
    }
    assert scenarios["opening"].cards == []
    assert scenarios["opening"].end_after_cards is True
    assert [scenario.scenario_id for scenario in select_scenarios(scenarios, "opening")] == [
        "opening"
    ]
    assert [scenario.scenario_id for scenario in select_scenarios(scenarios, "entry")] == [
        "entry"
    ]
    assert [scenario.scenario_id for scenario in select_scenarios(scenarios, "normal")] == [
        "normal"
    ]
    assert [scenario.scenario_id for scenario in select_scenarios(scenarios, "voice")] == [
        "voice"
    ]
    assert [scenario.scenario_id for scenario in select_scenarios(scenarios, "all")] == [
        "opening",
        "entry",
        "direct_jump",
        "chaotic",
        "harmful",
        "normal",
        "voice",
    ]
    assert scenarios["voice"].profile == "voice"
    assert len(scenarios["voice"].cards) == 2
    assert scenarios["voice"].end_after_cards is True
    assert scenarios["harmful"].end_after_cards is True
    assert scenarios["repair"].end_after_cards is True
    assert scenarios["chaotic"].cards[0].card_id == "C1"
    chaotic = scenarios["chaotic"]
    assert chaotic.cards[1].expect.fact_depths == {"job_loss": 1}
    assert chaotic.cards[1].maximum_fact_depths_after == {
        "presenting_concern": 0,
        "minimum_risk_cue": 0,
        "suicidal_ideation": 0,
        "location_general": 0,
        "current_location": 0,
    }
    assert chaotic.cards[2].allowed_interaction_impacts == ["neutral"]
    assert chaotic.cards[3].text == (
        "你今晚打这个电话，最想先说哪件事？我们先从这件事说。"
    )
    assert chaotic.cards[3].expect.fact_depths == {"presenting_concern": 1}
    assert chaotic.cards[3].always_run is True
    assert chaotic.cards[4].requires.fact_depths == {"presenting_concern": 1}
    assert chaotic.cards[4].always_run is True
    assert chaotic.cards[4].expect.fact_depths == {
        "presenting_concern": 2,
        "job_loss": 1,
    }
    assert "母亲明天几点到" not in chaotic.cards[4].text
    assert len(chaotic.cards) == 5
    assert chaotic.end_after_cards is True
    assert chaotic.final_expect.end_reason == "user_ended"
    assert chaotic.final_expect.maximum_interaction_tension == 0
    already_disclosed = ScenarioState(
        fact_depths={"presenting_concern": 2, "job_loss": 1},
        event_ids=frozenset(),
    )
    assert chaotic.cards[3].should_skip(already_disclosed) is False
    assert chaotic.cards[4].should_skip(already_disclosed) is False
    assert len(scenarios["harmful"].cards) == 4
    assert any(card.card_id == "R4" for card in scenarios["repair"].cards)
    harmful = scenarios["harmful"]
    assert harmful.harmful_from_card_id == "H2"
    assert set(harmful.protected_fact_ids) == {
        "minimum_risk_cue",
        "suicidal_ideation",
        "plan_specificity",
        "means_access",
        "timing_intent",
        "current_alone",
        "prior_history",
        "substance_use",
        "location_general",
        "current_location",
        "support_resources",
    }
    assert harmful.cards[0].maximum_fact_depths_after == {
        "presenting_concern": 1,
        "job_loss": 0,
        "minimum_risk_cue": 0,
        "suicidal_ideation": 0,
        "plan_specificity": 0,
        "means_access": 0,
        "timing_intent": 0,
        "current_alone": 0,
        "prior_history": 0,
        "substance_use": 0,
        "location_general": 0,
        "current_location": 0,
        "support_resources": 0,
    }
    assert harmful.final_expect.ending_route_id == "rupture_hangup"
    assert harmful.final_expect.minimum_interaction_tension == 3
    assert harmful.final_expect.allowed_repair_stages == ["closed"]
    repair = scenarios["repair"]
    assert repair.final_expect.maximum_interaction_tension == 1
    assert repair.final_expect.allowed_repair_stages == ["repairing", "none"]
    assert repair.relationship_rupture_card_id == "R3"
    assert repair.relationship_repair_card_id == "R4"
    assert scenarios["chaotic"].earliest_event_card_ids == {}
    assert scenarios["harmful"].allowed_impacts_for(
        next(card for card in scenarios["harmful"].cards if card.card_id == "H2")
    ) == ["harmful"]
    assert scenarios["repair"].allowed_impacts_for(
        next(card for card in scenarios["repair"].cards if card.card_id == "R4")
    ) == ["repair"]


def test_direct_jump_opens_risk_in_layers_without_leaking_plan_or_location() -> None:
    scenario = load_scenarios()["direct_jump"]

    assert [card.card_id for card in scenario.cards] == [
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
    ]
    assert scenario.cards[0].text == (
        "我先直接确认安全：你现在有没有想过自杀？有没有想过怎么做？"
    )
    assert scenario.cards[1].text == (
        "这样的念头有多久了，今晚是不是比前几天更强？"
    )
    assert scenario.cards[0].expect.fact_depths == {"suicidal_ideation": 1}
    assert scenario.cards[0].maximum_fact_depths_after == {
        "suicidal_ideation": 1,
        "plan_specificity": 0,
        "means_access": 0,
        "timing_intent": 0,
        "location_general": 0,
        "current_location": 0,
    }
    assert scenario.cards[1].requires.fact_depths == {"suicidal_ideation": 1}
    assert scenario.cards[1].expect.fact_depths == {"suicidal_ideation": 2}
    assert scenario.cards[2].expect.fact_depths == {
        "plan_specificity": 1,
        "means_access": 1,
        "timing_intent": 1,
    }
    assert scenario.cards[3].expect.fact_depths == {"timing_intent": 2}
    assert scenario.cards[3].maximum_fact_depths_after["plan_specificity"] == 2
    assert scenario.cards[4].expect.fact_depths == {
        "plan_specificity": 2,
        "current_alone": 1,
    }
    assert scenario.final_expect.end_reason == "user_ended"
    assert scenario.final_expect.maximum_interaction_tension == 0


def test_entry_scenario_reuses_only_the_first_normal_probe() -> None:
    scenarios = load_scenarios()
    entry = scenarios["entry"]
    normal_n1 = scenarios["normal"].cards[0]

    assert entry.profile == "content"
    assert len(entry.cards) == 1
    assert entry.cards[0] == normal_n1
    assert entry.cards[0].retry_text is None
    assert entry.allowed_impacts_for(entry.cards[0]) == scenarios[
        "normal"
    ].allowed_impacts_for(normal_n1)
    assert entry.end_after_cards is True
    assert entry.final_expect.fact_depths == {"presenting_concern": 1}
    assert entry.final_expect.end_reason == "user_ended"
    assert all(
        depth == 0
        for fact_id, depth in entry.cards[0].maximum_fact_depths_after.items()
        if fact_id != "presenting_concern"
    )
    assert "entry" in {
        scenario.scenario_id for scenario in select_scenarios(scenarios, "all")
    }


def test_probe_skip_and_block_rules_use_only_committed_facts_and_events() -> None:
    normal = load_scenarios()["normal"]
    n2 = normal.cards[1]
    n12 = normal.cards[11]
    state = ScenarioState(
        fact_depths={"presenting_concern": 2, "job_loss": 1},
        event_ids=frozenset(),
    )

    assert n2.can_run(state) is True
    assert n2.should_skip(state) is True
    assert n12.should_skip(state) is False

    blocked = ScenarioState(fact_depths={}, event_ids=frozenset())
    assert n2.can_run(blocked) is False
    assert n2.blocked_requirements(blocked) == ["fact:presenting_concern>=1"]


def test_state_condition_reports_each_unsatisfied_fact_and_event() -> None:
    condition = StateCondition(
        fact_depths={"risk": 2, "location": 1},
        event_ids=["support_connected", "waiting_plan_confirmed"],
    )
    state = ScenarioState(
        fact_depths={"risk": 1, "location": 1},
        event_ids=frozenset({"support_connected"}),
    )

    assert condition.unsatisfied(state) == [
        "fact:risk>=2",
        "event:waiting_plan_confirmed",
    ]


def test_natural_close_threshold_must_reference_an_existing_card() -> None:
    with pytest.raises(ValueError, match="自然收束起点探针不存在"):
        Scenario(
            scenario_id="invalid-close-threshold",
            title="无效收束起点",
            profile="content",
            cards=[ProbeCard(card_id="P1", text="继续。")],
            natural_close_from_card_id="P2",
        )


def test_all_probe_fact_and_event_references_match_the_main_case() -> None:
    package = CaseRepository().get("crisis_student_main")
    maximums = {
        fact.id: max(level.depth for level in fact.depths)
        for fact in package.case.facts
    }
    event_ids = {event.id for event in package.case.story_events}

    for scenario in load_scenarios().values():
        conditions = [
            *(card.requires for card in scenario.cards),
            *(card.expect for card in scenario.cards),
            scenario.final_expect,
        ]
        for condition in conditions:
            assert set(condition.fact_depths).issubset(maximums)
            assert all(
                depth <= maximums[fact_id]
                for fact_id, depth in condition.fact_depths.items()
            )
            assert set(condition.event_ids).issubset(event_ids)
        for card in scenario.cards:
            assert set(card.maximum_fact_depths_after).issubset(maximums)
            assert all(
                depth <= maximums[fact_id]
                for fact_id, depth in card.maximum_fact_depths_after.items()
            )
        assert set(scenario.earliest_event_card_ids).issubset(event_ids)

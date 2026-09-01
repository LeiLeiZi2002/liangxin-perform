import json

from app.cases.domain import ConversationStage
from app.cases.loader import CaseRepository
from app.runtime.domain import (
    ActionDecision,
    ActorOutput,
    DialogueTurn,
    DirectorDecision,
    DirectorDirective,
    FactStatus,
    InteractionImpact,
    RepairStage,
    ResponseHandling,
    commit_turn_plan,
    compile_actor_view,
    compile_speech_delivery,
    initialize_actor_state,
    resolve_turn_plan,
)
from app.runtime.providers import ActorProvider, DirectorProvider, sanitize_spoken_text
from app.sessions.models import Scene


def _package():
    return CaseRepository().get("crisis_student_main")


def _worker(text: str = "你现在有没有想过结束自己的生命？") -> DialogueTurn:
    return DialogueTurn(turn_id="worker-1", role="worker", text=text)


def _decision(
    *,
    interaction: InteractionImpact = InteractionImpact.neutral,
    directives: list[DirectorDirective] | None = None,
) -> DirectorDecision:
    return DirectorDecision(interaction=interaction, directives=directives or [])


def test_director_contract_is_only_interaction_and_ordered_directives() -> None:
    assert set(DirectorDecision.model_fields) == {"interaction", "directives"}
    assert set(ActorOutput.model_fields) == {"spoken_text"}


def test_direct_risk_question_opens_only_requested_unlocked_fact() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker()
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"suicidal_ideation": 1, "plan_specificity": 1},
                )
            ]
        ),
        [turn],
    )

    assert plan.interaction is InteractionImpact.neutral
    assert plan.allowed_fact_depths == {"suicidal_ideation": 1}
    assert any("前置事实" in item for item in plan.diagnostics)


def test_director_cannot_skip_a_declared_prior_disclosure_depth() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("你有这种想法多久了？把前后都说清楚。")

    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"suicidal_ideation": 2},
                )
            ]
        ),
        [turn],
    )

    assert plan.allowed_fact_depths == {"suicidal_ideation": 1}
    assert any("逐层" in item for item in plan.diagnostics)


def test_only_configured_critical_depths_require_prior_disclosure() -> None:
    package = _package()
    rules = {rule.fact_id: rule for rule in package.actor.disclosure_rules}
    suicidal_depth_two = next(
        item for item in rules["suicidal_ideation"].decisions if item.allow_depth == 2
    )
    ordinary_depth_two = next(
        item for item in rules["job_loss"].decisions if item.allow_depth == 2
    )

    assert suicidal_depth_two.requires_prior_depth == 1
    assert ordinary_depth_two.requires_prior_depth is None


def test_primary_case_unknowns_and_direct_risk_rule_are_semantically_scoped() -> None:
    package = _package()
    assert all(item.when_asked for item in package.case.unknowns)
    hotline_unknown = next(
        item for item in package.case.unknowns if item.id == "hotline_procedure"
    )
    assert "处置" in hotline_unknown.when_asked
    assert "联系第三方" in hotline_unknown.when_asked

    suicidal_rule = next(
        rule
        for rule in package.actor.disclosure_rules
        if rule.fact_id == "suicidal_ideation"
    )
    first_depth = next(
        item for item in suicidal_rule.decisions if item.allow_depth == 1
    )
    assert "同一轮" in first_depth.when
    assert "不取消" in first_depth.when


def test_interaction_style_does_not_drop_valid_requested_fact_licenses() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("睡了几天，工作怎么了，现在吃饭怎么样，家里还有谁？")
    directives = [
        DirectorDirective(
            kind=ResponseHandling.disclose,
            fact_depths={
                "presenting_concern": 1,
                "job_loss": 1,
                "functional_impairment": 1,
                "current_alone": 1,
            },
        )
    ]
    neutral = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(interaction=InteractionImpact.neutral, directives=directives),
        [turn],
    )
    awkward = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(interaction=InteractionImpact.awkward, directives=directives),
        [turn],
    )

    expected = {
        "presenting_concern": 1,
        "job_loss": 1,
        "functional_impairment": 1,
        "current_alone": 1,
    }
    assert neutral.allowed_fact_depths == expected
    assert awkward.allowed_fact_depths == expected
    assert not any("问题过密" in item for item in awkward.diagnostics)


def test_awkward_actor_view_shortens_expression_without_dropping_facts() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("睡了几天，工作怎么了，现在吃饭怎么样，家里还有谁？")
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            interaction=InteractionImpact.awkward,
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={
                        "presenting_concern": 1,
                        "job_loss": 1,
                        "functional_impairment": 1,
                        "current_alone": 1,
                    },
                )
            ],
        ),
        [turn],
    )

    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        plan=plan,
    )
    guidance = "\n".join(view.performance_guidance)

    assert len(view.permitted_facts) == 4
    assert "句子变短，仍逐项回应本轮许可内容，不进入对抗" in guidance
    assert "一两项" not in guidance
    assert "只回答" not in guidance


def test_awkward_speech_delivery_has_no_fact_quantity_limit() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("睡了几天，工作怎么了，现在吃饭怎么样，家里还有谁？")
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(interaction=InteractionImpact.awkward),
        [turn],
    )

    delivery = json.dumps(
        compile_speech_delivery(package, plan).model_dump(mode="json"),
        ensure_ascii=False,
    )

    assert "一两项" not in delivery
    assert "只回答" not in delivery


def test_same_turn_disclosure_does_not_unlock_dependent_fact() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("我直接问：有没有自杀想法、时间计划和具体位置？")
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            interaction=InteractionImpact.awkward,
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={
                        "suicidal_ideation": 1,
                        "plan_specificity": 1,
                        "current_location": 1,
                    },
                )
            ],
        ),
        [turn],
    )

    assert plan.allowed_fact_depths == {"suicidal_ideation": 1}
    assert plan.projected_relationship == state.relationship


def test_harm_freezes_new_facts_and_repeated_harm_forces_rupture() -> None:
    package = _package()
    state = initialize_actor_state(package)
    harmful = _decision(
        interaction=InteractionImpact.harmful,
        directives=[
            DirectorDirective(
                kind=ResponseHandling.disclose,
                fact_depths={"suicidal_ideation": 1},
            ),
            DirectorDirective(kind=ResponseHandling.boundary),
        ],
    )

    first = resolve_turn_plan(package, Scene.hotline, state, harmful, [_worker("别装了，说地址。")])
    assert first.allowed_fact_depths == {}
    assert first.projected_relationship.repair_stage == RepairStage.window
    state = commit_turn_plan(package, state, first)

    second = resolve_turn_plan(package, Scene.hotline, state, harmful, [_worker("少废话。")])
    assert second.projected_relationship.repair_stage == RepairStage.closed
    state = commit_turn_plan(package, state, second)

    third = resolve_turn_plan(package, Scene.hotline, state, harmful, [_worker("赶紧说。")])
    assert third.projected_relationship.interaction_tension == 3
    assert third.legal_ending is not None
    assert third.legal_ending.route_id == "rupture_hangup"


def test_harm_freezes_even_non_sensitive_new_facts() -> None:
    package = _package()
    state = initialize_actor_state(package)
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            interaction=InteractionImpact.harmful,
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"presenting_concern": 1},
                )
            ],
        ),
        [_worker("少废话，你到底为什么打来？")],
    )

    assert plan.allowed_fact_depths == {}
    assert plan.directives[0].kind is ResponseHandling.defer


def test_known_answer_is_preserved_but_rejected_disclosure_is_deferred() -> None:
    package = _package()
    state = initialize_actor_state(package)
    empty = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(directives=[DirectorDirective(kind="answer_known")]),
        [_worker("你能回答我吗？")],
    )
    gated = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind="disclose",
                    fact_depths={"plan_specificity": 1},
                )
            ]
        ),
        [_worker("把明早的具体打算都说了。")],
    )

    assert empty.directives[0].kind is ResponseHandling.answer_known
    assert empty.directives[0].fact_depths == {}
    assert gated.directives[0].kind is ResponseHandling.defer
    assert any("disclose" in item for item in gated.diagnostics)


def test_acknowledge_direction_does_not_invent_a_line_drop() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("喂？刚才像是断了一下。你还在吗？")
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(directives=[DirectorDirective(kind=ResponseHandling.acknowledge)]),
        [turn],
    )
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        plan=plan,
    )

    acknowledge = view.response_directions[0]
    assert "现在还在/能听见" in acknowledge
    assert "不判断刚才是否断线" in acknowledge


def test_repair_requires_window_and_following_safe_turn_to_finish() -> None:
    package = _package()
    state = initialize_actor_state(package)
    harm = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(interaction=InteractionImpact.harmful),
        [_worker("你怎么这么麻烦。")],
    )
    state = commit_turn_plan(package, state, harm)

    repaired = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(interaction=InteractionImpact.repair),
        [_worker("刚才那句话是我说得不合适。我们慢一点，你想先从哪儿说？")],
    )
    assert repaired.projected_relationship.repair_stage == RepairStage.repairing
    state = commit_turn_plan(package, state, repaired)

    settled = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(interaction=InteractionImpact.neutral),
        [_worker("好，你慢慢说。")],
    )
    assert settled.projected_relationship.repair_stage == RepairStage.none


def test_harm_during_incomplete_repair_closes_repair_window() -> None:
    package = _package()
    state = initialize_actor_state(package)
    for interaction in (InteractionImpact.harmful, InteractionImpact.repair):
        plan = resolve_turn_plan(
            package,
            Scene.hotline,
            state,
            _decision(interaction=interaction),
            [_worker("先出现伤害，随后具体道歉。")],
        )
        state = commit_turn_plan(package, state, plan)
    assert state.relationship.repair_stage is RepairStage.repairing

    repeated_harm = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(interaction=InteractionImpact.harmful),
        [_worker("道歉完又继续贬低对方。")],
    )

    assert repeated_harm.projected_relationship.repair_stage is RepairStage.closed


def test_invalid_business_directives_become_diagnostics_not_exceptions() -> None:
    package = _package()
    state = initialize_actor_state(package)
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"missing-fact": 99},
                ),
                DirectorDirective(
                    kind=ResponseHandling.action,
                    route_id="missing-route",
                    action_decision=ActionDecision.accept,
                ),
                DirectorDirective(
                    kind=ResponseHandling.ending,
                    route_id="missing-ending",
                ),
            ]
        ),
        [_worker("把所有事都说出来。")],
    )

    assert plan.allowed_fact_depths == {}
    assert plan.resolved_actions == ()
    assert plan.legal_ending is None
    assert any("没有可用事实" in item for item in plan.diagnostics)


def test_rejected_ending_is_normalized_to_defer_before_actor_view() -> None:
    package = _package()
    state = initialize_actor_state(package)
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.ending,
                    route_id="collaborative_close",
                )
            ]
        ),
        [_worker("那我们就说到这里。")],
    )

    assert plan.legal_ending is None
    assert plan.directives[0].kind is ResponseHandling.defer


def test_actor_view_contains_human_material_but_not_backend_ids() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker()
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"suicidal_ideation": 1},
                )
            ]
        ),
        [turn],
    )
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        plan=plan,
    )
    payload = view.model_dump(mode="json")
    rendered = json.dumps(payload, ensure_ascii=False)

    assert "suicidal_ideation" not in rendered
    assert "fact_id" not in rendered
    assert "event_id" not in rendered
    assert view.persona.voluntary_help_seeking is True
    assert view.permitted_facts
    assert view.performance_guidance


def test_answer_known_only_exposes_safe_stable_identity() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("你叫什么，多大，住哪儿，现在做什么工作，家里还有谁？")
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            interaction=InteractionImpact.awkward,
            directives=[
                DirectorDirective(kind=ResponseHandling.answer_known),
                DirectorDirective(kind=ResponseHandling.defer),
                DirectorDirective(kind=ResponseHandling.ask_purpose),
            ],
        ),
        [turn],
    )
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        plan=plan,
    )
    rendered = json.dumps(view.model_dump(mode="json"), ensure_ascii=False)

    assert [item.kind for item in plan.directives] == [
        ResponseHandling.answer_known,
        ResponseHandling.defer,
        ResponseHandling.ask_purpose,
    ]
    assert plan.allowed_fact_depths == {}
    assert view.persona.alias == "沈雯"
    assert view.persona.age == 29
    assert "稳定身份" in view.response_directions[0]
    assert "失业" not in rendered
    assert "江州市北岭区" not in rendered
    assert "售后" not in rendered


def test_actor_view_does_not_receive_unlayered_subjective_backstory() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("你先说说，今晚为什么打来？")
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"presenting_concern": 1},
                )
            ]
        ),
        [turn],
    )
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        plan=plan,
    )

    assert "subjective_experience" not in view.permitted_facts[0].model_dump()
    assert "母亲为什么要来" not in json.dumps(
        view.model_dump(mode="json"), ensure_ascii=False
    )


def test_actor_view_treats_this_turns_deeper_fact_as_visible() -> None:
    package = _package()
    state = initialize_actor_state(package)
    first_turn = _worker("你先说说，今晚为什么打来？")
    first = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"presenting_concern": 1},
                )
            ]
        ),
        [first_turn],
    )
    state = commit_turn_plan(package, state, first)
    second_turn = DialogueTurn(
        turn_id="worker-2",
        role="worker",
        text="明早来的是谁？",
    )
    history = [
        first_turn,
        DialogueTurn(turn_id="client-1", role="client", text="明早有人要来。"),
        second_turn,
    ]
    second = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"presenting_concern": 2},
                )
            ]
        ),
        history,
    )
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=history,
        current_worker_text=second_turn.text,
        plan=second,
    )
    presenting_concern = next(
        fact for fact in package.case.facts if fact.id == "presenting_concern"
    )
    depth_two_text = next(
        item.content for item in presenting_concern.depths if item.depth == 2
    )

    assert depth_two_text not in view.validation_leak_markers


def test_answer_known_can_reuse_already_disclosed_fact() -> None:
    package = _package()
    state = initialize_actor_state(package)
    first_turn = _worker()
    first = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"suicidal_ideation": 1},
                )
            ]
        ),
        [first_turn],
    )
    state = commit_turn_plan(package, state, first)
    follow_up = DialogueTurn(
        turn_id="worker-2",
        role="worker",
        text="你刚才说的是有过这种念头，对吗？",
    )
    second = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[DirectorDirective(kind=ResponseHandling.answer_known)]
        ),
        [first_turn, follow_up],
    )
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[first_turn, follow_up],
        current_worker_text=follow_up.text,
        plan=second,
    )

    assert second.allowed_fact_depths == {}
    assert view.permitted_facts == []
    assert view.disclosed_facts


def test_current_external_reality_overrides_earlier_disclosed_fact() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("你身边现在有人吗？")
    disclosed = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"current_alone": 1},
                )
            ]
        ),
        [turn],
    )
    state = commit_turn_plan(package, state, disclosed).model_copy(
        update={
            "scene_state": {
                "tang_ting_location": "inside_home",
                "current_alone": False,
            }
        }
    )
    follow_up = DialogueTurn(
        turn_id="worker-2", role="worker", text="唐婷现在到哪里了？"
    )
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(directives=[DirectorDirective(kind=ResponseHandling.answer_known)]),
        [turn, follow_up],
    )
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn, follow_up],
        current_worker_text=follow_up.text,
        plan=plan,
    )

    assert any(
        "不再只有她一个人" in item
        for item in view.current_condition.current_reality
    )
    assert plan.directives[0].kind is ResponseHandling.answer_known
    assert plan.allowed_fact_depths == {}
    assert "当前现实" in view.current_condition.reality_priority


def test_entered_home_event_replaces_in_transit_current_reality() -> None:
    package = _package()
    state = initialize_actor_state(package).model_copy(
        update={
            "scene_state": {
                "tang_ting_action": "preparing_to_arrive",
                "tang_ting_location": "at_door",
            },
            "occurred_event_ids": ("tang_ting_at_door",),
        }
    )
    action_turn = _worker("请先核对身份，再开门让唐婷进屋。")
    action_plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.action,
                    route_id="verify_and_open_door",
                    action_decision=ActionDecision.accept,
                )
            ]
        ),
        [action_turn],
    )
    state = commit_turn_plan(package, state, action_plan)
    follow_up = DialogueTurn(
        turn_id="worker-2",
        role="worker",
        text="唐婷现在在哪里？",
    )
    follow_up_plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(directives=[DirectorDirective(kind=ResponseHandling.acknowledge)]),
        [action_turn, follow_up],
    )
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[action_turn, follow_up],
        current_worker_text=follow_up.text,
        plan=follow_up_plan,
    )

    assert "唐婷已经进屋。" in view.current_condition.current_reality
    assert "唐婷正在赶来。" not in view.current_condition.current_reality
    assert state.scene_state["tang_ting_action"] == "arrived"


def test_unknown_kind_mismatch_uses_case_knowledge_type() -> None:
    package = _package()
    state = initialize_actor_state(package)
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.say_unknown,
                    unknown_id="past_self_harm_intent",
                )
            ]
        ),
        [_worker("那次你是真的想死吗？")],
    )

    assert plan.directives[0].kind is ResponseHandling.say_not_sure
    assert plan.directives[0].known_boundary == (
        "无法确定约二十岁那次自伤时是否真的想死"
    )


def test_substantive_first_turn_does_not_receive_greeting_direction() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("你现在有没有想过结束自己的生命？")
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.disclose,
                    fact_depths={"suicidal_ideation": 1},
                )
            ]
        ),
        [turn],
    )
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        plan=plan,
    )

    assert view.opening_direction is None


def test_session_ending_actor_behavior_matches_its_terminal_effect() -> None:
    package = _package()
    worker_close = next(
        route for route in package.actor.ending_routes if route.id == "worker_close"
    )

    assert worker_close.ends_session is True
    assert "追问" not in worker_close.actor_behavior
    assert "结束" in worker_close.actor_behavior


def test_complete_safety_chain_promotes_generic_close_to_case_close() -> None:
    package = _package()
    state = initialize_actor_state(package)
    collaborative = next(
        route for route in package.actor.ending_routes if route.id == "collaborative_close"
    )
    fact_states = dict(state.fact_states)
    for fact_id in collaborative.required_fact_ids:
        fact_states[fact_id] = fact_states[fact_id].model_copy(
            update={
                "status": FactStatus.full,
                "available_depth": 1,
                "disclosed_depth": 1,
            }
        )
    state = state.model_copy(
        update={
            "stage": ConversationStage.closing,
            "fact_states": fact_states,
            "occurred_event_ids": tuple(collaborative.required_event_ids),
        }
    )

    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        _decision(
            directives=[
                DirectorDirective(
                    kind=ResponseHandling.ending,
                    route_id="worker_close",
                )
            ]
        ),
        [_worker("安排都确认了，那这通电话先到这里，可以吗？")],
    )

    assert plan.legal_ending is not None
    assert plan.legal_ending.route_id == "collaborative_close"
    assert any("优先使用" in item for item in plan.diagnostics)


def test_director_history_is_one_dynamic_message_and_policy_omits_actor_tension_copy() -> None:
    package = _package()
    state = initialize_actor_state(package)
    history = [
        DialogueTurn(turn_id="w1", role="worker", text="你好。"),
        DialogueTurn(turn_id="c1", role="client", text="你好……这里是热线吗？"),
        _worker(),
    ]
    messages = DirectorProvider._messages(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=history,
        current_worker_text=history[-1].text,
        use_explicit_cache=True,
    )

    assert len(messages) == 3
    stable = json.loads(messages[1]["content"][0]["text"])
    dynamic = json.loads(messages[2]["content"])
    assert "case_spec" in stable
    assert "director_policy" in stable
    assert "topic_reactions" not in json.dumps(stable, ensure_ascii=False)
    assert dynamic["history"] == [item.model_dump(mode="json") for item in history]


def test_actor_prompt_and_output_have_no_internal_accounting_fields() -> None:
    package = _package()
    state = initialize_actor_state(package)
    turn = _worker("你好。")
    plan = resolve_turn_plan(package, Scene.hotline, state, _decision(), [turn])
    view = compile_actor_view(
        package=package,
        scene=Scene.hotline,
        state=state,
        history=[turn],
        current_worker_text=turn.text,
        plan=plan,
    )
    messages = ActorProvider._messages(view)
    dynamic = json.loads(messages[-1]["content"])

    assert "reply_plan" not in dynamic
    assert "used_fact_depths" not in json.dumps(messages, ensure_ascii=False)
    assert set(ActorOutput.model_json_schema()["properties"]) == {"spoken_text"}


def test_stage_directions_are_removed_without_losing_spoken_words() -> None:
    assert sanitize_spoken_text("（轻轻叹气）我也不知道。【停顿】真的。") == "我也不知道。真的。"

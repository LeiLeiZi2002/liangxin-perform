from app.cases.loader import CaseRepository
from app.runtime.domain import (
    ActionDecision,
    ConversationStage,
    DialogueTurn,
    DirectorDecision,
    DirectorDirective,
    EndingState,
    FactState,
    FactStatus,
    PendingEventState,
    commit_turn_plan,
    initialize_actor_state,
    resolve_turn_plan,
)
from app.sessions.models import Scene


def _disclose(state, **depths: int):
    fact_states = dict(state.fact_states)
    for fact_id, depth in depths.items():
        fact_states[fact_id] = FactState(
            status=FactStatus.full,
            available_depth=depth,
            disclosed_depth=depth,
            evidence_turn_ids=("seed",),
        )
    return state.model_copy(update={"fact_states": fact_states})


def _turn(text: str, index: int = 1) -> list[DialogueTurn]:
    return [DialogueTurn(turn_id=f"worker-{index}", role="worker", text=text)]


def test_accepted_action_is_committed_by_workflow_not_actor() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = _disclose(initialize_actor_state(package), support_resources=2)
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        DirectorDecision(
            interaction="neutral",
            directives=[
                DirectorDirective(
                    kind="action",
                    route_id="attempt_tang_ting_contact",
                    action_decision=ActionDecision.accept,
                )
            ],
        ),
        _turn("我们先给唐婷打一次电话，让她自己决定能不能来，好吗？"),
    )

    assert plan.resolved_actions[0].event_id == "first_contact_tang_ting"
    committed = commit_turn_plan(package, state, plan)
    assert "first_contact_tang_ting" in committed.occurred_event_ids
    assert committed.event_results["first_contact_tang_ting"] == "no_answer"


def test_invalid_action_becomes_defer_without_state_change() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)
    plan = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        DirectorDecision(
            interaction="neutral",
            directives=[
                DirectorDirective(
                    kind="action",
                    route_id="attempt_tang_ting_contact",
                    action_decision="accept",
                )
            ],
        ),
        _turn("现在联系唐婷。"),
    )

    assert plan.resolved_actions == ()
    assert plan.directives[0].kind.value == "defer"
    assert commit_turn_plan(package, state, plan).occurred_event_ids == ()


def test_deferred_door_event_waits_one_complete_actor_turn() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package).model_copy(
        update={
            "occurred_event_ids": ("waiting_plan_confirmed",),
            "pending_events": {
                "tang_ting_at_door": PendingEventState(available_after_actor_turn=3)
            },
        }
    )
    history_before_due = [
        DialogueTurn(turn_id="c1", role="client", text="她说十二三分钟到。"),
        DialogueTurn(turn_id="w2", role="worker", text="我们等一会儿。"),
    ]
    early = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        DirectorDecision(interaction="neutral"),
        history_before_due,
    )
    assert early.due_observations == ()

    due = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        DirectorDecision(interaction="neutral"),
        [
            *history_before_due,
            DialogueTurn(turn_id="c2", role="client", text="嗯，我还在客厅。"),
            DialogueTurn(turn_id="w3", role="worker", text="门外现在有动静吗？"),
        ],
    )
    assert [item.event_id for item in due.due_observations] == ["tang_ting_at_door"]
    committed = commit_turn_plan(package, state, due)
    assert "tang_ting_at_door" in committed.occurred_event_ids
    assert "tang_ting_at_door" not in committed.pending_events


def test_collaborative_close_cannot_use_same_turn_final_action() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = _disclose(
        initialize_actor_state(package),
        suicidal_ideation=1,
        timing_intent=2,
        current_alone=1,
        support_resources=2,
    ).model_copy(
        update={
            "stage": ConversationStage.closing,
            "occurred_event_ids": (
                "first_contact_tang_ting",
                "second_contact_tang_ting",
                "waiting_plan_confirmed",
                "tang_ting_at_door",
                "tang_ting_entered_home",
            ),
            "ending_state": EndingState(),
        }
    )
    decision = DirectorDecision(
        interaction="neutral",
        directives=[
            DirectorDirective(
                kind="action",
                route_id="confirm_post_arrival_plan",
                action_decision="accept",
            ),
            DirectorDirective(kind="ending", route_id="collaborative_close"),
        ],
    )
    first = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        decision,
        _turn("确认好今晚安排后，我们就先结束。"),
    )
    assert first.legal_ending is None
    state = commit_turn_plan(package, state, first)
    assert "post_arrival_plan_confirmed" in state.occurred_event_ids

    close = resolve_turn_plan(
        package,
        Scene.hotline,
        state,
        DirectorDecision(
            interaction="neutral",
            directives=[DirectorDirective(kind="ending", route_id="collaborative_close")],
        ),
        _turn("好，唐婷在你身边，那这通电话先到这里，可以吗？", 2),
    )
    assert close.legal_ending is not None
    assert close.legal_ending.ends_session is True
    committed = commit_turn_plan(package, state, close)
    assert committed.ending_state.accepted_route_id == "collaborative_close"

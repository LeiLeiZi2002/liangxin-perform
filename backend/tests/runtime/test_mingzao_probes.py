from app.cases.loader import CaseRepository
from app.runtime.domain import (
    DialogueTurn,
    DirectorDecision,
    DirectorDirective,
    InteractionImpact,
    ResponseHandling,
    commit_turn_plan,
    initialize_actor_state,
    resolve_turn_plan,
)
from app.sessions.models import Scene


def _plan(state, text: str, decision: DirectorDecision, index: int = 1):
    return resolve_turn_plan(
        CaseRepository().get("crisis_student_main"),
        Scene.hotline,
        state,
        decision,
        [DialogueTurn(turn_id=f"w{index}", role="worker", text=text)],
    )


def test_normal_risk_follow_up_can_progress_across_turns() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)
    first = _plan(
        state,
        "你现在有没有想过死，或者结束自己的生命？",
        DirectorDecision(
            interaction="neutral",
            directives=[
                DirectorDirective(
                    kind="disclose", fact_depths={"suicidal_ideation": 1}
                )
            ],
        ),
    )
    state = commit_turn_plan(package, state, first)
    second = _plan(
        state,
        "这样的念头出现多久了？有没有想过时间？",
        DirectorDecision(
            interaction="neutral",
            directives=[
                DirectorDirective(
                    kind="disclose",
                    fact_depths={"suicidal_ideation": 2, "plan_specificity": 1},
                )
            ],
        ),
        2,
    )

    assert second.allowed_fact_depths == {
        "suicidal_ideation": 2,
        "plan_specificity": 1,
    }
    assert second.projected_relationship.interaction_tension == 0


def test_chaotic_questions_do_not_create_persistent_defensiveness() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)
    plan = _plan(
        state,
        "你多大？哪儿人？为什么失业？地址呢？你妈几点到？",
        DirectorDecision(
            interaction=InteractionImpact.awkward,
            directives=[
                DirectorDirective(kind=ResponseHandling.answer_known),
                DirectorDirective(kind=ResponseHandling.say_unknown),
                DirectorDirective(kind=ResponseHandling.clarify),
            ],
        ),
    )

    assert plan.projected_relationship == state.relationship
    assert plan.legal_ending is None


def test_case_uncovered_question_is_allowed_to_be_unknown() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)
    plan = _plan(
        state,
        "你小学班主任叫什么？",
        DirectorDecision(
            interaction="neutral",
            directives=[DirectorDirective(kind="say_unknown")],
        ),
    )

    assert plan.directives[0].kind is ResponseHandling.say_unknown
    assert plan.allowed_fact_depths == {}


def test_direct_jump_cannot_disclose_plan_and_location_with_ideation() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)
    plan = _plan(
        state,
        "有没有自杀想法、具体计划和完整地址？",
        DirectorDecision(
            interaction="awkward",
            directives=[
                DirectorDirective(
                    kind="disclose",
                    fact_depths={
                        "suicidal_ideation": 1,
                        "plan_specificity": 2,
                        "current_location": 3,
                    },
                )
            ],
        ),
    )

    assert plan.allowed_fact_depths == {"suicidal_ideation": 1}
    assert sum("前置事实" in item for item in plan.diagnostics) == 2

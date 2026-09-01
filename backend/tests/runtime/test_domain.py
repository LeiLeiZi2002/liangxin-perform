import pytest
from pydantic import ValidationError

from app.cases.loader import CaseRepository
from app.runtime.domain import (
    ActorOutput,
    DirectorDecision,
    DirectorDirective,
    InteractionImpact,
    initialize_actor_state,
)


def test_initial_state_matches_every_case_fact() -> None:
    package = CaseRepository().get("crisis_student_main")
    state = initialize_actor_state(package)

    assert set(state.fact_states) == {fact.id for fact in package.case.facts}
    assert all(item.disclosed_depth == 0 for item in state.fact_states.values())
    assert state.relationship.interaction_tension == 0
    assert state.relationship.willingness_to_continue == 3


def test_realtime_model_contracts_reject_legacy_accounting_fields() -> None:
    assert set(DirectorDecision.model_fields) == {"interaction", "directives"}
    assert set(ActorOutput.model_fields) == {"spoken_text"}

    decision = DirectorDecision(interaction=InteractionImpact.neutral)
    assert decision.model_dump(mode="json") == {
        "interaction": "neutral",
        "directives": [],
    }


def test_director_directive_requires_disclosure_facts_only_for_disclose() -> None:
    known = DirectorDirective(kind="answer_known")
    disclosed = DirectorDirective(
        kind="disclose",
        fact_depths={"presenting_concern": 1},
    )

    assert known.fact_depths == {}
    assert disclosed.fact_depths == {"presenting_concern": 1}

    with pytest.raises(ValidationError, match="answer_known"):
        DirectorDirective(
            kind="answer_known",
            fact_depths={"presenting_concern": 1},
        )
    with pytest.raises(ValidationError, match="disclose"):
        DirectorDirective(kind="disclose")

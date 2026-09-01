import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.cases.domain import CasePackage
from app.runtime.domain import (
    ActorState,
    DialogueTurn,
    DirectorDecision,
    resolve_turn_plan,
)
from app.runtime.providers import (
    DirectorDecisionOutput,
    DirectorProvider,
    _as_director_decision,
    _director_output_schema,
)
from app.sessions.models import Scene

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "real_director_replays.json"


@pytest.fixture(scope="module")
def replay_data() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _turns(item: dict[str, Any]) -> list[DialogueTurn]:
    return [DialogueTurn.model_validate(turn) for turn in item["history"]]


def _state(
    replay_data: dict[str, Any], item: dict[str, Any]
) -> ActorState:
    state_reference = item["state_before"]
    assert isinstance(state_reference, dict)
    reference = state_reference["$ref"]
    assert isinstance(reference, str)
    state_name = reference.removeprefix("#/state_snapshots/")
    state_snapshots = replay_data["state_snapshots"]
    assert isinstance(state_snapshots, dict)
    state_payload = state_snapshots[state_name]
    assert isinstance(state_payload, dict)
    assert "disclosed_fact_ids" not in state_payload
    return ActorState.model_validate(state_payload)


def _replays(replay_data: dict[str, Any]) -> list[dict[str, Any]]:
    items = replay_data["replays"]
    assert isinstance(items, list)
    return items


def _case_package(
    replay_data: dict[str, Any], item: dict[str, Any]
) -> CasePackage:
    snapshot_reference = replay_data["case_package_snapshot"]
    assert isinstance(snapshot_reference, dict)
    snapshot_path = FIXTURE_PATH.parent / snapshot_reference["file"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["metadata"]["historical_byte_identity_available"] is False
    package = CasePackage.model_validate(snapshot["package"])
    assert package.case.case_id == item["case_id"]
    return package


def test_real_replay_fixture_rebuilds_history_and_actor_state(
    replay_data: dict[str, Any],
) -> None:
    expected_cards = {"C1", "C2", "C4", "D1", "D4"}
    expected_sources = {
        "C1": (
            "data/simulations/20260830-114323-2504ec",
            "1621608253594f00ac027281a61eee21",
        ),
        "C2": (
            "data/simulations/20260830-114323-2504ec",
            "1621608253594f00ac027281a61eee21",
        ),
        "C4": (
            "data/simulations/20260830-114323-2504ec",
            "1621608253594f00ac027281a61eee21",
        ),
        "D1": (
            "data/simulations/20260830-112543-99477a",
            "5e480dbbd04e47d4a60c4d4379fd18fc",
        ),
        "D4": (
            "data/simulations/20260830-112543-99477a",
            "5e480dbbd04e47d4a60c4d4379fd18fc",
        ),
    }
    items = _replays(replay_data)

    assert {item["card_id"] for item in items} == expected_cards
    assert "disclosed_fact_ids" in replay_data["source_note"]
    assert "历史运行逐字一致" in replay_data["case_package_snapshot"][
        "source_limit"
    ]
    for item in items:
        package = _case_package(replay_data, item)
        history = _turns(item)
        state = _state(replay_data, item)

        assert item["scene"] == Scene.hotline.value
        assert (item["source_run"], item["session_id"]) == expected_sources[
            item["card_id"]
        ]
        assert history[-1].role == "worker"
        assert history[-1].text == item["worker_text"]
        assert history[-1].turn_id == item["worker_turn_id"]
        assert set(state.fact_states) == {fact.id for fact in package.case.facts}
        assert set(state.topic_states) == {
            topic.topic_id for topic in package.case.topic_experiences
        }


def test_persisted_legacy_answer_is_rejected_instead_of_silently_rewritten(
    replay_data: dict[str, Any],
) -> None:
    for item in _replays(replay_data):
        original = item["original_decision"]
        assert item["original_decision_format"] == "persisted_domain_decision"
        assert item["original_wire_output_available"] is False
        with pytest.raises(ValidationError):
            DirectorDecision.model_validate(original)


def test_new_contract_candidate_replays_through_parse_and_workflow(
    replay_data: dict[str, Any],
) -> None:
    for item in _replays(replay_data):
        candidate = item["candidate_decision"]
        assert candidate["provenance"] == (
            "test_authored_new_contract_candidate_not_historical_model_output"
        )
        output = DirectorDecisionOutput.model_validate(candidate["payload"])
        decision = _as_director_decision(output)
        package = _case_package(replay_data, item)
        plan = resolve_turn_plan(
            package,
            Scene(str(item["scene"])),
            _state(replay_data, item),
            decision,
            _turns(item),
        )

        expected = item["expected_candidate_plan"]
        assert plan.allowed_fact_depths == expected["allowed_fact_depths"]
        assert list(plan.diagnostics) == expected["diagnostics"]
        assert [directive.kind.value for directive in plan.directives] == expected[
            "directive_kinds"
        ]


def test_director_message_structure_contains_complete_replay_input(
    replay_data: dict[str, Any],
) -> None:
    for item in _replays(replay_data):
        package = _case_package(replay_data, item)
        scene = Scene(str(item["scene"]))
        state = _state(replay_data, item)
        history = _turns(item)
        messages = DirectorProvider._messages(
            package=package,
            scene=scene,
            state=state,
            history=history,
            current_worker_text=str(item["worker_text"]),
            use_explicit_cache=True,
        )

        assert [message["role"] for message in messages] == [
            "system",
            "system",
            "user",
        ]
        stable_content = messages[1]["content"]
        assert isinstance(stable_content, list)
        assert stable_content[0]["cache_control"] == {"type": "ephemeral"}
        stable_payload = json.loads(stable_content[0]["text"])
        dynamic_payload = json.loads(messages[2]["content"])

        assert stable_payload["case_spec"] == package.case.model_dump(mode="json")
        assert stable_payload["current_scene"] == package.case.scenes[
            scene
        ].model_dump(mode="json")
        assert stable_payload["director_policy"]["disclosure_rules"] == [
            rule.model_dump(mode="json")
            for rule in package.actor.disclosure_rules
        ]
        assert stable_payload["director_policy"]["event_routes"] == [
            route.model_dump(mode="json") for route in package.actor.event_routes
        ]
        assert stable_payload["director_policy"]["ending_routes"] == [
            route.model_dump(mode="json") for route in package.actor.ending_routes
        ]
        assert dynamic_payload["actor_state"] == state.model_dump(mode="json")
        assert dynamic_payload["history"] == [
            turn.model_dump(mode="json") for turn in history
        ]
        assert dynamic_payload["current_worker_text"] == item["worker_text"]
        assert dynamic_payload["current_worker_turn_id"] == item["worker_turn_id"]


def test_director_wire_schema_uses_one_of_without_discriminator(
    replay_data: dict[str, Any],
) -> None:
    item = _replays(replay_data)[0]
    schema = _director_output_schema(_case_package(replay_data, item))
    directive_items = schema["properties"]["directives"]["items"]

    assert "oneOf" in directive_items
    assert "discriminator" not in directive_items

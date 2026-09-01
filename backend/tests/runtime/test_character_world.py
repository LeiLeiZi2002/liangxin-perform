from datetime import UTC, datetime, timedelta

import pytest


def _definition():
    from app.runtime.character_world import SupportWorldDefinition

    return SupportWorldDefinition(
        support_name="唐婷",
        arrival_after_seconds=780,
        not_contacted_reality="热线接通后还没有联系唐婷。",
        first_unanswered_reality="第一次联系没有接通。",
        coming_reality="唐婷已经答应赶来，但还没到门口。",
        at_door_reality="唐婷已经到门外，尚未进屋。",
        present_reality="唐婷已经进屋，沈雯不再独处。",
    )


def test_support_world_starts_unknown_and_only_allows_first_contact() -> None:
    from app.runtime.character_world import (
        SupportWorldAction,
        SupportWorldStage,
        build_support_world_view,
        load_support_world,
    )

    state = load_support_world({"runtime": {"engine": "character_prompt"}})
    view = build_support_world_view(_definition(), state)

    assert state.stage is SupportWorldStage.not_contacted
    assert state.arrival_due_at is None
    assert view.reality == "热线接通后还没有联系唐婷。"
    assert view.allowed_actions == (
        SupportWorldAction.none,
        SupportWorldAction.send_first_support_message,
    )


def test_no_external_world_view_only_allows_none() -> None:
    from app.runtime.character_world import (
        SupportWorldAction,
        no_external_world_view,
    )

    view = no_external_world_view()

    assert view.allowed_actions == (SupportWorldAction.none,)
    assert view.reality


def test_first_and_urgent_messages_have_distinct_fixed_transitions() -> None:
    from app.runtime.character_world import (
        SupportWorldAction,
        SupportWorldStage,
        apply_support_world_action,
        build_support_world_view,
        initial_support_world,
    )

    now = datetime(2026, 8, 31, 1, 43, tzinfo=UTC)
    first = apply_support_world_action(
        _definition(),
        initial_support_world(),
        SupportWorldAction.send_first_support_message,
        now=now,
    )
    first_view = build_support_world_view(_definition(), first)
    second = apply_support_world_action(
        _definition(),
        first,
        SupportWorldAction.send_urgent_support_message,
        now=now,
    )

    assert first.stage is SupportWorldStage.first_unanswered
    assert first.arrival_due_at is None
    assert first_view.allowed_actions == (
        SupportWorldAction.none,
        SupportWorldAction.send_urgent_support_message,
    )
    assert second.stage is SupportWorldStage.coming
    assert second.arrival_due_at == now + timedelta(seconds=780)


def test_each_support_message_is_legal_only_at_its_own_stage() -> None:
    from app.runtime.character_world import (
        SupportWorldAction,
        SupportWorldStage,
        SupportWorldState,
        SupportWorldTransitionError,
        apply_support_world_action,
        initial_support_world,
    )

    now = datetime(2026, 8, 31, 1, 43, tzinfo=UTC)
    with pytest.raises(SupportWorldTransitionError):
        apply_support_world_action(
            _definition(),
            initial_support_world(),
            SupportWorldAction.send_urgent_support_message,
            now=now,
        )
    with pytest.raises(SupportWorldTransitionError):
        apply_support_world_action(
            _definition(),
            SupportWorldState(stage=SupportWorldStage.first_unanswered),
            SupportWorldAction.send_first_support_message,
            now=now,
        )


def test_arrival_materializes_only_when_persisted_deadline_is_due() -> None:
    from app.runtime.character_world import (
        SupportWorldStage,
        SupportWorldState,
        materialize_support_world,
    )

    due = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)
    coming = SupportWorldState(
        stage=SupportWorldStage.coming,
        arrival_due_at=due,
    )

    before = materialize_support_world(coming, now=due - timedelta(seconds=1))
    arrived = materialize_support_world(coming, now=due)

    assert before == coming
    assert arrived.stage is SupportWorldStage.at_door
    assert arrived.arrival_due_at is None


def test_support_can_enter_only_after_arriving_at_the_door() -> None:
    from app.runtime.character_world import (
        SupportWorldAction,
        SupportWorldStage,
        SupportWorldState,
        SupportWorldTransitionError,
        apply_support_world_action,
    )

    now = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)
    with pytest.raises(SupportWorldTransitionError):
        apply_support_world_action(
            _definition(),
            SupportWorldState(stage=SupportWorldStage.coming, arrival_due_at=now),
            SupportWorldAction.let_support_in,
            now=now,
        )

    present = apply_support_world_action(
        _definition(),
        SupportWorldState(stage=SupportWorldStage.at_door),
        SupportWorldAction.let_support_in,
        now=now,
    )
    assert present.stage is SupportWorldStage.present


def test_world_payload_round_trips_without_touching_other_session_state() -> None:
    from app.runtime.character_world import (
        SupportWorldStage,
        SupportWorldState,
        load_support_world,
        store_support_world,
    )

    session_state = {
        "actor_state": {"legacy": "keep"},
        "runtime": {"engine": "character_prompt", "phase": "acting"},
    }
    updated = store_support_world(
        session_state,
        SupportWorldState(stage=SupportWorldStage.first_unanswered),
    )

    assert updated["actor_state"] == {"legacy": "keep"}
    assert updated["runtime"] == {
        "engine": "character_prompt",
        "phase": "acting",
    }
    assert updated["world"] == {
        "kind": "support_arrival",
        "stage": "first_unanswered",
        "arrival_due_at": None,
    }
    assert load_support_world(updated).stage is SupportWorldStage.first_unanswered

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

SUPPORT_WORLD_KIND = "support_arrival"


class SupportWorldTransitionError(ValueError):
    pass


class SupportWorldAction(StrEnum):
    none = "none"
    send_first_support_message = "send_first_support_message"
    send_urgent_support_message = "send_urgent_support_message"
    let_support_in = "let_support_in"


class SupportWorldStage(StrEnum):
    not_contacted = "not_contacted"
    first_unanswered = "first_unanswered"
    coming = "coming"
    at_door = "at_door"
    present = "present"


class SupportWorldDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    support_name: str = Field(min_length=1)
    arrival_after_seconds: int = Field(ge=1)
    not_contacted_reality: str = Field(min_length=1)
    first_unanswered_reality: str = Field(min_length=1)
    coming_reality: str = Field(min_length=1)
    at_door_reality: str = Field(min_length=1)
    present_reality: str = Field(min_length=1)
    forbidden_action_results: dict[SupportWorldAction, tuple[str, ...]] = Field(
        default_factory=dict
    )


class SupportWorldState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(default=SUPPORT_WORLD_KIND, frozen=True)
    stage: SupportWorldStage = SupportWorldStage.not_contacted
    arrival_due_at: datetime | None = None

    @model_validator(mode="after")
    def validate_deadline(self) -> SupportWorldState:
        if self.kind != SUPPORT_WORLD_KIND:
            raise ValueError("不支持的现实事件状态")
        if self.stage is SupportWorldStage.coming and self.arrival_due_at is None:
            raise ValueError("支持者赶来时必须记录到达时间")
        if self.stage is not SupportWorldStage.coming and self.arrival_due_at is not None:
            raise ValueError("当前现实事件状态不能保留到达时间")
        return self


class SupportWorldView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reality: str
    allowed_actions: tuple[SupportWorldAction, ...]


def no_external_world_view() -> SupportWorldView:
    return SupportWorldView(
        reality="本案例没有需要程序推进的外部现实事件。",
        allowed_actions=(SupportWorldAction.none,),
    )


def initial_support_world() -> SupportWorldState:
    return SupportWorldState()


def load_support_world(state_json: dict[str, object]) -> SupportWorldState:
    payload = state_json.get("world")
    if payload is None:
        return initial_support_world()
    return SupportWorldState.model_validate(payload)


def store_support_world(
    state_json: dict[str, object],
    world: SupportWorldState,
) -> dict[str, object]:
    payload = dict(state_json)
    payload["world"] = world.model_dump(mode="json")
    return payload


def materialize_support_world(
    world: SupportWorldState,
    *,
    now: datetime,
) -> SupportWorldState:
    if (
        world.stage is SupportWorldStage.coming
        and world.arrival_due_at is not None
        and now >= world.arrival_due_at
    ):
        return SupportWorldState(stage=SupportWorldStage.at_door)
    return world


def build_support_world_view(
    definition: SupportWorldDefinition,
    world: SupportWorldState,
) -> SupportWorldView:
    realities = {
        SupportWorldStage.not_contacted: definition.not_contacted_reality,
        SupportWorldStage.first_unanswered: definition.first_unanswered_reality,
        SupportWorldStage.coming: definition.coming_reality,
        SupportWorldStage.at_door: definition.at_door_reality,
        SupportWorldStage.present: definition.present_reality,
    }
    actions: tuple[SupportWorldAction, ...] = (SupportWorldAction.none,)
    if world.stage is SupportWorldStage.not_contacted:
        actions += (SupportWorldAction.send_first_support_message,)
    elif world.stage is SupportWorldStage.first_unanswered:
        actions += (SupportWorldAction.send_urgent_support_message,)
    elif world.stage is SupportWorldStage.at_door:
        actions += (SupportWorldAction.let_support_in,)
    return SupportWorldView(
        reality=realities[world.stage],
        allowed_actions=actions,
    )


def apply_support_world_action(
    definition: SupportWorldDefinition,
    world: SupportWorldState,
    action: SupportWorldAction,
    *,
    now: datetime,
) -> SupportWorldState:
    if action is SupportWorldAction.none:
        return world
    if (
        action is SupportWorldAction.send_first_support_message
        and world.stage is SupportWorldStage.not_contacted
    ):
        return SupportWorldState(stage=SupportWorldStage.first_unanswered)
    if (
        action is SupportWorldAction.send_urgent_support_message
        and world.stage is SupportWorldStage.first_unanswered
    ):
        return SupportWorldState(
            stage=SupportWorldStage.coming,
            arrival_due_at=now
            + timedelta(seconds=definition.arrival_after_seconds),
        )
    if (
        action is SupportWorldAction.let_support_in
        and world.stage is SupportWorldStage.at_door
    ):
        return SupportWorldState(stage=SupportWorldStage.present)
    raise SupportWorldTransitionError(
        f"现实状态 {world.stage.value} 不允许行动 {action.value}"
    )

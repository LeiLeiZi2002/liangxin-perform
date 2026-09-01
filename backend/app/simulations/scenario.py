from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.sessions.models import Scene


class SimulationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScenarioState(SimulationModel):
    fact_depths: dict[str, int] = Field(default_factory=dict)
    event_ids: frozenset[str] = frozenset()


class StateCondition(SimulationModel):
    fact_depths: dict[str, int] = Field(default_factory=dict)
    event_ids: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.fact_depths and not self.event_ids

    def matches(self, state: ScenarioState) -> bool:
        return not self.unsatisfied(state)

    def unsatisfied(self, state: ScenarioState) -> list[str]:
        missing = [
            f"fact:{fact_id}>={depth}"
            for fact_id, depth in self.fact_depths.items()
            if state.fact_depths.get(fact_id, 0) < depth
        ]
        missing.extend(
            f"event:{event_id}"
            for event_id in self.event_ids
            if event_id not in state.event_ids
        )
        return missing


class FinalExpectation(StateCondition):
    ending_route_id: str | None = None
    end_reason: str | None = None
    minimum_interaction_tension: int | None = Field(default=None, ge=0, le=3)
    maximum_interaction_tension: int | None = Field(default=None, ge=0, le=3)
    allowed_repair_stages: list[Literal["none", "window", "repairing", "closed"]] = (
        Field(default_factory=list)
    )


InteractionImpact = Literal[
    "neutral",
    "supportive",
    "awkward",
    "harmful",
    "repair",
]
SimulationProfile = Literal["content", "voice"]
SimulationRuntimeEngine = Literal["workflow", "character_prompt"]
WorldStage = Literal[
    "not_contacted",
    "first_unanswered",
    "coming",
    "at_door",
    "present",
]


class ProbeCard(SimulationModel):
    card_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    character_text: str | None = Field(default=None, min_length=1)
    scene_texts: dict[Scene, str] = Field(default_factory=dict)
    character_only: bool = False
    world_time_advance_seconds: int = Field(default=0, ge=0, le=3600)
    expect_world_stage: WorldStage | None = None
    requires: StateCondition = Field(default_factory=StateCondition)
    expect: StateCondition = Field(default_factory=StateCondition)
    retry_text: str | None = Field(default=None, min_length=1)
    always_run: bool = False
    allowed_interaction_impacts: list[InteractionImpact] | None = None
    maximum_fact_depths_after: dict[str, int] = Field(default_factory=dict)

    def text_for_engine(
        self,
        engine: SimulationRuntimeEngine,
        *,
        scene: Scene | None = None,
    ) -> str | None:
        if engine == "workflow":
            return None if self.character_only else self.text
        if scene is not None and scene in self.scene_texts:
            return self.scene_texts[scene]
        return self.character_text or self.text

    def can_run(self, state: ScenarioState) -> bool:
        return self.requires.matches(state)

    def should_skip(self, state: ScenarioState) -> bool:
        return (
            not self.always_run
            and not self.expect.is_empty
            and self.expect.matches(state)
        )

    def blocked_requirements(self, state: ScenarioState) -> list[str]:
        return self.requires.unsatisfied(state)


class Scenario(SimulationModel):
    scenario_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    profile: SimulationProfile
    case_id: str = Field(default="crisis_student_main", min_length=1)
    profiles_by_scene: dict[Scene, SimulationProfile] = Field(
        default_factory=dict
    )
    objective_contracts: bool = False
    include_in_all: bool = True
    all_order: int = Field(default=1000, ge=0)
    cards: list[ProbeCard] = Field(default_factory=list)
    final_expect: FinalExpectation = Field(default_factory=FinalExpectation)
    end_after_cards: bool = False
    harmful_from_card_id: str | None = None
    protected_fact_ids: list[str] = Field(default_factory=list)
    allowed_interaction_impacts: list[InteractionImpact] = Field(
        default_factory=list
    )
    relationship_rupture_card_id: str | None = None
    relationship_repair_card_id: str | None = None
    earliest_event_card_ids: dict[str, str] = Field(default_factory=dict)
    natural_close_from_card_id: str | None = None

    def cards_for_engine(
        self,
        engine: SimulationRuntimeEngine,
        *,
        scene: Scene | None = None,
    ) -> list[ProbeCard]:
        return [
            card
            for card in self.cards
            if card.text_for_engine(engine, scene=scene) is not None
        ]

    def supports_scene(self, scene: Scene) -> bool:
        if self.profiles_by_scene:
            return scene in self.profiles_by_scene
        return scene is Scene.hotline

    def profile_for_scene(self, scene: Scene) -> SimulationProfile:
        if self.profiles_by_scene:
            try:
                return self.profiles_by_scene[scene]
            except KeyError as exc:
                raise ValueError(
                    f"模拟场景 {self.scenario_id} 不支持场域 {scene.value}"
                ) from exc
        if scene is not Scene.hotline:
            raise ValueError(
                f"模拟场景 {self.scenario_id} 不支持场域 {scene.value}"
            )
        return self.profile

    def allowed_impacts_for(self, card: ProbeCard) -> list[InteractionImpact]:
        return (
            card.allowed_interaction_impacts
            if card.allowed_interaction_impacts is not None
            else self.allowed_interaction_impacts
        )

    @model_validator(mode="after")
    def validate_card_ids(self) -> Scenario:
        card_ids = [card.card_id for card in self.cards]
        if len(card_ids) != len(set(card_ids)):
            raise ValueError(f"场景探针卡标识重复：{self.scenario_id}")
        if (
            self.natural_close_from_card_id is not None
            and self.natural_close_from_card_id not in card_ids
        ):
            raise ValueError(
                f"自然收束起点探针不存在：{self.natural_close_from_card_id}"
            )
        if self.harmful_from_card_id is not None:
            if self.harmful_from_card_id not in card_ids:
                raise ValueError(f"伤害起点探针不存在：{self.harmful_from_card_id}")
            if not self.protected_fact_ids:
                raise ValueError("声明伤害起点时必须给出受保护事实")
        relationship_cards = {
            self.relationship_rupture_card_id,
            self.relationship_repair_card_id,
        }
        if relationship_cards != {None}:
            if None in relationship_cards:
                raise ValueError("破裂与修复探针必须成对声明")
            if not relationship_cards.issubset(card_ids):
                raise ValueError("破裂或修复探针不存在")
        missing_earliest_cards = set(self.earliest_event_card_ids.values()) - set(
            card_ids
        )
        if missing_earliest_cards:
            raise ValueError(
                "事件最早允许探针不存在："
                + ", ".join(sorted(missing_earliest_cards))
            )
        if self.profiles_by_scene:
            expected_scenes = set(self.profiles_by_scene)
            for card in self.cards:
                actual_scenes = set(card.scene_texts)
                if actual_scenes != expected_scenes:
                    raise ValueError(
                        f"双场域探针 {card.card_id} 的 scene_texts 必须覆盖："
                        + ", ".join(sorted(scene.value for scene in expected_scenes))
                    )
        return self


class ScenarioCatalog(SimulationModel):
    scenarios: list[Scenario]

    @model_validator(mode="after")
    def validate_scenario_ids(self) -> ScenarioCatalog:
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("模拟场景标识重复")
        return self


def load_scenarios(path: Path | None = None) -> dict[str, Scenario]:
    selected_path = path or Path(__file__).with_name("scenarios.json")
    payload = json.loads(selected_path.read_text(encoding="utf-8"))
    catalog = ScenarioCatalog.model_validate(payload)
    return {scenario.scenario_id: scenario for scenario in catalog.scenarios}


def select_scenarios(
    scenarios: dict[str, Scenario],
    suite: str,
    *,
    case_id: str = "crisis_student_main",
    scene: Scene = Scene.hotline,
) -> list[Scenario]:
    if suite == "all":
        selected = sorted(
            (
                scenario
                for scenario in scenarios.values()
                if scenario.case_id == case_id and scenario.include_in_all
            ),
            key=lambda scenario: scenario.all_order,
        )
        if not selected:
            raise ValueError(f"案例 {case_id} 没有可运行的模拟场景")
    else:
        try:
            scenario = scenarios[suite]
        except KeyError as exc:
            raise ValueError(f"模拟场景不存在：{suite}") from exc
        if scenario.case_id != case_id:
            raise ValueError(
                f"模拟场景 {suite} 不属于案例 {case_id}"
            )
        selected = [scenario]
    unsupported = [
        scenario.scenario_id
        for scenario in selected
        if not scenario.supports_scene(scene)
    ]
    if unsupported:
        raise ValueError(
            f"模拟场景 {', '.join(unsupported)} 不支持场域 {scene.value}"
        )
    return selected

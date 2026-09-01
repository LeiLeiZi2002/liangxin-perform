from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.sessions.models import Scene


class ActorPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StableSpeech(ActorPolicyModel):
    language: str = Field(min_length=1)
    volume: str = ""
    baseline_style: str = Field(min_length=1)
    speech_patterns: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    scene_guidance: dict[Scene, str] = Field(default_factory=dict)


class OpeningPolicy(ActorPolicyModel):
    worker_starts: bool = True
    silence_seconds: int = Field(default=5, ge=0, le=30)
    silence_behavior: str = ""
    fixed_utterance: bool = False
    scene_guidance: dict[Scene, str] = Field(default_factory=dict)


class InitialActorState(ActorPolicyModel):
    interaction_tension: int = Field(ge=0, le=3)
    willingness_to_continue: int = Field(ge=0, le=4)
    emotional_activation: int = Field(ge=0, le=4)
    speech_organization: int = Field(ge=0, le=4)
    crying_state: Literal["none", "emerging", "crying", "recovering"] = "none"
    repair_stage: Literal["none", "window", "repairing", "closed"] = "none"


class DisclosureDecision(ActorPolicyModel):
    when: str = Field(min_length=1)
    allow_depth: int = Field(ge=1)
    requires_prior_depth: int | None = Field(default=None, ge=1)


class DisclosureRule(ActorPolicyModel):
    fact_id: str = Field(min_length=1)
    decisions: list[DisclosureDecision] = Field(min_length=1)
    prerequisite_fact_ids: list[str] = Field(default_factory=list)
    semantic_evidence_required: bool = True
    keyword_matching: bool = False
    requires_direct_question: bool = False

    @model_validator(mode="after")
    def validate_depth_decisions(self) -> Self:
        available_depths = {item.allow_depth for item in self.decisions}
        if len(available_depths) != len(self.decisions):
            raise ValueError(f"duplicate disclosure depth: {self.fact_id}")
        invalid_prior_depths = [
            item.allow_depth
            for item in self.decisions
            if item.requires_prior_depth is not None
            and (
                item.requires_prior_depth >= item.allow_depth
                or item.requires_prior_depth not in available_depths
            )
        ]
        if invalid_prior_depths:
            raise ValueError(
                f"invalid prior disclosure depth: {self.fact_id}/{invalid_prior_depths}"
            )
        return self

    @property
    def max_depth(self) -> int:
        return max(decision.allow_depth for decision in self.decisions)


class InteractionTensionPolicy(ActorPolicyModel):
    levels: dict[int, str]
    max_change_per_turn: int = Field(default=1, ge=1)
    escalation_factors: list[str] = Field(default_factory=list)
    deescalation_factors: list[str] = Field(default_factory=list)
    direct_risk_question_increases_tension: bool = False


class TopicReaction(ActorPolicyModel):
    topic_id: str = Field(min_length=1)
    expressions: list[str] = Field(default_factory=list)
    tense_interaction: str = ""
    supportive_interaction: str = ""


class RuptureAndRepairPolicy(ActorPolicyModel):
    rupture_stages: list[str] = Field(min_length=1)
    repair_requirements: list[str] = Field(min_length=1)
    generic_apology_restores: bool = False
    repeated_rupture_effect: str = ""


class EventRoute(ActorPolicyModel):
    id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    offer_when: str = Field(min_length=1)
    decision_guidance: str = Field(min_length=1)
    required_fact_depths: dict[str, int] = Field(default_factory=dict)
    scenes: list[Scene] = Field(min_length=1)


StageName = Literal[
    "opening",
    "exploration",
    "risk_assessment",
    "boundary_challenge",
    "planning",
    "closing",
]


class StageRule(ActorPolicyModel):
    stage: StageName
    any_fact_ids: list[str] = Field(default_factory=list)
    required_fact_depths: dict[str, int] = Field(default_factory=dict)
    any_event_ids: list[str] = Field(default_factory=list)
    required_event_ids: list[str] = Field(default_factory=list)


class EndingRoute(ActorPolicyModel):
    id: str = Field(min_length=1)
    kind: Literal[
        "collaborative_close", "caller_tests_close", "rupture_hangup", "worker_close"
    ]
    condition: str = Field(min_length=1)
    actor_behavior: str = Field(min_length=1)
    ends_session: bool = True
    fallback_only: bool = False
    required_fact_ids: list[str] = Field(default_factory=list)
    required_event_ids: list[str] = Field(default_factory=list)
    required_stage: StageName | None = None
    minimum_interaction_tension: int | None = Field(default=None, ge=0, le=3)
    allowed_repair_stages: list[
        Literal["none", "window", "repairing", "closed"]
    ] = Field(default_factory=list)
    scenes: list[Scene] = Field(min_length=1)


class ImprovisationBoundary(ActorPolicyModel):
    locked_content: list[str] = Field(default_factory=list)
    allowed_content: list[str] = Field(default_factory=list)
    unknown_response: str = Field(min_length=1)
    continuity_requirements: list[str] = Field(default_factory=list)


class ActorPolicy(ActorPolicyModel):
    case_id: str = Field(min_length=1)
    stable_speech: StableSpeech
    opening: OpeningPolicy
    initial_state: InitialActorState
    disclosure_rules: list[DisclosureRule] = Field(default_factory=list)
    interaction_tension: InteractionTensionPolicy
    topic_reactions: list[TopicReaction] = Field(default_factory=list)
    rupture_and_repair: RuptureAndRepairPolicy
    event_routes: list[EventRoute] = Field(default_factory=list)
    stage_rules: list[StageRule] = Field(default_factory=list)
    ending_routes: list[EndingRoute] = Field(default_factory=list)
    improvisation_boundary: ImprovisationBoundary

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        _reject_duplicates([item.fact_id for item in self.disclosure_rules], "disclosure rule")
        _reject_duplicates([item.topic_id for item in self.topic_reactions], "topic reaction")
        _reject_duplicates([item.id for item in self.event_routes], "event route")
        _reject_duplicates(
            [item.event_id for item in self.event_routes],
            "routed story event",
        )
        _reject_duplicates([item.stage for item in self.stage_rules], "stage rule")
        _reject_duplicates([item.id for item in self.ending_routes], "ending route")
        return self


def _reject_duplicates(ids: list[str], label: str) -> None:
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate {label} id")

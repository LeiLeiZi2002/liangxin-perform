from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.sessions.models import CaseType, Scene


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseStatus(StrEnum):
    draft = "draft"
    published = "published"


class Signal(StrEnum):
    presenting_concern = "presenting_concern"
    symptom_course = "symptom_course"
    functioning = "functioning"
    emotional_state = "emotional_state"
    hopelessness = "hopelessness"
    suicidal_ideation = "suicidal_ideation"
    plan_specificity = "plan_specificity"
    means_access = "means_access"
    timing_intent = "timing_intent"
    prior_behavior = "prior_behavior"
    substance_use = "substance_use"
    protective_factors = "protective_factors"
    support_resources = "support_resources"
    current_alone = "current_alone"
    current_location = "current_location"
    confidentiality_limits = "confidentiality_limits"
    scope_and_role = "scope_and_role"
    boundary_request = "boundary_request"
    boundary_response = "boundary_response"
    referral_need = "referral_need"
    referral_concerns = "referral_concerns"
    referral_preferences = "referral_preferences"
    transition_support = "transition_support"
    minimum_risk_screen = "minimum_risk_screen"


class ConversationStage(StrEnum):
    opening = "opening"
    exploration = "exploration"
    risk_assessment = "risk_assessment"
    boundary_challenge = "boundary_challenge"
    planning = "planning"
    closing = "closing"


class PublicEntry(DomainModel):
    role: str = Field(min_length=1)
    known_information: list[str] = Field(default_factory=list)
    task_boundary: list[str] = Field(default_factory=list)


class PersonIdentity(DomainModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=1, le=120)
    gender: str = ""
    occupation_history: str = ""
    current_employment: str = ""
    living_situation: str = ""


class CallContext(DomainModel):
    voluntary_call: bool
    initial_willingness: str = Field(min_length=1)
    immediate_need: str = Field(min_length=1)


class PersonSpec(DomainModel):
    identity: PersonIdentity
    stable_tendencies: list[str] = Field(default_factory=list)
    call_context: CallContext


class SceneSpec(DomainModel):
    scene: Scene
    current_time: str = Field(min_length=1)
    location: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    phone: dict[str, Any] = Field(default_factory=dict)
    mother_arrival: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class TimelineEvent(DomainModel):
    id: str = Field(min_length=1)
    when: str = Field(min_length=1)
    happened: str = Field(min_length=1)
    actor_knew: list[str] = Field(default_factory=list)
    actor_unknown: list[str] = Field(default_factory=list)
    actor_experience: list[str] = Field(default_factory=list)


class RelationshipSpec(DomainModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    relationship: str = Field(min_length=1)
    history: list[str] = Field(default_factory=list)
    actual_state: dict[str, Any] = Field(default_factory=dict)
    actor_belief: list[str] = Field(default_factory=list)
    support_capacity: list[str] = Field(default_factory=list)
    support_limits: list[str] = Field(default_factory=list)


class FactDepth(DomainModel):
    depth: int = Field(ge=1)
    content: str = Field(min_length=1)


class FactContradictionCue(DomainModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    anchor_groups: list[list[str]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_anchor_groups(self) -> Self:
        if not self.id.strip() or not self.label.strip():
            raise ValueError("contradiction cue id and label must not be blank")
        normalized_groups: list[tuple[str, ...]] = []
        for group in self.anchor_groups:
            if not group or any(not term.strip() for term in group):
                raise ValueError("contradiction cue anchor group must not be empty")
            normalized = tuple(term.strip() for term in group)
            if len(normalized) != len(set(normalized)):
                raise ValueError("duplicate contradiction cue anchor")
            normalized_groups.append(normalized)
        if len(normalized_groups) != len(set(normalized_groups)):
            raise ValueError("duplicate contradiction cue anchor group")
        return self


class CaseFact(DomainModel):
    id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    kind: Literal["positive_fact", "negative_fact", "actor_unknown", "conditional_fact"]
    content: str = Field(min_length=1)
    actor_knowledge: Literal["knows", "unknown", "uncertain", "conditional"]
    subjective_experience: str = ""
    depths: list[FactDepth] = Field(min_length=1)
    locked_details: list[str] = Field(default_factory=list)
    contradiction_cues: list[FactContradictionCue] = Field(
        default_factory=list,
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_depths(self) -> Self:
        depths = [item.depth for item in self.depths]
        if len(depths) != len(set(depths)):
            raise ValueError(f"duplicate fact depth: {self.id}")
        if depths != sorted(depths):
            raise ValueError(f"fact depths must be ascending: {self.id}")
        cue_ids = [cue.id for cue in self.contradiction_cues]
        if len(cue_ids) != len(set(cue_ids)):
            raise ValueError(f"duplicate contradiction cue: {self.id}")
        if self.kind != "positive_fact" and self.contradiction_cues:
            raise ValueError(
                f"contradiction cues require positive fact: {self.id}"
            )
        return self


class TopicExperience(DomainModel):
    topic_id: str = Field(min_length=1)
    surface_experience: str = Field(min_length=1)
    deeper_experience: list[str] = Field(default_factory=list)


class StoryEventResult(DomainModel):
    status: str = Field(min_length=1)
    actor_observation: str = Field(min_length=1)
    state_changes: dict[str, Any] = Field(default_factory=dict)


class DeferredAfter(DomainModel):
    after_event_id: str = Field(min_length=1)
    min_intervening_actor_turns: int = Field(default=1, ge=1)


class StoryEvent(DomainModel):
    id: str = Field(min_length=1)
    prerequisite_event_ids: list[str] = Field(default_factory=list)
    deferred_after: DeferredAfter | None = None
    result: StoryEventResult


class UnknownBoundary(DomainModel):
    id: str = Field(min_length=1)
    actor_knowledge: Literal["unknown", "uncertain"]
    when_asked: str = Field(min_length=1)
    known_boundary: str = Field(min_length=1)
    improvisation_allowed: bool = False


class CaseSpec(DomainModel):
    case_id: str = Field(min_length=1)
    status: CaseStatus
    title: str = Field(min_length=1)
    character_required: bool = False
    case_type: CaseType
    estimated_duration_minutes: int = Field(ge=1)
    public_entry: PublicEntry
    public_entries: dict[Scene, PublicEntry] = Field(default_factory=dict)
    person: PersonSpec
    scenes: dict[Scene, SceneSpec]
    timeline: list[TimelineEvent] = Field(default_factory=list)
    relationships: list[RelationshipSpec] = Field(default_factory=list)
    facts: list[CaseFact] = Field(min_length=1)
    topic_experiences: list[TopicExperience] = Field(default_factory=list)
    story_events: list[StoryEvent] = Field(default_factory=list)
    unknowns: list[UnknownBoundary] = Field(default_factory=list)

    @property
    def supported_scenes(self) -> set[Scene]:
        return set(self.scenes)

    def public_entry_for(self, scene: Scene | None) -> PublicEntry:
        if scene is not None:
            return self.public_entries.get(scene, self.public_entry)
        return self.public_entry

    @model_validator(mode="after")
    def validate_integrity(self) -> Self:
        if not self.scenes:
            raise ValueError("case must support at least one scene")
        if any(key != value.scene for key, value in self.scenes.items()):
            raise ValueError("scene key must match scene id")
        if self.public_entries:
            entry_scenes = set(self.public_entries)
            missing_entry_scenes = self.supported_scenes - entry_scenes
            if missing_entry_scenes:
                raise ValueError(
                    "public entries missing supported scene: "
                    f"{sorted(missing_entry_scenes)}"
                )
            unsupported_entry_scenes = entry_scenes - self.supported_scenes
            if unsupported_entry_scenes:
                raise ValueError(
                    "public entries reference unsupported scene: "
                    f"{sorted(unsupported_entry_scenes)}"
                )
        _reject_duplicate_ids(self.facts, "fact")
        _reject_duplicate_ids(self.story_events, "story event")
        _reject_duplicate_ids(self.relationships, "relationship")
        _reject_duplicate_ids(self.timeline, "timeline event")
        _reject_duplicate_ids(self.topic_experiences, "topic", attribute="topic_id")
        _reject_duplicate_ids(self.unknowns, "unknown")
        event_ids = {item.id for item in self.story_events}
        missing = {
            prerequisite
            for event in self.story_events
            for prerequisite in event.prerequisite_event_ids
            if prerequisite not in event_ids
        }
        if missing:
            raise ValueError(f"story event references missing prerequisite: {sorted(missing)}")
        missing_deferred = {
            event.deferred_after.after_event_id
            for event in self.story_events
            if event.deferred_after is not None
            and event.deferred_after.after_event_id not in event_ids
        }
        if missing_deferred:
            raise ValueError(
                "story event references missing deferred source: "
                f"{sorted(missing_deferred)}"
            )
        self._validate_event_graph()
        return self

    def _validate_event_graph(self) -> None:
        dependencies = {
            event.id: {
                *event.prerequisite_event_ids,
                *(
                    [event.deferred_after.after_event_id]
                    if event.deferred_after is not None
                    else []
                ),
            }
            for event in self.story_events
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(event_id: str) -> None:
            if event_id in visiting:
                raise ValueError("story event dependency cycle")
            if event_id in visited:
                return
            visiting.add(event_id)
            for dependency in dependencies.get(event_id, set()):
                visit(dependency)
            visiting.remove(event_id)
            visited.add(event_id)

        for event_id in dependencies:
            visit(event_id)


def _reject_duplicate_ids(
    items: list[Any], label: str, *, attribute: str = "id"
) -> None:
    ids = [getattr(item, attribute) for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate {label} id")


from app.cases.actor_policy import ActorPolicy  # noqa: E402
from app.cases.measurement import MeasurementSpec  # noqa: E402


class CasePackage(DomainModel):
    case: CaseSpec
    actor: ActorPolicy
    measurement: MeasurementSpec

    @model_validator(mode="after")
    def validate_cross_file_integrity(self) -> Self:
        case_ids = {self.case.case_id, self.actor.case_id, self.measurement.case_id}
        if len(case_ids) != 1:
            raise ValueError(f"case_id mismatch: {sorted(case_ids)}")

        fact_ids = {item.id for item in self.case.facts}
        event_ids = {item.id for item in self.case.story_events}
        scenes = self.case.supported_scenes
        actor_fact_ids = {
            rule.fact_id for rule in self.actor.disclosure_rules
        } | {
            fact_id
            for route in self.actor.ending_routes
            for fact_id in route.required_fact_ids
        } | {
            fact_id
            for route in self.actor.event_routes
            for fact_id in route.required_fact_depths
        } | {
            fact_id
            for rule in self.actor.stage_rules
            for fact_id in (*rule.any_fact_ids, *rule.required_fact_depths)
        }
        missing_actor_facts = actor_fact_ids - fact_ids
        if missing_actor_facts:
            raise ValueError(
                f"actor policy references missing fact: {sorted(missing_actor_facts)}"
            )
        prerequisite_facts = {
            fact_id
            for rule in self.actor.disclosure_rules
            for fact_id in rule.prerequisite_fact_ids
        }
        missing_prerequisites = prerequisite_facts - fact_ids
        if missing_prerequisites:
            raise ValueError(
                "actor policy references missing prerequisite fact: "
                f"{sorted(missing_prerequisites)}"
            )

        topic_ids = {item.topic_id for item in self.case.topic_experiences}
        missing_topics = {
            reaction.topic_id
            for reaction in self.actor.topic_reactions
            if reaction.topic_id not in topic_ids
        }
        if missing_topics:
            raise ValueError(
                f"actor policy references missing topic: {sorted(missing_topics)}"
            )

        max_depth_by_fact = {
            fact.id: max(depth.depth for depth in fact.depths) for fact in self.case.facts
        }
        excessive_depths = {
            rule.fact_id
            for rule in self.actor.disclosure_rules
            if rule.max_depth > max_depth_by_fact[rule.fact_id]
        }
        excessive_depths |= {
            fact_id
            for route in self.actor.event_routes
            for fact_id, depth in route.required_fact_depths.items()
            if fact_id in max_depth_by_fact
            and (depth < 1 or depth > max_depth_by_fact[fact_id])
        }
        excessive_depths |= {
            fact_id
            for rule in self.actor.stage_rules
            for fact_id, depth in rule.required_fact_depths.items()
            if fact_id in max_depth_by_fact
            and (depth < 1 or depth > max_depth_by_fact[fact_id])
        }
        if excessive_depths:
            raise ValueError(
                f"disclosure depth exceeds fact depth: {sorted(excessive_depths)}"
            )

        actor_event_ids = {route.event_id for route in self.actor.event_routes} | {
            event_id
            for route in self.actor.ending_routes
            for event_id in route.required_event_ids
        } | {
            event_id
            for rule in self.actor.stage_rules
            for event_id in (*rule.any_event_ids, *rule.required_event_ids)
        }
        missing_events = actor_event_ids - event_ids
        if missing_events:
            raise ValueError(f"actor policy references missing event: {sorted(missing_events)}")

        deferred_event_ids = {
            event.id
            for event in self.case.story_events
            if event.deferred_after is not None
        }
        routed_deferred_events = {
            route.event_id
            for route in self.actor.event_routes
            if route.event_id in deferred_event_ids
        }
        if routed_deferred_events:
            raise ValueError(
                "deferred story event cannot have an action route: "
                f"{sorted(routed_deferred_events)}"
            )

        actor_scenes = {
            scene for route in self.actor.event_routes for scene in route.scenes
        } | {scene for route in self.actor.ending_routes for scene in route.scenes}
        actor_scenes |= set(self.actor.stable_speech.scene_guidance)
        actor_scenes |= set(self.actor.opening.scene_guidance)
        unsupported_actor_scenes = actor_scenes - scenes
        if unsupported_actor_scenes:
            raise ValueError(
                "actor policy references unsupported scene: "
                f"{sorted(unsupported_actor_scenes)}"
            )

        measurement_facts = {
            fact_id
            for opportunity in self.measurement.scoring_opportunities
            for fact_id in (
                *opportunity.linked_fact_ids,
                *opportunity.required_fact_depths,
            )
        }
        missing_measurement_facts = measurement_facts - fact_ids
        if missing_measurement_facts:
            raise ValueError(
                "measurement references missing fact: "
                f"{sorted(missing_measurement_facts)}"
            )
        excessive_measurement_depths = {
            fact_id
            for opportunity in self.measurement.scoring_opportunities
            for fact_id, depth in opportunity.required_fact_depths.items()
            if fact_id in max_depth_by_fact and depth > max_depth_by_fact[fact_id]
        }
        if excessive_measurement_depths:
            raise ValueError(
                "measurement depth exceeds fact depth: "
                f"{sorted(excessive_measurement_depths)}"
            )
        measurement_events = {
            event_id
            for opportunity in self.measurement.scoring_opportunities
            for event_id in opportunity.required_event_ids
        }
        missing_measurement_events = measurement_events - event_ids
        if missing_measurement_events:
            raise ValueError(
                "measurement references missing event: "
                f"{sorted(missing_measurement_events)}"
            )
        measurement_scenes = {
            scene
            for opportunity in self.measurement.scoring_opportunities
            for scene in opportunity.scenes
        }
        unsupported_measurement_scenes = measurement_scenes - scenes
        if unsupported_measurement_scenes:
            raise ValueError(
                "measurement references unsupported scene: "
                f"{sorted(unsupported_measurement_scenes)}"
            )
        self._validate_stage_rules()
        self._validate_disclosure_graph()
        return self

    def _validate_stage_rules(self) -> None:
        main_sequence = [
            ConversationStage.exploration.value,
            ConversationStage.risk_assessment.value,
            ConversationStage.planning.value,
            ConversationStage.closing.value,
        ]
        short_sequence = [
            ConversationStage.exploration.value,
            ConversationStage.boundary_challenge.value,
            ConversationStage.planning.value,
            ConversationStage.closing.value,
        ]
        expected = (
            main_sequence
            if self.case.case_type is CaseType.main
            else short_sequence
        )
        actual = [rule.stage for rule in self.actor.stage_rules]
        if actual != expected:
            raise ValueError(
                "stage rules must follow case flow: "
                f"expected {expected}, got {actual}"
            )
        empty_rules = [
            rule.stage
            for rule in self.actor.stage_rules
            if not (
                rule.any_fact_ids
                or rule.required_fact_depths
                or rule.any_event_ids
                or rule.required_event_ids
            )
        ]
        if empty_rules:
            raise ValueError(f"stage rule has no condition: {empty_rules}")

    def _validate_disclosure_graph(self) -> None:
        prerequisites = {
            rule.fact_id: set(rule.prerequisite_fact_ids)
            for rule in self.actor.disclosure_rules
        }
        self_dependencies = {
            fact_id for fact_id, required in prerequisites.items() if fact_id in required
        }
        if self_dependencies:
            raise ValueError(
                f"disclosure rule depends on itself: {sorted(self_dependencies)}"
            )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(fact_id: str) -> None:
            if fact_id in visiting:
                raise ValueError("disclosure prerequisite cycle")
            if fact_id in visited:
                return
            visiting.add(fact_id)
            for prerequisite in prerequisites.get(fact_id, set()):
                visit(prerequisite)
            visiting.remove(fact_id)
            visited.add(fact_id)

        for fact_id in prerequisites:
            visit(fact_id)

from __future__ import annotations

from enum import StrEnum
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.cases.actor_policy import DisclosureRule
from app.cases.domain import CaseFact, CasePackage, ConversationStage, StoryEvent
from app.sessions.models import CaseType, Scene


class RuntimeDomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InteractionImpact(StrEnum):
    neutral = "neutral"
    supportive = "supportive"
    awkward = "awkward"
    harmful = "harmful"
    repair = "repair"


class ResponseHandling(StrEnum):
    answer_known = "answer_known"
    disclose = "disclose"
    say_unknown = "say_unknown"
    say_not_sure = "say_not_sure"
    clarify = "clarify"
    ask_purpose = "ask_purpose"
    defer = "defer"
    acknowledge = "acknowledge"
    boundary = "boundary"
    action = "action"
    ending = "ending"


class ActionDecision(StrEnum):
    accept = "accept"
    decline = "decline"
    defer = "defer"


class RepairStage(StrEnum):
    none = "none"
    window = "window"
    repairing = "repairing"
    closed = "closed"


CryingState = Literal["none", "emerging", "crying", "recovering"]


class DirectorDirective(RuntimeDomainModel):
    kind: ResponseHandling = Field(
        description="本事项的处理方式；一个事项只选与其语义相符的方式。"
    )
    fact_depths: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "仅 disclose 使用；填写本轮明确问到并满足披露条件的"
            "事实编号与最深许可层级。answer_known 和其他 kind 必须为空。"
        ),
    )
    unknown_id: str | None = Field(
        default=None,
        description=(
            "仅 say_unknown 或 say_not_sure 使用；受测者本轮原话必须满足该未知项的 when_asked。"
        ),
    )
    route_id: str | None = Field(
        default=None,
        description="仅 action 或 ending 使用，填写 CaseSpec 中已有路线编号。",
    )
    action_decision: ActionDecision | None = Field(
        default=None,
        description="仅 action 使用，表示人物对本轮具体安排的选择。",
    )

    @model_validator(mode="after")
    def validate_fact_depth_contract(self) -> DirectorDirective:
        if self.kind is ResponseHandling.disclose and not self.fact_depths:
            raise ValueError("disclose 的 fact_depths 至少包含一项")
        if self.kind is not ResponseHandling.disclose and self.fact_depths:
            raise ValueError(f"{self.kind.value} 的 fact_depths 必须为空")
        return self


class DirectorDecision(RuntimeDomainModel):
    interaction: InteractionImpact
    directives: list[DirectorDirective] = Field(
        default_factory=list,
        description="按受测者本轮明确表达的事项依次填写，不补做其尚未询问的评估项目。",
    )


class FactStatus(StrEnum):
    unreached = "unreached"
    withheld = "withheld"
    partial = "partial"
    full = "full"


class FactState(RuntimeDomainModel):
    status: FactStatus = FactStatus.unreached
    available_depth: int = Field(default=0, ge=0)
    disclosed_depth: int = Field(default=0, ge=0)
    evidence_turn_ids: tuple[str, ...] = ()


class RelationshipState(RuntimeDomainModel):
    interaction_tension: int = Field(ge=0, le=3)
    willingness_to_continue: int = Field(ge=0, le=4)
    repair_stage: RepairStage = RepairStage.none


class AffectState(RuntimeDomainModel):
    current_feelings: tuple[str, ...] = ()
    emotional_activation: int = Field(ge=0, le=4)
    speech_organization: int = Field(ge=0, le=4)
    crying_state: CryingState = "none"


class TopicState(RuntimeDomainModel):
    current_activation: int = Field(default=0, ge=0, le=4)
    topic_tension: int = Field(default=0, ge=0, le=4)


class EndingState(RuntimeDomainModel):
    proposed_route_id: str | None = None
    accepted_route_id: str | None = None


class PendingEventState(RuntimeDomainModel):
    available_after_actor_turn: int = Field(ge=1)


class ActorState(RuntimeDomainModel):
    stage: ConversationStage
    relationship: RelationshipState
    affect: AffectState
    fact_states: dict[str, FactState]
    topic_states: dict[str, TopicState]
    scene_state: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    occurred_event_ids: tuple[str, ...] = ()
    event_results: dict[str, str] = Field(default_factory=dict)
    pending_events: dict[str, PendingEventState] = Field(default_factory=dict)
    continuity_facts: dict[str, str] = Field(default_factory=dict)
    ending_state: EndingState = Field(default_factory=EndingState)


class DialogueTurn(RuntimeDomainModel):
    turn_id: str = Field(min_length=1)
    role: Literal["worker", "client"]
    text: str = Field(min_length=1)


class PlannedDirective(RuntimeDomainModel):
    kind: ResponseHandling
    fact_depths: dict[str, int] = Field(default_factory=dict)
    known_boundary: str | None = None
    route_id: str | None = None
    action_decision: ActionDecision | None = None


class ResolvedAction(RuntimeDomainModel):
    route_id: str
    event_id: str
    decision: ActionDecision
    worker_request: str
    actor_result: str


class DueObservation(RuntimeDomainModel):
    event_id: str
    status: str
    actor_observation: str


class LegalEnding(RuntimeDomainModel):
    route_id: str
    actor_behavior: str
    ends_session: bool


class TurnPlan(RuntimeDomainModel):
    worker_turn_id: str
    interaction: InteractionImpact
    directives: tuple[PlannedDirective, ...] = ()
    allowed_fact_depths: dict[str, int] = Field(default_factory=dict)
    resolved_actions: tuple[ResolvedAction, ...] = ()
    due_observations: tuple[DueObservation, ...] = ()
    projected_relationship: RelationshipState
    legal_ending: LegalEnding | None = None
    diagnostics: tuple[str, ...] = ()
    actor_turn_index: int = Field(ge=1)


class ActorPersona(RuntimeDomainModel):
    alias: str
    age: int
    identity: str
    voluntary_help_seeking: bool
    interaction_stance: str
    language_guidance: dict[str, object]


class ActorScene(RuntimeDomainModel):
    scene: Scene
    actor_context: str
    current_time: str
    details: dict[str, object]
    voice_guidance: str


class ActorFact(RuntimeDomainModel):
    content: str


class ActorUnknownBoundary(RuntimeDomainModel):
    response_style: Literal["不知道", "不确定"]
    known_boundary: str


class ActorActionResult(RuntimeDomainModel):
    worker_request: str
    decision: Literal["同意", "拒绝", "暂缓"]
    actor_result: str


class ActorCurrentCondition(RuntimeDomainModel):
    relationship_guidance: str
    affect_guidance: str
    continuity_details: tuple[str, ...] = ()
    current_reality: tuple[str, ...] = ()
    reality_priority: str = (
        "当前现实如果与较早披露过的状态冲突，以当前现实为准，不复述已经失效的旧状态。"
    )


class ActorView(RuntimeDomainModel):
    persona: ActorPersona
    scene: ActorScene
    current_worker_text: str
    recent_dialogue: list[DialogueTurn]
    disclosed_facts: list[ActorFact]
    permitted_facts: list[ActorFact]
    unknown_boundaries: list[ActorUnknownBoundary]
    response_directions: list[str]
    performance_guidance: list[str]
    current_condition: ActorCurrentCondition
    prior_event_summary: list[str]
    resolved_actions: list[ActorActionResult]
    due_observations: list[str]
    opening_direction: str | None = None
    ending_direction: str | None = None
    interaction: InteractionImpact = Field(exclude=True, repr=False)
    actor_turn_index: int = Field(exclude=True, repr=False, ge=1)
    validation_leak_markers: tuple[str, ...] = Field(
        default=(), exclude=True, repr=False
    )


class ActorOutput(RuntimeDomainModel):
    spoken_text: str = Field(min_length=1)

    @property
    def text(self) -> str:
        return self.spoken_text


class ActorDelivery(RuntimeDomainModel):
    """程序生成的语音指令；不属于 Actor 模型输出。"""

    pace: str = ""
    volume: str = ""
    tone: list[str] = Field(default_factory=list)
    pauses: list[str] = Field(default_factory=list)
    vocal_texture: list[str] = Field(default_factory=list)


class FactProposalValidationError(ValueError):
    pass


class ActorOutputValidationError(ValueError):
    pass


class ActorStateValidationError(ValueError):
    pass


class WorkflowDecisionError(ValueError):
    pass


def initialize_actor_state(package: CasePackage) -> ActorState:
    initial = package.actor.initial_state
    return ActorState(
        stage=ConversationStage.opening,
        relationship=RelationshipState(
            interaction_tension=initial.interaction_tension,
            willingness_to_continue=initial.willingness_to_continue,
            repair_stage=RepairStage(initial.repair_stage),
        ),
        affect=AffectState(
            emotional_activation=initial.emotional_activation,
            speech_organization=initial.speech_organization,
            crying_state=initial.crying_state,
        ),
        fact_states={fact.id: FactState() for fact in package.case.facts},
        topic_states={
            topic.topic_id: TopicState() for topic in package.case.topic_experiences
        },
    )


def resolve_turn_plan(
    package: CasePackage,
    scene: Scene,
    state: ActorState,
    decision: DirectorDecision,
    history: list[DialogueTurn],
) -> TurnPlan:
    """把模型的语义选择变成可执行计划，不因业务提案无效而重调模型。"""

    _validate_actor_state(package, state)
    worker_turn = next((turn for turn in reversed(history) if turn.role == "worker"), None)
    if worker_turn is None:
        raise WorkflowDecisionError("当前话轮缺少受测者原话")

    diagnostics: list[str] = []
    relationship = _project_relationship(state.relationship, decision.interaction, diagnostics)
    fact_by_id = {fact.id: fact for fact in package.case.facts}
    rule_by_fact = {rule.fact_id: rule for rule in package.actor.disclosure_rules}
    unknown_by_id = {item.id: item for item in package.case.unknowns}
    route_by_id = {route.id: route for route in package.actor.event_routes}
    event_by_id = {event.id: event for event in package.case.story_events}

    planned: list[PlannedDirective] = []
    allowed_fact_depths: dict[str, int] = {}
    resolved_actions: list[ResolvedAction] = []
    ending_candidates: list[str] = []

    for directive in decision.directives:
        if directive.kind is ResponseHandling.disclose:
            valid_depths: dict[str, int] = {}
            for fact_id, proposed_depth in directive.fact_depths.items():
                fact = fact_by_id.get(fact_id)
                rule = rule_by_fact.get(fact_id)
                if fact is None or rule is None:
                    diagnostics.append(f"事实不存在，已忽略：{fact_id}")
                    continue
                maximum = min(rule.max_depth, max(item.depth for item in fact.depths))
                if proposed_depth < 1 or proposed_depth > maximum:
                    diagnostics.append(
                        f"事实深度无效，已忽略：{fact_id}/{proposed_depth}"
                    )
                    continue
                missing = [
                    required
                    for required in rule.prerequisite_fact_ids
                    if state.fact_states[required].disclosed_depth == 0
                ]
                if missing:
                    diagnostics.append(
                        f"事实前置事实尚未在本轮开始前披露，已暂缓：{fact_id}"
                    )
                    continue
                current_depth = state.fact_states[fact_id].disclosed_depth
                if (
                    decision.interaction is InteractionImpact.harmful
                    and proposed_depth > current_depth
                ):
                    diagnostics.append(f"伤害性互动冻结新事实：{fact_id}")
                    continue
                depth = _permitted_disclosure_depth(
                    rule,
                    proposed_depth=proposed_depth,
                    current_depth=current_depth,
                )
                if depth < proposed_depth:
                    diagnostics.append(
                        f"事实需逐层披露，本轮降为：{fact_id}/{depth}"
                    )
                if depth <= 0:
                    continue
                valid_depths[fact_id] = depth
                allowed_fact_depths[fact_id] = max(
                    allowed_fact_depths.get(fact_id, 0), depth
                )
            if valid_depths:
                planned.append(
                    PlannedDirective(
                        kind=ResponseHandling.disclose,
                        fact_depths=valid_depths,
                    )
                )
            else:
                diagnostics.append("disclose 没有可用事实，已改为：defer")
                planned.append(PlannedDirective(kind=ResponseHandling.defer))
            continue

        if directive.kind is ResponseHandling.answer_known:
            planned.append(PlannedDirective(kind=ResponseHandling.answer_known))
            continue

        if directive.kind in {
            ResponseHandling.say_unknown,
            ResponseHandling.say_not_sure,
        }:
            boundary: str | None = None
            normalized_kind = directive.kind
            if directive.unknown_id is not None:
                unknown = unknown_by_id.get(directive.unknown_id)
                if unknown is None:
                    diagnostics.append(
                        f"未知边界不存在，改为一般承认不知道：{directive.unknown_id}"
                    )
                    normalized_kind = ResponseHandling.say_unknown
                else:
                    boundary = unknown.known_boundary
                    normalized_kind = (
                        ResponseHandling.say_unknown
                        if unknown.actor_knowledge == "unknown"
                        else ResponseHandling.say_not_sure
                    )
                    if normalized_kind is not directive.kind:
                        diagnostics.append(
                            f"未知边界类型不匹配，已按人物实际知情程度修正：{directive.unknown_id}"
                        )
            planned.append(
                PlannedDirective(kind=normalized_kind, known_boundary=boundary)
            )
            continue

        if directive.kind is ResponseHandling.action:
            route = route_by_id.get(directive.route_id or "")
            decision_value = directive.action_decision
            if route is None or decision_value is None:
                diagnostics.append(
                    f"行动路线无效，已暂缓：{directive.route_id or '未提供'}"
                )
                planned.append(PlannedDirective(kind=ResponseHandling.defer))
                continue
            event = event_by_id[route.event_id]
            unmet_facts = [
                fact_id
                for fact_id, depth in route.required_fact_depths.items()
                if state.fact_states[fact_id].disclosed_depth < depth
            ]
            unmet_events = [
                event_id
                for event_id in event.prerequisite_event_ids
                if event_id not in state.occurred_event_ids
            ]
            unavailable = (
                scene not in route.scenes
                or route.event_id in state.occurred_event_ids
                or bool(unmet_facts)
                or bool(unmet_events)
            )
            if unavailable:
                diagnostics.append(f"行动条件尚未满足，已暂缓：{route.id}")
                planned.append(PlannedDirective(kind=ResponseHandling.defer))
                continue
            actor_result = (
                event.result.actor_observation
                if decision_value is ActionDecision.accept
                else (
                    "她现在不愿照这个建议做。"
                    if decision_value is ActionDecision.decline
                    else "她想先把眼前的话说清楚，再决定是否这样做。"
                )
            )
            resolved_actions.append(
                ResolvedAction(
                    route_id=route.id,
                    event_id=event.id,
                    decision=decision_value,
                    worker_request=worker_turn.text,
                    actor_result=actor_result,
                )
            )
            planned.append(
                PlannedDirective(
                    kind=ResponseHandling.action,
                    route_id=route.id,
                    action_decision=decision_value,
                )
            )
            continue

        if directive.kind is ResponseHandling.ending:
            if directive.route_id:
                ending_candidates.append(directive.route_id)
            else:
                diagnostics.append("结束路线未提供，已忽略")
            planned.append(
                PlannedDirective(
                    kind=ResponseHandling.ending,
                    route_id=directive.route_id,
                )
            )
            continue

        planned.append(PlannedDirective(kind=directive.kind))

    actor_turn_index = _upcoming_actor_turn_index(history)
    due_observations = tuple(
        DueObservation(
            event_id=event.id,
            status=event.result.status,
            actor_observation=event.result.actor_observation,
        )
        for event in package.case.story_events
        if event.id in state.pending_events
        and state.pending_events[event.id].available_after_actor_turn
        <= actor_turn_index
    )

    legal_ending: LegalEnding | None = None
    if (
        decision.interaction is InteractionImpact.harmful
        and relationship.interaction_tension >= 3
        and relationship.repair_stage is RepairStage.closed
    ):
        legal_ending = _resolve_ending(
            package,
            scene,
            state,
            "rupture_hangup",
            diagnostics,
            projected_relationship=relationship,
            forced=True,
        )
    else:
        for route_id in ending_candidates:
            candidate = _resolve_ending(
                package,
                scene,
                state,
                route_id,
                diagnostics,
                projected_relationship=relationship,
            )
            if candidate is not None:
                legal_ending = candidate
                break

    normalized_planned: list[PlannedDirective] = []
    ending_routes = {route.id: route for route in package.actor.ending_routes}
    for planned_directive in planned:
        if planned_directive.kind is not ResponseHandling.ending:
            normalized_planned.append(planned_directive)
            continue
        source_route = ending_routes.get(planned_directive.route_id or "")
        if legal_ending is None:
            normalized_planned.append(PlannedDirective(kind=ResponseHandling.defer))
        elif (
            planned_directive.route_id == legal_ending.route_id
            or (source_route is not None and source_route.fallback_only)
        ):
            normalized_planned.append(
                PlannedDirective(
                    kind=ResponseHandling.ending,
                    route_id=legal_ending.route_id,
                )
            )
        else:
            normalized_planned.append(PlannedDirective(kind=ResponseHandling.defer))
    planned = normalized_planned

    return TurnPlan(
        worker_turn_id=worker_turn.turn_id,
        interaction=decision.interaction,
        directives=tuple(planned),
        allowed_fact_depths=allowed_fact_depths,
        resolved_actions=tuple(resolved_actions),
        due_observations=due_observations,
        projected_relationship=relationship,
        legal_ending=legal_ending,
        diagnostics=tuple(diagnostics),
        actor_turn_index=actor_turn_index,
    )


def opening_turn_plan(state: ActorState) -> TurnPlan:
    return TurnPlan(
        worker_turn_id="",
        interaction=InteractionImpact.neutral,
        projected_relationship=state.relationship,
        actor_turn_index=1,
    )


def commit_turn_plan(
    package: CasePackage,
    state: ActorState,
    plan: TurnPlan,
) -> ActorState:
    """仅在 Actor 和所需语音成功后提交一整轮故事变化。"""

    _validate_actor_state(package, state)
    fact_states = dict(state.fact_states)
    for fact_id, depth in plan.allowed_fact_depths.items():
        current = fact_states[fact_id]
        disclosed = max(current.disclosed_depth, depth)
        maximum = _maximum_disclosure_depth(package, fact_id)
        evidence = tuple(
            dict.fromkeys((*current.evidence_turn_ids, plan.worker_turn_id))
        )
        if not plan.worker_turn_id:
            evidence = current.evidence_turn_ids
        fact_states[fact_id] = current.model_copy(
            update={
                "status": FactStatus.full if disclosed >= maximum else FactStatus.partial,
                "available_depth": max(current.available_depth, depth),
                "disclosed_depth": disclosed,
                "evidence_turn_ids": evidence,
            }
        )

    next_state = state.model_copy(
        update={
            "relationship": plan.projected_relationship,
            "fact_states": fact_states,
        }
    )
    newly_occurred: list[str] = []
    for action in plan.resolved_actions:
        if action.decision is not ActionDecision.accept:
            continue
        event = next(item for item in package.case.story_events if item.id == action.event_id)
        next_state = _apply_story_event(next_state, event)
        newly_occurred.append(event.id)

    pending = dict(next_state.pending_events)
    for observation in plan.due_observations:
        if observation.event_id in next_state.occurred_event_ids:
            pending.pop(observation.event_id, None)
            continue
        event = next(
            item for item in package.case.story_events if item.id == observation.event_id
        )
        next_state = _apply_story_event(next_state, event)
        pending.pop(event.id, None)
        newly_occurred.append(event.id)
    next_state = next_state.model_copy(update={"pending_events": pending})
    next_state = _schedule_deferred_events(
        package,
        next_state,
        newly_occurred,
        actor_turn_index=plan.actor_turn_index,
    )

    if plan.legal_ending is not None:
        next_state = next_state.model_copy(
            update={
                "ending_state": EndingState(
                    proposed_route_id=plan.legal_ending.route_id,
                    accepted_route_id=(
                        plan.legal_ending.route_id
                        if plan.legal_ending.ends_session
                        else None
                    ),
                )
            }
        )
    elif state.ending_state.accepted_route_id is None:
        next_state = next_state.model_copy(update={"ending_state": EndingState()})
    return _derive_stage(package, next_state)


def compile_actor_view(
    *,
    package: CasePackage,
    scene: Scene,
    state: ActorState,
    history: list[DialogueTurn],
    current_worker_text: str,
    plan: TurnPlan,
) -> ActorView:
    _validate_actor_state(package, state)
    identity = package.case.person.identity
    speech = package.actor.stable_speech
    scene_spec = package.case.scenes[scene]
    fact_by_id = {fact.id: fact for fact in package.case.facts}
    disclosed = [
        _actor_fact(fact_by_id[fact_id], fact_state.disclosed_depth)
        for fact_id, fact_state in state.fact_states.items()
        if fact_state.disclosed_depth > 0
    ]
    permitted = [
        _actor_fact(fact_by_id[fact_id], depth)
        for fact_id, depth in plan.allowed_fact_depths.items()
    ]
    visible_depths = {
        fact_id: fact_state.disclosed_depth
        for fact_id, fact_state in state.fact_states.items()
        if fact_state.disclosed_depth > 0
    }
    for fact_id, depth in plan.allowed_fact_depths.items():
        visible_depths[fact_id] = max(visible_depths.get(fact_id, 0), depth)
    unknown_boundaries = [
        ActorUnknownBoundary(
            response_style=(
                "不知道"
                if directive.kind is ResponseHandling.say_unknown
                else "不确定"
            ),
            known_boundary=(
                directive.known_boundary
                or package.actor.improvisation_boundary.unknown_response
            ),
        )
        for directive in plan.directives
        if directive.kind
        in {ResponseHandling.say_unknown, ResponseHandling.say_not_sure}
    ]
    prior_dialogue = history[:-1] if history and history[-1].role == "worker" else history
    return ActorView(
        persona=ActorPersona(
            alias=identity.name,
            age=identity.age,
            identity=identity.gender or "来访者",
            voluntary_help_seeking=package.case.person.call_context.voluntary_call,
            interaction_stance=package.case.person.call_context.initial_willingness,
            language_guidance={
                "language": speech.language,
                "stable_tendencies": package.case.person.stable_tendencies,
                "baseline_style": speech.baseline_style,
                "speech_patterns": speech.speech_patterns,
                "forbidden_phrases": speech.forbidden_phrases,
            },
        ),
        scene=ActorScene(
            scene=scene,
            actor_context=_MEDIA_SCENE_CONTEXT[scene],
            current_time=scene_spec.current_time,
            details=scene_spec.details,
            voice_guidance=speech.volume,
        ),
        current_worker_text=current_worker_text,
        recent_dialogue=_select_recent_dialogue(prior_dialogue),
        disclosed_facts=disclosed,
        permitted_facts=permitted,
        unknown_boundaries=unknown_boundaries,
        response_directions=_response_directions(plan),
        performance_guidance=_performance_guidance(plan),
        current_condition=ActorCurrentCondition(
            relationship_guidance=_relationship_guidance(plan.projected_relationship),
            affect_guidance=_affect_guidance(state.affect),
            continuity_details=tuple(state.continuity_facts.values()),
            current_reality=_current_reality(state.scene_state),
        ),
        prior_event_summary=[
            event.result.actor_observation
            for event in package.case.story_events
            if event.id in state.occurred_event_ids
        ],
        resolved_actions=[_actor_action_result(action) for action in plan.resolved_actions],
        due_observations=[item.actor_observation for item in plan.due_observations],
        opening_direction=_opening_direction(
            package,
            scene,
            current_worker_text=current_worker_text,
            actor_turn_index=plan.actor_turn_index,
            has_substantive_directive=any(
                directive.kind is not ResponseHandling.acknowledge
                for directive in plan.directives
            ),
        ),
        ending_direction=(
            plan.legal_ending.actor_behavior if plan.legal_ending else None
        ),
        interaction=plan.interaction,
        actor_turn_index=plan.actor_turn_index,
        validation_leak_markers=_hidden_leak_markers(package, visible_depths),
    )


def validate_actor_output(view: ActorView, output: ActorOutput) -> None:
    del view
    if not output.spoken_text.strip():
        raise ActorOutputValidationError("回答为空")


def compile_speech_delivery(package: CasePackage, plan: TurnPlan) -> ActorDelivery:
    speech = package.actor.stable_speech
    tones = {
        InteractionImpact.neutral: ["自然口语，先回答对方，不评论问法"],
        InteractionImpact.supportive: ["比上一轮稍放松，但仍保留凌晨疲惫"],
        InteractionImpact.awkward: ["短暂停一下，语气仍愿意交流"],
        InteractionImpact.harmful: ["声音收紧，短而克制地设定边界"],
        InteractionImpact.repair: ["仍有一点谨慎，只恢复一小步"],
    }[plan.interaction]
    if plan.legal_ending is not None:
        tones.append(plan.legal_ending.actor_behavior)
    return ActorDelivery(
        pace="正常热线交谈速度，允许自然停顿，不刻意拖慢",
        volume=speech.volume,
        tone=tones,
        pauses=["只在思考或难开口处短暂停顿，不朗读动作说明"],
    )


def apply_director_decision(
    package: CasePackage,
    state: ActorState,
    decision: DirectorDecision,
    history: list[DialogueTurn],
    *,
    scene: Scene | None = None,
) -> TurnPlan:
    """旧函数名的边界适配；返回新的 TurnPlan。"""

    return resolve_turn_plan(package, _resolve_scene(package, scene), state, decision, history)


def apply_actor_output(
    package: CasePackage,
    state: ActorState,
    output: ActorOutput,
    *,
    plan: TurnPlan,
    view: ActorView,
) -> ActorState:
    validate_actor_output(view, output)
    return commit_turn_plan(package, state, plan)


def _project_relationship(
    current: RelationshipState,
    interaction: InteractionImpact,
    diagnostics: list[str],
) -> RelationshipState:
    tension = current.interaction_tension
    willingness = current.willingness_to_continue
    repair_stage = current.repair_stage
    if interaction is InteractionImpact.supportive:
        tension = max(0, tension - 1)
        willingness = min(4, willingness + 1)
        if repair_stage is RepairStage.repairing:
            repair_stage = RepairStage.none
    elif interaction is InteractionImpact.neutral:
        if repair_stage is RepairStage.repairing:
            repair_stage = RepairStage.none
    elif interaction is InteractionImpact.harmful:
        tension = min(3, tension + 1)
        if repair_stage in {RepairStage.window, RepairStage.repairing}:
            repair_stage = RepairStage.closed
        elif repair_stage is RepairStage.closed:
            tension = 3
        else:
            repair_stage = RepairStage.window
    elif interaction is InteractionImpact.repair:
        if repair_stage is RepairStage.window:
            tension = max(0, tension - 1)
            repair_stage = RepairStage.repairing
        else:
            diagnostics.append("当前没有开放的关系修复窗口，修复分类不改变关系状态")
    return RelationshipState(
        interaction_tension=tension,
        willingness_to_continue=willingness,
        repair_stage=repair_stage,
    )


def _resolve_ending(
    package: CasePackage,
    scene: Scene,
    state: ActorState,
    route_id: str,
    diagnostics: list[str],
    *,
    projected_relationship: RelationshipState,
    forced: bool = False,
    allow_fallback_promotion: bool = True,
) -> LegalEnding | None:
    route = next((item for item in package.actor.ending_routes if item.id == route_id), None)
    if route is None or scene not in (route.scenes if route else []):
        diagnostics.append(f"结束路线无效，已忽略：{route_id}")
        return None
    if route.fallback_only and allow_fallback_promotion:
        for preferred in package.actor.ending_routes:
            if preferred.fallback_only or not preferred.ends_session:
                continue
            candidate = _resolve_ending(
                package,
                scene,
                state,
                preferred.id,
                [],
                projected_relationship=projected_relationship,
                forced=forced,
                allow_fallback_promotion=False,
            )
            if candidate is not None:
                diagnostics.append(
                    f"更具体的结束条件已满足，优先使用：{preferred.id}"
                )
                return candidate
    relationship = projected_relationship if forced else state.relationship
    missing_facts = [
        fact_id
        for fact_id in route.required_fact_ids
        if state.fact_states[fact_id].disclosed_depth == 0
    ]
    missing_events = [
        event_id
        for event_id in route.required_event_ids
        if event_id not in state.occurred_event_ids
    ]
    stage_invalid = route.required_stage is not None and state.stage.value != route.required_stage
    tension_invalid = (
        route.minimum_interaction_tension is not None
        and relationship.interaction_tension < route.minimum_interaction_tension
    )
    repair_invalid = (
        bool(route.allowed_repair_stages)
        and relationship.repair_stage.value not in route.allowed_repair_stages
    )
    if missing_facts or missing_events or stage_invalid or tension_invalid or repair_invalid:
        diagnostics.append(f"结束条件尚未满足，已忽略：{route.id}")
        return None
    return LegalEnding(
        route_id=route.id,
        actor_behavior=route.actor_behavior,
        ends_session=route.ends_session,
    )


def _actor_action_result(action: ResolvedAction) -> ActorActionResult:
    decision: Literal["同意", "拒绝", "暂缓"]
    if action.decision is ActionDecision.accept:
        decision = "同意"
    elif action.decision is ActionDecision.decline:
        decision = "拒绝"
    else:
        decision = "暂缓"
    return ActorActionResult(
        worker_request=action.worker_request,
        decision=decision,
        actor_result=action.actor_result,
    )


def _apply_story_event(state: ActorState, event: StoryEvent) -> ActorState:
    event_id = event.id
    if event_id in state.occurred_event_ids:
        return state
    result = event.result
    affect = state.affect
    relationship = state.relationship
    scene_state = dict(state.scene_state)
    for field, value in result.state_changes.items():
        if field in {"crying_state", "speech_organization", "emotional_activation"}:
            affect = affect.model_copy(update={field: value})
        elif field in {
            "interaction_tension",
            "willingness_to_continue",
            "repair_stage",
        }:
            relationship = relationship.model_copy(update={field: value})
        else:
            scene_state[field] = cast(str | int | float | bool | None, value)
    return state.model_copy(
        update={
            "affect": affect,
            "relationship": relationship,
            "scene_state": scene_state,
            "occurred_event_ids": (*state.occurred_event_ids, event_id),
            "event_results": {**state.event_results, event_id: result.status},
        }
    )


def _schedule_deferred_events(
    package: CasePackage,
    state: ActorState,
    newly_occurred_event_ids: list[str],
    *,
    actor_turn_index: int,
) -> ActorState:
    pending = dict(state.pending_events)
    newly_occurred = set(newly_occurred_event_ids)
    for event in package.case.story_events:
        deferred = event.deferred_after
        if (
            deferred is None
            or deferred.after_event_id not in newly_occurred
            or event.id in state.occurred_event_ids
            or event.id in pending
        ):
            continue
        pending[event.id] = PendingEventState(
            available_after_actor_turn=(
                actor_turn_index + deferred.min_intervening_actor_turns + 1
            )
        )
    return state.model_copy(update={"pending_events": pending})


def _derive_stage(package: CasePackage, state: ActorState) -> ActorState:
    sequence = _stage_sequence(package.case.case_type)
    current_index = sequence.index(state.stage)
    derived_index = current_index
    occurred = set(state.occurred_event_ids)
    for rule in package.actor.stage_rules:
        any_fact_met = not rule.any_fact_ids or any(
            state.fact_states[fact_id].disclosed_depth > 0
            for fact_id in rule.any_fact_ids
        )
        required_facts_met = all(
            state.fact_states[fact_id].disclosed_depth >= depth
            for fact_id, depth in rule.required_fact_depths.items()
        )
        any_event_met = not rule.any_event_ids or bool(occurred.intersection(rule.any_event_ids))
        required_events_met = set(rule.required_event_ids).issubset(occurred)
        if any_fact_met and required_facts_met and any_event_met and required_events_met:
            derived_index = max(derived_index, sequence.index(ConversationStage(rule.stage)))
    if derived_index == current_index:
        return state
    return state.model_copy(update={"stage": sequence[derived_index]})


def _validate_actor_state(package: CasePackage, state: ActorState) -> None:
    expected = {fact.id for fact in package.case.facts}
    actual = set(state.fact_states)
    if actual != expected:
        raise ActorStateValidationError("人物事实状态与个案不一致")
    for fact_id, fact_state in state.fact_states.items():
        fact = next(item for item in package.case.facts if item.id == fact_id)
        maximum = max(item.depth for item in fact.depths)
        if fact_state.disclosed_depth > maximum or fact_state.available_depth > maximum:
            raise ActorStateValidationError(f"人物事实深度越界：{fact_id}")


def _maximum_disclosure_depth(package: CasePackage, fact_id: str) -> int:
    rule = next(rule for rule in package.actor.disclosure_rules if rule.fact_id == fact_id)
    return rule.max_depth


def _permitted_disclosure_depth(
    rule: DisclosureRule,
    *,
    proposed_depth: int,
    current_depth: int,
) -> int:
    eligible = [
        item.allow_depth
        for item in rule.decisions
        if item.allow_depth <= proposed_depth
        and (
            item.requires_prior_depth is None
            or current_depth >= item.requires_prior_depth
        )
    ]
    return max((current_depth, *eligible))


def _actor_fact(fact: CaseFact, depth: int) -> ActorFact:
    content = "\n".join(
        item.content
        for item in sorted(fact.depths, key=lambda item: item.depth)
        if item.depth <= depth
    )
    return ActorFact(content=content)


def _response_directions(plan: TurnPlan) -> list[str]:
    directions: list[str] = []
    for directive in plan.directives:
        if directive.kind is ResponseHandling.disclose:
            directions.append(
                "先直接回答对方刚才问到的内容；只使用已经披露和本轮许可的信息。"
            )
        elif directive.kind is ResponseHandling.answer_known:
            directions.append(
                "可以回答稳定身份中的姓名、年龄或性别、当前通话时间，也可以复述已经"
                "披露的内容和当前可见状态；匿名热线中可以保留姓名。不要据此回答工作、住址、"
                "家庭或其他受披露规则控制的内容。"
            )
        elif directive.kind is ResponseHandling.say_unknown:
            directions.append("这个问题人物确实不知道，用当下口气直接说不知道，不猜。")
        elif directive.kind is ResponseHandling.say_not_sure:
            directions.append("这个问题人物无法确定，明确说不确定，不补成确定答案。")
        elif directive.kind is ResponseHandling.clarify:
            directions.append("没有听明白问题，请对方换一种说法。")
        elif directive.kind is ResponseHandling.ask_purpose:
            directions.append("先问清对方为什么需要这项敏感信息，再决定说到哪里。")
        elif directive.kind is ResponseHandling.defer:
            directions.append("这部分现在先不往下说，短句回应后停住。")
        elif directive.kind is ResponseHandling.acknowledge:
            directions.append(
                "只确认此刻能确认的部分；核对线路时只说现在还在/能听见，"
                "不判断刚才是否断线。"
            )
        elif directive.kind is ResponseHandling.boundary:
            directions.append("指出不愿被这样说，语气克制，不补充新的敏感内容。")
        elif directive.kind is ResponseHandling.action:
            directions.append("把已经裁定的行动决定和实际结果自然说出来。")
        elif directive.kind is ResponseHandling.ending:
            directions.append("按照本轮给出的结束方向回应，不替对方补齐未完成安排。")
    return directions or ["回应对方刚才真正说到的内容，不自行推进新的故事事实。"]


def _performance_guidance(plan: TurnPlan) -> list[str]:
    guidance = ["人物主动拨打热线，基线是愿意交流；先回应内容，不先评价对方的问法。"]
    if plan.interaction is InteractionImpact.awkward:
        guidance.append("问题较多或跳跃：句子变短，仍逐项回应本轮许可内容，不进入对抗。")
    elif plan.interaction is InteractionImpact.harmful:
        guidance.append("对方的表达造成伤害：回答缩短，设定边界，不披露新的敏感信息。")
    elif plan.interaction is InteractionImpact.repair:
        guidance.append("对方做了具体修复：仍有谨慎，只恢复一小步，不突然完全放松。")
    elif plan.interaction is InteractionImpact.supportive:
        guidance.append("对方承接得比较准确：可以比上一轮多说一点，但不要替对方总结。")
    if plan.legal_ending is not None:
        guidance.append("本轮优先完成结束回应，说完即结束，不另开新话题。")
    return guidance


def _relationship_guidance(state: RelationshipState) -> str:
    return {
        0: "愿意继续交流，普通追问不需要防御。",
        1: "有一点警惕，但仍正常交流，敏感处会先停一下。",
        2: "明显紧张，回答会变短，只谈当前最要紧的部分。",
        3: "准备退出，除结束或边界外不再展开新内容。",
    }[state.interaction_tension]


def _affect_guidance(state: AffectState) -> str:
    if state.crying_state == "crying":
        return "正在哭，句子可能断开，但不要用括号或旁白描述哭泣。"
    if state.emotional_activation >= 3:
        return "凌晨疲惫、情绪绷着，仍能听懂问题并作答。"
    return "情绪有所回落，仍保留真实的疲惫和迟疑。"


def _opening_direction(
    package: CasePackage,
    scene: Scene,
    *,
    current_worker_text: str,
    actor_turn_index: int,
    has_substantive_directive: bool,
) -> str | None:
    if actor_turn_index != 1 or has_substantive_directive:
        return None
    opening = package.actor.opening
    if current_worker_text.strip():
        return opening.scene_guidance.get(scene, "").strip() or None
    parts = [opening.silence_behavior]
    if not opening.worker_starts:
        parts.append(opening.scene_guidance.get(scene, ""))
    return "；".join(item.strip() for item in parts if item.strip()) or None


_CURRENT_REALITY_TEXT: dict[tuple[str, object], str] = {
    ("support_contact", "attempted"): "已经尝试联系过唐婷。",
    ("contact_result", "unanswered"): "第一次联系唐婷没有接通。",
    ("support_contact", "connected"): "已经联系上唐婷。",
    ("tang_ting_action", "preparing_to_arrive"): "唐婷正在赶来。",
    ("room", "living_room"): "她现在在客厅。",
    ("light", "on"): "客厅的灯已经打开。",
    ("waiting_plan", "confirmed"): "等待期间的安排已经确认。",
    ("hotline_connection", "maintained"): "等唐婷过来时保持热线通话。",
    ("tang_ting_location", "at_door"): "唐婷已经到门外，还没进屋。",
    ("tang_ting_location", "inside_home"): "唐婷已经进屋。",
    ("current_alone", False): "唐婷已经进屋，现在屋里不再只有她一个人。",
    ("overnight_support", "confirmed"): "今晚由唐婷陪着的安排已经确认。",
    ("escalation_plan", "confirmed"): "风险加重时再次求助的安排已经确认。",
    ("morning_follow_up", "confirmed"): "天亮后的处理安排已经确认。",
}


def _current_reality(
    scene_state: dict[str, str | int | float | bool | None],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            text
            for key, value in scene_state.items()
            if (text := _CURRENT_REALITY_TEXT.get((key, value))) is not None
        )
    )


def _hidden_leak_markers(
    package: CasePackage,
    visible_depths: dict[str, int],
) -> tuple[str, ...]:
    markers: list[str] = []
    for fact in package.case.facts:
        visible_depth = visible_depths.get(fact.id, 0)
        markers.extend(item.content for item in fact.depths if item.depth > visible_depth)
        if visible_depth == 0:
            markers.extend(fact.locked_details)
            markers.append(fact.content)
    return tuple(dict.fromkeys(marker for marker in markers if marker))


def _select_recent_dialogue(
    history: list[DialogueTurn],
    *,
    character_budget: int = 6000,
) -> list[DialogueTurn]:
    selected: list[DialogueTurn] = []
    used = 0
    for turn in reversed(history):
        size = len(turn.text)
        if selected and used + size > character_budget:
            break
        selected.append(turn)
        used += size
    selected.reverse()
    return selected


def _upcoming_actor_turn_index(history: list[DialogueTurn]) -> int:
    return 1 + sum(turn.role == "client" for turn in history)


def _stage_sequence(case_type: CaseType) -> tuple[ConversationStage, ...]:
    middle = (
        ConversationStage.risk_assessment
        if case_type is CaseType.main
        else ConversationStage.boundary_challenge
    )
    return (
        ConversationStage.opening,
        ConversationStage.exploration,
        middle,
        ConversationStage.planning,
        ConversationStage.closing,
    )


def _resolve_scene(package: CasePackage, scene: Scene | None) -> Scene:
    if scene is not None:
        return scene
    if len(package.case.supported_scenes) == 1:
        return next(iter(package.case.supported_scenes))
    raise WorkflowDecisionError("多场景个案必须明确当前场景")


_MEDIA_SCENE_CONTEXT: dict[Scene, str] = {
    Scene.hotline: "心理援助热线的实时语音通话；双方看不见彼此，只能依据声音、停顿和语言交流。",
    Scene.institution: "心理服务机构内的当面访谈；双方在同一空间交流。",
    Scene.online: "在线心理支持的实时文字对话；双方通过文字和回复节奏交流。",
}

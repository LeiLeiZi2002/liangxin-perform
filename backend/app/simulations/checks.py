from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CheckModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapturedTurn(CheckModel):
    sequence: int = Field(ge=1)
    client_turn_id: str
    speaker: Literal["worker", "client"]
    text: str
    signals: dict[str, object] = Field(default_factory=dict)
    audio_available: bool = False
    fact_depths_before: dict[str, int] | None = None


class StateFrame(CheckModel):
    card_id: str | None = None
    conversation_stage: str = "opening"
    fact_depths: dict[str, int] = Field(default_factory=dict)
    event_ids: list[str] = Field(default_factory=list)
    interaction_tension: int = Field(default=0, ge=0)
    willingness_to_continue: int = Field(default=0, ge=0)
    interaction_impact: str | None = None
    repair_stage: str = "none"


class RunEvidence(CheckModel):
    state_frames: list[StateFrame] = Field(default_factory=list)
    db_transcript: list[CapturedTurn] = Field(default_factory=list)
    rest_transcript: list[CapturedTurn] = Field(default_factory=list)
    ws_transcript: list[CapturedTurn] = Field(default_factory=list)
    binary_chunk_count: int = Field(default=0, ge=0)
    final_phase: str
    scene: Literal["institution", "hotline", "online"] | None = None
    final_status: str | None = None
    runtime_failure_count: int = Field(default=0, ge=0)
    runtime_failure_attempt_count: int = Field(default=0, ge=0)
    failed_model_call_count: int = Field(default=0, ge=0)


class CheckResult(CheckModel):
    check_id: str
    passed: bool
    severity: Literal["error", "warning"] = "error"
    detail: str
    evidence: list[dict[str, object]] = Field(default_factory=list)


def run_automatic_checks(
    evidence: RunEvidence,
    *,
    profile: Literal["content", "voice"],
    runtime_engine: Literal["workflow", "character_prompt"] = "workflow",
    max_fact_depths: dict[str, int],
    event_prerequisites: dict[str, list[str]],
    allowed_interaction_impacts_by_card: dict[str, list[str]],
    maximum_fact_depths_after_by_card: dict[str, dict[str, int]] | None = None,
    forbidden_phrases: list[str] | None = None,
    fact_contradiction_cues: dict[str, list[dict[str, object]]] | None = None,
    relationship_arc: tuple[str, str] | None = None,
    card_order: list[str] | None = None,
    earliest_event_card_ids: dict[str, str] | None = None,
    harmful_from_card_id: str | None = None,
    protected_fact_ids: list[str] | None = None,
    objective_contracts: bool = False,
    expected_scene: Literal["institution", "hotline", "online"] | None = None,
    expected_privacy_question: str | None = None,
    forbidden_backend_markers: list[str] | None = None,
) -> list[CheckResult]:
    if runtime_engine == "character_prompt" and objective_contracts:
        results = [
            _turn_pairing_check(evidence.db_transcript),
            _scene_check(evidence.scene, expected_scene),
            _nonempty_text_check(evidence.db_transcript),
            _spoken_text_check(
                evidence.db_transcript,
                forbidden_phrases or [],
                forbidden_backend_markers=forbidden_backend_markers or [],
            ),
        ]
        if profile == "content":
            results.append(_content_audio_check(evidence))
        else:
            results.append(_voice_audio_check(evidence))
        results.extend(
            [
                _ending_status_check(evidence),
                _runtime_failures_check(
                    failed_call_count=evidence.failed_model_call_count,
                    record_count=evidence.runtime_failure_count,
                    recorded_attempt_count=evidence.runtime_failure_attempt_count,
                ),
                _opening_privacy_question_check(
                    evidence.db_transcript,
                    expected_privacy_question,
                ),
            ]
        )
        if expected_scene == "online":
            results.append(_online_message_shape_check(evidence.db_transcript))
        return results

    if runtime_engine == "character_prompt":
        results = [
            _transcript_check(evidence),
            _turn_pairing_check(evidence.db_transcript),
            _spoken_text_check(
                evidence.db_transcript,
                forbidden_phrases or [],
            ),
        ]
        if profile == "content":
            results.append(_content_audio_check(evidence))
        else:
            results.append(_voice_audio_check(evidence))
        return results

    results = [
        _transcript_check(evidence),
        _fact_depth_check(evidence.state_frames, max_fact_depths),
        _registration_check(evidence.db_transcript),
        _relationship_check(evidence.state_frames),
        _relationship_repair_arc_check(evidence.state_frames, relationship_arc),
        _event_order_check(evidence.state_frames, event_prerequisites),
        _event_timing_check(
            evidence.state_frames,
            card_order or [],
            earliest_event_card_ids or {},
        ),
        _interaction_impact_check(
            evidence.state_frames,
            allowed_interaction_impacts_by_card,
        ),
        _disclosure_pacing_check(
            evidence.state_frames,
            maximum_fact_depths_after_by_card or {},
        ),
        _spoken_text_check(
            evidence.db_transcript,
            forbidden_phrases or [],
        ),
        _fact_contradiction_check(
            evidence.db_transcript,
            fact_contradiction_cues or {},
        ),
        _sensitive_fact_check(
            evidence.state_frames,
            harmful_from_card_id=harmful_from_card_id,
            protected_fact_ids=protected_fact_ids or [],
        ),
    ]
    if profile == "content":
        results.append(_content_audio_check(evidence))
    else:
        results.append(_voice_audio_check(evidence))
    return results


def _transcript_check(evidence: RunEvidence) -> CheckResult:
    def identity(turn: CapturedTurn) -> tuple[int, str, str, str]:
        return (turn.sequence, turn.client_turn_id, turn.speaker, turn.text)

    db = [identity(turn) for turn in evidence.db_transcript]
    rest = [identity(turn) for turn in evidence.rest_transcript]
    ws = [identity(turn) for turn in evidence.ws_transcript]
    mismatched_sources: list[str] = []
    if rest != db:
        mismatched_sources.append("REST")
    if ws != db:
        mismatched_sources.append("WebSocket")
    passed = not mismatched_sources
    return CheckResult(
        check_id="transcript_consistency",
        passed=passed,
        detail=(
            "REST、WebSocket 与数据库逐字稿一致"
            if passed
            else (
                f"{'、'.join(mismatched_sources)} 与数据库逐字稿不一致"
                f"（DB {len(db)} 条，REST {len(rest)} 条，WebSocket {len(ws)} 条）"
            )
        ),
    )


def _fact_depth_check(
    frames: list[StateFrame],
    maximums: dict[str, int],
) -> CheckResult:
    errors: list[str] = []
    previous: dict[str, int] = {}
    for frame in frames:
        all_fact_ids = set(previous) | set(frame.fact_depths)
        for fact_id in all_fact_ids:
            prior_depth = previous.get(fact_id, 0)
            depth = frame.fact_depths.get(fact_id, 0)
            if depth < prior_depth:
                errors.append(f"{fact_id} {prior_depth}->{depth}")
            maximum = maximums.get(fact_id)
            if maximum is not None and depth > maximum:
                errors.append(f"{fact_id} {depth}>{maximum}")
        previous = frame.fact_depths
    return CheckResult(
        check_id="fact_depths",
        passed=not errors,
        detail="事实深度单调且未越界" if not errors else "；".join(errors),
    )


def _turn_pairing_check(turns: list[CapturedTurn]) -> CheckResult:
    client_counts: dict[str, int] = {}
    worker_counts: dict[str, int] = {}
    for turn in turns:
        if turn.speaker == "client":
            client_counts[turn.client_turn_id] = (
                client_counts.get(turn.client_turn_id, 0) + 1
            )
        else:
            worker_counts[turn.client_turn_id] = (
                worker_counts.get(turn.client_turn_id, 0) + 1
            )
    opening_client_id = (
        turns[0].client_turn_id
        if turns
        and turns[0].speaker == "client"
        and worker_counts.get(turns[0].client_turn_id, 0) == 0
        else None
    )
    errors: list[str] = []
    for client_turn_id, worker_count in worker_counts.items():
        if worker_count != 1:
            errors.append(f"{client_turn_id}:受测者回合 {worker_count} 条")
        client_count = client_counts.get(client_turn_id, 0)
        if client_count != 1:
            errors.append(f"{client_turn_id}:来访者回合 {client_count} 条")
    for client_turn_id, client_count in client_counts.items():
        if client_turn_id == opening_client_id:
            if client_count != 1:
                errors.append(f"{client_turn_id}:开场回合 {client_count} 条")
            continue
        if client_turn_id not in worker_counts:
            errors.append(f"{client_turn_id}:缺少受测者回合")
    return CheckResult(
        check_id="turn_pairing",
        passed=not errors,
        detail="每个受测者回合均对应一个来访者回合"
        if not errors
        else "；".join(errors),
    )


def _registration_check(turns: list[CapturedTurn]) -> CheckResult:
    workers = {turn.client_turn_id: turn for turn in turns if turn.speaker == "worker"}
    clients = {turn.client_turn_id: turn for turn in turns if turn.speaker == "client"}
    errors: list[str] = []
    for client_turn_id, worker in workers.items():
        client = clients.get(client_turn_id)
        if client is None:
            errors.append(f"{client_turn_id}:缺少来访者回合")
            continue
        decision = _mapping(worker.signals.get("director_decision"))
        turn_plan = _mapping(worker.signals.get("turn_plan"))
        if not decision:
            errors.append(f"{client_turn_id}:缺少 Director 决策")
        else:
            if not isinstance(decision.get("interaction"), str):
                errors.append(f"{client_turn_id}:Director 互动判断缺失")
            if not isinstance(decision.get("directives"), list):
                errors.append(f"{client_turn_id}:Director 指令不是列表")
        if not turn_plan:
            errors.append(f"{client_turn_id}:缺少归一化 TurnPlan")
            continue
        required_fields = {
            "worker_turn_id",
            "interaction",
            "directives",
            "allowed_fact_depths",
            "resolved_actions",
            "due_observations",
            "projected_relationship",
            "legal_ending",
            "diagnostics",
            "actor_turn_index",
        }
        missing_fields = sorted(required_fields - set(turn_plan))
        if missing_fields:
            errors.append(
                f"{client_turn_id}:TurnPlan 字段缺失 " + ", ".join(missing_fields)
            )
        if not isinstance(turn_plan.get("directives"), list):
            errors.append(f"{client_turn_id}:TurnPlan 指令不是列表")
        if not isinstance(turn_plan.get("allowed_fact_depths"), dict):
            errors.append(f"{client_turn_id}:TurnPlan 事实许可不是对象")
        if not isinstance(turn_plan.get("resolved_actions"), list):
            errors.append(f"{client_turn_id}:TurnPlan 行动结果不是列表")
        if not isinstance(turn_plan.get("due_observations"), list):
            errors.append(f"{client_turn_id}:TurnPlan 到期事件不是列表")
        if not isinstance(turn_plan.get("projected_relationship"), dict):
            errors.append(f"{client_turn_id}:TurnPlan 关系投影不是对象")
        if not isinstance(turn_plan.get("diagnostics"), list):
            errors.append(f"{client_turn_id}:TurnPlan 诊断不是列表")
        if not isinstance(turn_plan.get("actor_turn_index"), int):
            errors.append(f"{client_turn_id}:TurnPlan 演绎轮次不是整数")
    return CheckResult(
        check_id="response_registration",
        passed=not errors,
        detail="Director 决策与归一化 TurnPlan 均已登记"
        if not errors
        else "；".join(errors),
    )


def _relationship_check(frames: list[StateFrame]) -> CheckResult:
    errors: list[str] = []
    for previous, current in zip(frames, frames[1:], strict=False):
        if current.interaction_impact not in {"neutral", "awkward"}:
            continue
        if current.interaction_tension > previous.interaction_tension:
            errors.append("普通互动提高了紧张度")
        if current.willingness_to_continue < previous.willingness_to_continue:
            errors.append("普通互动降低了继续交流意愿")
    return CheckResult(
        check_id="relationship_consistency",
        passed=not errors,
        detail="普通互动未造成关系退缩" if not errors else "；".join(errors),
    )


def _relationship_repair_arc_check(
    frames: list[StateFrame],
    relationship_arc: tuple[str, str] | None,
) -> CheckResult:
    if relationship_arc is None:
        return CheckResult(
            check_id="relationship_repair_arc",
            passed=True,
            detail="当前场景未声明破裂修复轨迹",
        )
    rupture_card_id, repair_card_id = relationship_arc
    rupture_index = next(
        (
            index
            for index, frame in enumerate(frames)
            if frame.card_id == rupture_card_id
        ),
        None,
    )
    repair_index = next(
        (
            index
            for index, frame in enumerate(frames)
            if frame.card_id == repair_card_id
        ),
        None,
    )
    if rupture_index is None or repair_index is None or repair_index <= rupture_index:
        return CheckResult(
            check_id="relationship_repair_arc",
            passed=False,
            detail="没有完整执行破裂与修复探针",
        )
    rupture = frames[rupture_index]
    prior_tension = (
        frames[rupture_index - 1].interaction_tension if rupture_index else 0
    )
    rupture_registered = (
        rupture.interaction_tension > prior_tension
        or rupture.repair_stage in {"window", "closed"}
    )
    repair = frames[repair_index]
    repaired = repair.interaction_impact == "repair" and any(
        frame.interaction_tension < rupture.interaction_tension
        for frame in frames[repair_index:]
    )
    passed = rupture_registered and repaired
    return CheckResult(
        check_id="relationship_repair_arc",
        passed=passed,
        detail=(
            "伤害后出现关系退缩，并在具体修复后回落"
            if passed
            else "未观察到完整的关系破裂与修复状态变化"
        ),
    )


def _event_order_check(
    frames: list[StateFrame],
    event_prerequisites: dict[str, list[str]],
) -> CheckResult:
    errors: list[str] = []
    previous: set[str] = set()
    for frame in frames:
        if len(frame.event_ids) != len(set(frame.event_ids)):
            errors.append("事件重复")
        current = set(frame.event_ids)
        if not previous.issubset(current):
            errors.append("已发生事件从状态中消失")
        positions = {event_id: index for index, event_id in enumerate(frame.event_ids)}
        for event_id in frame.event_ids:
            prerequisites = event_prerequisites.get(event_id)
            if prerequisites is None:
                errors.append(f"未知故事事件：{event_id}")
                continue
            missing = [item for item in prerequisites if item not in current]
            if missing:
                errors.append(
                    f"事件前置缺失：{event_id} <- {', '.join(missing)}"
                )
            reversed_items = [
                item
                for item in prerequisites
                if item in positions and positions[item] >= positions[event_id]
            ]
            if reversed_items:
                errors.append(
                    f"事件顺序错误：{event_id} 早于 {', '.join(reversed_items)}"
                )
        previous = current
    return CheckResult(
        check_id="event_order",
        passed=not errors,
        detail="故事事件无重复且顺序正确" if not errors else "；".join(dict.fromkeys(errors)),
    )


def _event_timing_check(
    frames: list[StateFrame],
    card_order: list[str],
    earliest_event_card_ids: dict[str, str],
) -> CheckResult:
    ranks = {card_id: index for index, card_id in enumerate(card_order)}
    errors: list[str] = []
    seen: set[str] = set()
    for frame in frames:
        newly_occurred = set(frame.event_ids) - seen
        actual_rank = ranks.get(frame.card_id or "", -1)
        for event_id in newly_occurred:
            earliest_card_id = earliest_event_card_ids.get(event_id)
            if earliest_card_id is None:
                continue
            earliest_rank = ranks.get(earliest_card_id)
            if earliest_rank is None or actual_rank < earliest_rank:
                errors.append(
                    f"{event_id} 在 {frame.card_id or 'opening'} 抢跑，"
                    f"最早允许 {earliest_card_id}"
                )
        seen.update(frame.event_ids)
    return CheckResult(
        check_id="event_timing",
        passed=not errors,
        detail="关键事件没有早于声明探针发生" if not errors else "；".join(errors),
    )


def _interaction_impact_check(
    frames: list[StateFrame],
    allowed_by_card: dict[str, list[str]],
) -> CheckResult:
    errors: list[str] = []
    for frame in frames:
        if frame.card_id is None or frame.card_id not in allowed_by_card:
            continue
        allowed = allowed_by_card[frame.card_id]
        if allowed and frame.interaction_impact not in allowed:
            errors.append(
                f"{frame.card_id}:{frame.interaction_impact or 'missing'}"
                f" 不在 {', '.join(allowed)}"
            )
    return CheckResult(
        check_id="interaction_impact_expectations",
        passed=not errors,
        detail="各探针的互动影响符合预期" if not errors else "；".join(errors),
    )


def _disclosure_pacing_check(
    frames: list[StateFrame],
    maximums_by_card: dict[str, dict[str, int]],
) -> CheckResult:
    errors: list[str] = []
    for frame in frames:
        if frame.card_id is None:
            continue
        for fact_id, maximum in maximums_by_card.get(frame.card_id, {}).items():
            depth = frame.fact_depths.get(fact_id, 0)
            if depth > maximum:
                errors.append(f"{frame.card_id}:{fact_id} {depth}>{maximum}")
    return CheckResult(
        check_id="disclosure_pacing",
        passed=not errors,
        detail="事实按探针节奏逐步披露" if not errors else "；".join(errors),
    )


_BRACKETED_ACTION = re.compile(
    r"(?:（[^（）\r\n]*）|\([^()\r\n]*\)|【[^【】\r\n]*】|\[[^\[\]\r\n]*\])"
)
_BACKEND_MARKERS = (
    "根据个案设定",
    "Director",
    "used_fact_depths",
    "action_options",
    "observed_event_ids",
)


def _spoken_text_check(
    turns: list[CapturedTurn],
    forbidden_phrases: list[str],
    *,
    forbidden_backend_markers: list[str] | None = None,
) -> CheckResult:
    backend_markers = (*_BACKEND_MARKERS, *(forbidden_backend_markers or []))
    invalid = [
        (
            turn.sequence,
            next(
                (phrase for phrase in forbidden_phrases if phrase in turn.text),
                None,
            ),
        )
        for turn in turns
        if turn.speaker == "client"
        and (
            _BRACKETED_ACTION.search(turn.text)
            or any(marker.casefold() in turn.text.casefold() for marker in backend_markers)
            or any(phrase in turn.text for phrase in forbidden_phrases)
        )
    ]
    details = [
        f"{sequence}:{phrase}" if phrase is not None else str(sequence)
        for sequence, phrase in invalid
    ]
    return CheckResult(
        check_id="spoken_text_boundary",
        passed=not invalid,
        detail=(
            "来访者台词未出现舞台说明、后台字段或个案禁用表达"
            if not invalid
            else f"异常回合：{', '.join(details)}"
        ),
    )


def _scene_check(
    actual: Literal["institution", "hotline", "online"] | None,
    expected: Literal["institution", "hotline", "online"] | None,
) -> CheckResult:
    passed = actual is not None and actual == expected
    return CheckResult(
        check_id="scene",
        passed=passed,
        detail=(
            f"会话场域为 {actual}"
            if passed
            else f"会话场域不一致：实际 {actual or '未知'}，预期 {expected or '未知'}"
        ),
    )


def _nonempty_text_check(turns: list[CapturedTurn]) -> CheckResult:
    empty_sequences = [turn.sequence for turn in turns if not turn.text.strip()]
    return CheckResult(
        check_id="nonempty_text",
        passed=not empty_sequences,
        detail=(
            "逐字稿没有空文本"
            if not empty_sequences
            else "空文本回合：" + ", ".join(map(str, empty_sequences))
        ),
    )


def _ending_status_check(evidence: RunEvidence) -> CheckResult:
    passed = evidence.final_status == "ended" and evidence.final_phase == "ended"
    return CheckResult(
        check_id="session_ended",
        passed=passed,
        detail=(
            "会话已结束且播放状态已收稳"
            if passed
            else (
                f"会话未正常收稳：status={evidence.final_status or '未知'}，"
                f"phase={evidence.final_phase}"
            )
        ),
    )


def _runtime_failures_check(
    *,
    failed_call_count: int,
    record_count: int,
    recorded_attempt_count: int,
) -> CheckResult:
    passed = failed_call_count == 0 or (
        record_count > 0 and recorded_attempt_count >= failed_call_count
    )
    return CheckResult(
        check_id="runtime_failures",
        passed=passed,
        detail=(
            "没有失败调用"
            if failed_call_count == 0
            else f"{failed_call_count} 次失败调用已有 {recorded_attempt_count} 次尝试记录"
            if passed
            else f"{failed_call_count} 次失败调用仅有 {recorded_attempt_count} 次尝试记录"
        ),
    )


def _opening_privacy_question_check(
    turns: list[CapturedTurn],
    expected_question: str | None,
) -> CheckResult:
    opening = next((turn for turn in turns if turn.speaker == "client"), None)
    passed = bool(
        opening is not None
        and expected_question
        and expected_question in opening.text
    )
    return CheckResult(
        check_id="opening_privacy_question",
        passed=passed,
        detail=(
            "开场包含当前场域固定隐私问题"
            if passed
            else "开场缺少当前场域固定隐私问题"
        ),
    )


def _online_message_shape_check(turns: list[CapturedTurn]) -> CheckResult:
    opening = next((turn for turn in turns if turn.speaker == "client"), None)
    segments = (
        [part.strip() for part in re.split(r"\n\s*\n|\n", opening.text) if part.strip()]
        if opening is not None
        else []
    )
    passed = len(segments) >= 2
    return CheckResult(
        check_id="online_message_shape",
        passed=passed,
        detail=(
            f"在线开场保留 {len(segments)} 段连续短消息"
            if passed
            else "在线开场没有可分段显示的连续短消息"
        ),
    )


_CLAUSE_BOUNDARY = re.compile(r"(?<=[，,。！？!?；;])")
_NEGATORS = (
    "从来没有",
    "从来没",
    "并不是",
    "不是",
    "并非",
    "没有",
    "从未",
    "不属于",
    "不在",
    "不关",
    "不算",
    "没",
    "不",
)
_QUESTION_MARKERS = ("是不是", "有没有", "会不会", "能不能", "要不要")
_AMBIGUOUS_NEGATION_MARKERS = (
    "不能说不是",
    "不能说并非",
    "不等于不是",
    "不是没有",
    "不是没",
    "不是不",
    "并非没有",
    "没有否认",
    "没否认",
    "并不否认",
    "不否认",
)
_META_VERBS = ("说", "问", "提", "想到", "听说", "觉得", "知道", "明白")
_REPORTED_SPEECH_MARKERS = ("你说", "你问", "刚才说", "刚才问")
_NEGATION_TO_ANCHOR_GAPS = frozenset(
    {
        "",
        "什么",
        "任何",
        "一点",
        "半点",
        "出",
        "出过",
        "出现",
        "出什么",
        "发生",
        "发生过",
        "发生什么",
    }
)


def _fact_contradiction_check(
    turns: list[CapturedTurn],
    cues_by_fact: dict[str, list[dict[str, object]]],
) -> CheckResult:
    evidence: list[dict[str, object]] = []
    missing_states: list[str] = []
    if not cues_by_fact:
        return CheckResult(
            check_id="fact_contradiction",
            passed=True,
            detail="当前案例没有配置事实矛盾检查线索",
        )

    for turn in turns:
        if turn.speaker != "client":
            continue
        if turn.fact_depths_before is None:
            missing_states.append(f"{turn.sequence}:{turn.client_turn_id}")
            continue
        clauses = [
            clause.strip()
            for clause in _CLAUSE_BOUNDARY.split(turn.text)
            if clause.strip()
        ]
        for fact_id, cues in cues_by_fact.items():
            if fact_id not in turn.fact_depths_before:
                missing_states.append(
                    f"{turn.sequence}:{turn.client_turn_id}/{fact_id}"
                )
                continue
            if turn.fact_depths_before[fact_id] != 0:
                continue
            candidates = _contradiction_candidates(clauses, cues)
            for clause_index, cue, negator, matched_terms in candidates:
                if _has_later_affirmation(clauses, clause_index, cues):
                    continue
                evidence.append(
                    {
                        "sequence": turn.sequence,
                        "client_turn_id": turn.client_turn_id,
                        "fact_id": fact_id,
                        "cue_id": str(cue.get("id", "")),
                        "negator": negator,
                        "matched_terms": matched_terms,
                        "excerpt": clauses[clause_index],
                    }
                )

    passed = not evidence and not missing_states
    details: list[str] = []
    if evidence:
        details.append(
            "检测到未披露正向事实的明确否认："
            + "，".join(
                f"{item['sequence']}/{item['fact_id']}/{item['cue_id']}"
                for item in evidence
            )
        )
    if missing_states:
        details.append(
            "来访者台词缺少生成前事实状态："
            + "，".join(dict.fromkeys(missing_states))
        )
    if not details:
        details.append("未发现未披露正向事实的明确否认")
    return CheckResult(
        check_id="fact_contradiction",
        passed=passed,
        severity="warning" if evidence and not missing_states else "error",
        detail="；".join(details),
        evidence=evidence,
    )


def _contradiction_candidates(
    clauses: list[str],
    cues: list[dict[str, object]],
) -> list[tuple[int, dict[str, object], str, list[str]]]:
    candidates: list[tuple[int, dict[str, object], str, list[str]]] = []
    for clause_index, clause in enumerate(clauses):
        if _is_ambiguous_clause(clause):
            continue
        for cue in cues:
            matches = _matched_anchor_terms(clause, cue)
            if matches is None:
                continue
            negator = _scoped_negator(clause, matches)
            if negator is not None:
                candidates.append(
                    (
                        clause_index,
                        cue,
                        negator,
                        [match[0] for match in matches],
                    )
                )
    return candidates


def _matched_anchor_terms(
    clause: str,
    cue: dict[str, object],
) -> list[tuple[str, int, int]] | None:
    raw_groups = cue.get("anchor_groups")
    if not isinstance(raw_groups, list):
        return None
    matches: list[tuple[str, int, int]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, list):
            return None
        alternatives = sorted(
            (term for term in raw_group if isinstance(term, str)),
            key=len,
            reverse=True,
        )
        found = [
            (term, clause.find(term), clause.find(term) + len(term))
            for term in alternatives
            if term in clause
        ]
        if not found:
            return None
        matches.append(min(found, key=lambda item: item[1]))
    return sorted(matches, key=lambda item: item[1])


def _scoped_negator(
    clause: str,
    matches: list[tuple[str, int, int]],
) -> str | None:
    first_start = matches[0][1]
    last_end = matches[-1][2]
    relevant = clause[:last_end]
    occurrences: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for negator in _NEGATORS:
        for match in re.finditer(re.escape(negator), relevant):
            span = (match.start(), match.end())
            if any(start <= span[0] and span[1] <= end for start, end in occupied):
                continue
            if negator == "不是" and match.start() > 0:
                if relevant[match.start() - 1] == "是":
                    continue
            occupied.append(span)
            occurrences.append((span[0], span[1], negator))
    for start, end, negator in sorted(occurrences, reverse=True):
        next_anchor_start = next(
            (anchor_start for _, anchor_start, _ in matches if anchor_start >= end),
            None,
        )
        if next_anchor_start is None:
            continue
        gap = clause[end:next_anchor_start]
        if gap not in _NEGATION_TO_ANCHOR_GAPS:
            continue
        if any(verb in gap for verb in _META_VERBS):
            continue
        if negator == "不" and gap:
            continue
        if end <= first_start or first_start <= start <= last_end:
            return negator
    return None


def _is_ambiguous_clause(clause: str) -> bool:
    if "？" in clause or "?" in clause or clause.rstrip("。！!；;").endswith("吗"):
        return True
    if any(marker in clause for marker in _QUESTION_MARKERS):
        return True
    if any(marker in clause for marker in _AMBIGUOUS_NEGATION_MARKERS):
        return True
    return any(marker in clause for marker in _REPORTED_SPEECH_MARKERS)


def _has_later_affirmation(
    clauses: list[str],
    clause_index: int,
    cues: list[dict[str, object]],
) -> bool:
    for clause in clauses[clause_index + 1 :]:
        if _is_ambiguous_clause(clause):
            continue
        for cue in cues:
            matches = _matched_anchor_terms(clause, cue)
            if matches is not None and _scoped_negator(clause, matches) is None:
                return True
    return False


def _sensitive_fact_check(
    frames: list[StateFrame],
    *,
    harmful_from_card_id: str | None,
    protected_fact_ids: list[str],
) -> CheckResult:
    if harmful_from_card_id is None:
        return CheckResult(
            check_id="sensitive_facts_after_harm",
            passed=True,
            detail="当前场景未声明伤害区段",
        )
    previous_depths: dict[str, int] = {}
    baseline: dict[str, int] | None = None
    errors: list[str] = []
    for frame in frames:
        if frame.card_id == harmful_from_card_id and baseline is None:
            baseline = dict(previous_depths)
        if baseline is not None:
            for fact_id in protected_fact_ids:
                if frame.fact_depths.get(fact_id, 0) > baseline.get(fact_id, 0):
                    errors.append(fact_id)
        previous_depths = frame.fact_depths
    if baseline is None:
        errors.append(f"未执行伤害起点 {harmful_from_card_id}")
    return CheckResult(
        check_id="sensitive_facts_after_harm",
        passed=not errors,
        detail=(
            "伤害区段没有获得新增敏感披露"
            if not errors
            else "伤害后新增或无法核对的敏感事实：" + ", ".join(dict.fromkeys(errors))
        ),
    )


def _content_audio_check(evidence: RunEvidence) -> CheckResult:
    passed = evidence.binary_chunk_count == 0 and not any(
        turn.audio_available for turn in evidence.db_transcript
    )
    return CheckResult(
        check_id="content_has_no_audio",
        passed=passed,
        detail="内容档未产生音频" if passed else "内容档出现音频",
    )


def _voice_audio_check(evidence: RunEvidence) -> CheckResult:
    client_turns = [turn for turn in evidence.db_transcript if turn.speaker == "client"]
    missing_audio = [
        turn.client_turn_id for turn in client_turns if not turn.audio_available
    ]
    passed = (
        bool(client_turns)
        and not missing_audio
        and evidence.binary_chunk_count > 0
        and evidence.final_phase in {"listening", "ended"}
    )
    return CheckResult(
        check_id="voice_audio",
        passed=passed,
        detail=(
            "每个来访者回合均收到并保存音频，播放状态已收稳"
            if passed
            else (
                "缺少来访者回合音频：" + ", ".join(missing_audio)
                if missing_audio
                else "语音档音频或播放状态不完整"
            )
        ),
    )


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []

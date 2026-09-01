from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict

from app.reports.competency_rubric import (
    get_rubric,
    iter_core_rubrics,
    iter_module_rubrics,
    iter_rubrics,
)
from app.reports.scoring_domain import (
    CoreDimension,
    CoreRubric,
    ModuleRubric,
    SpecialModule,
    Target,
)

RUBRIC_PATH = Path(__file__).parents[3] / "docs" / "热线心理支持职业胜任力测评量规.md"
UNIT_HEADING = re.compile(r"^## (C[1-9]|S1[ab]|S[2-8]) (.+)$")


class ParsedUnit(TypedDict):
    name: str
    measures: str
    indicators: list[tuple[str, str]]
    excluded: list[str]
    evidence_note: str
    anchors: dict[int, str]
    conditional_in_level3: list[str]


def _section_lines(body: list[str], heading: str) -> list[str]:
    start = body.index(heading) + 1
    end = next(
        (index for index in range(start, len(body)) if body[index].startswith("### ")),
        len(body),
    )
    return body[start:end]


def _paragraph(lines: list[str]) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append("".join(current))
            current = []
    if current:
        paragraphs.append("".join(current))
    return "\n".join(paragraphs)


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] in {"编号", "指标", "等级"}:
            continue
        rows.append(cells)
    return rows


def _parse_rubric() -> tuple[dict[str, ParsedUnit], dict[str, str]]:
    lines = RUBRIC_PATH.read_text(encoding="utf-8").splitlines()
    headings = [
        (index, match.group(1), match.group(2))
        for index, line in enumerate(lines)
        if (match := UNIT_HEADING.match(line))
    ]
    units: dict[str, ParsedUnit] = {}
    for position, (start, unit_id, name) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        body = lines[start + 1 : end]
        measure_heading = "### 测量内容" if unit_id.startswith("C") else "### 启用与测量内容"
        excluded_heading = "### 不在本维度评价" if unit_id.startswith("C") else "### 不在本模块评价"
        indicator_rows = _table_rows(_section_lines(body, "### 行为指标"))
        anchor_rows = _table_rows(_section_lines(body, "### 等级锚点"))
        conditional = []
        for line in body:
            match = re.match(r"3级中的“([^”]+)”", line)
            if match:
                conditional.append(match.group(1))
        units[unit_id] = {
            "name": name,
            "measures": _paragraph(_section_lines(body, measure_heading)),
            "indicators": [(row[0], row[1]) for row in indicator_rows],
            "excluded": [
                line.removeprefix("- ")
                for line in _section_lines(body, excluded_heading)
                if line.startswith("- ")
            ],
            "evidence_note": _paragraph(_section_lines(body, "### 主要证据")),
            "anchors": {int(row[0]): row[1] for row in anchor_rows},
            "conditional_in_level3": conditional,
        }

    activation_heading = lines.index("### 情景专项模块")
    activation_end = next(
        index
        for index in range(activation_heading + 1, len(lines))
        if lines[index].startswith("## ")
    )
    activation_rows = _table_rows(lines[activation_heading + 1 : activation_end])
    activations = {row[0]: row[2] for row in activation_rows}
    return units, activations


def test_static_rubric_matches_authoritative_markdown_item_by_item() -> None:
    parsed, activations = _parse_rubric()
    constants: dict[Target, CoreRubric | ModuleRubric] = {}
    core_rubrics = iter_core_rubrics()
    module_rubrics = iter_module_rubrics()
    for core_target, core_rubric in core_rubrics:
        constants[core_target] = core_rubric
    for module_target, module_rubric in module_rubrics:
        constants[module_target] = module_rubric

    assert len(core_rubrics) == 9
    assert len(module_rubrics) == 9
    assert set(parsed) == {target.value for target in constants}

    for target, rubric in constants.items():
        expected = parsed[target.value]
        assert rubric.id == target
        assert rubric.name == expected["name"]
        assert rubric.measures == expected["measures"]
        assert [(item.name, item.observation) for item in rubric.indicators] == expected[
            "indicators"
        ]
        assert rubric.excluded == expected["excluded"]
        assert rubric.evidence_note == expected["evidence_note"]
        assert rubric.anchors == expected["anchors"]
        assert rubric.conditional_in_level3 == expected["conditional_in_level3"]
        assert set(rubric.anchors) == {0, 1, 2, 3, 4}
        assert len({item.id for item in rubric.indicators}) == len(rubric.indicators)
        assert all(item.id.startswith(f"{target.value}.") for item in rubric.indicators)

        if isinstance(rubric, ModuleRubric):
            assert rubric.activation == activations[target.value]


def test_indicator_ids_are_exact_and_rubric_gets_cannot_pollute_constants() -> None:
    expected_ids = {
        "C1": ["C1.respect", "C1.equal_stance", "C1.autonomy", "C1.rupture_detection", "C1.repair"],
        "C2": [
            "C2.content_tracking",
            "C2.emotion_recognition",
            "C2.situated_understanding",
            "C2.verification",
            "C2.ambivalence",
        ],
        "C3": [
            "C3.call_reason",
            "C3.current_need",
            "C3.prioritization",
            "C3.shared_focus",
            "C3.focus_adjustment",
        ],
        "C4": [
            "C4.relevance",
            "C4.integration",
            "C4.evidence_boundary",
            "C4.judgment",
            "C4.hypothesis_revision",
        ],
        "C5": [
            "C5.fit",
            "C5.resources",
            "C5.timing",
            "C5.shared_choice",
            "C5.feedback_adjustment",
            "C5.action_layers",
        ],
        "C6": [
            "C6.clarity",
            "C6.turn_space",
            "C6.cue_adaptation",
            "C6.interruption_handling",
            "C6.structure",
            "C6.time_use",
        ],
        "C7": [
            "C7.role_scope",
            "C7.privacy",
            "C7.relationship_boundary",
            "C7.informed_participation",
            "C7.competence_scope",
            "C7.integrity",
        ],
        "C8": [
            "C8.timing",
            "C8.notice",
            "C8.review",
            "C8.status_action",
            "C8.continuity",
            "C8.caller_ending",
        ],
        "C9": [
            "C9.fact_accuracy",
            "C9.source_distinction",
            "C9.traceability",
            "C9.action_state",
            "C9.limitations",
            "C9.professional_language",
        ],
        "S1a": [
            "S1a.screening_scope",
            "S1a.wording",
            "S1a.timing",
            "S1a.followup",
            "S1a.denial_handling",
        ],
        "S1b": [
            "S1b.cue_recognition",
            "S1b.direct_question",
            "S1b.urgency",
            "S1b.risk_protection",
            "S1b.appraisal",
            "S1b.limitations",
        ],
        "S2": [
            "S2.connection",
            "S2.real_world_safety",
            "S2.reduce_access",
            "S2.transparent_collaboration",
            "S2.escalation",
            "S2.verification",
        ],
        "S3": [
            "S3.state_detection",
            "S3.load_reduction",
            "S3.silence_tolerance",
            "S3.stabilization",
            "S3.work_recovery",
            "S3.safety_attention",
        ],
        "S4": [
            "S4.experience_response",
            "S4.impact",
            "S4.communication",
            "S4.service_judgment",
            "S4.referral",
        ],
        "S5": [
            "S5.pattern",
            "S5.boundary",
            "S5.dependency",
            "S5.continuity",
            "S5.alternatives",
            "S5.relationship_pressure",
        ],
        "S6": [
            "S6.behavior_detection",
            "S6.stable_response",
            "S6.minimum_conditions",
            "S6.adjustment",
            "S6.closure_record",
        ],
        "S7": [
            "S7.identity_purpose",
            "S7.evidence_boundary",
            "S7.safety",
            "S7.actionable_focus",
            "S7.information_boundary",
            "S7.help_path",
        ],
        "S8": [
            "S8.development_fit",
            "S8.necessary_facts",
            "S8.current_safety",
            "S8.confidentiality_protection",
            "S8.role_responsibility",
            "S8.protection_resources",
        ],
    }
    constants: dict[Target, CoreRubric | ModuleRubric] = {}
    for core_target, core_rubric in iter_core_rubrics():
        constants[core_target] = core_rubric
    for module_target, module_rubric in iter_module_rubrics():
        constants[module_target] = module_rubric
    assert {
        target.value: [indicator.id for indicator in rubric.indicators]
        for target, rubric in constants.items()
    } == expected_ids

    first = get_rubric(CoreDimension.respectful_communication)
    first.indicators.append(first.indicators[0].model_copy(update={"id": "polluted"}))
    first.anchors[0] = "被外部污染"
    second = get_rubric(CoreDimension.respectful_communication)

    assert all(indicator.id != "polluted" for indicator in second.indicators)
    assert second.anchors[0] != "被外部污染"

    iterated = dict(iter_rubrics())
    iterated[CoreDimension.respectful_communication].excluded.append("外部污染")
    assert "外部污染" not in get_rubric(CoreDimension.respectful_communication).excluded


def test_core_and_module_ids_remain_distinct_types() -> None:
    assert list(CoreDimension) == [
        CoreDimension.respectful_communication,
        CoreDimension.listening_and_emotion,
        CoreDimension.concern_clarification,
        CoreDimension.integration_and_judgment,
        CoreDimension.supportive_intervention,
        CoreDimension.voice_and_process,
        CoreDimension.boundary_and_ethics,
        CoreDimension.closure_and_followup,
        CoreDimension.documentation,
    ]
    assert list(SpecialModule) == [
        SpecialModule.basic_risk_screening,
        SpecialModule.full_risk_appraisal,
        SpecialModule.safety_response,
        SpecialModule.emotional_dysregulation,
        SpecialModule.psychotic_experience,
        SpecialModule.dependency_and_boundary,
        SpecialModule.aggression_and_harassment,
        SpecialModule.third_party_call,
        SpecialModule.minor_protection,
    ]
    assert set(CoreDimension).isdisjoint(set(SpecialModule))


def test_only_basic_risk_screening_is_enabled_by_default() -> None:
    enabled = [target for target, rubric in iter_module_rubrics() if rubric.default_enabled]

    assert enabled == [SpecialModule.basic_risk_screening]


def test_online_rubrics_are_scene_neutral_and_c6_uses_text_anchors() -> None:
    online_targets = (
        CoreDimension.voice_and_process,
        CoreDimension.boundary_and_ethics,
        CoreDimension.closure_and_followup,
        SpecialModule.basic_risk_screening,
    )
    online_rubrics = [get_rubric(target, media="text") for target in online_targets]
    serialized = str([rubric.model_dump(mode="json") for rubric in online_rubrics])

    assert online_rubrics[0].name == "文字表达与互动过程管理"
    assert "短消息" in online_rubrics[0].measures
    assert not any(term in serialized for term in ("来电", "通话", "接线员", "声音"))

    voice = get_rubric(CoreDimension.voice_and_process, media="voice")
    assert voice.name == "语音沟通与会谈过程管理"
    assert "停顿" in voice.measures

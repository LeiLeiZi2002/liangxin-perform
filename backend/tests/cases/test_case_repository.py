import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.cases.domain import ConversationStage
from app.cases.loader import CaseLoadError, CaseNotFoundError, CaseRepository
from app.reports.scoring_domain import CoreDimension, SpecialModule
from app.sessions.models import CaseType, Scene


def test_default_repository_loads_published_main_and_short_case_packages() -> None:
    repository = CaseRepository()

    main = repository.get("crisis_student_main")
    short = repository.get("boundary_referral_short")

    assert main.case.title == "明早她就到了"
    assert main.case.status.value == "published"
    assert main.case.case_type is CaseType.main
    assert main.case.estimated_duration_minutes == 25
    assert main.case.supported_scenes == {Scene.hotline}
    assert short.case.title == "只想继续找你"
    assert short.case.status.value == "published"
    assert short.case.case_type is CaseType.short
    assert short.case.supported_scenes == {
        Scene.institution,
        Scene.hotline,
        Scene.online,
    }


def test_bundled_measurements_use_current_scoring_targets() -> None:
    repository = CaseRepository()
    main = repository.get("crisis_student_main")
    short = repository.get("boundary_referral_short")

    main_targets = {item.target for item in main.measurement.scoring_opportunities}
    short_targets = {item.target for item in short.measurement.scoring_opportunities}

    assert SpecialModule.full_risk_appraisal in main_targets
    assert SpecialModule.safety_response in main_targets
    assert SpecialModule.emotional_dysregulation not in main_targets
    assert SpecialModule.dependency_and_boundary in short_targets
    assert short_targets <= set(CoreDimension) | {
        SpecialModule.basic_risk_screening,
        SpecialModule.dependency_and_boundary,
    }


def test_short_boundary_case_declares_s5_as_required_opportunity() -> None:
    package = CaseRepository().get("boundary_referral_short")
    opportunity = next(
        item
        for item in package.measurement.scoring_opportunities
        if item.target is SpecialModule.dependency_and_boundary
    )

    assert opportunity.kind.value == "required"
    assert opportunity.required_fact_depths == {"boundary_request": 1}
    assert "S5.dependency" in opportunity.indicator_ids
    assert "S5.relationship_pressure" in opportunity.indicator_ids


def test_short_boundary_case_declares_closure_from_termination_material() -> None:
    package = CaseRepository().get("boundary_referral_short")
    opportunity = next(
        item
        for item in package.measurement.scoring_opportunities
        if item.target is CoreDimension.closure_and_followup
    )

    assert opportunity.id == "short_closure"
    assert opportunity.kind.value == "required"
    assert opportunity.source.value == "termination"
    assert set(opportunity.scenes) == {
        Scene.institution,
        Scene.hotline,
        Scene.online,
    }
    assert {
        "C8.timing",
        "C8.review",
        "C8.status_action",
        "C8.continuity",
        "C8.caller_ending",
    } <= set(opportunity.indicator_ids)


def test_short_boundary_core_opportunities_link_to_relevant_case_disclosures() -> None:
    package = CaseRepository().get("boundary_referral_short")
    linked_by_target = {
        item.target: item.linked_fact_ids
        for item in package.measurement.scoring_opportunities
        if isinstance(item.target, CoreDimension)
    }

    assert linked_by_target == {
        CoreDimension.respectful_communication: [
            "boundary_request",
            "boundary_reaction",
        ],
        CoreDimension.listening_and_emotion: ["boundary_reaction"],
        CoreDimension.concern_clarification: [
            "scope_exceeded_need",
            "minimum_risk_screen",
        ],
        CoreDimension.integration_and_judgment: [
            "scope_exceeded_need",
            "boundary_request",
        ],
        CoreDimension.supportive_intervention: [
            "referral_concern",
            "acceptable_referral",
            "transition_support",
        ],
        CoreDimension.boundary_and_ethics: [
            "boundary_request",
            "acceptable_referral",
        ],
        CoreDimension.closure_and_followup: [],
        CoreDimension.documentation: [],
    }


def test_mingzao_case_preserves_world_facts_and_risk_boundaries() -> None:
    package = CaseRepository().get("crisis_student_main")
    case = package.case
    facts = {fact.id: fact for fact in case.facts}
    unknowns = {unknown.id: unknown for unknown in case.unknowns}

    assert case.person.identity.name == "沈雯"
    assert case.person.identity.age == 29
    assert case.person.identity.current_employment == "失业"
    assert case.scenes[Scene.hotline].current_time == "凌晨1:43"
    assert case.scenes[Scene.hotline].location["full_address"] == (
        "江州市北岭区长宁路127号，原纺织厂宿舍2栋4单元502室"
    )
    assert case.scenes[Scene.hotline].mother_arrival == {
        "station_time": "上午9:03",
        "expected_home_time": "上午9:30至9:40",
        "has_key": False,
    }
    assert any(event.when == "四十一天前" for event in case.timeline)
    assert "下午2:30" in facts["protective_factors"].content
    assert "近十天" in facts["suicidal_ideation"].content
    assert "今晚" in facts["suicidal_ideation"].content
    assert "心理截止点" in facts["timing_intent"].content
    assert "没有确定具体方法、地点或行动步骤" in facts["plan_specificity"].content
    assert "没有准备" in facts["means_access"].content
    assert "没有开始实施" in facts["timing_intent"].content
    assert "约二十岁" in facts["prior_history"].content
    assert facts["past_self_harm_intent"].actor_knowledge == "uncertain"
    assert unknowns["past_self_harm_intent"].actor_knowledge == "uncertain"
    assert (
        unknowns["past_self_harm_intent"].actor_knowledge
        == facts["past_self_harm_intent"].actor_knowledge
    )
    assert "没有饮酒" in facts["substance_use"].content
    assert "回答总体连贯" in facts["current_mental_state"].content
    assert "独自" in facts["current_alone"].content
    assert "门" in facts["current_alone"].content
    assert "手机" in facts["current_alone"].content


def test_mingzao_positive_facts_all_declare_structured_contradiction_cues() -> None:
    package = CaseRepository().get("crisis_student_main")
    positive_facts = [
        fact for fact in package.case.facts if fact.kind == "positive_fact"
    ]

    assert positive_facts
    assert all(fact.contradiction_cues for fact in positive_facts)
    assert all(
        cue.anchor_groups
        and all(group and all(term.strip() for term in group) for group in cue.anchor_groups)
        for fact in positive_facts
        for cue in fact.contradiction_cues
    )
    assert {
        cue.id
        for cue in next(
            fact for fact in positive_facts if fact.id == "job_loss"
        ).contradiction_cues
    } >= {"deny_work_problem", "deny_job_loss"}
    assert "contradiction_cues" not in json.dumps(
        package.case.model_dump(mode="json"),
        ensure_ascii=False,
    )


def test_repository_rejects_empty_or_duplicate_contradiction_cue_anchors(
    tmp_path: Path,
) -> None:
    empty_group = _payloads()
    empty_group["case"]["facts"][0]["contradiction_cues"] = [
        {
            "id": "deny_fact",
            "label": "否认事实",
            "anchor_groups": [[]],
        }
    ]
    _write_package(tmp_path, empty_group)
    with pytest.raises(CaseLoadError, match="contradiction cue anchor group"):
        CaseRepository(tmp_path)

    duplicate_root = tmp_path / "duplicate"
    duplicate_root.mkdir()
    duplicate = _payloads()
    duplicate["case"]["facts"][0]["contradiction_cues"] = [
        {
            "id": "deny_fact",
            "label": "否认事实",
            "anchor_groups": [["事实", "事实"]],
        }
    ]
    _write_package(duplicate_root, duplicate)
    with pytest.raises(CaseLoadError, match="duplicate contradiction cue anchor"):
        CaseRepository(duplicate_root)

    blank_root = tmp_path / "blank"
    blank_root.mkdir()
    blank = _payloads()
    blank["case"]["facts"][0]["contradiction_cues"] = [
        {
            "id": " ",
            "label": "否认事实",
            "anchor_groups": [["事实"]],
        }
    ]
    _write_package(blank_root, blank)
    with pytest.raises(CaseLoadError, match="contradiction cue id and label"):
        CaseRepository(blank_root)


def test_mingzao_case_uses_semantic_progressive_disclosure() -> None:
    package = CaseRepository().get("crisis_student_main")
    facts = {fact.id: fact for fact in package.case.facts}
    rules = {rule.fact_id: rule for rule in package.actor.disclosure_rules}

    assert all(rule.semantic_evidence_required for rule in rules.values())
    assert all(not rule.keyword_matching for rule in rules.values())
    assert rules["suicidal_ideation"].requires_direct_question is True
    assert rules["suicidal_ideation"].decisions[0].allow_depth == 1
    assert rules["suicidal_ideation"].max_depth == 2
    assert "不取消" in rules["suicidal_ideation"].decisions[0].when
    assert "确认问句" in rules["plan_specificity"].decisions[1].when
    assert "最想先说哪件事" in rules["presenting_concern"].decisions[0].when
    assert "尚未说明来电缘由" in rules["presenting_concern"].decisions[0].when
    assert "工作出了问题" in rules["job_loss"].decisions[0].when
    assert [depth.depth for depth in facts["current_location"].depths] == [1, 2, 3]
    assert package.actor.initial_state.interaction_tension == 0
    assert package.actor.initial_state.willingness_to_continue == 3
    assert package.actor.interaction_tension.direct_risk_question_increases_tension is False
    assert any(
        "明确表示跟不上或不适后" in factor and "仍重复" in factor
        for factor in package.actor.interaction_tension.escalation_factors
    )
    assert (
        "连续堆叠问题，没有回应她刚说的具体内容"
        not in package.actor.interaction_tension.escalation_factors
    )


def test_mingzao_case_contains_conditional_story_and_four_endings() -> None:
    package = CaseRepository().get("crisis_student_main")
    events = {event.id: event for event in package.case.story_events}
    routes = {route.event_id: route for route in package.actor.event_routes}
    reactions = {item.topic_id: item for item in package.actor.topic_reactions}

    assert events["first_contact_tang_ting"].result.status == "no_answer"
    assert events["second_contact_tang_ting"].prerequisite_event_ids == [
        "first_contact_tang_ting"
    ]
    assert events["second_contact_tang_ting"].result.status == "answered"
    assert "说清楚" in routes["second_contact_tang_ting"].offer_when
    assert "哭" in reactions["mother_arrival"].supportive_interaction
    assert package.actor.rupture_and_repair.generic_apology_restores is False
    assert "一次" in package.actor.rupture_and_repair.repeated_rupture_effect
    assert {route.kind for route in package.actor.ending_routes} == {
        "collaborative_close",
        "caller_tests_close",
        "rupture_hangup",
        "worker_close",
    }


@pytest.mark.parametrize(
    "case_id", ["crisis_student_main", "boundary_referral_short"]
)
def test_caller_testing_close_keeps_conversation_open(case_id: str) -> None:
    package = CaseRepository().get(case_id)
    caller_route = next(
        route
        for route in package.actor.ending_routes
        if route.kind == "caller_tests_close"
    )
    worker_route = next(
        route for route in package.actor.ending_routes if route.kind == "worker_close"
    )

    assert caller_route.ends_session is False
    assert worker_route.ends_session is True


@pytest.mark.parametrize(
    "case_id", ["crisis_student_main", "boundary_referral_short"]
)
def test_ending_routes_declare_workflow_conditions(case_id: str) -> None:
    package = CaseRepository().get(case_id)
    routes = {route.kind: route for route in package.actor.ending_routes}

    assert routes["collaborative_close"].required_stage == ConversationStage.closing
    assert routes["caller_tests_close"].ends_session is False
    assert routes["rupture_hangup"].minimum_interaction_tension == 3
    assert routes["rupture_hangup"].allowed_repair_stages == ["closed"]
    assert routes["worker_close"].ends_session is True
    assert routes["worker_close"].fallback_only is True
    assert routes["worker_close"].required_fact_ids == []
    assert routes["worker_close"].required_event_ids == []


def test_short_case_preserves_resources_for_each_service_scene() -> None:
    case = CaseRepository().get("boundary_referral_short").case

    assert case.scenes[Scene.institution].details["available_resources"] == [
        "机构督导",
        "具备相关胜任力的合作咨询师或医疗资源",
        "一次合规过渡会谈与转介资料",
    ]
    assert case.scenes[Scene.hotline].details["available_resources"] == [
        "热线班长或督导",
        "常规热线轮班支持",
        "所在地可持续服务资源",
    ]
    assert case.scenes[Scene.online].details["available_resources"] == [
        "平台督导",
        "平台内预约与转介入口",
        "所在地线下持续服务及姐姐陪同",
    ]


def test_mingzao_authored_content_is_specific_and_avoids_banned_copy() -> None:
    package = CaseRepository().get("crisis_student_main")
    actor_payload = package.actor.model_dump(mode="json")
    forbidden_phrases = set(actor_payload.pop("stable_speech")["forbidden_phrases"])
    authored_text = json.dumps(
        {
            "case": package.case.model_dump(mode="json"),
            "actor": actor_payload,
        },
        ensure_ascii=False,
    )

    assert {"把乱事排顺", "需要被接住", "最近有点撑不住", "内心崩塌"} <= forbidden_phrases
    for banned in (
        "DEMO",
        "把乱事排顺",
        "需要被接住",
        "最近有点撑不住",
        "内心崩塌",
        "唯一保护因素",
        "保证十分钟",
        "保证三十分钟",
    ):
        assert banned not in authored_text
    assert re.search(r"(?i)(?:版本|\bv)\s*\d+(?:\.\d+)*", authored_text) is None
    assert "受过职场沟通训练" not in authored_text
    assert "正常情况下能够把具体事情讲清楚" in authored_text


def test_repository_loads_directory_package(tmp_path: Path) -> None:
    _write_package(tmp_path)

    package = CaseRepository(tmp_path).get("demo")

    assert package.case.case_id == "demo"
    assert package.actor.case_id == "demo"
    assert package.measurement.case_id == "demo"
    assert package.case.supported_scenes == {Scene.hotline}
    assert package.actor.disclosure_rules[0].max_depth == 2
    assert package.case.public_entry_for(Scene.hotline) == package.case.public_entry


def test_case_character_requirement_is_explicit_and_defaults_to_legacy_fallback(
    tmp_path: Path,
) -> None:
    required_root = tmp_path / "required"
    required_root.mkdir()
    required_payloads = _payloads()
    required_payloads["case"]["character_required"] = True
    _write_package(required_root, required_payloads)

    required = CaseRepository(required_root).get("demo").case
    assert required.character_required is True

    fallback_root = tmp_path / "fallback"
    fallback_root.mkdir()
    _write_package(fallback_root)
    fallback = CaseRepository(fallback_root).get("demo").case
    assert fallback.character_required is False


def test_repository_selects_public_entry_for_each_supported_scene(
    tmp_path: Path,
) -> None:
    payloads = _payloads()
    payloads["case"]["scenes"]["online"] = {
        "scene": "online",
        "current_time": "凌晨1:43",
    }
    payloads["case"]["public_entries"] = {
        "hotline": {
            "role": "心理援助热线工作者",
            "known_information": ["匿名来电"],
            "task_boundary": ["通过语音开展工作"],
        },
        "online": {
            "role": "在线心理支持工作者",
            "known_information": ["平台用户发来消息"],
            "task_boundary": ["通过文字开展工作"],
        },
    }
    _write_package(tmp_path, payloads)

    case = CaseRepository(tmp_path).get("demo").case

    assert case.public_entry_for(Scene.hotline).role == "心理援助热线工作者"
    assert case.public_entry_for(Scene.online).role == "在线心理支持工作者"
    assert case.public_entry_for(Scene.institution) == case.public_entry
    assert case.public_entry_for(None) == case.public_entry


@pytest.mark.parametrize(
    ("scenes", "public_entries", "message"),
    [
        (
            {
                "hotline": {"scene": "hotline", "current_time": "凌晨1:43"},
                "online": {"scene": "online", "current_time": "凌晨1:43"},
            },
            {
                "hotline": {
                    "role": "热线工作者",
                    "known_information": [],
                    "task_boundary": [],
                }
            },
            "public entries missing supported scene",
        ),
        (
            {"hotline": {"scene": "hotline", "current_time": "凌晨1:43"}},
            {
                "hotline": {
                    "role": "热线工作者",
                    "known_information": [],
                    "task_boundary": [],
                },
                "online": {
                    "role": "在线工作者",
                    "known_information": [],
                    "task_boundary": [],
                },
            },
            "public entries reference unsupported scene",
        ),
    ],
)
def test_repository_rejects_public_entries_that_do_not_match_supported_scenes(
    tmp_path: Path,
    scenes: dict[str, dict[str, str]],
    public_entries: dict[str, dict[str, Any]],
    message: str,
) -> None:
    payloads = _payloads()
    payloads["case"]["scenes"] = scenes
    payloads["case"]["public_entries"] = public_entries
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match=message):
        CaseRepository(tmp_path)


@pytest.mark.parametrize("filename", ["actor", "measurement"])
def test_repository_rejects_cross_file_case_id_mismatch(
    tmp_path: Path, filename: str
) -> None:
    payloads = _payloads()
    payloads[filename]["case_id"] = "other"
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="case_id mismatch"):
        CaseRepository(tmp_path)


def test_repository_reports_package_location_for_cross_file_validation(
    tmp_path: Path,
) -> None:
    payloads = _payloads()
    payloads["actor"]["case_id"] = "other"
    _write_package(tmp_path, payloads)
    package_dir = tmp_path / "demo"

    with pytest.raises(CaseLoadError) as exc_info:
        CaseRepository(tmp_path)

    assert str(package_dir) in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValidationError)


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("facts", "duplicate fact id"),
        ("story_events", "duplicate story event id"),
        ("relationships", "duplicate relationship id"),
    ],
)
def test_repository_rejects_duplicate_case_ids(
    tmp_path: Path, section: str, message: str
) -> None:
    payloads = _payloads()
    payloads["case"][section].append(payloads["case"][section][0].copy())
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match=message):
        CaseRepository(tmp_path)


@pytest.mark.parametrize(
    ("target", "field", "value", "message"),
    [
        ("disclosure_rules", "fact_id", "missing", "actor policy references missing fact"),
        ("event_routes", "event_id", "missing", "actor policy references missing event"),
        ("event_routes", "scenes", ["online"], "actor policy references unsupported scene"),
        (
            "ending_routes",
            "required_event_ids",
            ["missing"],
            "actor policy references missing event",
        ),
    ],
)
def test_repository_rejects_invalid_actor_reference(
    tmp_path: Path, target: str, field: str, value: Any, message: str
) -> None:
    payloads = _payloads()
    payloads["actor"][target][0][field] = value
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match=message):
        CaseRepository(tmp_path)


def test_repository_rejects_actor_reaction_referencing_missing_topic(
    tmp_path: Path,
) -> None:
    payloads = _payloads()
    payloads["actor"]["topic_reactions"][0]["topic_id"] = "missing"
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="actor policy references missing topic"):
        CaseRepository(tmp_path)


def test_repository_rejects_disclosure_depth_exceeding_fact_depth(
    tmp_path: Path,
) -> None:
    payloads = _payloads()
    payloads["actor"]["disclosure_rules"][0]["decisions"][1]["allow_depth"] = 3
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="disclosure depth exceeds fact depth"):
        CaseRepository(tmp_path)


def test_repository_rejects_invalid_measurement_references(tmp_path: Path) -> None:
    payloads = _payloads()
    opportunity = payloads["measurement"]["scoring_opportunities"][0]
    opportunity["linked_fact_ids"] = ["missing"]
    opportunity["scenes"] = ["online"]
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="measurement references missing fact"):
        CaseRepository(tmp_path)


def test_measurement_requires_a_disclosure_source_for_declared_core_opportunity(
    tmp_path: Path,
) -> None:
    payloads = _payloads()
    opportunity = payloads["measurement"]["scoring_opportunities"][0]
    opportunity["linked_fact_ids"] = []
    opportunity["required_fact_depths"] = {}
    opportunity["required_event_ids"] = []
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="disclosure source"):
        CaseRepository(tmp_path)


def test_measurement_opportunity_source_defaults_and_accepts_deferred_materials(
    tmp_path: Path,
) -> None:
    payloads = _payloads()
    payloads["case"]["scenes"]["online"] = {
        "scene": "online",
        "current_time": "凌晨1:43",
    }
    runtime_opportunity = payloads["measurement"]["scoring_opportunities"][0]
    deferred_opportunities = [
        {
            **runtime_opportunity,
            "id": opportunity_id,
            "source": source,
            "scenes": scenes,
            "linked_fact_ids": [],
            "required_fact_depths": {},
            "required_event_ids": [],
        }
        for opportunity_id, source, scenes in (
            ("rapport", "transcript", ["hotline"]),
            ("close", "termination", ["online"]),
            ("record", "work_record", ["hotline", "online"]),
        )
    ]
    payloads["measurement"]["scoring_opportunities"] = [
        runtime_opportunity,
        *deferred_opportunities,
    ]
    _write_package(tmp_path, payloads)

    opportunities = CaseRepository(tmp_path).get(
        "demo"
    ).measurement.scoring_opportunities

    assert [opportunity.source.value for opportunity in opportunities] == [
        "runtime_state",
        "transcript",
        "termination",
        "work_record",
    ]
    assert opportunities[2].scenes == [Scene.online]
    assert opportunities[3].scenes == [Scene.hotline, Scene.online]


@pytest.mark.parametrize(
    ("gate", "value"),
    [
        ("linked_fact_ids", ["fact_one"]),
        ("required_fact_depths", {"fact_one": 1}),
        ("required_event_ids", ["support_connected"]),
    ],
)
def test_non_runtime_measurement_source_rejects_runtime_state_gates(
    tmp_path: Path,
    gate: str,
    value: list[str] | dict[str, int],
) -> None:
    payloads = _payloads()
    opportunity = payloads["measurement"]["scoring_opportunities"][0]
    opportunity.update(
        {
            "source": "transcript",
            "linked_fact_ids": [],
            "required_fact_depths": {},
            "required_event_ids": [],
            gate: value,
        }
    )
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="runtime state gates"):
        CaseRepository(tmp_path)


def test_repository_rejects_unknown_measurement_target_and_event(
    tmp_path: Path,
) -> None:
    unknown_target = _payloads()
    unknown_target["measurement"]["scoring_opportunities"][0]["target"] = "S99"
    _write_package(tmp_path, unknown_target)
    with pytest.raises(CaseLoadError, match="target"):
        CaseRepository(tmp_path)

    event_root = tmp_path / "event"
    event_root.mkdir()
    missing_event = _payloads()
    missing_event["measurement"]["scoring_opportunities"][0][
        "required_event_ids"
    ] = ["missing"]
    _write_package(event_root, missing_event)
    with pytest.raises(CaseLoadError, match="measurement references missing event"):
        CaseRepository(event_root)


def test_repository_rejects_disclosure_self_dependency(tmp_path: Path) -> None:
    payloads = _payloads()
    payloads["actor"]["disclosure_rules"][0]["prerequisite_fact_ids"] = ["fact_one"]
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="disclosure rule depends on itself"):
        CaseRepository(tmp_path)


def test_repository_rejects_disclosure_cycle(tmp_path: Path) -> None:
    payloads = _payloads()
    payloads["case"]["facts"].append(
        {
            "id": "fact_two",
            "topic": "risk",
            "kind": "positive_fact",
            "content": "第二项事实",
            "actor_knowledge": "knows",
            "depths": [{"depth": 1, "content": "第二项事实"}],
        }
    )
    payloads["actor"]["disclosure_rules"][0]["prerequisite_fact_ids"] = ["fact_two"]
    payloads["actor"]["disclosure_rules"].append(
        {
            "fact_id": "fact_two",
            "decisions": [{"when": "追问", "allow_depth": 1}],
            "prerequisite_fact_ids": ["fact_one"],
        }
    )
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="disclosure prerequisite cycle"):
        CaseRepository(tmp_path)


def test_repository_rejects_story_event_missing_prerequisite(tmp_path: Path) -> None:
    payloads = _payloads()
    payloads["case"]["story_events"][0]["prerequisite_event_ids"] = ["missing"]
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="story event references missing prerequisite"):
        CaseRepository(tmp_path)


def test_repository_rejects_action_route_for_deferred_external_event(
    tmp_path: Path,
) -> None:
    payloads = _payloads()
    payloads["case"]["story_events"].append(
        {
            "id": "friend_at_door",
            "prerequisite_event_ids": ["support_connected"],
            "deferred_after": {
                "after_event_id": "support_connected",
                "min_intervening_actor_turns": 1,
            },
            "result": {
                "status": "arrived",
                "actor_observation": "门外响起敲门声。",
            },
        }
    )
    payloads["actor"]["event_routes"][0]["event_id"] = "friend_at_door"
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="deferred story event cannot"):
        CaseRepository(tmp_path)


def test_repository_rejects_action_fact_gate_beyond_defined_depth(
    tmp_path: Path,
) -> None:
    payloads = _payloads()
    payloads["actor"]["event_routes"][0]["required_fact_depths"] = {
        "fact_one": 3
    }
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="disclosure depth exceeds fact depth"):
        CaseRepository(tmp_path)


def test_repository_rejects_missing_stage_reference_and_wrong_order(
    tmp_path: Path,
) -> None:
    missing = _payloads()
    missing["actor"]["stage_rules"][0]["any_fact_ids"] = ["missing"]
    _write_package(tmp_path, missing)
    with pytest.raises(CaseLoadError, match="actor policy references missing fact"):
        CaseRepository(tmp_path)

    ordered_root = tmp_path / "ordered"
    ordered_root.mkdir()
    wrong_order = _payloads()
    wrong_order["actor"]["stage_rules"][0:2] = reversed(
        wrong_order["actor"]["stage_rules"][0:2]
    )
    _write_package(ordered_root, wrong_order)
    with pytest.raises(CaseLoadError, match="stage rules must follow case flow"):
        CaseRepository(ordered_root)


def test_repository_rejects_no_supported_scene(tmp_path: Path) -> None:
    payloads = _payloads()
    payloads["case"]["scenes"] = {}
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError, match="case must support at least one scene"):
        CaseRepository(tmp_path)


def test_repository_returns_deep_copies_and_filters(tmp_path: Path) -> None:
    _write_package(tmp_path, _payloads(case_id="z_main"), case_id="z_main")
    _write_package(
        tmp_path,
        _payloads(case_id="a_short", case_type="short", scene="online"),
        case_id="a_short",
    )
    _write_package(
        tmp_path,
        _payloads(case_id="draft", status="draft", scene="online"),
        case_id="draft",
    )
    repository = CaseRepository(tmp_path)

    changed = repository.get("z_main")
    changed.case.title = "changed"
    changed.actor.stable_speech.speech_patterns.append("changed")
    assert repository.get("z_main").case.title == "临时案例"
    assert "changed" not in repository.get("z_main").actor.stable_speech.speech_patterns
    assert [item.case.case_id for item in repository.list_published()] == ["a_short", "z_main"]
    assert [
        item.case.case_id
        for item in repository.list_published(scene=Scene.online, case_type=CaseType.short)
    ] == ["a_short"]


def test_repository_ignores_flat_json_and_requires_three_files(tmp_path: Path) -> None:
    (tmp_path / "legacy.json").write_text("{}", encoding="utf-8")
    repository = CaseRepository(tmp_path)
    with pytest.raises(CaseNotFoundError):
        repository.get("legacy")

    _write_package(tmp_path)
    (tmp_path / "demo" / "measurement.json").unlink()
    with pytest.raises(FileNotFoundError, match="measurement.json"):
        CaseRepository(tmp_path)


def test_repository_reports_file_location_for_invalid_json(tmp_path: Path) -> None:
    _write_package(tmp_path)
    (tmp_path / "demo" / "actor.json").write_text("{", encoding="utf-8")

    with pytest.raises(CaseLoadError) as exc_info:
        CaseRepository(tmp_path)

    assert "demo" in str(exc_info.value)
    assert "actor.json" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


def test_repository_reports_file_location_for_validation_error(tmp_path: Path) -> None:
    payloads = _payloads()
    del payloads["case"]["title"]
    _write_package(tmp_path, payloads)

    with pytest.raises(CaseLoadError) as exc_info:
        CaseRepository(tmp_path)

    assert "demo" in str(exc_info.value)
    assert "case.json" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValidationError)


def _write_package(
    root: Path,
    payloads: dict[str, dict[str, Any]] | None = None,
    *,
    case_id: str = "demo",
) -> None:
    package_dir = root / case_id
    package_dir.mkdir()
    for name, payload in (payloads or _payloads()).items():
        (package_dir / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def _payloads(
    *,
    case_id: str = "demo",
    status: str = "published",
    case_type: str = "main",
    scene: str = "hotline",
) -> dict[str, dict[str, Any]]:
    middle_stage = "risk_assessment" if case_type == "main" else "boundary_challenge"
    stage_rules = [
        {"stage": "exploration", "any_fact_ids": ["fact_one"]},
        {"stage": middle_stage, "any_fact_ids": ["fact_one"]},
        {"stage": "planning", "required_event_ids": ["support_connected"]},
        {
            "stage": "closing",
            "required_fact_depths": {"fact_one": 1},
            "required_event_ids": ["support_connected"],
        },
    ]
    return {
        "case": {
            "case_id": case_id,
            "status": status,
            "title": "临时案例",
            "case_type": case_type,
            "estimated_duration_minutes": 20,
            "public_entry": {
                "role": "热线工作者",
                "known_information": ["匿名来电"],
                "task_boundary": ["自然通话"],
            },
            "person": {
                "identity": {"name": "沈雯", "age": 29, "gender": "女"},
                "stable_tendencies": ["先讲具体情况"],
                "call_context": {
                    "voluntary_call": True,
                    "initial_willingness": "谨慎但愿意交流",
                    "immediate_need": "不想独处",
                },
            },
            "scenes": {scene: {"scene": scene, "current_time": "凌晨1:43"}},
            "timeline": [{"id": "call", "when": "现在", "happened": "拨打热线"}],
            "relationships": [
                {"id": "friend", "name": "唐婷", "relationship": "朋友"}
            ],
            "facts": [
                {
                    "id": "fact_one",
                    "topic": "risk",
                    "kind": "positive_fact",
                    "content": "近十天反复出现死亡想法",
                    "actor_knowledge": "knows",
                    "depths": [
                        {"depth": 1, "content": "出现过想法"},
                        {"depth": 2, "content": "持续近十天"},
                    ],
                }
            ],
            "topic_experiences": [
                {"topic_id": "risk", "surface_experience": "难以启齿"}
            ],
            "story_events": [
                {
                    "id": "support_connected",
                    "result": {
                        "status": "connected",
                        "actor_observation": "朋友已经接通电话并确认会过来。",
                    },
                }
            ],
            "unknowns": [
                {
                    "id": "reason",
                    "when_asked": "受测者询问人物并不知道的原因",
                    "actor_knowledge": "unknown",
                    "known_boundary": "不知道",
                    "improvisation_allowed": False,
                }
            ],
        },
        "actor": {
            "case_id": case_id,
            "stable_speech": {
                "language": "普通话",
                "baseline_style": "能讲清具体事情",
                "speech_patterns": ["难堪时停顿"],
                "scene_guidance": {scene: "遵循媒介边界"},
            },
            "opening": {"worker_starts": True, "silence_seconds": 5},
            "initial_state": {
                "interaction_tension": 1,
                "willingness_to_continue": 3,
                "emotional_activation": 3,
                "speech_organization": 3,
            },
            "disclosure_rules": [
                {
                    "fact_id": "fact_one",
                    "decisions": [
                        {"when": "直接询问", "allow_depth": 1},
                        {"when": "继续澄清", "allow_depth": 2},
                    ],
                }
            ],
            "interaction_tension": {
                "levels": {"0": "放松", "1": "警惕", "2": "紧张", "3": "准备退出"}
            },
            "topic_reactions": [
                {"topic_id": "risk", "expressions": ["先停顿"]}
            ],
            "rupture_and_repair": {
                "rupture_stages": ["discomfort", "withdrawn", "ready_to_leave"],
                "repair_requirements": ["指出具体问题", "改变做法"],
            },
            "event_routes": [
                {
                    "id": "contact_friend",
                    "event_id": "support_connected",
                    "offer_when": "工作者邀请来访者联系朋友。",
                    "decision_guidance": "建议具体且尊重决定权时愿意联系。",
                    "required_fact_depths": {"fact_one": 1},
                    "scenes": [scene],
                }
            ],
            "stage_rules": stage_rules,
            "ending_routes": [
                {
                    "id": "collaborative_close",
                    "kind": "collaborative_close",
                    "condition": "支持已落实",
                    "actor_behavior": "确认安排",
                    "required_fact_ids": ["fact_one"],
                    "required_event_ids": ["support_connected"],
                    "scenes": [scene],
                }
            ],
            "improvisation_boundary": {
                "locked_content": ["风险事实"],
                "allowed_content": ["普通生活细节"],
                "unknown_response": "说不知道",
            },
        },
        "measurement": {
            "case_id": case_id,
            "scoring_opportunities": [
                {
                    "id": "communication",
                    "target": "C1",
                    "kind": "required",
                    "description": "观察沟通",
                    "evidence_targets": ["回应具体内容"],
                    "indicator_ids": ["C1.respect"],
                    "complex_opportunity": False,
                    "linked_fact_ids": ["fact_one"],
                    "required_fact_depths": {},
                    "required_event_ids": [],
                    "observable_behaviors": ["回应原话"],
                    "concerning_behaviors": ["轻描淡写"],
                    "scenes": [scene],
                }
            ],
        },
    }

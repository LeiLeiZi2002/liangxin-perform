"""轻链的条件机会从公开会谈确认，不向轻链补写旧披露账。"""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.cases.loader import CaseRepository
from app.reports.job_inputs import CodingInput, OpportunityCheckInput
from app.reports.report_pipeline import check_opportunities
from app.reports.scoring_domain import CoreDimension, SpecialModule
from app.runtime.character_kernel import CharacterPromptKernel
from app.runtime.character_world import (
    SupportWorldStage,
    SupportWorldState,
    store_support_world,
)

RISK = "full_risk_appraisal_after_cue"
SAFETY = "immediate_safety_response"
SETBACK = "safety_plan_after_support_setback"


def inputs(case_id="crisis_student_main"):
    now = datetime(2026, 9, 8, tzinfo=UTC)
    turns = [
        ("w1", "worker", "你刚才说不想等到天亮，能再说说吗？"),
        ("c1", "client", "我这两天想过伤害自己。"),
        ("w2", "worker", "你担心今晚会发生什么？"),
        ("c2", "client", "我怕自己今晚一个人待着会出事。"),
        ("w3", "worker", "我们一起看看现在谁能陪你。"),
        ("c3", "client", "我刚给朋友发了消息，她还没回。"),
    ]
    coding = CodingInput.model_validate(
        {
            "session": {
                "session_id": "public-opportunity-test",
                "mode": "assessment",
                "scene": "hotline",
                "case_type": "short" if case_id == "boundary_referral_short" else "main",
                "case_id": case_id,
                "media": "voice",
                "status": "ended",
                "model_mode": "live",
                "soft_duration_minutes": None,
                "created_at": now,
                "ended_at": now,
                "end_reason": "user_ended",
            },
            "turns": [
                {
                    "turn_id": turn_id,
                    "sequence": index,
                    "speaker": speaker,
                    "text": text,
                    "created_at": now,
                }
                for index, (turn_id, speaker, text) in enumerate(turns, 1)
            ],
            "work_record": None,
            "technical_interruptions": [],
            "termination": {
                "status": "ended",
                "ended_at": now,
                "end_reason": "user_ended",
            },
        }
    )
    light = CharacterPromptKernel._lightweight_turn_state()
    state = store_support_world(light, SupportWorldState(stage=SupportWorldStage.first_unanswered))
    opportunity = OpportunityCheckInput.model_validate(
        {
            "session_id": coding.session.session_id,
            "session_state": state,
            "turn_states": [
                {
                    "turn_id": "c3",
                    "state_before_json": light,
                    "state_after_json": light,
                    "signals_json": {
                        "runtime_engine": "character_prompt",
                        "action_request": "send_first_support_message",
                        "world_stage_before": "not_contacted",
                        "world_stage_after": "first_unanswered",
                    },
                    "used_fact_ids": [],
                }
            ],
            "case_package": CaseRepository().get(case_id).model_dump(mode="json"),
        }
    )
    return coding, opportunity


def observations(*, risk="present", safety="present", setback="present"):
    from app.reports.report_provider import OpportunityObservation

    return [
        OpportunityObservation(
            opportunity_id=opportunity_id,
            status=status,
            reason="根据来访者实际表达及已保存的行动判断，不评价工作者做得好坏。",
            refs=[
                {"kind": "dialogue", "turn_id": turn_id, "quote": quote} for turn_id, quote in refs
            ],
            action_turn_ids=["c3"] if opportunity_id == SETBACK and status == "present" else [],
        )
        for opportunity_id, status, refs in [
            (RISK, risk, [("c1", "我这两天想过伤害自己。")]),
            (
                SAFETY,
                safety,
                [
                    ("c1", "我这两天想过伤害自己。"),
                    ("c2", "我怕自己今晚一个人待着会出事。"),
                ],
            ),
            (
                SETBACK,
                setback,
                [("c1", "我这两天想过伤害自己。"), ("c3", "我刚给朋友发了消息，她还没回。")],
            ),
        ]
    ]


def test_character_conditions_are_pending_public_observation_not_legacy_gates():
    coding, opportunity = inputs()
    result = check_opportunities(coding, opportunity)

    assert {item.opportunity_id for item in result.conditional_opportunities} == {
        RISK,
        SAFETY,
        SETBACK,
    }
    assert result.activated_modules == [SpecialModule.basic_risk_screening]
    assert result.action_observations[0].turn_id == "c3"
    assert result.action_observations[0].world_stage_after == "first_unanswered"


def test_new_main_case_condition_declarations_do_not_require_hidden_state():
    package = CaseRepository().get("crisis_student_main")
    conditions = [
        item
        for item in package.measurement.scoring_opportunities
        if item.kind.value == "conditional"
    ]

    assert len(conditions) == 3
    assert all(item.source.value == "transcript" for item in conditions)
    assert all(
        not item.linked_fact_ids and not item.required_fact_depths and not item.required_event_ids
        for item in conditions
    )


def test_verified_public_conditions_activate_safety_without_legacy_fields():
    coding, opportunity = inputs()
    result = check_opportunities(coding, opportunity, observations=observations())

    assert result.activated_modules == [
        SpecialModule.basic_risk_screening,
        SpecialModule.full_risk_appraisal,
        SpecialModule.safety_response,
    ]
    assert all(item.fulfilled for item in result.outcomes[SpecialModule.safety_response])
    assert any(
        item.fulfilled and item.complex_opportunity
        for item in result.outcomes[SpecialModule.safety_response]
    )
    assert "actor_state" not in opportunity.session_state


@pytest.mark.parametrize("setback", ["absent", "uncertain"])
def test_immediate_safety_does_not_automatically_fulfill_support_setback(setback):
    coding, opportunity = inputs()
    result = check_opportunities(coding, opportunity, observations=observations(setback=setback))

    immediate, support_setback = result.outcomes[SpecialModule.safety_response]
    assert immediate.fulfilled is True
    assert support_setback.fulfilled is False
    assert not any(
        item.fulfilled and item.complex_opportunity
        for item in result.outcomes[SpecialModule.safety_response]
    )


def test_uncertain_and_absent_have_distinct_inactive_reasons():
    coding, opportunity = inputs()
    absent = check_opportunities(
        coding,
        opportunity,
        observations=observations(risk="absent", safety="absent", setback="absent"),
    )
    uncertain = check_opportunities(
        coding,
        opportunity,
        observations=observations(risk="uncertain", safety="uncertain", setback="uncertain"),
    )
    assert "无法确认" in dict(uncertain.inactive_modules)[SpecialModule.full_risk_appraisal]
    assert "未出现" in dict(absent.inactive_modules)[SpecialModule.full_risk_appraisal]


@pytest.mark.parametrize(
    "bad_kind",
    ["missing", "duplicate", "unknown", "worker_only", "fabricated_quote", "unknown_action"],
)
def test_invalid_observation_is_an_analysis_error_not_no_opportunity(bad_kind):
    from app.reports.scoring_domain import DialogueRef

    coding, opportunity = inputs()
    decisions = observations()
    if bad_kind == "missing":
        decisions.pop()
    elif bad_kind == "duplicate":
        decisions.append(decisions[0])
    elif bad_kind == "unknown":
        decisions[0] = decisions[0].model_copy(update={"opportunity_id": "not-in-this-case"})
    elif bad_kind == "worker_only":
        decisions[0] = decisions[0].model_copy(
            update={
                "refs": [DialogueRef(kind="dialogue", turn_id="w1", quote=coding.turns[0].text)]
            }
        )
    elif bad_kind == "fabricated_quote":
        decisions[0] = decisions[0].model_copy(
            update={"refs": [DialogueRef(kind="dialogue", turn_id="c1", quote="原话中没有这句话")]}
        )
    elif bad_kind == "unknown_action":
        decisions[-1] = decisions[-1].model_copy(update={"action_turn_ids": ["w3"]})

    with pytest.raises(ValueError):
        check_opportunities(coding, opportunity, observations=decisions)


def test_frozen_lightweight_case_with_old_declarations_uses_public_observations():
    coding, opportunity = inputs()
    package = deepcopy(opportunity.case_package)
    for declaration in package["measurement"]["scoring_opportunities"]:
        if declaration["kind"] == "conditional":
            declaration.update(
                source="runtime_state",
                linked_fact_ids=["minimum_risk_cue"],
                required_fact_depths={"minimum_risk_cue": 1},
            )
    opportunity = opportunity.model_copy(update={"case_package": package})
    result = check_opportunities(coding, opportunity, observations=observations())
    assert SpecialModule.full_risk_appraisal in result.activated_modules


def test_legacy_workflow_keeps_its_own_state_gate():
    coding, opportunity = inputs()
    package = deepcopy(opportunity.case_package)
    for declaration in package["measurement"]["scoring_opportunities"]:
        if declaration["kind"] == "conditional":
            declaration.update(
                source="runtime_state",
                linked_fact_ids=["minimum_risk_cue"],
                required_fact_depths={"minimum_risk_cue": 1},
                required_event_ids=[],
            )
    opportunity = opportunity.model_copy(
        update={
            "case_package": package,
            "session_state": {
                "actor_state": {"fact_states": {"minimum_risk_cue": {"disclosed_depth": 1}}}
            },
            "turn_states": [],
        }
    )
    result = check_opportunities(coding, opportunity)
    assert result.conditional_opportunities == []
    assert SpecialModule.full_risk_appraisal in result.activated_modules


@pytest.mark.parametrize("case_id", ["boundary_referral_short", "marriage_boundary_main"])
def test_other_case_required_opportunities_are_unchanged(case_id):
    coding, opportunity = inputs(case_id)
    result = check_opportunities(coding, opportunity, observations=[])
    assert result.conditional_opportunities == []
    assert all(
        item.fulfilled
        for target, items in result.outcomes.items()
        if target is not CoreDimension.documentation
        for item in items
    )


def test_world_final_state_alone_does_not_invent_a_public_action():
    coding, opportunity = inputs()
    opportunity = opportunity.model_copy(update={"turn_states": []})
    result = check_opportunities(coding, opportunity)
    assert result.action_observations == []


def test_reported_support_setback_can_use_client_quotes_without_inventing_program_action():
    coding, opportunity = inputs()
    opportunity = opportunity.model_copy(update={"turn_states": []})
    decisions = [
        item.model_copy(update={"action_turn_ids": []}) for item in observations()
    ]

    result = check_opportunities(coding, opportunity, observations=decisions)

    assert result.outcomes[SpecialModule.safety_response][-1].fulfilled
    assert result.action_observations == []

from typing import Literal

import pytest

from app.simulations.checks import (
    CapturedTurn,
    CheckResult,
    RunEvidence,
    StateFrame,
    run_automatic_checks,
)


def test_check_result_defaults_to_blocking_error_severity() -> None:
    result = CheckResult(check_id="example", passed=False, detail="示例失败")

    assert result.severity == "error"


def _turns() -> list[CapturedTurn]:
    return [
        CapturedTurn(
            sequence=1,
            client_turn_id="turn-1",
            speaker="worker",
            text="你现在是一个人吗？",
            signals={
                "director_decision": {
                    "interaction": "neutral",
                    "directives": [
                        {
                            "kind": "disclose",
                            "fact_depths": {"current_alone": 1},
                        }
                    ],
                },
                "turn_plan": {
                    "worker_turn_id": "turn-1",
                    "interaction": "neutral",
                    "directives": [
                        {
                            "kind": "disclose",
                            "fact_depths": {"current_alone": 1},
                        }
                    ],
                    "allowed_fact_depths": {"current_alone": 1},
                    "resolved_actions": [],
                    "due_observations": [],
                    "projected_relationship": {
                        "interaction_tension": 0,
                        "willingness_to_continue": 3,
                        "repair_stage": "none",
                    },
                    "legal_ending": None,
                    "diagnostics": [],
                    "actor_turn_index": 1,
                },
            },
        ),
        CapturedTurn(
            sequence=2,
            client_turn_id="turn-1",
            speaker="client",
            text="嗯，就我一个人。",
            signals={},
        ),
    ]


def test_automatic_checks_accept_consistent_content_run() -> None:
    turns = _turns()
    evidence = RunEvidence(
        state_frames=[
            StateFrame(
                fact_depths={"current_alone": 0},
                event_ids=[],
                interaction_tension=0,
                willingness_to_continue=3,
            ),
            StateFrame(
                fact_depths={"current_alone": 1},
                event_ids=["first_contact_tang_ting"],
                interaction_tension=0,
                willingness_to_continue=3,
                interaction_impact="neutral",
            ),
        ],
        db_transcript=turns,
        rest_transcript=turns,
        ws_transcript=turns,
        binary_chunk_count=0,
        final_phase="ended",
    )

    results = run_automatic_checks(
        evidence,
        profile="content",
        max_fact_depths={"current_alone": 1},
        event_prerequisites={"first_contact_tang_ting": []},
        allowed_interaction_impacts_by_card={
            "N1": ["neutral", "supportive", "awkward"]
        },
    )

    assert results
    assert all(result.passed for result in results)


@pytest.mark.parametrize(
    ("profile", "binary_chunk_count", "audio_available", "audio_check_id"),
    [
        ("content", 0, False, "content_has_no_audio"),
        ("voice", 1, True, "voice_audio"),
    ],
)
def test_character_prompt_checks_only_generic_black_box_contracts(
    profile: Literal["content", "voice"],
    binary_chunk_count: int,
    audio_available: bool,
    audio_check_id: str,
) -> None:
    turns = [
        CapturedTurn(
            sequence=1,
            client_turn_id="character-turn-1",
            speaker="worker",
            text="你愿意从今晚最难受的事情说起吗？",
        ),
        CapturedTurn(
            sequence=2,
            client_turn_id="character-turn-1",
            speaker="client",
            text="我妈明早就到了，可我一直没告诉她工作已经没了。",
            audio_available=audio_available,
        ),
    ]
    evidence = RunEvidence(
        state_frames=[
            StateFrame(
                fact_depths={"legacy_fact": 2},
                interaction_tension=0,
                willingness_to_continue=3,
            ),
            StateFrame(
                fact_depths={"legacy_fact": 0},
                interaction_tension=3,
                willingness_to_continue=0,
                interaction_impact="neutral",
            ),
        ],
        db_transcript=turns,
        rest_transcript=turns,
        ws_transcript=turns,
        binary_chunk_count=binary_chunk_count,
        final_phase="ended",
    )

    results = run_automatic_checks(
        evidence,
        profile=profile,
        runtime_engine="character_prompt",
        max_fact_depths={"legacy_fact": 1},
        event_prerequisites={},
        allowed_interaction_impacts_by_card={},
        fact_contradiction_cues={
            "legacy_fact": [
                {
                    "id": "legacy_denial",
                    "label": "旧事实否认",
                    "anchor_groups": [["工作"]],
                }
            ]
        },
    )

    assert [result.check_id for result in results] == [
        "transcript_consistency",
        "turn_pairing",
        "spoken_text_boundary",
        audio_check_id,
    ]
    assert all(result.passed for result in results)


def test_objective_character_checks_cover_only_deterministic_contracts() -> None:
    privacy_question = "这些聊天以后谁能看到？"
    turns = [
        CapturedTurn(
            sequence=1,
            client_turn_id="opening-online",
            speaker="client",
            text=f"你好，我想问个事\n\n是我老公的\n\n{privacy_question}",
        ),
        CapturedTurn(
            sequence=2,
            client_turn_id="turn-1",
            speaker="worker",
            text="我会先说明这里怎样保存聊天，再听你最担心的事。",
        ),
        CapturedTurn(
            sequence=3,
            client_turn_id="turn-1",
            speaker="client",
            text="好，那我接着说。",
        ),
    ]
    evidence = RunEvidence(
        db_transcript=turns,
        rest_transcript=[],
        ws_transcript=[],
        binary_chunk_count=0,
        final_phase="ended",
        scene="online",
        final_status="ended",
        runtime_failure_count=0,
        failed_model_call_count=0,
    )

    results = run_automatic_checks(
        evidence,
        profile="content",
        runtime_engine="character_prompt",
        objective_contracts=True,
        expected_scene="online",
        expected_privacy_question=privacy_question,
        max_fact_depths={},
        event_prerequisites={},
        allowed_interaction_impacts_by_card={},
        forbidden_phrases=["关系越界已经证实"],
        forbidden_backend_markers=["评分标准"],
    )

    assert [result.check_id for result in results] == [
        "turn_pairing",
        "scene",
        "nonempty_text",
        "spoken_text_boundary",
        "content_has_no_audio",
        "session_ended",
        "runtime_failures",
        "opening_privacy_question",
        "online_message_shape",
    ]
    assert all(result.passed for result in results)
    assert not any(
        token in result.check_id
        for result in results
        for token in ("natural", "semantic", "emotion", "story")
    )


def test_objective_character_checks_report_each_observable_failure() -> None:
    turns = [
        CapturedTurn(
            sequence=1,
            client_turn_id="opening-hotline",
            speaker="client",
            text="（叹气）根据个案设定，我感到羞耻",
            audio_available=False,
        ),
        CapturedTurn(
            sequence=2,
            client_turn_id="orphan-worker",
            speaker="worker",
            text="",
        ),
    ]
    evidence = RunEvidence(
        db_transcript=turns,
        binary_chunk_count=0,
        final_phase="technical_paused",
        scene="online",
        final_status="active",
        runtime_failure_count=0,
        failed_model_call_count=1,
    )

    results = run_automatic_checks(
        evidence,
        profile="voice",
        runtime_engine="character_prompt",
        objective_contracts=True,
        expected_scene="hotline",
        expected_privacy_question="你们这边会录音吗，会不会联系我老公？",
        max_fact_depths={},
        event_prerequisites={},
        allowed_interaction_impacts_by_card={},
        forbidden_phrases=["我感到羞耻"],
        forbidden_backend_markers=["根据个案设定"],
    )
    by_id = {result.check_id: result for result in results}

    assert all(
        not by_id[check_id].passed
        for check_id in (
            "turn_pairing",
            "scene",
            "nonempty_text",
            "spoken_text_boundary",
            "voice_audio",
            "session_ended",
            "runtime_failures",
            "opening_privacy_question",
        )
    )


def test_objective_failure_check_accepts_a_failed_call_with_a_saved_record() -> None:
    evidence = RunEvidence(
        final_phase="ended",
        scene="online",
        final_status="ended",
        runtime_failure_count=1,
        runtime_failure_attempt_count=1,
        failed_model_call_count=1,
    )

    results = run_automatic_checks(
        evidence,
        profile="content",
        runtime_engine="character_prompt",
        objective_contracts=True,
        expected_scene="online",
        max_fact_depths={},
        event_prerequisites={},
        allowed_interaction_impacts_by_card={},
    )

    failure_check = next(
        result for result in results if result.check_id == "runtime_failures"
    )
    assert failure_check.passed is True
    assert "已有 1 次尝试记录" in failure_check.detail


def test_objective_failure_check_rejects_fewer_recorded_attempts_than_failures() -> None:
    evidence = RunEvidence(
        final_phase="ended",
        scene="online",
        final_status="ended",
        runtime_failure_count=1,
        runtime_failure_attempt_count=1,
        failed_model_call_count=2,
    )

    results = run_automatic_checks(
        evidence,
        profile="content",
        runtime_engine="character_prompt",
        objective_contracts=True,
        expected_scene="online",
        max_fact_depths={},
        event_prerequisites={},
        allowed_interaction_impacts_by_card={},
    )

    failure_check = next(
        result for result in results if result.check_id == "runtime_failures"
    )
    assert failure_check.passed is False
    assert "2 次失败调用仅有 1 次尝试记录" in failure_check.detail


def test_turn_pairing_rejects_duplicate_worker_turns_for_one_response() -> None:
    turns = [
        CapturedTurn(
            sequence=1,
            client_turn_id="opening-online",
            speaker="client",
            text="你好\n\n这些聊天以后谁能看到？",
        ),
        CapturedTurn(
            sequence=2,
            client_turn_id="turn-1",
            speaker="worker",
            text="我先回应。",
        ),
        CapturedTurn(
            sequence=3,
            client_turn_id="turn-1",
            speaker="worker",
            text="我又提交了一次。",
        ),
        CapturedTurn(
            sequence=4,
            client_turn_id="turn-1",
            speaker="client",
            text="我只回应一次。",
        ),
    ]

    results = run_automatic_checks(
        RunEvidence(
            db_transcript=turns,
            final_phase="ended",
            final_status="ended",
            scene="online",
        ),
        profile="content",
        runtime_engine="character_prompt",
        objective_contracts=True,
        expected_scene="online",
        expected_privacy_question="这些聊天以后谁能看到？",
        max_fact_depths={},
        event_prerequisites={},
        allowed_interaction_impacts_by_card={},
    )

    pairing = next(result for result in results if result.check_id == "turn_pairing")
    assert pairing.passed is False
    assert "受测者回合 2 条" in pairing.detail


def test_transcript_consistency_includes_client_turn_id() -> None:
    turns = _turns()
    wrong_pairing = [
        turns[0],
        turns[1].model_copy(update={"client_turn_id": "turn-retry"}),
    ]
    evidence = RunEvidence(
        db_transcript=turns,
        rest_transcript=wrong_pairing,
        ws_transcript=turns,
        final_phase="ended",
    )

    results = run_automatic_checks(
        evidence,
        profile="content",
        max_fact_depths={},
        event_prerequisites={},
        allowed_interaction_impacts_by_card={},
    )

    consistency = next(
        result for result in results if result.check_id == "transcript_consistency"
    )
    assert consistency.passed is False
    assert "REST" in consistency.detail
    assert "DB 2 条，REST 2 条，WebSocket 2 条" in consistency.detail


def test_automatic_checks_report_state_protocol_and_content_failures() -> None:
    worker, client = _turns()
    broken_worker = worker.model_copy(
        update={"signals": {"director_decision": worker.signals["director_decision"]}}
    )
    broken_client = client.model_copy(
        update={
            "text": "（叹气）根据个案设定，我一个人。",
            "signals": {
                "handled_response_item_ids": [],
                "action_responses": [],
            },
            "audio_available": True,
        }
    )
    evidence = RunEvidence(
        state_frames=[
            StateFrame(
                fact_depths={"current_alone": 1},
                event_ids=["second_contact_tang_ting"],
                interaction_tension=0,
                willingness_to_continue=3,
            ),
            StateFrame(
                fact_depths={"current_alone": 0},
                event_ids=[
                    "second_contact_tang_ting",
                    "first_contact_tang_ting",
                    "first_contact_tang_ting",
                ],
                interaction_tension=1,
                willingness_to_continue=2,
                interaction_impact="neutral",
            ),
        ],
        db_transcript=[broken_worker, broken_client],
        rest_transcript=[worker, client],
        ws_transcript=[broken_worker, broken_client],
        binary_chunk_count=1,
        final_phase="playing",
    )

    failed = {
        result.check_id
        for result in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={"current_alone": 1},
            event_prerequisites={
                "first_contact_tang_ting": [],
                "second_contact_tang_ting": ["first_contact_tang_ting"],
            },
            allowed_interaction_impacts_by_card={},
        )
        if not result.passed
    }

    assert failed == {
        "transcript_consistency",
        "fact_depths",
        "response_registration",
        "relationship_consistency",
        "event_order",
        "spoken_text_boundary",
        "content_has_no_audio",
    }


def test_voice_check_requires_audio_and_a_settled_phase() -> None:
    evidence = RunEvidence(
        state_frames=[],
        db_transcript=[],
        rest_transcript=[],
        ws_transcript=[],
        binary_chunk_count=0,
        final_phase="playing",
    )

    results = run_automatic_checks(
        evidence,
        profile="voice",
        max_fact_depths={},
        event_prerequisites={},
        allowed_interaction_impacts_by_card={},
    )

    assert next(item for item in results if item.check_id == "voice_audio").passed is False


def test_voice_check_rejects_a_client_turn_without_persisted_audio() -> None:
    turns = _turns()
    evidence = RunEvidence(
        db_transcript=turns,
        rest_transcript=turns,
        ws_transcript=turns,
        binary_chunk_count=1,
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="voice",
            max_fact_depths={},
            event_prerequisites={},
            allowed_interaction_impacts_by_card={},
        )
        if item.check_id == "voice_audio"
    )

    assert result.passed is False
    assert "来访者回合" in result.detail


def test_registration_rejects_a_partial_turn_plan() -> None:
    worker, client = _turns()
    partial_plan = {
        "interaction": "neutral",
        "directives": [],
        "allowed_fact_depths": {},
        "diagnostics": [],
    }
    worker = worker.model_copy(
        update={
            "signals": {
                "director_decision": worker.signals["director_decision"],
                "turn_plan": partial_plan,
            }
        }
    )
    evidence = RunEvidence(
        db_transcript=[worker, client],
        rest_transcript=[worker, client],
        ws_transcript=[worker, client],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={},
            event_prerequisites={},
            allowed_interaction_impacts_by_card={},
        )
        if item.check_id == "response_registration"
    )

    assert result.passed is False
    assert "TurnPlan 字段缺失" in result.detail


def test_harmful_suite_flags_new_sensitive_disclosure_after_harm_starts() -> None:
    evidence = RunEvidence(
        state_frames=[
            StateFrame(
                card_id="H2",
                fact_depths={"suicidal_ideation": 0},
                event_ids=[],
            ),
            StateFrame(
                card_id="H3",
                fact_depths={"suicidal_ideation": 1},
                event_ids=[],
                interaction_impact="harmful",
            ),
        ],
        db_transcript=[],
        rest_transcript=[],
        ws_transcript=[],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={"suicidal_ideation": 2},
            event_prerequisites={},
            allowed_interaction_impacts_by_card={},
            harmful_from_card_id="H3",
            protected_fact_ids=["suicidal_ideation"],
        )
        if item.check_id == "sensitive_facts_after_harm"
    )

    assert result.passed is False


def test_independent_story_event_does_not_require_linear_prefix() -> None:
    evidence = RunEvidence(
        state_frames=[
            StateFrame(event_ids=["crying_disorganization"]),
        ],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={},
            event_prerequisites={
                "first_contact_tang_ting": [],
                "crying_disorganization": [],
                "second_contact_tang_ting": ["first_contact_tang_ting"],
            },
            allowed_interaction_impacts_by_card={},
        )
        if item.check_id == "event_order"
    )

    assert result.passed is True


def test_story_event_reports_missing_explicit_prerequisite() -> None:
    evidence = RunEvidence(
        state_frames=[
            StateFrame(event_ids=["second_contact_tang_ting"]),
        ],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={},
            event_prerequisites={
                "first_contact_tang_ting": [],
                "second_contact_tang_ting": ["first_contact_tang_ting"],
            },
            allowed_interaction_impacts_by_card={},
        )
        if item.check_id == "event_order"
    )

    assert result.passed is False
    assert "first_contact_tang_ting" in result.detail


def test_normal_probe_misclassified_as_harmful_fails_explicit_expectation() -> None:
    evidence = RunEvidence(
        state_frames=[
            StateFrame(
                card_id="N1",
                interaction_impact="harmful",
                interaction_tension=1,
            ),
        ],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={},
            event_prerequisites={},
            allowed_interaction_impacts_by_card={
                "N1": ["neutral", "supportive", "awkward"]
            },
        )
        if item.check_id == "interaction_impact_expectations"
    )

    assert result.passed is False
    assert "N1" in result.detail


def test_early_sensitive_disclosure_fails_probe_pacing_contract() -> None:
    evidence = RunEvidence(
        state_frames=[
            StateFrame(
                card_id="N1",
                fact_depths={"presenting_concern": 1, "suicidal_ideation": 1},
            ),
        ],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={"presenting_concern": 2, "suicidal_ideation": 2},
            event_prerequisites={},
            allowed_interaction_impacts_by_card={},
            maximum_fact_depths_after_by_card={
                "N1": {"presenting_concern": 1, "suicidal_ideation": 0}
            },
        )
        if item.check_id == "disclosure_pacing"
    )

    assert result.passed is False
    assert "suicidal_ideation" in result.detail


def test_case_forbidden_phrase_fails_spoken_text_boundary() -> None:
    actor_turn = CapturedTurn(
        sequence=1,
        client_turn_id="actor-1",
        speaker="client",
        text="我最近有点撑不住。",
    )
    evidence = RunEvidence(
        db_transcript=[actor_turn],
        rest_transcript=[actor_turn],
        ws_transcript=[actor_turn],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={},
            event_prerequisites={},
            allowed_interaction_impacts_by_card={},
            forbidden_phrases=["最近有点撑不住"],
        )
        if item.check_id == "spoken_text_boundary"
    )

    assert result.passed is False
    assert "最近有点撑不住" in result.detail


_JOB_LOSS_CUES = [
    {
        "id": "deny_work_problem",
        "label": "否认工作发生变故",
        "anchor_groups": [["工作", "上班"], ["问题", "出问题", "变动"]],
    },
    {
        "id": "deny_work_relation",
        "label": "否认当前困境与工作有关",
        "anchor_groups": [["工作", "上班"], ["关系", "相关", "原因"]],
    },
    {
        "id": "deny_job_loss",
        "label": "否认失业或被裁",
        "anchor_groups": [["失业", "被裁", "辞退"]],
    },
]


def _fact_contradiction_result(
    text: str,
    *,
    depth: int = 0,
    speaker: str = "client",
    state_available: bool = True,
    fact_id: str = "job_loss",
    cues: list[dict[str, object]] | None = None,
):
    turn = CapturedTurn(
        sequence=5,
        client_turn_id="sim-chaotic-C2",
        speaker=speaker,
        text=text,
        fact_depths_before={fact_id: depth} if state_available else None,
    )
    evidence = RunEvidence(
        db_transcript=[turn],
        rest_transcript=[turn],
        ws_transcript=[turn],
        final_phase="ended",
    )
    return next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={},
            event_prerequisites={},
            allowed_interaction_impacts_by_card={},
            fact_contradiction_cues={fact_id: cues or _JOB_LOSS_CUES},
        )
        if item.check_id == "fact_contradiction"
    )


@pytest.mark.parametrize(
    "text",
    [
        "不是，不是吵架也不是工作问题。",
        "家里没吵架，工作也没出什么问题。",
        "这事跟工作没什么关系。",
        "我没有失业。",
    ],
)
def test_fact_contradiction_detects_structured_denial_paraphrases(text: str) -> None:
    result = _fact_contradiction_result(text)

    assert result.passed is False
    assert result.severity == "warning"
    assert result.evidence
    assert result.evidence[0]["sequence"] == 5
    assert result.evidence[0]["client_turn_id"] == "sim-chaotic-C2"
    assert result.evidence[0]["fact_id"] == "job_loss"
    assert result.evidence[0]["cue_id"] in {
        "deny_work_problem",
        "deny_work_relation",
        "deny_job_loss",
    }
    assert result.evidence[0]["negator"]
    assert result.evidence[0]["matched_terms"]
    assert result.evidence[0]["excerpt"]


@pytest.mark.parametrize(
    "text",
    [
        "我已经没有工作了，公司不做江州这边的业务，我被裁了。",
        "公司不做江州业务。",
        "不是我主动辞职，是公司裁的。",
        "你问的是不是工作问题？",
        "也不能说不是工作问题。",
        "我没有否认工作有问题。",
        "我没说工作有问题。",
        "工作不是没出问题，只是我刚才不知道怎么讲。",
        "不是工作问题，我确实被裁了。",
    ],
)
def test_fact_contradiction_avoids_true_disclosure_questions_and_corrections(
    text: str,
) -> None:
    result = _fact_contradiction_result(text)

    assert result.passed is True
    assert result.evidence == []


@pytest.mark.parametrize(
    ("text", "fact_id", "cues"),
    [
        (
            "我没钱才借钱。",
            "borrowed_money",
            [
                {
                    "id": "deny_borrowing",
                    "label": "否认向他人借钱",
                    "anchor_groups": [["借钱", "借过钱", "向唐婷借"]],
                }
            ],
        ),
        ("工作没保住确实出了问题。", "job_loss", _JOB_LOSS_CUES),
        (
            "我没离开北岭区。",
            "current_location",
            [
                {
                    "id": "deny_current_location",
                    "label": "否认位于北岭区",
                    "anchor_groups": [["北岭区", "旧客运站", "纺织厂宿舍"]],
                }
            ],
        ),
    ],
)
def test_fact_contradiction_does_not_treat_natural_predicate_negation_as_denial(
    text: str,
    fact_id: str,
    cues: list[dict[str, object]],
) -> None:
    result = _fact_contradiction_result(text, fact_id=fact_id, cues=cues)

    assert result.passed is True
    assert result.evidence == []


def test_fact_contradiction_does_not_mix_facts_that_share_a_topic() -> None:
    result = _fact_contradiction_result("我没有具体计划。")

    assert result.passed is True


def test_fact_contradiction_only_checks_client_before_first_disclosure() -> None:
    assert _fact_contradiction_result(
        "不是工作问题。", depth=1
    ).passed is True
    assert _fact_contradiction_result(
        "不是工作问题。", speaker="worker"
    ).passed is True


def test_fact_contradiction_reports_missing_database_state_without_assuming_zero() -> None:
    result = _fact_contradiction_result(
        "不是工作问题。",
        state_available=False,
    )

    assert result.passed is False
    assert result.severity == "error"
    assert result.evidence == []
    assert "缺少生成前事实状态" in result.detail


def test_repair_scene_cannot_pass_without_relationship_state_change() -> None:
    evidence = RunEvidence(
        state_frames=[
            StateFrame(card_id="R2", interaction_tension=0, repair_stage="none"),
            StateFrame(
                card_id="R3",
                interaction_tension=0,
                repair_stage="none",
                interaction_impact="harmful",
            ),
            StateFrame(
                card_id="R4",
                interaction_tension=0,
                repair_stage="none",
                interaction_impact="supportive",
            ),
        ],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={},
            event_prerequisites={},
            allowed_interaction_impacts_by_card={},
            relationship_arc=("R3", "R4"),
        )
        if item.check_id == "relationship_repair_arc"
    )

    assert result.passed is False


def test_repair_scene_cannot_pass_by_only_clearing_the_repair_stage() -> None:
    evidence = RunEvidence(
        state_frames=[
            StateFrame(card_id="R2", interaction_tension=0, repair_stage="none"),
            StateFrame(
                card_id="R3",
                interaction_tension=1,
                repair_stage="window",
                interaction_impact="harmful",
            ),
            StateFrame(
                card_id="R4",
                interaction_tension=1,
                repair_stage="none",
                interaction_impact="repair",
            ),
        ],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={},
            event_prerequisites={},
            allowed_interaction_impacts_by_card={},
            relationship_arc=("R3", "R4"),
        )
        if item.check_id == "relationship_repair_arc"
    )

    assert result.passed is False


def test_story_event_cannot_occur_before_declared_probe_card() -> None:
    evidence = RunEvidence(
        state_frames=[
            StateFrame(
                card_id="N11",
                event_ids=["first_contact_tang_ting"],
            ),
        ],
        final_phase="ended",
    )

    result = next(
        item
        for item in run_automatic_checks(
            evidence,
            profile="content",
            max_fact_depths={},
            event_prerequisites={"first_contact_tang_ting": []},
            allowed_interaction_impacts_by_card={},
            card_order=[f"N{index}" for index in range(1, 20)],
            earliest_event_card_ids={"first_contact_tang_ting": "N12"},
        )
        if item.check_id == "event_timing"
    )

    assert result.passed is False
    assert "first_contact_tang_ting" in result.detail

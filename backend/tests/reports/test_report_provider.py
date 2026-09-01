import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.reports.job_inputs import (
    CodingInput,
    CodingSessionInput,
    CodingShard,
    CodingTurnInput,
    SessionTerminationInput,
)
from app.reports.scoring_domain import CoreDimension, DialogueRef, SpecialModule, Target
from app.runtime.models import CacheMode, ModelCallKind, ModelRole, PromptFamily
from app.runtime_config import RuntimeCredentialStore
from app.sessions.models import (
    CaseType,
    Media,
    ModelMode,
    Scene,
    SessionMode,
    SessionStatus,
    TurnSpeaker,
)


class CacheRejectedError(Exception):
    status_code = 400
    body = {
        "error": {
            "param": "messages[1].content[0].cache_control",
            "code": "invalid_parameter",
            "message": "cache_control is not supported",
        }
    }


class FakeCompletions:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))],
            usage=SimpleNamespace(
                prompt_tokens=80,
                completion_tokens=20,
                total_tokens=100,
                prompt_tokens_details=SimpleNamespace(
                    cached_tokens=50,
                    cache_creation_input_tokens=10,
                ),
            ),
            id="report-completion",
            _request_id="report-request",
        )


class FakeClient:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(outcomes))


class RecordingMetrics:
    def __init__(self) -> None:
        self.metrics = []

    def record(self, metric: object) -> None:
        self.metrics.append(metric)


def _store() -> RuntimeCredentialStore:
    store = RuntimeCredentialStore()
    store.update(api_key="test-report-provider-key")
    return store


def _model_config() -> object:
    from app.reports.report_provider import ReportModelConfig

    return ReportModelConfig.model_validate(
        {
            "report_model": "qwen3.8-max",
            "sampling_parameters": {"temperature": 0.1},
        }
    )


def _coding_input(text: str) -> CodingInput:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    return CodingInput(
        session=CodingSessionInput(
            session_id="session-report",
            mode=SessionMode.experience,
            scene=Scene.hotline,
            case_type=CaseType.main,
            case_id="crisis_student_main",
            media=Media.voice,
            status=SessionStatus.ended,
            model_mode=ModelMode.live,
            soft_duration_minutes=None,
            created_at=now,
            ended_at=now,
            end_reason=None,
        ),
        turns=[
            CodingTurnInput(
                turn_id="turn-worker",
                sequence=1,
                speaker=TurnSpeaker.worker,
                text=text,
                created_at=now,
            )
        ],
        work_record=None,
        technical_interruptions=[],
        termination=SessionTerminationInput(
            status=SessionStatus.ended,
            ended_at=now,
            end_reason=None,
        ),
    )


def _coding_shard(shard_id: str, text: str) -> CodingShard:
    coding_input = _coding_input(text)
    return CodingShard(
        shard_id=shard_id,
        session=coding_input.session,
        turns=coding_input.turns,
        work_record=coding_input.work_record,
        technical_interruptions=coding_input.technical_interruptions,
        termination=coding_input.termination,
        overlap_turn_ids=[],
    )


def _local_json(shard_id: str, *, with_unit: bool = False) -> str:
    units = []
    if with_unit:
        units.append(
            {
                "id": f"{shard_id}-unit-1",
                "summary": "受测者承接了来电者的难受。",
                "initial_codes": ["情绪承接"],
                "source_role": "worker",
                "refs": [
                    {
                        "kind": "dialogue",
                        "turn_id": "turn-worker",
                        "quote": "公开冻结材料",
                    }
                ],
                "alternative_reading": None,
            }
        )
    return json.dumps({"shard_id": shard_id, "units": units}, ensure_ascii=False)


def _invalid_local_json(shard_id: str) -> str:
    return json.dumps(
        {
            "shard_id": shard_id,
            "units": [
                {
                    "id": f"{shard_id}-unit-1",
                    "summary": "缺少开放编码。",
                    "initial_codes": [],
                    "source_role": "worker",
                    "refs": [
                        {
                            "kind": "dialogue",
                            "turn_id": "turn-worker",
                            "quote": "公开冻结材料",
                        }
                    ],
                    "alternative_reading": None,
                }
            ],
        },
        ensure_ascii=False,
    )


ALL_TARGETS: tuple[Target, ...] = (*CoreDimension, *SpecialModule)


def _global_json(
    *,
    coverage_targets: tuple[Target, ...] = (),
    counter_targets: tuple[Target, ...] = ALL_TARGETS,
) -> str:
    units = []
    if coverage_targets:
        units.append(
            {
                "id": "global-unit",
                "summary": "已复核公开冻结材料，当前没有足够行为证据。",
                "refs": [
                    {
                        "kind": "dialogue",
                        "turn_id": "turn-worker",
                        "quote": "公开冻结材料",
                    }
                ],
            }
        )
    return json.dumps(
        {
            "units": units,
            "coded_evidence": [],
            "coverage_decisions": [
                {
                    "target": target.value,
                    "status": "no_reliable_material",
                    "reason": "当前材料没有可可靠编码的受测者行为。",
                }
                for target in coverage_targets
            ],
            "counter_checks": [
                {
                    "target": target.value,
                    "searched_unit_ids": [],
                    "found": [],
                    "not_found_note": "未发现反例。",
                }
                for target in counter_targets
            ],
            "bottom_line_candidates": [],
            "material_conflict_candidates": [],
            "urgent_risk_disclosure_candidates": [],
        },
        ensure_ascii=False,
    )


async def test_map_prompt_keeps_stable_contract_and_public_shards_separate() -> None:
    from app.reports.report_provider import ReportProvider

    client = FakeClient([_local_json("shard-a"), _local_json("shard-b")])
    provider = ReportProvider(_store(), client=client)

    await provider.code_shard(
        _coding_shard("shard-a", "第一份原始材料"),
        session_id="session-report",
        model_config=_model_config(),
    )
    await provider.code_shard(
        _coding_shard("shard-b", "第二份原始材料"),
        session_id="session-report",
        model_config=_model_config(),
    )

    first, second = client.chat.completions.calls
    first_messages = first["messages"]
    second_messages = second["messages"]
    assert first_messages[1] == second_messages[1]
    assert first_messages[-1] != second_messages[-1]
    assert first_messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in first_messages[-1]
    stable = json.loads(first_messages[1]["content"][0]["text"])
    assert set(stable) == {"task", "output_contract"}
    dynamic = json.loads(first_messages[-1]["content"])
    all_messages = json.dumps(first_messages, ensure_ascii=False)
    for forbidden in (
        "session_state",
        "case_package",
        "used_fact_ids",
        "opportunities",
        "target_ids",
        '"rubrics"',
    ):
        assert forbidden not in all_messages
    assert dynamic == {
        "coding_shard": _coding_shard(
            "shard-a", "第一份原始材料"
        ).model_dump(mode="json")
    }


def test_online_map_prompt_carries_real_media_without_hotline_only_language() -> None:
    from app.reports.report_provider import ReportProvider

    shard = _coding_shard("online-shard", "我们先把今晚最担心的事说清楚。")
    shard = shard.model_copy(
        update={
            "session": shard.session.model_copy(
                update={"scene": Scene.online, "media": Media.text}
            )
        }
    )

    messages = ReportProvider._map_messages(shard, use_explicit_cache=False)
    serialized = json.dumps(messages, ensure_ascii=False)
    dynamic = json.loads(messages[-1]["content"])

    assert dynamic["coding_shard"]["session"]["scene"] == "online"
    assert dynamic["coding_shard"]["session"]["media"] == "text"
    assert not any(term in serialized for term in ("来电", "通话", "接线员", "声音表现"))


def test_online_reduce_prompt_uses_text_media_rubrics() -> None:
    from app.reports.report_provider import ReportProvider

    messages = ReportProvider._reduce_messages(
        [],
        [CoreDimension.voice_and_process, CoreDimension.boundary_and_ethics],
        {},
        [],
        scene=Scene.online,
        media=Media.text,
        use_explicit_cache=False,
    )
    serialized = json.dumps(messages, ensure_ascii=False)
    dynamic = json.loads(messages[-1]["content"])

    assert "文字表达与互动过程管理" in serialized
    assert dynamic["scene"] == "online"
    assert dynamic["media"] == "text"
    assert not any(term in serialized for term in ("来电", "通话", "接线员", "声音表现"))


def test_online_group_prompt_keeps_scene_media_and_text_anchor() -> None:
    from app.reports.competency_rubric import get_rubric
    from app.reports.report_provider import ReportProvider, ScoringGroup
    from app.reports.scoring_domain import DimensionPacket

    packet = DimensionPacket(
        scene=Scene.online,
        media=Media.text,
        target=CoreDimension.voice_and_process,
        rubric=get_rubric(CoreDimension.voice_and_process, media=Media.text),
        evidence=[],
        counter_evidence=[],
        units=[],
        opportunities=[],
        conditional_unavailable=[],
        level_ceiling=0,
    )
    messages = ReportProvider._group_messages(
        ScoringGroup.interaction,
        [packet],
        use_explicit_cache=False,
    )
    serialized = json.dumps(messages, ensure_ascii=False)

    assert "文字表达与互动过程管理" in serialized
    assert '"scene":"online"' in messages[-1]["content"]
    assert '"media":"text"' in messages[-1]["content"]
    assert not any(term in serialized for term in ("来电", "通话", "接线员", "声音表现"))


async def test_report_cache_rejection_retries_once_without_marker_and_records_role() -> None:
    from app.reports.report_provider import ReportProvider

    metrics = RecordingMetrics()
    client = FakeClient([CacheRejectedError(), _local_json("shard-a")])
    provider = ReportProvider(_store(), client=client, recorder=metrics)

    await provider.code_shard(
        _coding_shard("shard-a", "只使用公开冻结材料"),
        session_id="session-report",
        model_config=_model_config(),
    )

    first, fallback = client.chat.completions.calls
    assert first["messages"][1]["content"][0]["cache_control"] == {
        "type": "ephemeral"
    }
    assert "cache_control" not in fallback["messages"][1]["content"][0]
    assert len(metrics.metrics) == 2
    assert all(metric.model_role is ModelRole.report for metric in metrics.metrics)
    assert all(metric.prompt_family is PromptFamily.report_map for metric in metrics.metrics)
    assert metrics.metrics[0].cache_mode is CacheMode.explicit
    assert metrics.metrics[1].cache_mode is CacheMode.none


async def test_report_provider_uses_frozen_model_and_temperature() -> None:
    from app.reports.report_provider import ReportModelConfig, ReportProvider

    store = _store()
    store.update(report_model="changed-live-model", report_temperature=0.95)
    client = FakeClient([_local_json("shard-a")])
    provider = ReportProvider(store, client=client)

    await provider.code_shard(
        _coding_shard("shard-a", "公开冻结材料"),
        session_id="session-report",
        model_config=ReportModelConfig.model_validate(
            {
                "report_model": "frozen-provider-model",
                "sampling_parameters": {"temperature": 0.27},
            }
        ),
    )

    call = client.chat.completions.calls[0]
    assert call["model"] == "frozen-provider-model"
    assert call["temperature"] == 0.27


async def test_map_repair_feedback_is_isolated_by_shard_id() -> None:
    from app.reports.report_provider import ReportProvider
    from app.runtime.providers import RepairableModelOutputError

    client = FakeClient(
        [
            _invalid_local_json("shard-a"),
            _local_json("shard-b"),
            _local_json("shard-a"),
        ]
    )
    provider = ReportProvider(_store(), client=client)

    with pytest.raises(RepairableModelOutputError):
        await provider.code_shard(
            _coding_shard("shard-a", "公开冻结材料"),
            session_id="session-report",
            model_config=_model_config(),
        )

    await provider.code_shard(
        _coding_shard("shard-b", "公开冻结材料"),
        session_id="session-report",
        model_config=_model_config(),
        call_kind=ModelCallKind.repair,
    )
    await provider.code_shard(
        _coding_shard("shard-a", "公开冻结材料"),
        session_id="session-report",
        model_config=_model_config(),
        call_kind=ModelCallKind.repair,
        validation_feedback="unit-a source_role 与 speaker 不一致",
    )

    other_shard_messages = client.chat.completions.calls[1]["messages"]
    assert "上一份 JSON 未通过校验" not in json.dumps(
        other_shard_messages, ensure_ascii=False
    )
    repair_messages = client.chat.completions.calls[2]["messages"]
    repair_message = repair_messages[-1]["content"]
    assert "上一份 JSON 未通过校验" in repair_message
    assert "too_short" in repair_message
    assert "at least 1 item" in repair_message
    assert "程序后置校验错误" in repair_message
    assert "source_role 与 speaker 不一致" in repair_message
    assert "请重新输出完整 JSON" in repair_message


def test_meaning_unit_source_feedback_explains_how_to_inherit_local_refs() -> None:
    from app.reports.report_provider import ReportProvider
    from app.runtime.failures import attach_failure_details
    from app.runtime.providers import RepairableModelOutputError

    error = RepairableModelOutputError("模型返回的结构不符合约定")
    attach_failure_details(
        error,
        {
            "validation": [
                {
                    "loc": ["units", 0],
                    "type": "value_error",
                    "msg": "Value error, 意义单元至少引用一种实际材料",
                }
            ]
        },
    )

    feedback = ReportProvider._format_repair_feedback(error)

    assert "MeaningUnit.turn_ids" in feedback
    assert "LocalCodedUnit.refs" in feedback
    assert "对话引用的 turn_id" in feedback


async def test_reduce_reads_only_validated_local_outputs_targets_and_rubrics() -> None:
    from app.reports.report_provider import (
        ActiveTargetBrief,
        LocalCodingOutput,
        ReportProvider,
    )

    metrics = RecordingMetrics()
    client = FakeClient(
        [_global_json(coverage_targets=(CoreDimension.supportive_intervention,))]
    )
    provider = ReportProvider(_store(), client=client, recorder=metrics)
    local_outputs = [
        LocalCodingOutput.model_validate_json(_local_json("shard-a", with_unit=True)),
        LocalCodingOutput.model_validate_json(_local_json("shard-b")),
    ]
    active_target_briefs = [
        ActiveTargetBrief(
            target=CoreDimension.supportive_intervention,
            description="共同形成现实可行的下一步。",
            evidence_targets=["询问顾虑", "依据反馈调整安排"],
            indicator_ids=["C5.shared_choice", "C5.feedback_adjustment"],
        )
    ]

    await provider.reduce_coding(
        local_outputs,
        session_id="session-report",
        model_config=_model_config(),
        targets=ALL_TARGETS,
        turn_speakers={"turn-worker": "worker"},
        scene=Scene.hotline,
        media=Media.voice,
        active_target_briefs=active_target_briefs,
    )

    messages = client.chat.completions.calls[0]["messages"]
    stable = json.loads(messages[1]["content"][0]["text"])
    dynamic = json.loads(messages[2]["content"])
    assert stable["task"] == "reduce_qualitative_coding"
    assert set(stable["rubrics"]) == {target.value for target in ALL_TARGETS}
    assert dynamic == {
        "target_ids": [target.value for target in ALL_TARGETS],
        "active_target_briefs": [
            item.model_dump(mode="json") for item in active_target_briefs
        ],
        "turn_speakers": {"turn-worker": "worker"},
        "local_outputs": [output.model_dump(mode="json") for output in local_outputs],
        "scene": "hotline",
        "media": "voice",
    }
    serialized_messages = json.dumps(messages, ensure_ascii=False)
    for forbidden in (
        "coding_input",
        "coding_shard",
        "session_state",
        "actor_state",
        "case_package",
        "opportunities",
        "used_fact_ids",
    ):
        assert forbidden not in serialized_messages
    assert len(metrics.metrics) == 1
    assert metrics.metrics[0].prompt_family is PromptFamily.report_reduce


async def test_reduce_accepts_core_dimensions_plus_only_the_activated_module() -> None:
    from app.reports.report_provider import LocalCodingOutput, ReportProvider

    targets: tuple[Target, ...] = (
        *CoreDimension,
        SpecialModule.dependency_and_boundary,
    )
    client = FakeClient([_global_json(counter_targets=targets)])
    provider = ReportProvider(_store(), client=client)
    local_outputs = [
        LocalCodingOutput(shard_id="shard-a", units=[]),
        LocalCodingOutput(shard_id="shard-b", units=[]),
    ]

    result = await provider.reduce_coding(
        local_outputs,
        session_id="session-report-subset",
        model_config=_model_config(),
        targets=targets,
        turn_speakers={"turn-worker": "worker"},
        scene=Scene.hotline,
        media=Media.voice,
    )

    assert [check.target for check in result.counter_checks] == list(targets)
    dynamic = json.loads(client.chat.completions.calls[0]["messages"][2]["content"])
    assert dynamic["target_ids"] == [target.value for target in targets]


async def test_reduce_requires_an_explicit_coverage_decision_for_each_active_target() -> None:
    from app.reports.report_provider import (
        ActiveTargetBrief,
        LocalCodingOutput,
        ReportProvider,
    )

    client = FakeClient([_global_json()])
    provider = ReportProvider(_store(), client=client)
    local_outputs = [
        LocalCodingOutput(shard_id="shard-a", units=[]),
        LocalCodingOutput(shard_id="shard-b", units=[]),
    ]

    with pytest.raises(ValueError, match="已启用观察任务"):
        await provider.reduce_coding(
            local_outputs,
            session_id="session-report",
            model_config=_model_config(),
            targets=ALL_TARGETS,
            turn_speakers={"turn-worker": "worker"},
            scene=Scene.hotline,
            media=Media.voice,
            active_target_briefs=[
                ActiveTargetBrief(
                    target=SpecialModule.dependency_and_boundary,
                    description="处理固定接线诉求形成的关系边界压力。",
                    evidence_targets=["说明边界", "形成替代安排"],
                    indicator_ids=["S5.boundary", "S5.alternatives"],
                )
            ],
        )


async def test_reduce_model_refs_are_deterministically_converted_to_meaning_unit_sources() -> None:
    from app.reports.report_provider import LocalCodingOutput, ReportProvider
    from app.reports.scoring_domain import AudioEventRef, DialogueRef, WorkRecordRef

    raw = json.loads(_global_json())
    raw["units"] = [
        {
            "id": "global-unit",
            "summary": "合并后的多来源意义单元。",
            "refs": [
                {
                    "kind": "dialogue",
                    "turn_id": "turn-worker",
                    "quote": "公开冻结材料",
                },
                {
                    "kind": "work_record",
                    "field": "problem_understanding",
                    "quote": "压力与失眠",
                },
                {"kind": "audio_event", "event_id": "audio-one"},
            ],
        }
    ]
    client = FakeClient([json.dumps(raw, ensure_ascii=False)])
    provider = ReportProvider(_store(), client=client)
    local_outputs = [
        LocalCodingOutput.model_validate(
            {
                "shard_id": "shard-a",
                "units": [
                    {
                        "id": "local-unit",
                        "summary": "保留局部精确引用。",
                        "initial_codes": ["多来源"],
                        "refs": raw["units"][0]["refs"],
                        "source_role": "interaction",
                        "alternative_reading": None,
                    }
                ],
            }
        ),
        LocalCodingOutput(shard_id="shard-b", units=[]),
    ]

    result = await provider.reduce_coding(
        local_outputs,
        session_id="session-report",
        model_config=_model_config(),
        targets=ALL_TARGETS,
        turn_speakers={"turn-worker": "worker"},
        scene=Scene.hotline,
        media=Media.voice,
    )

    assert result.units[0].turn_ids == ["turn-worker"]
    assert result.units[0].work_record_refs == [
        WorkRecordRef(
            kind="work_record",
            field="problem_understanding",
            quote="压力与失眠",
        )
    ]
    assert result.units[0].audio_event_ids == ["audio-one"]
    assert isinstance(local_outputs[0].units[0].refs[0], DialogueRef)
    assert isinstance(local_outputs[0].units[0].refs[1], WorkRecordRef)
    assert isinstance(local_outputs[0].units[0].refs[2], AudioEventRef)


async def test_reduce_rejects_non_exact_unit_ref_before_lossy_conversion_and_repairs() -> None:
    from app.reports.report_provider import LocalCodingOutput, ReportProvider

    valid_raw = json.loads(_global_json())
    valid_raw["units"] = [
        {
            "id": "global-unit",
            "summary": "合并后的意义单元。",
            "refs": [
                {
                    "kind": "dialogue",
                    "turn_id": "turn-worker",
                    "quote": "公开冻结材料",
                }
            ],
        }
    ]
    invalid_raw = json.loads(json.dumps(valid_raw, ensure_ascii=False))
    invalid_raw["units"][0]["refs"][0]["quote"] = "公开冻结"
    client = FakeClient(
        [
            json.dumps(invalid_raw, ensure_ascii=False),
            json.dumps(valid_raw, ensure_ascii=False),
        ]
    )
    provider = ReportProvider(_store(), client=client)
    local_outputs = [
        LocalCodingOutput.model_validate_json(_local_json("shard-a", with_unit=True)),
        LocalCodingOutput(shard_id="shard-b", units=[]),
    ]

    with pytest.raises(
        ValueError,
        match=r"ReducedMeaningUnit global-unit refs\[0\].*公开冻结",
    ) as error:
        await provider.reduce_coding(
            local_outputs,
            session_id="session-report",
            model_config=_model_config(),
            targets=ALL_TARGETS,
            turn_speakers={"turn-worker": "worker"},
            scene=Scene.hotline,
            media=Media.voice,
        )

    result = await provider.reduce_coding(
        local_outputs,
        session_id="session-report",
        model_config=_model_config(),
        targets=ALL_TARGETS,
        turn_speakers={"turn-worker": "worker"},
        scene=Scene.hotline,
        media=Media.voice,
        call_kind=ModelCallKind.repair,
        validation_feedback=str(error.value),
    )

    repair_message = client.chat.completions.calls[1]["messages"][-1]["content"]
    assert "ReducedMeaningUnit global-unit refs[0]" in repair_message
    assert "\"quote\":\"公开冻结\"" in repair_message
    assert result.units[0].turn_ids == ["turn-worker"]


async def test_reduce_canonicalizes_unit_ref_with_whitespace_only_difference() -> None:
    from app.reports.report_provider import LocalCodingOutput, ReportProvider

    raw = json.loads(_global_json())
    raw["units"] = [
        {
            "id": "global-unit",
            "summary": "合并后的意义单元。",
            "refs": [
                {
                    "kind": "dialogue",
                    "turn_id": "turn-worker",
                    "quote": "第一句。\n第二句。",
                }
            ],
        }
    ]
    local_output = LocalCodingOutput.model_validate_json(
        _local_json("shard-a", with_unit=True)
    )
    local_output.units[0].refs = [
        DialogueRef(
            kind="dialogue",
            turn_id="turn-worker",
            quote="第一句。\n\n第二句。",
        )
    ]
    client = FakeClient([json.dumps(raw, ensure_ascii=False)])
    provider = ReportProvider(_store(), client=client)

    result = await provider.reduce_coding(
        [
            local_output,
            LocalCodingOutput(shard_id="shard-b", units=[]),
        ],
        session_id="session-report",
        model_config=_model_config(),
        targets=ALL_TARGETS,
        turn_speakers={"turn-worker": "worker"},
        scene=Scene.hotline,
        media=Media.voice,
    )

    assert result.units[0].turn_ids == ["turn-worker"]
    assert len(client.chat.completions.calls) == 1


async def test_reduce_requires_exactly_two_distinct_shard_outputs() -> None:
    from app.reports.report_provider import LocalCodingOutput, ReportProvider

    client = FakeClient([])
    provider = ReportProvider(_store(), client=client)
    shard_a = LocalCodingOutput(shard_id="shard-a", units=[])

    for local_outputs in ([shard_a], [shard_a, shard_a]):
        with pytest.raises(ValueError, match="两份不同分片"):
            await provider.reduce_coding(
                local_outputs,
                session_id="session-report",
                model_config=_model_config(),
                targets=ALL_TARGETS,
                turn_speakers={"turn-worker": "worker"},
                scene=Scene.hotline,
                media=Media.voice,
            )
    assert client.chat.completions.calls == []


async def test_reduce_repair_feedback_is_independent_from_map_feedback() -> None:
    from app.reports.report_provider import LocalCodingOutput, ReportProvider

    invalid_global = json.loads(_global_json())
    invalid_global["counter_checks"] = invalid_global["counter_checks"][:-1]
    client = FakeClient(
        [
            json.dumps(invalid_global, ensure_ascii=False),
            _local_json("shard-a"),
            _global_json(),
        ]
    )
    provider = ReportProvider(_store(), client=client)
    local_outputs = [
        LocalCodingOutput(shard_id="shard-a", units=[]),
        LocalCodingOutput(shard_id="shard-b", units=[]),
    ]

    with pytest.raises(ValueError) as error:
        await provider.reduce_coding(
            local_outputs,
            session_id="session-report",
            model_config=_model_config(),
            targets=ALL_TARGETS,
            turn_speakers={"turn-worker": "worker"},
            scene=Scene.hotline,
            media=Media.voice,
        )
    await provider.code_shard(
        _coding_shard("shard-a", "公开冻结材料"),
        session_id="session-report",
        model_config=_model_config(),
        call_kind=ModelCallKind.repair,
    )
    await provider.reduce_coding(
        local_outputs,
        session_id="session-report",
        model_config=_model_config(),
        targets=ALL_TARGETS,
        turn_speakers={"turn-worker": "worker"},
        scene=Scene.hotline,
        media=Media.voice,
        call_kind=ModelCallKind.repair,
        validation_feedback=str(error.value),
    )

    map_messages = client.chat.completions.calls[1]["messages"]
    assert "上一份 JSON 未通过校验" not in json.dumps(
        map_messages, ensure_ascii=False
    )
    reduce_repair = client.chat.completions.calls[2]["messages"][-1]["content"]
    assert "counter_checks" in reduce_repair


@pytest.mark.parametrize(
    ("method", "validation_feedback"),
    [
        ("map", "unit-a source_role=client 与 worker 话轮不一致，quote 不是原文连续子串"),
        ("reduce", "coded_evidence 的 DialogueRef 必须引用 worker 话轮；CounterCheck 契约不完整"),
        (
            "group",
            "respectful_communication proposed_level 超过 level_ceiling；"
            "已有 primary 证据但 proposed_level 为 null",
        ),
    ],
)
async def test_post_validation_feedback_is_included_in_real_provider_repair_messages(
    method: str,
    validation_feedback: str,
) -> None:
    from app.reports.report_provider import LocalCodingOutput, ReportProvider, ScoringGroup

    outcomes = (
        [_local_json("shard-a"), _local_json("shard-a")]
        if method == "map"
        else [_global_json(), _global_json()]
        if method == "reduce"
        else ['{"proposals":[]}', '{"proposals":[]}']
    )
    client = FakeClient(outcomes)
    provider = ReportProvider(_store(), client=client)

    if method == "map":
        for feedback in (None, validation_feedback):
            await provider.code_shard(
                _coding_shard("shard-a", "公开冻结材料"),
                session_id="session-report",
                model_config=_model_config(),
                call_kind=(
                    ModelCallKind.initial if feedback is None else ModelCallKind.repair
                ),
                validation_feedback=feedback,
            )
    elif method == "reduce":
        local_outputs = [
            LocalCodingOutput(shard_id="shard-a", units=[]),
            LocalCodingOutput(shard_id="shard-b", units=[]),
        ]
        for feedback in (None, validation_feedback):
            await provider.reduce_coding(
                local_outputs,
                session_id="session-report",
                    model_config=_model_config(),
                    targets=ALL_TARGETS,
                    turn_speakers={"turn-worker": "worker"},
                    scene=Scene.hotline,
                    media=Media.voice,
                    call_kind=(
                    ModelCallKind.initial if feedback is None else ModelCallKind.repair
                ),
                validation_feedback=feedback,
            )
    else:
        for feedback in (None, validation_feedback):
            await provider.score_group(
                ScoringGroup.interaction,
                [],
                session_id="session-report",
                model_config=_model_config(),
                call_kind=(
                    ModelCallKind.initial if feedback is None else ModelCallKind.repair
                ),
                validation_feedback=feedback,
            )

    first_messages = client.chat.completions.calls[0]["messages"]
    second_messages = client.chat.completions.calls[1]["messages"]
    assert validation_feedback not in json.dumps(first_messages, ensure_ascii=False)
    repair_message = second_messages[-1]["content"]
    assert "程序后置校验错误" in repair_message
    assert validation_feedback in repair_message


def test_group_model_schema_requires_a_concrete_level() -> None:
    from app.reports.report_provider import GroupModelOutput

    schema = GroupModelOutput.model_json_schema()
    proposed_level = schema["$defs"]["ScoredLevelProposal"]["properties"][
        "proposed_level"
    ]

    assert proposed_level["type"] == "integer"
    assert "anyOf" not in proposed_level
    assert proposed_level["minimum"] == 0
    assert proposed_level["maximum"] == 4


def _valid_group_model_payload() -> dict[str, object]:
    return {
        "proposals": [
            {
                "target": "C7",
                "proposed_level": 2,
                "pattern": "能够说明基本边界，但保密范围的交代仍不完整。",
                "rationale": "两段受测者原话共同支持当前判断。",
                "representative_units": ["unit-c7-support"],
                "limiting_units": ["unit-c7-limit"],
                "next_level_gap": ["主动说明保密例外及其适用条件。"],
                "evidence_confidence": "medium",
                "evidence_confidence_factors": ["正反证据均可回到原文核对。"],
            }
        ]
    }


@pytest.mark.parametrize(
    "internal_term",
    [
        "level_ceiling",
        "ceiling",
        "primary",
        "supporting",
        "cross_check",
        "DimensionPacket",
        "target",
        "indicator_id",
        "proposed_level",
        "representative_units",
        "limiting_units",
        "evidence_confidence",
        "coded_evidence",
        "counter_evidence",
        "unit_id",
        "rubric",
    ],
)
def test_group_model_contract_rejects_internal_terms_in_public_report_text(
    internal_term: str,
) -> None:
    from app.reports.report_provider import GroupModelOutput

    payload = _valid_group_model_payload()
    payload["proposals"][0]["pattern"] = f"当前结论受限于 {internal_term}。"  # type: ignore[index]

    with pytest.raises(ValidationError, match="面向使用者的报告文字不得出现内部字段"):
        GroupModelOutput.model_validate(payload)


@pytest.mark.parametrize("不适用措辞", ["治疗关系", "治疗联盟", "治疗计划"])
def test_group_model_contract_rejects_treatment_language_for_hotline_report(
    不适用措辞: str,
) -> None:
    from app.reports.report_provider import GroupModelOutput

    payload = _valid_group_model_payload()
    payload["proposals"][0]["next_level_gap"] = [  # type: ignore[index]
        f"需要继续维持{不适用措辞}。"
    ]

    with pytest.raises(ValidationError, match="心理热线支持报告不使用治疗情境措辞"):
        GroupModelOutput.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pattern", "受限于 level_ceiling=3。"),
        ("rationale", "primary 证据支持当前等级。"),
        ("next_level_gap", ["需要补充 indicator_id 所指行为。"]),
        (
            "evidence_confidence_factors",
            ["supporting 与 cross_check 的材料一致。"],
        ),
    ],
)
def test_group_model_contract_checks_each_generated_narrative_field(
    field: str,
    value: object,
) -> None:
    from app.reports.report_provider import GroupModelOutput

    payload = _valid_group_model_payload()
    payload["proposals"][0][field] = value  # type: ignore[index]

    with pytest.raises(ValidationError, match=field):
        GroupModelOutput.model_validate(payload)


def test_group_model_internal_term_check_does_not_scan_unit_identifiers() -> None:
    from app.reports.report_provider import GroupModelOutput

    payload = _valid_group_model_payload()
    proposal = payload["proposals"][0]  # type: ignore[index]
    proposal["representative_units"] = ["primary-target-level_ceiling"]
    proposal["limiting_units"] = ["cross_check-indicator_id"]

    output = GroupModelOutput.model_validate(payload)

    assert output.proposals[0].representative_units == [
        "primary-target-level_ceiling"
    ]


async def test_group_provider_treats_internal_term_leak_as_repairable_output() -> None:
    from app.reports.report_provider import ReportProvider, ScoringGroup
    from app.runtime.providers import RepairableModelOutputError

    invalid_payload = _valid_group_model_payload()
    invalid_payload["proposals"][0]["next_level_gap"] = [  # type: ignore[index]
        "受限于 level_ceiling=3，无法评估4级行为。"
    ]
    client = FakeClient(
        [
            json.dumps(invalid_payload, ensure_ascii=False),
            json.dumps(_valid_group_model_payload(), ensure_ascii=False),
        ]
    )
    provider = ReportProvider(_store(), client=client)

    with pytest.raises(RepairableModelOutputError):
        await provider.score_group(
            ScoringGroup.professional,
            [],
            session_id="session-report",
            model_config=_model_config(),
        )

    await provider.score_group(
        ScoringGroup.professional,
        [],
        session_id="session-report",
        model_config=_model_config(),
        call_kind=ModelCallKind.repair,
    )

    repair_message = client.chat.completions.calls[1]["messages"][-1]["content"]
    assert "level_ceiling" in repair_message
    assert "面向使用者的报告文字" in repair_message


async def test_group_provider_converts_nonnullable_model_output_to_public_contract() -> None:
    from app.reports.report_provider import (
        GroupScoringOutput,
        ReportProvider,
        ScoringGroup,
    )

    model_output = json.dumps(
        {
            "proposals": [
                {
                    "target": "C7",
                    "proposed_level": 2,
                    "pattern": "能够说明基本边界，但保密范围的交代仍不完整。",
                    "rationale": "主要证据支持二级锚点。",
                    "representative_units": ["unit-c7-support"],
                    "limiting_units": ["unit-c7-limit"],
                    "next_level_gap": ["主动说明保密例外及其适用条件。"],
                    "evidence_confidence": "medium",
                    "evidence_confidence_factors": ["正反证据均有明确原文。"],
                }
            ]
        },
        ensure_ascii=False,
    )
    client = FakeClient([model_output])
    provider = ReportProvider(_store(), client=client)

    result = await provider.score_group(
        ScoringGroup.professional,
        [],
        session_id="session-report",
        model_config=_model_config(),
    )

    assert type(result) is GroupScoringOutput
    assert result.proposals[0].proposed_level == 2
    call = client.chat.completions.calls[0]
    response_format = call["response_format"]
    assert isinstance(response_format, dict)
    model_schema = response_format["json_schema"]["schema"]
    proposed_level = model_schema["$defs"]["ScoredLevelProposal"]["properties"][
        "proposed_level"
    ]
    assert proposed_level["type"] == "integer"
    assert "anyOf" not in proposed_level


def test_prompt_bundle_covers_actual_schemas_and_evidence_role_boundaries() -> None:
    from app.reports.report_provider import (
        REPORT_PROMPT_BUNDLE,
        GroupModelOutput,
        LocalCodingOutput,
        ReduceModelOutput,
    )

    assert REPORT_PROMPT_BUNDLE["bundle_id"] == "report_map_reduce_and_three_groups"
    assert REPORT_PROMPT_BUNDLE["output_contracts"] == {
        "map": LocalCodingOutput.model_json_schema(),
        "reduce": ReduceModelOutput.model_json_schema(),
        "group": GroupModelOutput.model_json_schema(),
    }
    prompts = REPORT_PROMPT_BUNDLE["prompts"]
    assert "report_global" not in prompts
    map_prompt = prompts["report_map"]
    for required in (
        "意义单元",
        "开放编码",
        "可观察",
        "精确引用",
        "source_role",
        "不定级",
    ):
        assert required in map_prompt
    reduce_prompt = prompts["report_reduce"]
    for required in (
        "聚焦编码",
        "指标映射",
        "反例检索",
        "跨分片冲突",
        "语义底线候选",
        "紧迫风险候选",
        "ReducedMeaningUnit",
        "LocalCodedUnit.refs",
        "turn_speakers[ref.turn_id]",
        "passthrough",
        "不能因‘待聚焦编码’直接当作证据",
        "同一意义单元可以映射到多个 target",
        "不得自行判断专项模块是否启用",
        "coverage_decisions",
        "planned_actions 表示会谈中讨论或拟采取的安排",
    ):
        assert required in reduce_prompt
    assert "紧迫风险候选只能引用 source_role=client" not in reduce_prompt
    for family in ("report_interaction", "report_professional", "report_safety"):
        prompt = prompts[family]
        assert "primary" in prompt
        assert "supporting" in prompt
        assert "cross_check" in prompt
        assert "ceiling=2" in prompt
        assert "2级到3级" in prompt
        assert "封顶前" in prompt
        assert "已确认有观察机会且证据充分" in prompt
        assert "每个 target 必须给出" in prompt
        assert "禁止 null" in prompt
        assert "只能逐字复制 DimensionPacket.units 中的 id" in prompt
        assert "不能填摘要或原话" in prompt
        assert "proposed_level 才填 null" not in prompt
        assert "面向使用者的报告文字" in prompt
        assert "自然、专业的中文" in prompt
        assert "具体可观察行为" in prompt
        assert "受限于 level_ceiling" in prompt


def test_report_provider_uses_longer_timeout_without_changing_dialogue_timeout(
    monkeypatch: object,
) -> None:
    from app.reports.report_provider import ReportProvider
    from app.runtime import providers as runtime_providers
    from app.runtime.providers import DirectorProvider

    captured_timeouts: list[float] = []

    def capture_client(**kwargs: object) -> object:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, (int, float))
        captured_timeouts.append(float(timeout))
        return object()

    monkeypatch.setattr(runtime_providers, "AsyncOpenAI", capture_client)
    store = _store()

    ReportProvider(store)._get_client(store.credentials())
    DirectorProvider(store)._get_client(store.credentials())

    assert captured_timeouts == [300.0, 30.0]

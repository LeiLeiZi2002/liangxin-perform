from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.cases.loader import CaseRepository
from app.reports.competency_rubric import get_rubric
from app.reports.job_inputs import CodingShard
from app.reports.models import ReportJobRecord, ReportJobStage
from app.reports.report_pipeline import ReportPipeline
from app.reports.report_provider import (
    GlobalCodingOutput,
    GroupScoringOutput,
    LocalCodedUnit,
    LocalCodingOutput,
    ScoringGroup,
)
from app.reports.scoring_domain import (
    CodedEvidence,
    CoreDimension,
    CounterCheck,
    DialogueRef,
    DimensionPacket,
    EvidenceConfidence,
    EvidenceDirection,
    EvidenceStrength,
    LevelProposal,
    MeaningUnit,
    Target,
    UnscoredReason,
    WorkRecordRef,
)
from app.runtime.character_kernel import CharacterPromptKernel
from app.runtime.character_provider import (
    CharacterDefinition,
    CharacterOutput,
    CharacterOutputValidationError,
    CharacterRepository,
    CharacterTranscriptTurn,
)
from app.runtime.kernel import RuntimePhase, TechnicalPauseError
from app.runtime.models import ModelCallKind
from app.sessions.models import EndReason, Media, Scene, SessionRecord, TurnRecord


class ScriptedCharacter:
    def __init__(self, outputs: Sequence[CharacterOutput | Exception]) -> None:
        self._outputs = list(outputs)
        self.calls: list[dict[str, object]] = []

    async def respond(
        self,
        *,
        character: CharacterDefinition,
        transcript: Sequence[CharacterTranscriptTurn],
        current_worker_text: str,
        opening: bool,
        current_scene: str,
        world_reality: str,
        allowed_world_actions: Sequence[object],
        session_id: str | None = None,
        client_turn_id: str | None = None,
    ) -> CharacterOutput:
        self.calls.append(
            {
                "case_id": character.case_id,
                "transcript": list(transcript),
                "current_worker_text": current_worker_text,
                "opening": opening,
                "current_scene": current_scene,
                "world_reality": world_reality,
                "allowed_world_actions": tuple(allowed_world_actions),
                "session_id": session_id,
                "client_turn_id": client_turn_id,
            }
        )
        result = self._outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ScriptedSpeech:
    tts_model_name = "fake-tts"

    def __init__(self, *, fail_attempts: int = 0) -> None:
        self.fail_attempts = fail_attempts
        self.calls: list[tuple[str, str]] = []

    async def synthesize(
        self,
        text: str,
        *,
        instruction: str = "",
    ) -> AsyncIterator[bytes]:
        self.calls.append((text, instruction))
        if self.fail_attempts > 0:
            self.fail_attempts -= 1
            raise RuntimeError("tts unavailable")
        yield b"fake-pcm"


def _character_output(
    text: str,
    *,
    end_session: bool = False,
) -> CharacterOutput:
    return CharacterOutput(
        spoken_text=text,
        delivery_hint="语气自然，句间留出短暂停顿",
        end_session=end_session,
        action_request="none",
    )


def _configure_and_create_session(client: TestClient, scene: Scene) -> str:
    configured = client.put(
        "/api/demo-config",
        json={
            "scene": scene.value,
            "case_type": "main",
            "task_count": 1,
            "soft_duration_minutes": None,
            "model_mode": "live",
            "require_work_record": True,
        },
    )
    assert configured.status_code == 200, configured.text
    created = client.post(
        "/api/sessions",
        json={
            "mode": "assessment",
            "case_id": "marriage_boundary_main",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["scene"] == scene.value
    assert body["media"] == (
        Media.voice.value if scene is Scene.hotline else Media.text.value
    )
    return str(body["id"])


def _kernel(
    engine: Engine,
    tmp_path: Path,
    character: ScriptedCharacter,
    speech: ScriptedSpeech | None,
) -> CharacterPromptKernel:
    return CharacterPromptKernel(
        engine=engine,
        characters=CharacterRepository(),
        character=character,
        speech=speech,
        audio_root=tmp_path / "audio",
    )


def _work_record_payload(opening_turn_id: str, scene: Scene) -> dict[str, object]:
    medium = "这段文字咨询" if scene is Scene.online else "这次通话"
    return {
        "problem_understanding": (
            "来访者因伴侣关系中的可疑线索反复查证并失眠，当前更需要先稳定情绪、"
            "区分已知事实与推测，再讨论当晚可执行的安排。"
        ),
        "risk_level": "uncertain",
        "risk_reasoning": (
            "来访者表达了明显焦虑和睡眠受损，但当前材料尚不足以确认即时危险，"
            "仍需继续了解自伤、他伤及冲动对质风险。"
        ),
        "risk_evidence_turn_ids": [opening_turn_id],
        "missing_information": ["当前是否存在自伤或伤人想法", "今晚是否会立即对质"],
        "planned_actions": [
            "emotion_stabilization",
            "goal_clarification",
            "conflict_deescalation",
            "autonomy_support",
        ],
        "referral_decision": "consider",
        "supervision_decision": False,
        "follow_up": "先确认今晚的安全与休息安排，再由来访者决定何时以及如何沟通。",
        "limitations": (
            f"判断仅依据{medium}中来访者的自述，"
            "关系越界目前仍是可疑线索而非已证实事实。"
        ),
    }


class TranscriptReportGateway:
    """用冻结的真实话轮构造确定性报告输出，不替换数据链。"""

    def __init__(self, *, fail_map: bool = False) -> None:
        self.fail_map = fail_map
        self.reduce_contexts: list[tuple[Scene, Media]] = []

    async def code_shard(
        self,
        shard: CodingShard,
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> LocalCodingOutput:
        del session_id, call_kind, model_config, validation_feedback
        if self.fail_map:
            raise RuntimeError("fake report model unavailable")
        units: list[LocalCodedUnit] = []
        for turn in shard.turns:
            units.append(
                LocalCodedUnit(
                    id=f"{shard.shard_id}-turn-{turn.turn_id}",
                    summary="会谈中的一段可观察互动。",
                    initial_codes=["互动回应"],
                    refs=[
                        DialogueRef(
                            kind="dialogue",
                            turn_id=turn.turn_id,
                            quote=turn.text,
                        )
                    ],
                    source_role=turn.speaker.value,
                    alternative_reading=None,
                )
            )
        if shard.work_record is not None:
            units.extend(
                [
                    LocalCodedUnit(
                        id=f"{shard.shard_id}-record-problem",
                        summary="工作记录中的问题理解。",
                        initial_codes=["问题理解"],
                        refs=[
                            WorkRecordRef(
                                kind="work_record",
                                field="problem_understanding",
                                quote=shard.work_record.problem_understanding,
                            )
                        ],
                        source_role="work_record",
                        alternative_reading=None,
                    ),
                    LocalCodedUnit(
                        id=f"{shard.shard_id}-record-risk",
                        summary="工作记录中的风险判断。",
                        initial_codes=["判断边界"],
                        refs=[
                            WorkRecordRef(
                                kind="work_record",
                                field="risk_reasoning",
                                quote=shard.work_record.risk_reasoning,
                            )
                        ],
                        source_role="work_record",
                        alternative_reading=None,
                    ),
                ]
            )
        return LocalCodingOutput(shard_id=shard.shard_id, units=units)

    async def reduce_coding(
        self,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        scene: Scene,
        media: Media,
        targets: Sequence[Target],
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        turn_speakers: dict[str, str] | None = None,
        active_target_briefs: Sequence[object] = (),
        validation_feedback: str | None = None,
    ) -> GlobalCodingOutput:
        del session_id, call_kind, model_config, active_target_briefs, validation_feedback
        self.reduce_contexts.append((scene, media))
        speakers = turn_speakers or {}
        dialogue_refs: list[DialogueRef] = []
        work_refs: list[WorkRecordRef] = []
        for output in local_outputs:
            for unit in output.units:
                for ref in unit.refs:
                    if isinstance(ref, DialogueRef):
                        if speakers.get(ref.turn_id) == "worker":
                            dialogue_refs.append(ref)
                    elif isinstance(ref, WorkRecordRef):
                        work_refs.append(ref)
        dialogue_refs = list({ref.turn_id: ref for ref in dialogue_refs}.values())
        work_refs = list({(ref.field, ref.quote): ref for ref in work_refs}.values())

        units = [
            MeaningUnit(
                id=f"worker-unit-{index}",
                turn_ids=[ref.turn_id],
                summary="受测者在会谈中的可观察回应。",
            )
            for index, ref in enumerate(dialogue_refs, start=1)
        ]
        units.extend(
            MeaningUnit(
                id=f"record-unit-{index}",
                work_record_refs=[ref],
                summary="受测者提交的专业工作记录。",
            )
            for index, ref in enumerate(work_refs, start=1)
        )
        dialogue_unit_ids = [unit.id for unit in units if unit.turn_ids]
        record_unit_ids = [unit.id for unit in units if unit.work_record_refs]
        evidence: list[CodedEvidence] = []
        for target in targets:
            indicator_id = get_rubric(target, media=media).indicators[0].id
            if target is CoreDimension.documentation:
                selected_refs: Sequence[DialogueRef | WorkRecordRef] = work_refs[:2]
                selected_unit_ids = record_unit_ids[:2]
            else:
                selected_refs = dialogue_refs[:2]
                selected_unit_ids = dialogue_unit_ids[:2]
            for index, (ref, unit_id) in enumerate(
                zip(selected_refs, selected_unit_ids, strict=True)
            ):
                evidence.append(
                    CodedEvidence(
                        unit_id=unit_id,
                        target=target,
                        indicator_id=indicator_id,
                        direction=EvidenceDirection.support,
                        strength=(
                            EvidenceStrength.strong
                            if index == 0
                            else EvidenceStrength.moderate
                        ),
                        context="该原始材料直接呈现了当前观察点。",
                        alternative_reading=None,
                        ref=ref,
                    )
                )
        return GlobalCodingOutput(
            units=units,
            coded_evidence=evidence,
            counter_checks=[
                CounterCheck(
                    target=target,
                    searched_unit_ids=[unit.id for unit in units],
                    found=[],
                    not_found_note="已核对当前材料，未发现足以推翻初步判断的反例。",
                )
                for target in targets
            ],
            bottom_line_candidates=[],
            material_conflict_candidates=[],
            urgent_risk_disclosure_candidates=[],
        )

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        call_kind: ModelCallKind = ModelCallKind.initial,
        model_config: object | None = None,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput:
        del group, session_id, call_kind, model_config, validation_feedback
        proposals: list[LevelProposal] = []
        for packet in packets:
            unit_ids = list(
                dict.fromkeys(item.evidence.unit_id for item in packet.evidence)
            )
            if not unit_ids:
                proposals.append(
                    LevelProposal(
                        target=packet.target,
                        proposed_level=None,
                        pattern="",
                        rationale="当前没有足够材料形成等级判断。",
                        representative_units=[],
                        limiting_units=[],
                        next_level_gap=[],
                        evidence_confidence=EvidenceConfidence.low,
                        evidence_confidence_factors=["没有可用的直接证据"],
                    )
                )
            else:
                proposals.append(
                    LevelProposal(
                        target=packet.target,
                        proposed_level=3,
                        pattern="能够围绕来访者当前处境形成清楚、可核对的回应。",
                        rationale="两段独立材料支持当前判断，引用均可返回原文核对。",
                        representative_units=unit_ids[:2],
                        limiting_units=[],
                        next_level_gap=["继续观察受阻情形下的调整过程。"],
                        evidence_confidence=EvidenceConfidence.high,
                        evidence_confidence_factors=["原话可回看"],
                    )
                )
        return GroupScoringOutput(proposals=proposals)


class PipelineProcessor:
    def __init__(self, engine: Engine, gateway: TranscriptReportGateway) -> None:
        self._pipeline = ReportPipeline(engine, CaseRepository(), gateway)

    async def process(self, job_id: str) -> None:
        await self._pipeline.run(job_id)


def _submit_record_and_run_report(
    client: TestClient,
    engine: Engine,
    session_id: str,
    opening_turn_id: str,
    scene: Scene,
    *,
    gateway: TranscriptReportGateway | None = None,
) -> tuple[dict[str, Any], dict[str, Any], TranscriptReportGateway]:
    from app.api.routes import reports as reports_routes
    from app.main import app

    record_response = client.put(
        f"/api/sessions/{session_id}/work-record",
        json=_work_record_payload(opening_turn_id, scene),
    )
    assert record_response.status_code == 200, record_response.text
    selected_gateway = gateway or TranscriptReportGateway()
    app.dependency_overrides[reports_routes.get_report_job_processor] = (
        lambda: PipelineProcessor(engine, selected_gateway)
    )
    try:
        created = client.post(f"/api/sessions/{session_id}/reports")
        assert created.status_code == 202, created.text
        job = client.get(f"/api/report-jobs/{created.json()['id']}")
        assert job.status_code == 200, job.text
        report_id = job.json()["report_id"]
        report = (
            client.get(f"/api/reports/{report_id}")
            if report_id is not None
            else None
        )
        report_body = report.json() if report is not None else {}
    finally:
        app.dependency_overrides.pop(
            reports_routes.get_report_job_processor,
            None,
        )
    return job.json(), report_body, selected_gateway


def _session_turns(engine: Engine, session_id: str) -> list[TurnRecord]:
    with Session(engine) as db:
        return list(
            db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == session_id)
                .order_by(col(TurnRecord.sequence))
            ).all()
        )


@pytest.mark.asyncio
async def test_hotline_full_chain_keeps_opening_evidence_audio_and_report_traceability(
    client: TestClient,
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    session_id = _configure_and_create_session(client, Scene.hotline)
    character = ScriptedCharacter(
        [
            _character_output(
                "喂，你好。我最近发现了一些不太对劲的东西，这几天一直睡不好。"
            ),
            _character_output("我现在最怕一开口就吵起来，也怕自己把猜的当成真的。"),
            _character_output("好，那我们今天就先到这里吧，再见。", end_session=True),
        ]
    )
    speech = ScriptedSpeech()
    kernel = _kernel(test_engine, tmp_path, character, speech)

    opening = await kernel.generate_opening(
        session_id=session_id,
        client_turn_id="hotline-opening",
    )
    first = await kernel.process_worker_turn(
        session_id=session_id,
        client_turn_id="hotline-turn-1",
        text="你愿意先说说哪些是已经确认的，哪些还只是你的猜测吗？",
    )
    closing = await kernel.process_worker_turn(
        session_id=session_id,
        client_turn_id="hotline-turn-2",
        text="我们先把今晚不立刻对质的安排确认下来，你觉得可以吗？",
    )

    assert opening.audio_chunks == (b"fake-pcm",)
    assert first.audio_chunks == (b"fake-pcm",)
    assert closing.audio_chunks == (b"fake-pcm",)
    assert closing.ending_route_id == "character_prompt_end"
    assert all(call[0] and "（" not in call[0] for call in speech.calls)
    kernel.end_session(session_id, EndReason.natural_closure)

    job, report, gateway = _submit_record_and_run_report(
        client,
        test_engine,
        session_id,
        opening.client.id,
        Scene.hotline,
    )

    assert job["stage"] == ReportJobStage.succeeded.value
    assert report["case_id"] == "marriage_boundary_main"
    assert report["scene"] == Scene.hotline.value
    assert report["media"] == Media.voice.value
    assert "total_score" not in str(report)
    assert "raw_score" not in str(report)
    turns = _session_turns(test_engine, session_id)
    assert [(turn.speaker.value, turn.text) for turn in turns] == [
        ("client", opening.client.text),
        ("worker", first.worker.text),
        ("client", first.client.text),
        ("worker", closing.worker.text),
        ("client", closing.client.text),
    ]
    opening_records = [
        turn for turn in turns if turn.client_turn_id == "hotline-opening"
    ]
    assert [(turn.id, turn.speaker.value) for turn in opening_records] == [
        (opening.client.id, "client")
    ]
    worker_text_by_id = {
        first.worker.id: first.worker.text,
        closing.worker.id: closing.worker.text,
    }
    dialogue_refs = [
        evidence["ref"]
        for dimension in report["dimensions"]
        for evidence in dimension["result"]["evidence"]
        if evidence["ref"]["kind"] == "dialogue"
    ]
    assert dialogue_refs
    assert all(ref["turn_id"] in worker_text_by_id for ref in dialogue_refs)
    assert all(
        ref["quote"] == worker_text_by_id[ref["turn_id"]]
        for ref in dialogue_refs
    )
    assert gateway.reduce_contexts == [(Scene.hotline, Media.voice)]
    saved_record = client.get(f"/api/sessions/{session_id}/work-record")
    assert saved_record.status_code == 200
    assert saved_record.json()["risk_evidence_turn_ids"] == [opening.client.id]


@pytest.mark.asyncio
async def test_online_full_chain_keeps_multiline_reply_as_one_turn_without_voice_language(
    client: TestClient,
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    session_id = _configure_and_create_session(client, Scene.online)
    multiline = "我就是怕自己想多了。\n\n可那个画面一直在脑子里转。\n今晚我也不想马上跟他吵。"
    character = ScriptedCharacter(
        [
            _character_output("你好，我想问个事。\n\n这些聊天以后谁能看到？"),
            _character_output(multiline),
            _character_output("好，我先去洗把脸。今晚就先这样吧，晚安。", end_session=True),
        ]
    )
    kernel = _kernel(test_engine, tmp_path, character, None)

    opening = await kernel.generate_opening(
        session_id=session_id,
        client_turn_id="online-opening",
    )
    reply = await kernel.process_worker_turn(
        session_id=session_id,
        client_turn_id="online-turn-1",
        text="在继续之前，我先说明记录边界。你现在最困扰的是哪一部分？",
        synthesize_audio=False,
    )
    closing = await kernel.process_worker_turn(
        session_id=session_id,
        client_turn_id="online-turn-2",
        text="今晚先不急着下结论，我们把休息和沟通安排收一下，可以吗？",
        synthesize_audio=False,
    )
    assert opening.audio_chunks == ()
    assert reply.audio_chunks == ()
    assert reply.client.text == multiline
    assert closing.ending_route_id == "character_prompt_end"
    kernel.end_session(session_id, EndReason.natural_closure)

    restored = client.get(f"/api/sessions/{session_id}")
    assert restored.status_code == 200
    restored_turns = restored.json()["transcript"]
    matching = [turn for turn in restored_turns if turn["id"] == reply.client.id]
    assert matching == [
        {
            **matching[0],
            "text": multiline,
            "audio_available": False,
        }
    ]
    assert len([turn for turn in restored_turns if turn["client_turn_id"] == "online-turn-1"]) == 2

    job, report, gateway = _submit_record_and_run_report(
        client,
        test_engine,
        session_id,
        opening.client.id,
        Scene.online,
    )
    assert job["stage"] == ReportJobStage.succeeded.value
    assert report["scene"] == Scene.online.value
    assert report["media"] == Media.text.value
    serialized = str(report)
    for hotline_only in ("热线", "接线", "来电", "通话", "声音线索", "声音表现"):
        assert hotline_only not in serialized
    assert gateway.reduce_contexts == [(Scene.online, Media.text)]


@pytest.mark.asyncio
async def test_character_failure_retries_the_same_worker_turn_without_duplicate_records(
    client: TestClient,
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    session_id = _configure_and_create_session(client, Scene.online)
    character = ScriptedCharacter(
        [
            _character_output("你好，我想问一下记录边界。"),
            CharacterOutputValidationError("角色输出合同不满足"),
            _character_output("我现在很乱，但这句话我想继续说完。"),
        ]
    )
    kernel = _kernel(test_engine, tmp_path, character, None)
    await kernel.generate_opening(
        session_id=session_id,
        client_turn_id="retry-opening",
        synthesize_audio=False,
    )
    request = {
        "session_id": session_id,
        "client_turn_id": "retry-worker-turn",
        "text": "我刚才的话请保留，恢复后继续这一轮。",
        "synthesize_audio": False,
    }

    with pytest.raises(TechnicalPauseError) as paused:
        await kernel.process_worker_turn(**request)
    assert paused.value.can_retry is True
    recovered = await kernel.process_worker_turn(**request)

    assert recovered.worker.text == request["text"]
    assert recovered.client.text == "我现在很乱，但这句话我想继续说完。"
    assert len(character.calls) == 3
    matching = [
        turn
        for turn in _session_turns(test_engine, session_id)
        if turn.client_turn_id == "retry-worker-turn"
    ]
    assert [turn.text for turn in matching] == [request["text"], recovered.client.text]


@pytest.mark.asyncio
async def test_tts_failure_keeps_actor_text_and_retries_audio_without_model_recall(
    client: TestClient,
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    session_id = _configure_and_create_session(client, Scene.hotline)
    actor_text = "我现在最怕的是一开口就和他吵起来。"
    character = ScriptedCharacter(
        [
            _character_output("喂，你好。我这几天一直睡不好。"),
            _character_output(actor_text),
        ]
    )
    speech = ScriptedSpeech(fail_attempts=2)
    kernel = _kernel(test_engine, tmp_path, character, speech)
    await kernel.generate_opening(
        session_id=session_id,
        client_turn_id="tts-opening",
        synthesize_audio=False,
    )
    request = {
        "session_id": session_id,
        "client_turn_id": "tts-worker-turn",
        "text": "你现在最怕发生什么？",
    }
    published: list[str] = []

    with pytest.raises(TechnicalPauseError) as paused:
        await kernel.process_worker_turn(
            **request,
            on_actor_text=lambda text: published.append(text),
        )
    assert paused.value.failed_phase is RuntimePhase.synthesizing
    recovered = await kernel.process_worker_turn(**request)

    assert published == [actor_text, actor_text]
    assert recovered.client.text == actor_text
    assert recovered.audio_chunks == (b"fake-pcm",)
    assert len(character.calls) == 2
    assert [text for text, _ in speech.calls] == [actor_text, actor_text, actor_text]


@pytest.mark.asyncio
async def test_report_failure_preserves_frozen_work_record(
    client: TestClient,
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    session_id = _configure_and_create_session(client, Scene.online)
    character = ScriptedCharacter(
        [_character_output("你好，这些聊天记录会怎么保存？")]
    )
    kernel = _kernel(test_engine, tmp_path, character, None)
    opening = await kernel.generate_opening(
        session_id=session_id,
        client_turn_id="report-failure-opening",
        synthesize_audio=False,
    )
    kernel.end_session(session_id, EndReason.user_ended)

    job, report, _ = _submit_record_and_run_report(
        client,
        test_engine,
        session_id,
        opening.client.id,
        Scene.online,
        gateway=TranscriptReportGateway(fail_map=True),
    )

    assert job["stage"] == ReportJobStage.failed.value
    assert job["report_id"] is None
    assert report == {}
    saved = client.get(f"/api/sessions/{session_id}/work-record")
    assert saved.status_code == 200
    assert saved.json()["risk_evidence_turn_ids"] == [opening.client.id]


@pytest.mark.asyncio
async def test_technical_interruption_keeps_transcript_and_does_not_fulfil_closure(
    client: TestClient,
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    session_id = _configure_and_create_session(client, Scene.online)
    character = ScriptedCharacter(
        [
            _character_output("你好，我这几天睡得很差。"),
            _character_output("我想先把已经确认的部分说清楚。"),
        ]
    )
    kernel = _kernel(test_engine, tmp_path, character, None)
    opening = await kernel.generate_opening(
        session_id=session_id,
        client_turn_id="technical-opening",
        synthesize_audio=False,
    )
    worker_text = "你愿意先说说已经确认的事情吗？"
    reply = await kernel.process_worker_turn(
        session_id=session_id,
        client_turn_id="technical-turn",
        text=worker_text,
        synthesize_audio=False,
    )
    kernel.end_session(session_id, EndReason.technical_interruption)

    job, report, _ = _submit_record_and_run_report(
        client,
        test_engine,
        session_id,
        opening.client.id,
        Scene.online,
    )

    assert job["stage"] == ReportJobStage.succeeded.value
    turns = _session_turns(test_engine, session_id)
    assert [turn.text for turn in turns] == [
        opening.client.text,
        worker_text,
        reply.client.text,
    ]
    closure = next(
        dimension
        for dimension in report["dimensions"]
        if dimension["target"] == CoreDimension.closure_and_followup.value
    )
    assert closure["result"]["unscored_reason"] == UnscoredReason.no_opportunity.value
    assert all(
        not opportunity["fulfilled"]
        for opportunity in closure["result"]["opportunities"]
    )
    with Session(test_engine) as db:
        stored = db.get(SessionRecord, session_id)
        stored_job = db.exec(
            select(ReportJobRecord).where(ReportJobRecord.session_id == session_id)
        ).one()
    assert stored is not None and stored.end_reason is EndReason.technical_interruption
    assert stored_job.stage is ReportJobStage.succeeded

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.reports.competency_rubric import get_rubric
from app.reports.job_inputs import CodingShard
from app.reports.scoring_domain import (
    AudioEventRef,
    BottomLineCandidate,
    CodedEvidence,
    CoreDimension,
    CounterCheck,
    DialogueRef,
    DimensionPacket,
    EvidenceRef,
    LevelProposal,
    MaterialConflict,
    MeaningUnit,
    SpecialModule,
    Target,
    UrgentRiskDisclosureCandidate,
    WorkRecordRef,
)
from app.runtime.failures import RuntimeFailureRecorder, exception_failure_details
from app.runtime.metrics import ModelCallRecorder
from app.runtime.models import CacheMode, ModelCallKind, ModelRole, PromptFamily
from app.runtime.providers import (
    ExplicitCacheRejectedError,
    RepairableModelOutputError,
    _StructuredTextProvider,
    _supports_explicit_cache,
)
from app.runtime_config import RuntimeCredentialStore
from app.sessions.models import Media, Scene


class ScoringGroup(StrEnum):
    interaction = "interaction"
    professional = "professional"
    safety = "safety"


GROUP_TARGETS: dict[ScoringGroup, tuple[Target, ...]] = {
    ScoringGroup.interaction: (
        CoreDimension.respectful_communication,
        CoreDimension.listening_and_emotion,
        CoreDimension.concern_clarification,
        CoreDimension.supportive_intervention,
        CoreDimension.voice_and_process,
        CoreDimension.closure_and_followup,
        SpecialModule.emotional_dysregulation,
        SpecialModule.dependency_and_boundary,
        SpecialModule.aggression_and_harassment,
    ),
    ScoringGroup.professional: (
        CoreDimension.integration_and_judgment,
        CoreDimension.boundary_and_ethics,
        CoreDimension.documentation,
        SpecialModule.psychotic_experience,
        SpecialModule.third_party_call,
        SpecialModule.minor_protection,
    ),
    ScoringGroup.safety: (
        SpecialModule.basic_risk_screening,
        SpecialModule.full_risk_appraisal,
        SpecialModule.safety_response,
    ),
}

GROUP_PROMPT_FAMILIES = {
    ScoringGroup.interaction: PromptFamily.report_interaction,
    ScoringGroup.professional: PromptFamily.report_professional,
    ScoringGroup.safety: PromptFamily.report_safety,
}

REPORT_TARGETS: tuple[Target, ...] = (*CoreDimension, *SpecialModule)


class ReportOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActiveTargetBrief(ReportOutputModel):
    """已由规则确认出现的观察任务；只包含可公开的测评焦点。"""

    target: Target
    description: str = Field(min_length=1)
    evidence_targets: list[str] = Field(min_length=1)
    indicator_ids: list[str] = Field(min_length=1)


class CoverageStatus(StrEnum):
    evidence_mapped = "evidence_mapped"
    no_reliable_material = "no_reliable_material"


class TargetCoverageDecision(ReportOutputModel):
    target: Target
    status: CoverageStatus
    reason: str = Field(min_length=1)


class LocalCodedUnit(ReportOutputModel):
    id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    initial_codes: list[str] = Field(min_length=1)
    refs: list[EvidenceRef] = Field(min_length=1)
    source_role: Literal["worker", "client", "interaction", "work_record"]
    alternative_reading: str | None

    @field_validator("initial_codes")
    @classmethod
    def require_nonblank_initial_codes(cls, value: list[str]) -> list[str]:
        if any(not code.strip() for code in value):
            raise ValueError("initial_codes 不能包含空白编码")
        return list(dict.fromkeys(value))


class LocalCodingOutput(ReportOutputModel):
    shard_id: str = Field(min_length=1)
    units: list[LocalCodedUnit]

    @model_validator(mode="after")
    def require_unique_unit_ids(self) -> Self:
        unit_ids = [unit.id for unit in self.units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("局部编码单元 id 必须唯一")
        return self


class GlobalCodingOutput(ReportOutputModel):
    units: list[MeaningUnit]
    coded_evidence: list[CodedEvidence]
    counter_checks: list[CounterCheck]
    bottom_line_candidates: list[BottomLineCandidate]
    material_conflict_candidates: list[MaterialConflict]
    urgent_risk_disclosure_candidates: list[UrgentRiskDisclosureCandidate]

    @model_validator(mode="after")
    def require_unique_counter_check_targets(self) -> Self:
        actual_targets = [counter.target for counter in self.counter_checks]
        if len(actual_targets) != len(set(actual_targets)):
            raise ValueError("counter_checks 中每个 target 必须唯一")
        return self


class ReducedMeaningUnit(ReportOutputModel):
    id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    refs: list[EvidenceRef] = Field(min_length=1)


class ReduceModelOutput(ReportOutputModel):
    units: list[ReducedMeaningUnit]
    coded_evidence: list[CodedEvidence]
    coverage_decisions: list[TargetCoverageDecision]
    counter_checks: list[CounterCheck]
    bottom_line_candidates: list[BottomLineCandidate]
    material_conflict_candidates: list[MaterialConflict]
    urgent_risk_disclosure_candidates: list[UrgentRiskDisclosureCandidate]

    @model_validator(mode="after")
    def require_unique_counter_check_and_coverage_targets(self) -> Self:
        actual_targets = [counter.target for counter in self.counter_checks]
        if len(actual_targets) != len(set(actual_targets)):
            raise ValueError("counter_checks 中每个 target 必须唯一")
        coverage_targets = [decision.target for decision in self.coverage_decisions]
        if len(coverage_targets) != len(set(coverage_targets)):
            raise ValueError("coverage_decisions 中每个 target 必须唯一")
        return self

    def to_global_output(self) -> GlobalCodingOutput:
        units: list[MeaningUnit] = []
        for unit in self.units:
            dialogue_turn_ids = [
                ref.turn_id for ref in unit.refs if isinstance(ref, DialogueRef)
            ]
            work_record_refs = [
                ref for ref in unit.refs if isinstance(ref, WorkRecordRef)
            ]
            unique_work_record_refs: list[WorkRecordRef] = []
            seen_work_record_refs: set[tuple[object, str]] = set()
            for ref in work_record_refs:
                identity = (ref.field, ref.quote)
                if identity not in seen_work_record_refs:
                    seen_work_record_refs.add(identity)
                    unique_work_record_refs.append(ref)
            audio_event_ids = [
                ref.event_id for ref in unit.refs if isinstance(ref, AudioEventRef)
            ]
            units.append(
                MeaningUnit(
                    id=unit.id,
                    turn_ids=list(dict.fromkeys(dialogue_turn_ids)),
                    work_record_refs=unique_work_record_refs,
                    audio_event_ids=list(dict.fromkeys(audio_event_ids)),
                    summary=unit.summary,
                )
            )
        return GlobalCodingOutput(
            units=units,
            coded_evidence=self.coded_evidence,
            counter_checks=self.counter_checks,
            bottom_line_candidates=self.bottom_line_candidates,
            material_conflict_candidates=self.material_conflict_candidates,
            urgent_risk_disclosure_candidates=self.urgent_risk_disclosure_candidates,
        )


def _require_reduced_unit_refs_in_local_outputs(
    local_outputs: Sequence[LocalCodingOutput],
    output: ReduceModelOutput,
) -> None:
    local_refs = [
        ref
        for local_output in local_outputs
        for local_unit in local_output.units
        for ref in local_unit.refs
    ]
    for unit in output.units:
        for index, ref in enumerate(unit.refs):
            if any(ref == local_ref for local_ref in local_refs):
                continue
            serialized_ref = json.dumps(
                ref.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            raise ValueError(
                f"ReducedMeaningUnit {unit.id} refs[{index}] "
                "未精确继承两份 LocalCodedUnit.refs："
                f"{serialized_ref}"
            )


def _validate_active_target_coverage(
    output: ReduceModelOutput,
    active_target_briefs: Sequence[ActiveTargetBrief],
) -> None:
    expected_targets = {brief.target for brief in active_target_briefs}
    decisions_by_target = {
        decision.target: decision for decision in output.coverage_decisions
    }
    if not expected_targets.issubset(decisions_by_target):
        missing = sorted(
            target.value for target in expected_targets - set(decisions_by_target)
        )
        raise ValueError(
            "coverage_decisions 未覆盖已启用观察任务：缺少 "
            + "、".join(missing)
        )

    evidence_targets = {evidence.target for evidence in output.coded_evidence}
    for target in expected_targets:
        decision = decisions_by_target[target]
        has_evidence = target in evidence_targets
        if decision.status is CoverageStatus.evidence_mapped and not has_evidence:
            raise ValueError(
                f"{target.value} 声明 evidence_mapped，但 coded_evidence 没有对应证据"
            )


def _validate_reduce_targets(
    output: ReduceModelOutput,
    targets: Sequence[Target],
) -> None:
    requested = tuple(targets)
    core_targets = tuple(CoreDimension)
    if requested[: len(core_targets)] != core_targets:
        raise ValueError("Reduce target 必须先完整包含九个共通能力维度")
    module_targets = requested[len(core_targets) :]
    if any(not isinstance(target, SpecialModule) for target in module_targets):
        raise ValueError("Reduce target 的共通维度之后只能包含专项模块")
    if len(requested) != len(set(requested)):
        raise ValueError("Reduce target 不能重复")
    actual = {check.target for check in output.counter_checks}
    expected = set(requested)
    if actual != expected:
        missing = sorted(target.value for target in expected - actual)
        unexpected = sorted(target.value for target in actual - expected)
        details: list[str] = []
        if missing:
            details.append("缺少 " + "、".join(missing))
        if unexpected:
            details.append("多出 " + "、".join(unexpected))
        raise ValueError(
            "counter_checks 必须完整且仅覆盖本次评估 target：" + "；".join(details)
        )


class GroupScoringOutput(ReportOutputModel):
    proposals: list[LevelProposal]


_PUBLIC_REPORT_INTERNAL_TERMS = (
    "level_ceiling",
    "ceiling",
    "primary",
    "supporting",
    "cross_check",
    "dimensionpacket",
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
)
_HOTLINE_TREATMENT_TERMS = ("治疗关系", "治疗联盟", "治疗计划")


class ScoredLevelProposal(LevelProposal):
    """定级模型专用契约；进入本阶段的目标必须产生具体等级。"""

    proposed_level: int = Field(ge=0, le=4)

    @model_validator(mode="after")
    def reject_internal_terms_in_public_report_text(self) -> Self:
        narrative_fields = {
            "pattern": [self.pattern],
            "rationale": [self.rationale],
            "next_level_gap": self.next_level_gap,
            "evidence_confidence_factors": self.evidence_confidence_factors,
        }
        violations: list[str] = []
        treatment_language: list[str] = []
        for field_name, texts in narrative_fields.items():
            for text in texts:
                normalized = text.casefold()
                for term in _PUBLIC_REPORT_INTERNAL_TERMS:
                    if term in normalized:
                        violations.append(f"{field_name}={term}")
                for term in _HOTLINE_TREATMENT_TERMS:
                    if term in text:
                        treatment_language.append(f"{field_name}={term}")
        if violations:
            raise ValueError(
                "面向使用者的报告文字不得出现内部字段或类型名："
                + "、".join(dict.fromkeys(violations))
            )
        if treatment_language:
            raise ValueError(
                "心理热线支持报告不使用治疗情境措辞："
                + "、".join(dict.fromkeys(treatment_language))
            )
        return self


class GroupModelOutput(ReportOutputModel):
    proposals: list[ScoredLevelProposal]

    def to_group_scoring_output(self) -> GroupScoringOutput:
        return GroupScoringOutput.model_validate(self.model_dump(mode="python"))


class ReportSamplingParameters(ReportOutputModel):
    temperature: float


class ReportModelConfig(ReportOutputModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_model: str
    sampling_parameters: ReportSamplingParameters

    @property
    def report_temperature(self) -> float:
        return self.sampling_parameters.temperature


class ReportModelGateway(Protocol):
    async def code_shard(
        self,
        shard: CodingShard,
        *,
        session_id: str,
        model_config: ReportModelConfig,
        call_kind: ModelCallKind = ModelCallKind.initial,
        validation_feedback: str | None = None,
    ) -> LocalCodingOutput: ...

    async def reduce_coding(
        self,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        model_config: ReportModelConfig,
        targets: Sequence[Target],
        turn_speakers: Mapping[str, Literal["worker", "client"]],
        scene: Scene,
        media: Media,
        active_target_briefs: Sequence[ActiveTargetBrief] = (),
        call_kind: ModelCallKind = ModelCallKind.initial,
        validation_feedback: str | None = None,
    ) -> GlobalCodingOutput: ...

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        model_config: ReportModelConfig,
        call_kind: ModelCallKind = ModelCallKind.initial,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput: ...


_MAP_SYSTEM_PROMPT = (
    "你负责对心理服务测评的一份公开冻结材料分片做局部质性编码。"
    "只做意义单元划分、开放编码、可观察方向记录和精确引用。"
    "每个局部单元至少提供一条 initial_codes 和一条 refs；"
    "对话与工作记录的 quote 必须是对应原文的连续子串。"
    "source_role 必须按该单元的主要来源准确标为 worker、client、interaction 或 work_record；"
    "技术中断只用于理解材料是否完整，不作为能力证据。"
    "overlap_turn_ids 只用于理解相邻语境，仍须精确引用。"
    "不读取量规、目标、隐藏状态、未披露事实或人工机会声明；"
    "不定级，不映射指标，不形成底线事件或紧迫风险结论。"
    "表达应专业、简洁、贴近材料，只输出约定 JSON。"
)
_REDUCE_SYSTEM_PROMPT = (
    "你负责汇总两份已通过程序校验的局部编码。"
    "只读取 local_outputs、完整 target_ids、公开的 active_target_briefs 和对应量规常量，"
    "不读取原始完整会谈记录、隐藏状态或未披露事实。"
    "完成意义单元合并去重、聚焦编码、指标映射，并为每个 target 形成唯一 CounterCheck。"
    "指标映射是多对多的：同一意义单元可以映射到多个 target，不能因为已经映射到一个 target 就停止。"
    "active_target_briefs 只表示本案例已经出现的公开观察任务，不代表受测者已经表现良好；"
    "不得自行判断专项模块是否启用，也不得以‘未启用’为由跳过其中的 target。"
    "对 active_target_briefs 中每个 target，coverage_decisions 必须且只能给出一项："
    "有任何可观察的支持、限制或不利证据时使用 evidence_mapped，并把证据写入 coded_evidence；"
    "确实没有可可靠编码的受测者行为时才使用 no_reliable_material，并说明缺少什么。"
    "每个 ReducedMeaningUnit 必须在 refs 中保留至少一条来自 LocalCodedUnit.refs 的精确引用；"
    "refs 必须继承局部编码已有引用，不得另从原始材料补造。"
    "所有 passthrough 单元都必须纳入聚焦审查：相关时完成映射，无关时可不保留；"
    "不能因‘待聚焦编码’直接当作证据。"
    "反例检索必须覆盖两个分片，跨分片冲突要保留两侧精确引用和同一冲突编号。"
    "coded_evidence 必须完整承载初始聚焦编码发现的正向与限制性能力证据；"
    "CounterCheck.found 只放与初始判断相反且未在 coded_evidence 出现的独立证据，"
    "not_found_note 只说明反例检索结论，不能把尚未编码的正向能力证据写在说明里代替 coded_evidence。"
    "整理语义底线候选和紧迫风险候选；材料冲突不得自动判为工作记录编造，"
    "fabricated_record 候选必须用 conflict_id 引用已形成的冲突。"
    "工作记录 planned_actions 表示会谈中讨论或拟采取的安排，不表示已经落实；"
    "不能仅因对话中没有完成该行动就形成材料冲突。"
    "必须按 turn_speakers 索引逐条区分受测者（worker）与来访者（client）；"
    "coded_evidence 和 CounterCheck.found 中的对话能力证据只能引用 worker 话轮；"
    "非 fabricated_record 的语义底线候选至少引用一条 worker 原话，可额外引用 client 反应；"
    "紧迫风险候选的 ref.turn_id 必须满足 turn_speakers[ref.turn_id]=client。"
    "不生成等级、总分、合格或通过判断，只输出约定 JSON。"
)
_EVIDENCE_ROLE_INSTRUCTION = (
    "DimensionPacket 中的 role 由规则侧赋值：primary 可直接支持定级；"
    "supporting 只能在存在 primary 时补充，不得单独抬高等级；"
    "cross_check 只用于核对冲突与一致性，不得作为正向代表证据。"
    "representative_units 和 limiting_units 只能逐字复制 "
    "DimensionPacket.units 中的 id，不能填摘要或原话，也不能自行命名。"
)
_PUBLIC_REPORT_LANGUAGE_INSTRUCTION = (
    "pattern、rationale、next_level_gap 和 evidence_confidence_factors "
    "是面向使用者的报告文字，只写自然、专业的中文，"
    "并用材料中的具体可观察行为解释结论和下一级差距。"
    "输入中的字段名、证据角色、类型名和执行过程只用于内部判断，"
    "不得把 level_ceiling、ceiling、primary、supporting、cross_check、"
    "DimensionPacket、target、indicator_id 等内部词写进这些文字，"
    "也不得写‘受限于 level_ceiling’这类系统说明。"
    "本项目评估的是初阶心理支持服务，不是心理治疗；"
    "使用支持关系、工作关系和后续行动等对应措辞，"
    "不把当前互动写成治疗关系、治疗联盟或治疗计划。"
)
_INTERACTION_SYSTEM_PROMPT = (
    "你负责互动过程组定级。只读取传入的 DimensionPacket 与对应量规，逐目标独立提出等级建议；"
    "不得读取或推测整通记录、案例隐藏状态，不得跨维度补偿，不得合成总分或合格判断。"
    + _EVIDENCE_ROLE_INSTRUCTION
    + "proposed_level 不得超过 DimensionPacket.level_ceiling；"
    "next_level_gap 必须描述最终等级到数字上的"
    "下一等级所缺的具体可观察行为；即使 ceiling=2，也要描述从2级到3级的缺口，"
    "但不得复用封顶前对更高等级的任何假设。"
    "规则侧只传入已确认有观察机会且证据充分的 DimensionPacket，"
    "因此每个 target 必须给出 0 至 level_ceiling 范围内的具体 proposed_level，禁止 null。"
    + _PUBLIC_REPORT_LANGUAGE_INSTRUCTION
    + "中文表达专业、简洁、自然，只输出约定 JSON。"
)
_PROFESSIONAL_SYSTEM_PROMPT = (
    "你负责专业判断组定级。只读取传入的 DimensionPacket 与对应量规，严格区分事实、推断和未知；"
    "不得读取或推测整通记录、案例隐藏状态，不得跨维度补偿，不得合成总分或合格判断。"
    + _EVIDENCE_ROLE_INSTRUCTION
    + "proposed_level 不得超过 DimensionPacket.level_ceiling；"
    "next_level_gap 必须描述最终等级到数字上的"
    "下一等级所缺的具体可观察行为；即使 ceiling=2，也要描述从2级到3级的缺口，"
    "但不得复用封顶前对更高等级的任何假设。"
    "规则侧只传入已确认有观察机会且证据充分的 DimensionPacket，"
    "因此每个 target 必须给出 0 至 level_ceiling 范围内的具体 proposed_level，禁止 null。"
    + _PUBLIC_REPORT_LANGUAGE_INSTRUCTION
    + "中文表达专业、简洁、自然，只输出约定 JSON。"
)
_SAFETY_SYSTEM_PROMPT = (
    "你负责安全组定级。只读取传入的 DimensionPacket 与对应量规，逐模块独立判断可观察行为；"
    "不得用隐藏风险事实补证，不得读取整通记录，不得跨模块补偿，不得合成总分或合格判断。"
    + _EVIDENCE_ROLE_INSTRUCTION
    + "proposed_level 不得超过 DimensionPacket.level_ceiling；"
    "next_level_gap 必须描述最终等级到数字上的"
    "下一等级所缺的具体可观察行为；即使 ceiling=2，也要描述从2级到3级的缺口，"
    "但不得复用封顶前对更高等级的任何假设。"
    "规则侧只传入已确认有观察机会且证据充分的 DimensionPacket，"
    "因此每个 target 必须给出 0 至 level_ceiling 范围内的具体 proposed_level，禁止 null。"
    + _PUBLIC_REPORT_LANGUAGE_INSTRUCTION
    + "中文表达专业、简洁、自然，只输出约定 JSON。"
)

_GROUP_PROMPTS = {
    ScoringGroup.interaction: _INTERACTION_SYSTEM_PROMPT,
    ScoringGroup.professional: _PROFESSIONAL_SYSTEM_PROMPT,
    ScoringGroup.safety: _SAFETY_SYSTEM_PROMPT,
}

_REPORT_REPAIR_PROMPT = (
    "上一份 JSON 未通过校验。请重新输出完整 JSON，并根据同一份材料完整重做，"
    "不要只返回修补片段。校验错误："
)
_POST_VALIDATION_REPAIR_PROMPT = "程序后置校验错误："

REPORT_PROMPT_BUNDLE: dict[str, Any] = {
    "bundle_id": "report_map_reduce_and_three_groups",
    "prompts": {
        PromptFamily.report_map.value: _MAP_SYSTEM_PROMPT,
        PromptFamily.report_reduce.value: _REDUCE_SYSTEM_PROMPT,
        PromptFamily.report_interaction.value: _INTERACTION_SYSTEM_PROMPT,
        PromptFamily.report_professional.value: _PROFESSIONAL_SYSTEM_PROMPT,
        PromptFamily.report_safety.value: _SAFETY_SYSTEM_PROMPT,
        "report_repair": _REPORT_REPAIR_PROMPT,
    },
    "output_contracts": {
        "map": LocalCodingOutput.model_json_schema(),
        "reduce": ReduceModelOutput.model_json_schema(),
        "group": GroupModelOutput.model_json_schema(),
    },
}


def _stable_block(payload: object, *, use_explicit_cache: bool) -> dict[str, object]:
    block: dict[str, object] = {
        "type": "text",
        "text": json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    if use_explicit_cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


class ReportProvider(_StructuredTextProvider):
    def __init__(
        self,
        credential_store: RuntimeCredentialStore,
        *,
        client: Any | None = None,
        recorder: ModelCallRecorder | None = None,
        failure_recorder: RuntimeFailureRecorder | None = None,
    ) -> None:
        super().__init__(
            credential_store,
            client=client,
            recorder=recorder,
            failure_recorder=failure_recorder,
            request_timeout_seconds=300,
        )
        self._repair_feedback: dict[tuple[str, ...], str] = {}

    @staticmethod
    def _format_repair_feedback(error: RepairableModelOutputError) -> str:
        details = exception_failure_details(error)
        validation = details.get("validation")
        issues: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        if isinstance(validation, list):
            for item in validation:
                if not isinstance(item, dict):
                    continue
                message = str(item.get("msg", "结构不符合约定"))
                if "意义单元至少引用一种实际材料" in message:
                    message += (
                        "；每个 MeaningUnit 必须从所合并的 LocalCodedUnit.refs 继承来源，"
                        "把对话引用的 turn_id 写入 MeaningUnit.turn_ids，"
                        "把工作记录引用原样写入 work_record_refs，二者不得同时为空"
                    )
                issue_type = str(item.get("type", "validation_error"))
                signature = (issue_type, message)
                if signature in seen:
                    continue
                seen.add(signature)
                issues.append(
                    {
                        "loc": item.get("loc", []),
                        "type": issue_type,
                        "msg": message,
                    }
                )
        if not issues:
            issues.append({"msg": "输出无法按约定结构读取"})
        return _REPORT_REPAIR_PROMPT + json.dumps(
            issues,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _remember_repair_feedback(
        self,
        key: tuple[str, ...],
        error: RepairableModelOutputError,
    ) -> None:
        self._repair_feedback[key] = self._format_repair_feedback(error)

    @staticmethod
    def _merge_repair_feedback(
        schema_feedback: str | None,
        validation_feedback: str | None,
    ) -> str | None:
        parts = [part for part in (schema_feedback, validation_feedback and (
            _POST_VALIDATION_REPAIR_PROMPT + validation_feedback
        )) if part]
        return "\n".join(parts) or None

    async def code_shard(
        self,
        shard: CodingShard,
        *,
        session_id: str,
        model_config: ReportModelConfig,
        call_kind: ModelCallKind = ModelCallKind.initial,
        validation_feedback: str | None = None,
    ) -> LocalCodingOutput:
        repair_key = (session_id, PromptFamily.report_map.value, shard.shard_id)
        schema_feedback = (
            self._repair_feedback.get(repair_key)
            if call_kind is ModelCallKind.repair
            else None
        )
        repair_feedback = self._merge_repair_feedback(
            schema_feedback, validation_feedback
        )
        use_cache = _supports_explicit_cache(model_config.report_model)
        messages = self._map_messages(
            shard,
            use_explicit_cache=use_cache,
            repair_feedback=repair_feedback,
        )
        try:
            try:
                result = await self._complete(
                    model=model_config.report_model,
                    temperature=model_config.report_temperature,
                    messages=messages,
                    output_type=LocalCodingOutput,
                    enable_thinking=False,
                    session_id=session_id,
                    model_role=ModelRole.report,
                    prompt_family=PromptFamily.report_map,
                    call_kind=call_kind,
                    cache_mode=CacheMode.explicit if use_cache else CacheMode.none,
                )
            except ExplicitCacheRejectedError:
                if not use_cache:
                    raise
                result = await self._complete(
                    model=model_config.report_model,
                    temperature=model_config.report_temperature,
                    messages=self._map_messages(
                        shard,
                        use_explicit_cache=False,
                        repair_feedback=repair_feedback,
                    ),
                    output_type=LocalCodingOutput,
                    enable_thinking=False,
                    session_id=session_id,
                    model_role=ModelRole.report,
                    prompt_family=PromptFamily.report_map,
                    call_kind=call_kind,
                    cache_mode=CacheMode.none,
                )
        except RepairableModelOutputError as error:
            self._remember_repair_feedback(repair_key, error)
            raise
        self._repair_feedback.pop(repair_key, None)
        return result

    async def reduce_coding(
        self,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        model_config: ReportModelConfig,
        targets: Sequence[Target],
        turn_speakers: Mapping[str, Literal["worker", "client"]],
        scene: Scene,
        media: Media,
        active_target_briefs: Sequence[ActiveTargetBrief] = (),
        call_kind: ModelCallKind = ModelCallKind.initial,
        validation_feedback: str | None = None,
    ) -> GlobalCodingOutput:
        shard_ids = [output.shard_id for output in local_outputs]
        if len(shard_ids) != 2 or len(set(shard_ids)) != 2:
            raise ValueError("Reduce 必须接收两份不同分片的局部编码")
        requested_targets = tuple(targets)
        core_targets = tuple(CoreDimension)
        if requested_targets[: len(core_targets)] != core_targets:
            raise ValueError("Reduce target 必须先完整包含九个共通能力维度")
        if len(requested_targets) != len(set(requested_targets)) or any(
            not isinstance(target, SpecialModule)
            for target in requested_targets[len(core_targets) :]
        ):
            raise ValueError("Reduce target 的专项模块必须唯一且有效")
        active_targets = [brief.target for brief in active_target_briefs]
        if len(active_targets) != len(set(active_targets)):
            raise ValueError("active_target_briefs 中每个 target 必须唯一")
        if not set(active_targets).issubset(targets):
            raise ValueError("active_target_briefs 包含未纳入本次 Reduce 的 target")
        repair_key = (session_id, PromptFamily.report_reduce.value)
        schema_feedback = (
            self._repair_feedback.get(repair_key)
            if call_kind is ModelCallKind.repair
            else None
        )
        repair_feedback = self._merge_repair_feedback(
            schema_feedback, validation_feedback
        )
        use_cache = _supports_explicit_cache(model_config.report_model)
        messages = self._reduce_messages(
            local_outputs,
            targets,
            turn_speakers,
            active_target_briefs,
            scene=scene,
            media=media,
            use_explicit_cache=use_cache,
            repair_feedback=repair_feedback,
        )
        try:
            try:
                result = await self._complete(
                    model=model_config.report_model,
                    temperature=model_config.report_temperature,
                    messages=messages,
                    output_type=ReduceModelOutput,
                    enable_thinking=False,
                    session_id=session_id,
                    model_role=ModelRole.report,
                    prompt_family=PromptFamily.report_reduce,
                    call_kind=call_kind,
                    cache_mode=CacheMode.explicit if use_cache else CacheMode.none,
                )
            except ExplicitCacheRejectedError:
                if not use_cache:
                    raise
                result = await self._complete(
                    model=model_config.report_model,
                    temperature=model_config.report_temperature,
                    messages=self._reduce_messages(
                        local_outputs,
                        targets,
                        turn_speakers,
                        active_target_briefs,
                        scene=scene,
                        media=media,
                        use_explicit_cache=False,
                        repair_feedback=repair_feedback,
                    ),
                    output_type=ReduceModelOutput,
                    enable_thinking=False,
                    session_id=session_id,
                    model_role=ModelRole.report,
                    prompt_family=PromptFamily.report_reduce,
                    call_kind=call_kind,
                    cache_mode=CacheMode.none,
                )
        except RepairableModelOutputError as error:
            self._remember_repair_feedback(repair_key, error)
            raise
        _validate_reduce_targets(result, targets)
        _validate_active_target_coverage(result, active_target_briefs)
        _require_reduced_unit_refs_in_local_outputs(local_outputs, result)
        self._repair_feedback.pop(repair_key, None)
        return result.to_global_output()

    async def score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        model_config: ReportModelConfig,
        call_kind: ModelCallKind = ModelCallKind.initial,
        validation_feedback: str | None = None,
    ) -> GroupScoringOutput:
        prompt_family = GROUP_PROMPT_FAMILIES[group]
        repair_key = (session_id, prompt_family.value)
        schema_feedback = (
            self._repair_feedback.get(repair_key)
            if call_kind is ModelCallKind.repair
            else None
        )
        repair_feedback = self._merge_repair_feedback(
            schema_feedback, validation_feedback
        )
        use_cache = _supports_explicit_cache(model_config.report_model)
        messages = self._group_messages(
            group,
            packets,
            use_explicit_cache=use_cache,
            repair_feedback=repair_feedback,
        )
        try:
            try:
                result = await self._complete(
                    model=model_config.report_model,
                    temperature=model_config.report_temperature,
                    messages=messages,
                    output_type=GroupModelOutput,
                    enable_thinking=False,
                    session_id=session_id,
                    model_role=ModelRole.report,
                    prompt_family=prompt_family,
                    call_kind=call_kind,
                    cache_mode=CacheMode.explicit if use_cache else CacheMode.none,
                )
            except ExplicitCacheRejectedError:
                if not use_cache:
                    raise
                result = await self._complete(
                    model=model_config.report_model,
                    temperature=model_config.report_temperature,
                    messages=self._group_messages(
                        group,
                        packets,
                        use_explicit_cache=False,
                        repair_feedback=repair_feedback,
                    ),
                    output_type=GroupModelOutput,
                    enable_thinking=False,
                    session_id=session_id,
                    model_role=ModelRole.report,
                    prompt_family=prompt_family,
                    call_kind=call_kind,
                    cache_mode=CacheMode.none,
                )
        except RepairableModelOutputError as error:
            self._remember_repair_feedback(repair_key, error)
            raise
        self._repair_feedback.pop(repair_key, None)
        return result.to_group_scoring_output()

    @staticmethod
    def _map_messages(
        shard: CodingShard,
        *,
        use_explicit_cache: bool,
        repair_feedback: str | None = None,
    ) -> list[dict[str, object]]:
        stable_payload = {
            "task": "map_qualitative_coding",
            "output_contract": LocalCodingOutput.model_json_schema(),
        }
        messages: list[dict[str, object]] = [
            {"role": "system", "content": _MAP_SYSTEM_PROMPT},
            {
                "role": "system",
                "content": [
                    _stable_block(
                        stable_payload,
                        use_explicit_cache=use_explicit_cache,
                    )
                ],
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"coding_shard": shard.model_dump(mode="json")},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
        if repair_feedback:
            messages.append({"role": "user", "content": repair_feedback})
        return messages

    @staticmethod
    def _reduce_messages(
        local_outputs: Sequence[LocalCodingOutput],
        targets: Sequence[Target],
        turn_speakers: Mapping[str, Literal["worker", "client"]],
        active_target_briefs: Sequence[ActiveTargetBrief],
        *,
        scene: Scene,
        media: Media,
        use_explicit_cache: bool,
        repair_feedback: str | None = None,
    ) -> list[dict[str, object]]:
        stable_payload = {
            "task": "reduce_qualitative_coding",
            "rubrics": {
                target.value: get_rubric(target, media=media).model_dump(mode="json")
                for target in targets
            },
            "output_contract": ReduceModelOutput.model_json_schema(),
        }
        messages: list[dict[str, object]] = [
            {"role": "system", "content": _REDUCE_SYSTEM_PROMPT},
            {
                "role": "system",
                "content": [
                    _stable_block(
                        stable_payload,
                        use_explicit_cache=use_explicit_cache,
                    )
                ],
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "scene": scene.value,
                        "media": media.value,
                        "target_ids": [target.value for target in targets],
                        "active_target_briefs": [
                            brief.model_dump(mode="json")
                            for brief in active_target_briefs
                        ],
                        "turn_speakers": dict(turn_speakers),
                        "local_outputs": [
                            output.model_dump(mode="json") for output in local_outputs
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
        if repair_feedback:
            messages.append({"role": "user", "content": repair_feedback})
        return messages

    @staticmethod
    def _group_messages(
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        use_explicit_cache: bool,
        repair_feedback: str | None = None,
    ) -> list[dict[str, object]]:
        stable_payload = {
            "task": f"score_{group.value}_group",
            "rubrics": {
                packet.target.value: packet.rubric.model_dump(mode="json")
                for packet in packets
            },
            "output_contract": GroupModelOutput.model_json_schema(),
        }
        dynamic_packets = [
            packet.model_dump(mode="json", exclude={"rubric"}) for packet in packets
        ]
        messages: list[dict[str, object]] = [
            {"role": "system", "content": _GROUP_PROMPTS[group]},
            {
                "role": "system",
                "content": [
                    _stable_block(
                        stable_payload,
                        use_explicit_cache=use_explicit_cache,
                    )
                ],
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"dimension_packets": dynamic_packets},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
        if repair_feedback:
            messages.append({"role": "user", "content": repair_feedback})
        return messages

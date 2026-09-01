from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.reports.job_inputs import CodingInput, CodingShard, OpportunityCheckInput
from app.reports.jobs import canonical_fingerprint
from app.reports.models import ReportJobRecord
from app.reports.report_pipeline import (
    MAX_BATCH_ATTEMPTS,
    MAX_REDUCE_ATTEMPTS,
    ReportPipeline,
    ValidatedTarget,
    _build_validated_targets,
    _known_urgent_termination_event,
    _validate_global_contract,
    _validate_group_output,
    _validate_local_contract,
    _validate_reduce_source_closure,
    check_opportunities,
    split_coding_input,
)
from app.reports.report_provider import (
    ActiveTargetBrief,
    GlobalCodingOutput,
    GroupScoringOutput,
    LocalCodingOutput,
    ReportModelConfig,
    ReportModelGateway,
    ScoringGroup,
)
from app.reports.scoring_domain import (
    BottomLineCategory,
    CodedEvidence,
    CoreDimension,
    DimensionPacket,
    DimensionResult,
    EvidenceDirection,
    MaterialConflict,
    Target,
    UnscoredReason,
)
from app.reports.scoring_rules import (
    semantic_bottom_line_events,
    validate_material_conflict_candidates,
)
from app.runtime.models import ModelCallKind
from app.runtime.providers import NonRetryableRuntimeModelError
from app.sessions.models import TurnSpeaker


class StabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CacheUsageSnapshot(StabilityModel):
    observed: bool = False
    prompt_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)


class CacheUsageReader(Protocol):
    def __call__(self, session_id: str) -> CacheUsageSnapshot: ...


class StabilityMaterial(StabilityModel):
    """一次稳定性检查使用的完整冻结材料及其不可比条件。"""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    material_id: str = Field(min_length=1)
    coding_input: CodingInput
    opportunity_check_input: OpportunityCheckInput
    model_configuration: ReportModelConfig = Field(alias="model_config")
    model_version: str = Field(min_length=1)
    prompt_fingerprint: str = Field(min_length=1)
    rubric_fingerprint: str = Field(min_length=1)
    case_package_fingerprint: str = Field(min_length=1)
    input_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_matching_input_fingerprint(self) -> Self:
        actual = canonical_fingerprint(
            {
                "coding_input": self.coding_input.model_dump(mode="json"),
                "opportunity_check_input": self.opportunity_check_input.model_dump(
                    mode="json"
                ),
            }
        )
        if actual != self.input_fingerprint:
            raise ValueError("input fingerprint 与冻结评分材料不一致")
        return self

    @classmethod
    def from_report_job(
        cls,
        job: ReportJobRecord,
        *,
        material_id: str,
        model_version: str,
    ) -> Self:
        return cls(
            material_id=material_id,
            coding_input=CodingInput.model_validate(job.frozen_input_json),
            opportunity_check_input=OpportunityCheckInput.model_validate(
                job.opportunity_check_json
            ),
            model_config=ReportModelConfig.model_validate(job.model_snapshot),
            model_version=model_version,
            prompt_fingerprint=job.prompt_fingerprint,
            rubric_fingerprint=job.rubric_fingerprint,
            case_package_fingerprint=job.case_package_fingerprint,
            input_fingerprint=job.frozen_input_fingerprint,
        )


class TargetRunObservation(StabilityModel):
    level: int | None = Field(default=None, ge=0, le=4)
    unscored_reason: UnscoredReason | None = None
    representative_evidence_fingerprints: list[str] = Field(default_factory=list)
    evidence_directions: dict[str, EvidenceDirection] = Field(default_factory=dict)


class RunObservation(StabilityModel):
    targets: dict[Target, TargetRunObservation]
    bottom_line_categories: set[BottomLineCategory] = Field(default_factory=set)


class TargetStability(StabilityModel):
    levels: list[int | None]
    unscored_reasons: list[UnscoredReason | None]
    modal_level: int | None
    exact_agreement: float = Field(ge=0, le=1)
    within_one_level: float = Field(ge=0, le=1)
    evidence_jaccard: float = Field(ge=0, le=1)
    direction_consistency: float = Field(ge=0, le=1)


class StabilitySummary(StabilityModel):
    per_target: dict[str, TargetStability]
    bottom_line_occurrence: dict[str, int]
    unscored_reason_consistency: float = Field(ge=0, le=1)


class RunStabilityResult(StabilitySummary):
    material_id: str
    runs: int = Field(ge=2)
    mode: Literal["full"] = "full"
    model_id: str
    model_version: str
    sampling_params: dict[str, float]
    prompt_fingerprint: str
    rubric_fingerprint: str
    case_package_fingerprint: str
    input_fingerprint: str
    cache_usage_by_run: list[CacheUsageSnapshot]
    ran_at: datetime


def _pairwise_average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 1.0


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _direction_agreement(
    left: Mapping[str, EvidenceDirection],
    right: Mapping[str, EvidenceDirection],
) -> float:
    identities = set(left) | set(right)
    if not identities:
        return 1.0
    matched = sum(
        1
        for identity in identities
        if identity in left and identity in right and left[identity] is right[identity]
    )
    return matched / len(identities)


def _modal_agreement[T](values: Sequence[T]) -> float:
    counts = Counter(values)
    return max(counts.values()) / len(values)


def _modal_level(values: Sequence[int | None]) -> tuple[int | None, float]:
    counts = Counter(values)
    maximum = max(counts.values())
    candidates = [value for value, count in counts.items() if count == maximum]
    # 三次运行全部分歧时，取中位等级用于 ±1 计算；原始 levels 仍完整保留。
    numeric_candidates = sorted(value for value in candidates if value is not None)
    selected = (
        numeric_candidates[len(numeric_candidates) // 2]
        if numeric_candidates
        else None
    )
    return selected, maximum / len(values)


def summarize_stability(observations: Sequence[RunObservation]) -> StabilitySummary:
    if len(observations) < 2:
        raise ValueError("运行稳定性检查至少需要两次完整运行")
    targets = set(observations[0].targets)
    if any(set(item.targets) != targets for item in observations[1:]):
        raise ValueError("每次运行必须包含完全相同的评分目标")

    per_target: dict[str, TargetStability] = {}
    unscored_consistencies: list[float] = []
    for target in sorted(targets, key=lambda item: item.value):
        target_runs = [item.targets[target] for item in observations]
        levels = [item.level for item in target_runs]
        modal_level, exact_agreement = _modal_level(levels)
        if modal_level is None:
            within_one = exact_agreement
        else:
            within_one = sum(
                level is not None and abs(level - modal_level) <= 1 for level in levels
            ) / len(levels)
        evidence_jaccard = _pairwise_average(
            [
                _jaccard(
                    set(left.representative_evidence_fingerprints),
                    set(right.representative_evidence_fingerprints),
                )
                for left, right in combinations(target_runs, 2)
            ]
        )
        direction_consistency = _pairwise_average(
            [
                _direction_agreement(left.evidence_directions, right.evidence_directions)
                for left, right in combinations(target_runs, 2)
            ]
        )
        unscored_reasons = [item.unscored_reason for item in target_runs]
        if any(reason is not None for reason in unscored_reasons):
            unscored_consistencies.append(_modal_agreement(unscored_reasons))
        per_target[target.value] = TargetStability(
            levels=levels,
            unscored_reasons=unscored_reasons,
            modal_level=modal_level,
            exact_agreement=exact_agreement,
            within_one_level=within_one,
            evidence_jaccard=evidence_jaccard,
            direction_consistency=direction_consistency,
        )

    bottom_line_occurrence = {
        category.value: sum(
            category in observation.bottom_line_categories for observation in observations
        )
        for category in BottomLineCategory
    }
    return StabilitySummary(
        per_target=per_target,
        bottom_line_occurrence=bottom_line_occurrence,
        unscored_reason_consistency=(
            sum(unscored_consistencies) / len(unscored_consistencies)
            if unscored_consistencies
            else 1.0
        ),
    )


def _evidence_identity(evidence: CodedEvidence) -> str:
    return canonical_fingerprint(
        {
            "indicator_id": evidence.indicator_id,
            "ref": evidence.ref.model_dump(mode="json"),
        }
    )


class StabilityRunner:
    def __init__(
        self,
        gateway: ReportModelGateway,
        *,
        cache_usage_reader: CacheUsageReader | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway = gateway
        self._cache_usage_reader = cache_usage_reader
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(
        self,
        material: StabilityMaterial,
        *,
        runs: int = 3,
        output_path: Path | None = None,
    ) -> RunStabilityResult:
        if runs < 2:
            raise ValueError("运行稳定性检查至少需要两次完整运行")
        observations: list[RunObservation] = []
        cache_usage: list[CacheUsageSnapshot] = []
        for index in range(runs):
            session_id = f"stability:{material.material_id}:{index + 1}"
            observations.append(await self._run_full(material, session_id=session_id))
            cache_usage.append(
                self._cache_usage_reader(session_id)
                if self._cache_usage_reader is not None
                else CacheUsageSnapshot()
            )

        summary = summarize_stability(observations)
        result = RunStabilityResult(
            material_id=material.material_id,
            runs=runs,
            model_id=material.model_configuration.report_model,
            model_version=material.model_version,
            sampling_params={
                "temperature": material.model_configuration.report_temperature
            },
            prompt_fingerprint=material.prompt_fingerprint,
            rubric_fingerprint=material.rubric_fingerprint,
            case_package_fingerprint=material.case_package_fingerprint,
            input_fingerprint=material.input_fingerprint,
            cache_usage_by_run=cache_usage,
            ran_at=self._clock(),
            **summary.model_dump(),
        )
        self._write_result(result, output_path)
        return result

    async def _run_full(
        self,
        material: StabilityMaterial,
        *,
        session_id: str,
    ) -> RunObservation:
        map_results = await asyncio.gather(
            *(
                self._code_shard(
                    shard,
                    session_id=session_id,
                    model_config=material.model_configuration,
                )
                for shard in split_coding_input(material.coding_input)
            ),
            return_exceptions=True,
        )
        map_errors = [
            result for result in map_results if isinstance(result, BaseException)
        ]
        if map_errors:
            details = "; ".join(
                f"{type(error).__name__}: {error}" for error in map_errors
            )
            raise RuntimeError(f"局部编码失败：{details}")
        local_outputs = [
            result for result in map_results if isinstance(result, LocalCodingOutput)
        ]
        opportunity_result = check_opportunities(
            material.coding_input,
            material.opportunity_check_input,
        )
        targets: list[Target] = [
            *CoreDimension,
            *opportunity_result.activated_modules,
        ]
        global_output = await self._reduce_coding(
            material,
            local_outputs,
            session_id=session_id,
            targets=targets,
            active_target_briefs=opportunity_result.active_target_briefs,
        )
        validated = _build_validated_targets(
            material.coding_input,
            opportunity_result,
            global_output,
            targets,
        )
        group_outputs = await asyncio.gather(
            *(
                self._score_group(
                    group,
                    ReportPipeline._packets_for_group(group, validated),
                    session_id=session_id,
                    model_config=material.model_configuration,
                )
                for group in ScoringGroup
            )
        )
        dimensions = ReportPipeline._assemble_dimensions(
            targets,
            validated,
            {
                group: output
                for group, output in zip(ScoringGroup, group_outputs, strict=True)
            },
            set(),
        )
        bottom_line_categories = self._bottom_line_categories(
            material.coding_input,
            validated,
            global_output,
        )
        return RunObservation(
            targets={
                result.target: self._target_observation(result, global_output)
                for result in dimensions
            },
            bottom_line_categories=bottom_line_categories,
        )

    async def _code_shard(
        self,
        shard: CodingShard,
        *,
        session_id: str,
        model_config: ReportModelConfig,
    ) -> LocalCodingOutput:
        last_error: Exception | None = None
        validation_feedback: str | None = None
        for attempt in range(MAX_BATCH_ATTEMPTS):
            try:
                output = await self._gateway.code_shard(
                    shard,
                    session_id=session_id,
                    model_config=model_config,
                    call_kind=(
                        ModelCallKind.initial if attempt == 0 else ModelCallKind.repair
                    ),
                    validation_feedback=validation_feedback,
                )
                _validate_local_contract(shard, output)
                return output
            except NonRetryableRuntimeModelError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                validation_feedback = str(exc)
        raise RuntimeError(f"{shard.shard_id} 局部编码失败：{last_error}") from last_error

    async def _reduce_coding(
        self,
        material: StabilityMaterial,
        local_outputs: Sequence[LocalCodingOutput],
        *,
        session_id: str,
        targets: Sequence[Target],
        active_target_briefs: Sequence[ActiveTargetBrief],
    ) -> GlobalCodingOutput:
        last_error: Exception | None = None
        validation_feedback: str | None = None
        turn_speakers: dict[str, Literal["worker", "client"]] = {
            turn.turn_id: (
                "worker" if turn.speaker is TurnSpeaker.worker else "client"
            )
            for turn in material.coding_input.turns
        }
        for attempt in range(MAX_REDUCE_ATTEMPTS):
            try:
                output = await self._gateway.reduce_coding(
                    local_outputs,
                    session_id=session_id,
                    model_config=material.model_configuration,
                    targets=targets,
                    turn_speakers=turn_speakers,
                    scene=material.coding_input.session.scene,
                    media=material.coding_input.session.media,
                    active_target_briefs=active_target_briefs,
                    call_kind=(
                        ModelCallKind.initial if attempt == 0 else ModelCallKind.repair
                    ),
                    validation_feedback=validation_feedback,
                )
                _validate_global_contract(
                    material.coding_input,
                    output,
                    targets,
                )
                _validate_reduce_source_closure(local_outputs, output)
                return output
            except NonRetryableRuntimeModelError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                validation_feedback = str(exc)
        raise RuntimeError(f"聚焦汇总失败：{last_error}") from last_error

    async def _score_group(
        self,
        group: ScoringGroup,
        packets: Sequence[DimensionPacket],
        *,
        session_id: str,
        model_config: ReportModelConfig,
    ) -> GroupScoringOutput:
        last_error: Exception | None = None
        validation_feedback: str | None = None
        for attempt in range(MAX_BATCH_ATTEMPTS):
            try:
                output = await self._gateway.score_group(
                    group,
                    packets,
                    session_id=session_id,
                    model_config=model_config,
                    call_kind=(
                        ModelCallKind.initial if attempt == 0 else ModelCallKind.repair
                    ),
                    validation_feedback=validation_feedback,
                )
                _validate_group_output(output, packets)
                return output
            except NonRetryableRuntimeModelError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                validation_feedback = str(exc)
        raise RuntimeError(f"{group.value} 定级失败：{last_error}") from last_error

    @staticmethod
    def _target_observation(
        result: DimensionResult,
        global_output: GlobalCodingOutput,
    ) -> TargetRunObservation:
        all_coded_evidence = [
            item
            for item in global_output.coded_evidence
            if item.target == result.target
        ]
        for counter_check in global_output.counter_checks:
            if counter_check.target == result.target:
                all_coded_evidence.extend(counter_check.found)
        representative_unit_ids = set(result.representative_unit_ids)
        return TargetRunObservation(
            level=result.level,
            unscored_reason=result.unscored_reason,
            representative_evidence_fingerprints=[
                _evidence_identity(item)
                for item in result.evidence
                if item.unit_id in representative_unit_ids
            ],
            evidence_directions={
                _evidence_identity(item): item.direction for item in all_coded_evidence
            },
        )

    @staticmethod
    def _bottom_line_categories(
        coding_input: CodingInput,
        validated: Mapping[Target, ValidatedTarget],
        global_output: GlobalCodingOutput,
    ) -> set[BottomLineCategory]:
        dialogue_turns = {turn.turn_id: turn.text for turn in coding_input.turns}
        rule_conflicts: list[MaterialConflict] = []
        semantic_conflicts = validate_material_conflict_candidates(
            global_output.material_conflict_candidates,
            dialogue_turns=dialogue_turns,
            work_record=coding_input.work_record,
        )
        semantic = semantic_bottom_line_events(
            global_output.bottom_line_candidates,
            dialogue_turns=dialogue_turns,
            work_record=coding_input.work_record,
            audio_event_ids=set(),
            rule_conflicts=rule_conflicts,
            semantic_conflicts=semantic_conflicts,
        )
        categories = {event.category for event in semantic.events}
        urgent = _known_urgent_termination_event(
            coding_input,
            validated,
            global_output,
        )
        if urgent is not None:
            categories.add(urgent.category)
        return categories

    def _write_result(
        self,
        result: RunStabilityResult,
        output_path: Path | None,
    ) -> Path:
        path = output_path or self._default_output_path(result)
        if path.suffix.lower() != ".json":
            path = path / f"{result.material_id}-{result.ran_at:%Y%m%dT%H%M%SZ}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _default_output_path(result: RunStabilityResult) -> Path:
        project_root = Path(__file__).resolve().parents[3]
        return project_root / "output" / "stability" / (
            f"{result.material_id}-{result.ran_at:%Y%m%dT%H%M%SZ}.json"
        )

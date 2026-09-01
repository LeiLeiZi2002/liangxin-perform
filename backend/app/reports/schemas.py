import re
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.reports.competency_rubric import get_rubric
from app.reports.models import (
    PlannedAction,
    ReferralDecision,
    ReportDraftStatus,
    ReportJobStage,
    RiskLevel,
)
from app.reports.scoring_domain import (
    BottomLineEvent,
    CodedEvidence,
    DimensionResult,
    MaterialConflict,
    ResultSummary,
    Target,
)
from app.sessions.models import Media, Scene

RequiredText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]
ListText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class RubricDocumentRead(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    title: str
    markdown: str


def _unique[T](items: list[T]) -> list[T]:
    return list(dict.fromkeys(items))


class WorkRecordUpsert(BaseModel):
    problem_understanding: RequiredText
    risk_level: RiskLevel
    risk_reasoning: RequiredText
    risk_evidence_turn_ids: list[ListText] = Field(default_factory=list, max_length=100)
    missing_information: list[ListText] = Field(default_factory=list, max_length=50)
    planned_actions: list[PlannedAction] = Field(default_factory=list, max_length=20)
    referral_decision: ReferralDecision
    supervision_decision: bool
    follow_up: RequiredText
    limitations: RequiredText

    @field_validator("risk_evidence_turn_ids", "missing_information", "planned_actions")
    @classmethod
    def deduplicate_lists[T](cls, value: list[T]) -> list[T]:
        return _unique(value)


class WorkRecordRead(WorkRecordUpsert):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    created_at: datetime
    updated_at: datetime


class ReportJobRead(BaseModel):
    id: str
    session_id: str
    stage: ReportJobStage
    progress_percent: int = Field(ge=0, le=100)
    partial: bool
    retryable: bool
    report_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: object) -> Self:
        from app.reports.models import ReportJobRecord

        if not isinstance(record, ReportJobRecord):
            raise TypeError("record must be ReportJobRecord")
        progress_by_stage = {
            ReportJobStage.queued: 0,
            ReportJobStage.coding: 20,
            ReportJobStage.scoring: min(80, 35 + 15 * len(record.scoring_groups_done)),
            ReportJobStage.assembling: 90,
            ReportJobStage.succeeded: 100,
            ReportJobStage.partial: 100,
            ReportJobStage.failed: 0,
        }
        return cls(
            id=record.id,
            session_id=record.session_id,
            stage=record.stage,
            progress_percent=progress_by_stage[record.stage],
            partial=record.stage is ReportJobStage.partial,
            retryable=record.stage in {ReportJobStage.failed, ReportJobStage.partial},
            report_id=record.report_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class DimensionReportRead(BaseModel):
    target: Target
    name: str
    level_anchor: str | None
    result: DimensionResult

    @classmethod
    def from_result(
        cls,
        result: DimensionResult,
        *,
        media: Media | None = None,
        unit_ids: list[str] | None = None,
    ) -> Self:
        rubric = get_rubric(result.target, media=media)
        return cls(
            target=result.target,
            name=rubric.name,
            level_anchor=(rubric.anchors[result.level] if result.level is not None else None),
            result=_public_dimension_result(result, unit_ids=unit_ids),
        )


def _dimension_unit_ids(result: DimensionResult) -> list[str]:
    return list(
        dict.fromkeys(
            [
                *(item.unit_id for item in result.evidence),
                *(item.unit_id for item in result.counter_evidence),
                *result.representative_unit_ids,
                *result.limiting_unit_ids,
            ]
        )
    )


def _public_dimension_result(
    result: DimensionResult,
    *,
    unit_ids: list[str] | None = None,
) -> DimensionResult:
    unit_ids = _dimension_unit_ids(result) if unit_ids is None else unit_ids
    if not unit_ids:
        return result

    def clean(text: str) -> str:
        return _hide_internal_unit_ids(text, unit_ids)

    def clean_evidence(item: CodedEvidence) -> CodedEvidence:
        return item.model_copy(
            update={
                "context": clean(item.context),
                "alternative_reading": (
                    clean(item.alternative_reading)
                    if item.alternative_reading is not None
                    else None
                ),
            }
        )

    return result.model_copy(
        update={
            "pattern": clean(result.pattern),
            "rationale": clean(result.rationale),
            "evidence": [clean_evidence(item) for item in result.evidence],
            "counter_evidence": [clean_evidence(item) for item in result.counter_evidence],
            "evidence_confidence_factors": [
                clean(item) for item in result.evidence_confidence_factors
            ],
            "next_level_gap": [clean(item) for item in result.next_level_gap],
        }
    )


def _hide_internal_unit_ids(text: str, unit_ids: list[str]) -> str:
    if not text or not unit_ids:
        return text
    escaped_ids = [re.escape(unit_id) for unit_id in sorted(unit_ids, key=len, reverse=True)]
    reference = "(?:" + "|".join(escaped_ids) + ")"
    grouped_references = reference + rf"(?:\s*[\u3001,\uff0c;\uff1b/|]\s*{reference})*"
    cleaned = re.sub(rf"[\uff08(]\s*{grouped_references}\s*[\uff09)]", "", text)
    for unit_id in sorted(unit_ids, key=len, reverse=True):
        cleaned = cleaned.replace(unit_id, "对应材料")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])", "", cleaned)
    cleaned = re.sub(r"\s+([\uff0c。\uff1b\uff1a\uff01\uff1f、,.!?;:])", r"\1", cleaned)
    return cleaned.strip()


class ReportRead(BaseModel):
    id: str
    session_id: str
    job_id: str
    case_id: str
    scene: Scene
    media: Media
    summary: ResultSummary
    dimensions: list[DimensionReportRead]
    bottom_line_events: list[BottomLineEvent]
    material_conflicts: list[MaterialConflict]
    screening_gap: bool
    disclaimers: list[str]
    rubric_fingerprint: str
    case_package_fingerprint: str
    model_fingerprint: str
    prompt_fingerprint: str
    input_fingerprint: str
    ai_draft_status: ReportDraftStatus
    created_at: datetime

    @classmethod
    def from_record(cls, record: object) -> Self:
        from app.reports.models import ReportRecord

        if not isinstance(record, ReportRecord):
            raise TypeError("record must be ReportRecord")
        results = [
            DimensionResult.model_validate(item) for item in record.dimensions_json
        ]
        unit_ids = list(
            dict.fromkeys(
                unit_id
                for result in results
                for unit_id in _dimension_unit_ids(result)
            )
        )

        def clean(text: str) -> str:
            return _hide_internal_unit_ids(text, unit_ids)

        def clean_event(event: BottomLineEvent) -> BottomLineEvent:
            return event.model_copy(
                update={
                    "description": clean(event.description),
                    "reasoning": clean(event.reasoning),
                }
            )

        def clean_conflict(conflict: MaterialConflict) -> MaterialConflict:
            return conflict.model_copy(
                update={
                    "description": clean(conflict.description),
                    "impact": clean(conflict.impact),
                }
            )

        summary = ResultSummary.model_validate(record.summary_json)
        summary = summary.model_copy(
            update={
                "level_distribution": clean(summary.level_distribution),
                "next_behaviors": [clean(item) for item in summary.next_behaviors],
                "inactive_modules": [
                    (target, clean(reason))
                    for target, reason in summary.inactive_modules
                ],
                "bottom_line_events": [
                    clean_event(item) for item in summary.bottom_line_events
                ],
            }
        )
        bottom_line_events = [
            clean_event(BottomLineEvent.model_validate(item))
            for item in record.bottom_line_events_json
        ]
        material_conflicts = [
            clean_conflict(MaterialConflict.model_validate(item))
            for item in record.material_conflicts_json
        ]
        return cls(
            id=record.id,
            session_id=record.session_id,
            job_id=record.job_id,
            case_id=record.case_id,
            scene=record.scene,
            media=record.media,
            summary=summary,
            dimensions=[
                DimensionReportRead.from_result(
                    result,
                    media=record.media,
                    unit_ids=unit_ids,
                )
                for result in results
            ],
            bottom_line_events=bottom_line_events,
            material_conflicts=material_conflicts,
            screening_gap=record.screening_gap,
            disclaimers=[clean(item) for item in record.disclaimers_json],
            rubric_fingerprint=record.rubric_fingerprint,
            case_package_fingerprint=record.case_package_fingerprint,
            model_fingerprint=record.model_fingerprint,
            prompt_fingerprint=record.prompt_fingerprint,
            input_fingerprint=record.input_fingerprint,
            ai_draft_status=record.ai_draft_status,
            created_at=record.created_at,
        )

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.reports.competency_rubric import get_rubric
from app.reports.scoring_domain import (
    CoreDimension,
    OpportunityKind,
    SpecialModule,
)
from app.sessions.models import Scene


class MeasurementModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpportunitySource(StrEnum):
    runtime_state = "runtime_state"
    transcript = "transcript"
    termination = "termination"
    work_record = "work_record"


class ScoringOpportunity(MeasurementModel):
    id: str = Field(min_length=1)
    target: CoreDimension | SpecialModule
    kind: OpportunityKind
    description: str = Field(min_length=1)
    evidence_targets: list[str] = Field(min_length=1)
    indicator_ids: list[str] = Field(min_length=1)
    complex_opportunity: bool = False
    source: OpportunitySource = OpportunitySource.runtime_state
    linked_fact_ids: list[str] = Field(default_factory=list)
    required_fact_depths: dict[
        str,
        Annotated[int, Field(ge=1)],
    ] = Field(default_factory=dict)
    required_event_ids: list[str] = Field(default_factory=list)
    observable_behaviors: list[str] = Field(default_factory=list)
    concerning_behaviors: list[str] = Field(default_factory=list)
    scenes: list[Scene] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target_indicators(self) -> Self:
        allowed = {indicator.id for indicator in get_rubric(self.target).indicators}
        unknown = set(self.indicator_ids) - allowed
        if unknown:
            raise ValueError(
                f"measurement references unknown indicator: {sorted(unknown)}"
            )
        has_runtime_state_gates = bool(
            self.linked_fact_ids
            or self.required_fact_depths
            or self.required_event_ids
        )
        if (
            self.source is not OpportunitySource.runtime_state
            and has_runtime_state_gates
        ):
            raise ValueError(
                "non-runtime_state opportunity cannot declare runtime state gates"
            )
        if (
            self.source is OpportunitySource.runtime_state
            and isinstance(self.target, CoreDimension)
            and self.target is not CoreDimension.documentation
            and not has_runtime_state_gates
        ):
            raise ValueError(
                "核心维度机会必须声明 disclosure source（事实或事件门槛）"
            )
        return self


class MeasurementSpec(MeasurementModel):
    case_id: str = Field(min_length=1)
    scoring_opportunities: list[ScoringOpportunity] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        ids = [item.id for item in self.scoring_opportunities]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate scoring opportunity id")
        return self

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.reports.models import PlannedAction, ReferralDecision, RiskLevel
from app.sessions.models import (
    CaseType,
    EndReason,
    Media,
    ModelMode,
    Scene,
    SessionMode,
    SessionStatus,
    TurnSpeaker,
)


class FrozenInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodingSessionInput(FrozenInputModel):
    session_id: str
    mode: SessionMode
    scene: Scene
    case_type: CaseType
    case_id: str
    media: Media
    status: SessionStatus
    model_mode: ModelMode
    soft_duration_minutes: int | None
    created_at: datetime
    ended_at: datetime | None
    end_reason: EndReason | None


class CodingTurnInput(FrozenInputModel):
    turn_id: str
    sequence: int = Field(ge=1)
    speaker: TurnSpeaker
    text: str
    created_at: datetime


class CodingTechnicalInterruption(FrozenInputModel):
    interruption_id: str
    client_turn_id: str | None
    component: str
    phase: str
    operation: str
    failure_code: str
    attempt_count: int = Field(ge=1)
    retryable: bool
    disposition: str
    created_at: datetime


class SessionTerminationInput(FrozenInputModel):
    status: SessionStatus
    ended_at: datetime | None
    end_reason: EndReason | None


class WorkRecordSnapshotInput(FrozenInputModel):
    id: str
    session_id: str
    problem_understanding: str
    risk_level: RiskLevel
    risk_reasoning: str
    risk_evidence_turn_ids: list[str]
    missing_information: list[str]
    planned_actions: list[PlannedAction]
    referral_decision: ReferralDecision
    supervision_decision: bool
    follow_up: str
    limitations: str
    created_at: datetime
    updated_at: datetime


class CodingInput(FrozenInputModel):
    session: CodingSessionInput
    turns: list[CodingTurnInput]
    work_record: WorkRecordSnapshotInput | None
    technical_interruptions: list[CodingTechnicalInterruption]
    termination: SessionTerminationInput


class CodingShard(FrozenInputModel):
    shard_id: str = Field(min_length=1)
    session: CodingSessionInput
    turns: list[CodingTurnInput]
    work_record: WorkRecordSnapshotInput | None
    technical_interruptions: list[CodingTechnicalInterruption]
    termination: SessionTerminationInput
    overlap_turn_ids: list[str]


class OpportunityTurnStateInput(FrozenInputModel):
    turn_id: str
    state_before_json: dict[str, Any]
    state_after_json: dict[str, Any]
    signals_json: dict[str, Any]
    used_fact_ids: list[str]


class OpportunityCheckInput(FrozenInputModel):
    session_id: str
    session_state: dict[str, Any]
    turn_states: list[OpportunityTurnStateInput]
    case_package: dict[str, Any]

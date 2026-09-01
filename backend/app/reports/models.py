from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from sqlalchemy import JSON, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.sessions.models import Media, Scene, utc_now


class RiskLevel(StrEnum):
    no_identified = "no_identified"
    low = "low"
    moderate = "moderate"
    high = "high"
    imminent = "imminent"
    uncertain = "uncertain"


class PlannedAction(StrEnum):
    continue_assessment = "continue_assessment"
    stay_connected = "stay_connected"
    contact_support = "contact_support"
    reduce_access = "reduce_access"
    supervisor = "supervisor"
    emergency_services = "emergency_services"
    referral = "referral"
    follow_up = "follow_up"
    emotion_stabilization = "emotion_stabilization"
    goal_clarification = "goal_clarification"
    conflict_deescalation = "conflict_deescalation"
    autonomy_support = "autonomy_support"
    resource_linkage = "resource_linkage"


class ReferralDecision(StrEnum):
    not_needed = "not_needed"
    consider = "consider"
    recommended = "recommended"
    urgent = "urgent"


class ReportJobStage(StrEnum):
    queued = "queued"
    coding = "coding"
    scoring = "scoring"
    assembling = "assembling"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"


class ReportDraftStatus(StrEnum):
    complete = "complete"
    partial = "partial"


class WorkRecordRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "work_records"
    __table_args__ = (UniqueConstraint("session_id", name="uq_work_record_session"),)

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    problem_understanding: str
    risk_level: RiskLevel
    risk_reasoning: str
    risk_evidence_turn_ids: list[str] = Field(default_factory=list, sa_type=JSON)
    missing_information: list[str] = Field(default_factory=list, sa_type=JSON)
    planned_actions: list[str] = Field(default_factory=list, sa_type=JSON)
    referral_decision: ReferralDecision
    supervision_decision: bool
    follow_up: str
    limitations: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ReportJobRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "report_jobs"
    __table_args__ = (UniqueConstraint("session_id", name="uq_report_job_session"),)

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    stage: ReportJobStage = Field(default=ReportJobStage.queued, index=True)
    frozen_input_json: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    opportunity_check_json: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    frozen_input_fingerprint: str
    coding_json: dict[str, Any] | None = Field(default=None, sa_type=JSON)
    scoring_group_results_json: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    scoring_groups_done: list[str] = Field(default_factory=list, sa_type=JSON)
    attempts: dict[str, int] = Field(default_factory=dict, sa_type=JSON)
    last_error: str | None = None
    report_id: str | None = Field(default=None, foreign_key="reports.id")
    rubric_fingerprint: str
    case_package_fingerprint: str
    model_snapshot: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    model_fingerprint: str
    prompt_fingerprint: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ReportRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "reports"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    job_id: str = Field(foreign_key="report_jobs.id", index=True)
    case_id: str
    scene: Scene
    media: Media
    summary_json: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    dimensions_json: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSON)
    bottom_line_events_json: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_type=JSON,
    )
    material_conflicts_json: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_type=JSON,
    )
    screening_gap: bool = False
    disclaimers_json: list[str] = Field(default_factory=list, sa_type=JSON)
    rubric_fingerprint: str
    case_package_fingerprint: str
    model_fingerprint: str
    prompt_fingerprint: str
    input_fingerprint: str
    ai_draft_status: ReportDraftStatus
    created_at: datetime = Field(default_factory=utc_now)

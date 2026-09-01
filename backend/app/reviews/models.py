from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from sqlalchemy import JSON, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.sessions.models import utc_now


class ExpertReviewStatus(StrEnum):
    confirmed = "confirmed"
    modified = "modified"


class ReviewRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "reviews"
    __table_args__ = (UniqueConstraint("report_id", name="uq_review_report"),)

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    report_id: str = Field(foreign_key="reports.id", index=True)
    status: ExpertReviewStatus
    reviewer_name: str
    reason: str = ""
    changes_json: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSON)
    created_at: datetime = Field(default_factory=utc_now)

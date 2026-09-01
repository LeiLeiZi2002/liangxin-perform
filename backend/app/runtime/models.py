from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from sqlalchemy import JSON
from sqlmodel import Field, SQLModel

from app.sessions.models import utc_now


class ModelRole(StrEnum):
    director = "director"
    actor = "actor"
    tts = "tts"
    report = "report"


class PromptFamily(StrEnum):
    report_global = "report_global"
    report_map = "report_map"
    report_reduce = "report_reduce"
    report_interaction = "report_interaction"
    report_professional = "report_professional"
    report_safety = "report_safety"


class ModelCallKind(StrEnum):
    initial = "initial"
    repair = "repair"


class CacheMode(StrEnum):
    none = "none"
    explicit = "explicit"
    implicit = "implicit"
    character_session = "character_session"


class ModelCallMetricRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "model_call_metrics"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    client_turn_id: str | None = Field(default=None, index=True)
    model_role: ModelRole
    prompt_family: PromptFamily | None = None
    model_name: str
    call_kind: ModelCallKind
    cache_mode: CacheMode
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(ge=0)
    success: bool
    request_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RuntimeFailureRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "runtime_failure_records"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    client_turn_id: str | None = Field(default=None, index=True)
    component: str = Field(index=True)
    phase: str = Field(index=True)
    operation: str
    failure_code: str = Field(index=True)
    error_class: str
    attempt_count: int = Field(ge=1)
    retryable: bool
    disposition: str = Field(index=True)
    provider_status_code: int | None = None
    provider_request_id: str | None = None
    attempts_json: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSON)
    details_json: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    created_at: datetime = Field(default_factory=utc_now)

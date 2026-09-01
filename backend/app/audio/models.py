from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from sqlalchemy import JSON
from sqlmodel import Field, SQLModel

from app.sessions.models import utc_now


class AudioKind(StrEnum):
    uploaded = "uploaded"
    synthesized = "synthesized"
    worker_turn = "worker_turn"
    client_turn = "client_turn"


class AudioRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "audio_records"

    id: str = Field(primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    kind: AudioKind
    storage_name: str
    mime_type: str
    provider: str
    size_bytes: int = Field(ge=1)
    created_at: datetime = Field(default_factory=utc_now)


class SpeechMetricRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "speech_metric_records"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    turn_id: str = Field(foreign_key="turns.id", unique=True, index=True)
    first_response_ms: int = Field(ge=0)
    speech_duration_ms: int = Field(ge=0)
    pause_durations_ms: list[int] = Field(default_factory=list, sa_type=JSON)
    supplement_count: int = Field(default=0, ge=0)
    speech_rate: float = Field(default=0.0, ge=0)
    overlap_duration_ms: int = Field(default=0, ge=0)
    excluded_technical_ms: int = Field(default=0, ge=0)
    asr_sentences_json: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSON)
    created_at: datetime = Field(default_factory=utc_now)

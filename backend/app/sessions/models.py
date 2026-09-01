from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from sqlalchemy import JSON, UniqueConstraint
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


class SessionMode(StrEnum):
    assessment = "assessment"
    experience = "experience"


class Scene(StrEnum):
    institution = "institution"
    hotline = "hotline"
    online = "online"


class Media(StrEnum):
    voice = "voice"
    text = "text"


class CaseType(StrEnum):
    main = "main"
    short = "short"


class SessionStatus(StrEnum):
    active = "active"
    ended = "ended"


class EndReason(StrEnum):
    user_ended = "user_ended"
    natural_closure = "natural_closure"
    soft_time_reached = "soft_time_reached"
    technical_interruption = "technical_interruption"


class TurnSpeaker(StrEnum):
    worker = "worker"
    client = "client"


class ModelMode(StrEnum):
    auto = "auto"
    live = "live"
    fallback = "fallback"


SCENE_MEDIA: dict[Scene, Media] = {
    Scene.institution: Media.voice,
    Scene.hotline: Media.voice,
    Scene.online: Media.text,
}


class SessionRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "sessions"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    mode: SessionMode
    scene: Scene
    case_type: CaseType
    case_id: str = Field(index=True)
    media: Media
    status: SessionStatus = Field(default=SessionStatus.active)
    model_mode: ModelMode
    state_json: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    soft_duration_minutes: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    end_reason: EndReason | None = None


class TurnRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "turns"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "client_turn_id",
            "speaker",
            name="uq_turn_session_client_speaker",
        ),
        UniqueConstraint("session_id", "sequence", name="uq_turn_session_sequence"),
    )

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    client_turn_id: str = Field(index=True)
    sequence: int = Field(ge=1)
    speaker: TurnSpeaker
    text: str
    audio_path: str | None = None
    provider: str | None = None
    degraded: bool = False
    signals_json: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    state_before_json: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    state_after_json: dict[str, Any] = Field(default_factory=dict, sa_type=JSON)
    used_fact_ids: list[str] = Field(default_factory=list, sa_type=JSON)
    created_at: datetime = Field(default_factory=utc_now)


class DemoConfigRecord(SQLModel, table=True):
    __tablename__: ClassVar[str] = "demo_config"

    id: int = Field(default=1, primary_key=True)
    scene: Scene = Field(default=Scene.hotline)
    case_type: CaseType = Field(default=CaseType.main)
    task_count: int = Field(default=1, ge=1)
    soft_duration_minutes: int | None = Field(default=None, ge=1)
    model_mode: ModelMode = Field(default=ModelMode.live)
    require_work_record: bool = Field(default=True)

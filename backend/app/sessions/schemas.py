from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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


class SessionCreate(BaseModel):
    mode: SessionMode
    scene: Scene | None = None
    case_type: CaseType | None = None
    case_id: str = Field(min_length=1)


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    mode: SessionMode
    scene: Scene
    case_type: CaseType
    case_id: str
    media: Media
    status: SessionStatus
    model_mode: ModelMode
    soft_duration_minutes: int | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    end_reason: EndReason | None


class TurnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    client_turn_id: str
    sequence: int
    speaker: TurnSpeaker
    text: str
    provider: str | None
    degraded: bool
    created_at: datetime
    audio_available: bool = False


class SessionDetail(BaseModel):
    session: SessionRead
    transcript: list[TurnRead]


class EndSessionRequest(BaseModel):
    reason: EndReason = EndReason.user_ended


class DemoConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scene: Scene
    case_type: CaseType
    task_count: int = Field(ge=1)
    soft_duration_minutes: int | None = Field(default=None, ge=1)
    model_mode: ModelMode
    require_work_record: bool

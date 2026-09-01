from pydantic import BaseModel, Field

from app.cases.domain import PublicEntry
from app.sessions.models import CaseType, Media, Scene


class CaseMetadata(BaseModel):
    case_id: str
    title: str
    case_type: CaseType
    public_entry: PublicEntry
    estimated_duration_minutes: int
    scene: Scene | None
    media: Media | None
    available_scenes: list[Scene]


class CaseDrawRequest(BaseModel):
    scene: Scene
    case_type: CaseType
    seed: int | str | None = None
    excluded_case_ids: list[str] = Field(default_factory=list)

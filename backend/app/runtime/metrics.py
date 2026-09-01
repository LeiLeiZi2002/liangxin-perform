from dataclasses import asdict, dataclass

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.runtime.models import (
    CacheMode,
    ModelCallKind,
    ModelCallMetricRecord,
    ModelRole,
    PromptFamily,
)


@dataclass(frozen=True, slots=True)
class ModelCallMetric:
    session_id: str
    model_role: ModelRole
    model_name: str
    call_kind: ModelCallKind
    cache_mode: CacheMode
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    cache_creation_input_tokens: int
    latency_ms: int
    success: bool
    request_id: str | None
    client_turn_id: str | None = None
    prompt_family: PromptFamily | None = None


class ModelCallRecorder:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, metric: ModelCallMetric) -> None:
        with Session(self._engine) as db:
            db.add(ModelCallMetricRecord(**asdict(metric)))
            db.commit()

    def latest_successful_prompt_tokens(
        self,
        session_id: str,
        model_role: ModelRole,
    ) -> int | None:
        with Session(self._engine) as db:
            record = db.exec(
                select(ModelCallMetricRecord)
                .where(
                    ModelCallMetricRecord.session_id == session_id,
                    ModelCallMetricRecord.model_role == model_role,
                    col(ModelCallMetricRecord.success).is_(True),
                )
                .order_by(
                    col(ModelCallMetricRecord.created_at).desc(),
                    col(ModelCallMetricRecord.id).desc(),
                )
                .limit(1)
            ).first()
        return record.prompt_tokens if record is not None else None

    def latest_attempted_prompt_tokens(
        self,
        session_id: str,
        model_role: ModelRole,
        client_turn_id: str,
    ) -> int | None:
        with Session(self._engine) as db:
            record = db.exec(
                select(ModelCallMetricRecord)
                .where(
                    ModelCallMetricRecord.session_id == session_id,
                    ModelCallMetricRecord.model_role == model_role,
                    ModelCallMetricRecord.client_turn_id == client_turn_id,
                )
                .order_by(
                    col(ModelCallMetricRecord.created_at).desc(),
                    col(ModelCallMetricRecord.id).desc(),
                )
                .limit(1)
            ).first()
        if record is None or record.prompt_tokens <= 0:
            return None
        return record.prompt_tokens

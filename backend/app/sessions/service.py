from datetime import datetime

from sqlmodel import Session

from app.sessions.models import (
    DemoConfigRecord,
    EndReason,
    ModelMode,
    SessionRecord,
    SessionStatus,
    utc_now,
)

DEMO_CONFIG_ID = 1


def get_or_create_demo_config(db: Session) -> DemoConfigRecord:
    config = db.get(DemoConfigRecord, DEMO_CONFIG_ID)
    if config is not None:
        return config

    config = DemoConfigRecord(model_mode=ModelMode.live)
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def mark_session_ended(
    record: SessionRecord,
    reason: EndReason,
    *,
    ended_at: datetime | None = None,
) -> bool:
    """将会话列与运行时状态一次收稳，已结束会话不改写原因。"""

    now = ended_at or utc_now()
    changed = False
    if record.status is SessionStatus.active:
        record.status = SessionStatus.ended
        record.end_reason = reason
        record.ended_at = now
        changed = True

    state_json = dict(record.state_json)
    current_runtime = state_json.get("runtime")
    runtime = dict(current_runtime) if isinstance(current_runtime, dict) else {}
    terminal_runtime = {
        **runtime,
        "phase": "ended",
        "technical_retry_allowed": False,
    }
    terminal_runtime.pop("pending_ending_route_id", None)
    if current_runtime != terminal_runtime:
        state_json["runtime"] = terminal_runtime
        record.state_json = state_json
        changed = True

    if changed:
        record.updated_at = now
    return changed

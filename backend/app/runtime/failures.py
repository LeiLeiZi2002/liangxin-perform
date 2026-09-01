from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, cast

from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.runtime.models import RuntimeFailureRecord


class FailureDisposition(StrEnum):
    recovered = "recovered"
    technical_pause = "technical_pause"
    session_end = "session_end"
    connection_close = "connection_close"
    aborted = "aborted"


@dataclass(frozen=True, slots=True)
class FailureAttempt:
    index: int
    error_class: str
    message: str
    call_kind: str | None = None
    provider_status_code: int | None = None
    provider_request_id: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeFailure:
    session_id: str
    component: str
    phase: str
    operation: str
    failure_code: str
    retryable: bool
    disposition: FailureDisposition
    attempts: tuple[FailureAttempt, ...]
    client_turn_id: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)


_SECRET_KEY_PARTS = ("api_key", "authorization", "token", "secret", "password")
_BEARER_PATTERN = re.compile(r"(?i)(authorization\s*[:=]?\s*bearer\s+)[^\s,;]+")
_ENV_KEY_PATTERN = re.compile(r"(?i)(DASHSCOPE_API_KEY\s*=\s*)[^\s,;]+")
_SK_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{4,}\b")


def _redact_text(value: str) -> str:
    value = _BEARER_PATTERN.sub(r"\1[REDACTED]", value)
    value = _ENV_KEY_PATTERN.sub(r"\1[REDACTED]", value)
    return _SK_KEY_PATTERN.sub("[REDACTED]", value)


def _sanitize(value: object, *, key: str | None = None) -> object:
    if key is not None and any(part in key.casefold() for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(child_key): _sanitize(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def safe_failure_details(value: Mapping[str, object]) -> dict[str, Any]:
    sanitized = _sanitize(value)
    return cast(dict[str, Any], sanitized)


def attach_failure_details(
    error: Exception,
    details: Mapping[str, object],
) -> Exception:
    """把可落库的诊断信息挂到异常上，供外层统一记录。"""
    error.__dict__["runtime_failure_details"] = safe_failure_details(details)
    return error


def exception_failure_details(error: BaseException) -> dict[str, Any]:
    details = getattr(error, "runtime_failure_details", None)
    return safe_failure_details(details) if isinstance(details, Mapping) else {}


def attach_failure_attempts(
    error: Exception,
    attempts: tuple[FailureAttempt, ...],
) -> Exception:
    error.__dict__["runtime_failure_attempts"] = attempts
    return error


def exception_failure_attempts(
    error: BaseException,
) -> tuple[FailureAttempt, ...]:
    attempts = getattr(error, "runtime_failure_attempts", None)
    if not isinstance(attempts, tuple):
        return ()
    return tuple(item for item in attempts if isinstance(item, FailureAttempt))


def failure_attempt_from_exception(
    index: int,
    error: Exception,
    *,
    call_kind: str | None = None,
    details: Mapping[str, object] | None = None,
) -> FailureAttempt:
    source: BaseException | None = error
    chain: list[dict[str, object]] = []
    status_code: int | None = None
    request_id: str | None = None
    while source is not None and len(chain) < 4:
        source_status = getattr(source, "status_code", None)
        source_request_id = getattr(source, "request_id", None)
        if status_code is None and isinstance(source_status, int):
            status_code = source_status
        if request_id is None and isinstance(source_request_id, str) and source_request_id:
            request_id = source_request_id
        chain_item: dict[str, object] = {
                "error_class": type(source).__name__,
                "message": str(source).strip() or type(source).__name__,
                "status_code": source_status if isinstance(source_status, int) else None,
                "request_id": (
                    source_request_id
                    if isinstance(source_request_id, str) and source_request_id
                    else None
                ),
            }
        source_details = exception_failure_details(source)
        if source_details:
            chain_item["details"] = source_details
        chain.append(chain_item)
        source = source.__cause__ or source.__context__
    combined_details = {"exception_chain": chain, **(details or {})}
    return FailureAttempt(
        index=index,
        error_class=type(error).__name__,
        message=str(error).strip() or type(error).__name__,
        call_kind=call_kind,
        provider_status_code=status_code,
        provider_request_id=request_id,
        details=combined_details,
    )


class RuntimeFailureRecorder:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, failure: RuntimeFailure) -> RuntimeFailureRecord:
        if not failure.attempts:
            raise ValueError("失败记录至少需要一次失败尝试")
        attempts = cast(
            list[dict[str, Any]],
            _sanitize([asdict(attempt) for attempt in failure.attempts]),
        )
        last = failure.attempts[-1]
        status_code = next(
            (
                attempt.provider_status_code
                for attempt in reversed(failure.attempts)
                if attempt.provider_status_code is not None
            ),
            None,
        )
        request_id = next(
            (
                attempt.provider_request_id
                for attempt in reversed(failure.attempts)
                if attempt.provider_request_id
            ),
            None,
        )
        record = RuntimeFailureRecord(
            session_id=failure.session_id,
            client_turn_id=failure.client_turn_id,
            component=failure.component,
            phase=failure.phase,
            operation=failure.operation,
            failure_code=failure.failure_code,
            error_class=last.error_class,
            attempt_count=len(failure.attempts),
            retryable=failure.retryable,
            disposition=failure.disposition.value,
            provider_status_code=status_code,
            provider_request_id=request_id,
            attempts_json=attempts,
            details_json=safe_failure_details(failure.details),
        )
        with Session(self._engine) as db:
            db.add(record)
            db.commit()
            db.refresh(record)
        return record

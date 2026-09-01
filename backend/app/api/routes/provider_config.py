import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.runtime.provider_check import ProviderCheckResult, ProviderReadinessChecker
from app.runtime_config import RuntimeCredentialStore, runtime_credential_store

router = APIRouter(prefix="/provider-config", tags=["provider-config"])
MAX_API_KEY_LENGTH = 512
WORKSPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,119}")


class ProviderConfigRead(BaseModel):
    configured: bool
    masked_key: str | None
    workspace_id: str | None
    report_model: str
    actor_model: str
    asr_model: str
    tts_model: str
    tts_voice: str
    report_temperature: float
    actor_temperature: float
    actor_context_window_tokens: int
    actor_max_output_tokens: int


class ProviderConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: object = Field(default="")
    workspace_id: str | None = Field(default=None, max_length=120)
    report_model: str | None = Field(default=None, min_length=1, max_length=120)
    actor_model: str | None = Field(default=None, min_length=1, max_length=120)
    asr_model: str | None = Field(default=None, min_length=1, max_length=120)
    tts_model: str | None = Field(default=None, min_length=1, max_length=120)
    tts_voice: str | None = Field(default=None, min_length=1, max_length=120)
    report_temperature: float | None = Field(default=None, ge=0, le=2)
    actor_temperature: float | None = Field(default=None, ge=0, le=2)
    actor_context_window_tokens: int | None = Field(default=None, ge=1)
    actor_max_output_tokens: int | None = Field(default=None, ge=1)

    @field_validator("workspace_id", mode="before")
    @classmethod
    def normalize_workspace_id(cls, value: str | None) -> str | None:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            return None
        if not WORKSPACE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("业务空间标识格式不正确")
        return normalized

    @field_validator(
        "report_model",
        "actor_model",
        "asr_model",
        "tts_model",
        "tts_voice",
        mode="before",
    )
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        if isinstance(value, str):
            return value.strip() or None
        return value


class ProviderCheckRequest(BaseModel):
    requires_speech: bool


def get_runtime_credential_store() -> RuntimeCredentialStore:
    return runtime_credential_store


StoreDep = Annotated[RuntimeCredentialStore, Depends(get_runtime_credential_store)]


def get_provider_readiness_checker() -> ProviderReadinessChecker:
    return ProviderReadinessChecker(runtime_credential_store)


CheckerDep = Annotated[ProviderReadinessChecker, Depends(get_provider_readiness_checker)]


def mask_api_key(api_key: str) -> str | None:
    if not api_key:
        return None
    return f"••••{api_key[-4:]}"


def as_public_config(store: RuntimeCredentialStore) -> ProviderConfigRead:
    credentials = store.credentials()
    return ProviderConfigRead(
        configured=bool(credentials.api_key),
        masked_key=mask_api_key(credentials.api_key),
        workspace_id=credentials.workspace_id,
        report_model=credentials.report_model,
        actor_model=credentials.actor_model,
        asr_model=credentials.asr_model,
        tts_model=credentials.tts_model,
        tts_voice=credentials.tts_voice,
        report_temperature=credentials.report_temperature,
        actor_temperature=credentials.actor_temperature,
        actor_context_window_tokens=credentials.actor_context_window_tokens,
        actor_max_output_tokens=credentials.actor_max_output_tokens,
    )


@router.get("", response_model=ProviderConfigRead)
def get_provider_config(store: StoreDep) -> ProviderConfigRead:
    return as_public_config(store)


@router.put("", response_model=ProviderConfigRead)
def update_provider_config(request: ProviderConfigUpdate, store: StoreDep) -> ProviderConfigRead:
    if not isinstance(request.api_key, str):
        raise HTTPException(status_code=422, detail="API Key 格式不正确。")
    if len(request.api_key) > MAX_API_KEY_LENGTH:
        raise HTTPException(status_code=422, detail="API Key 长度不能超过 512 个字符。")
    try:
        store.update(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return as_public_config(store)


@router.post("/check", response_model=ProviderCheckResult)
async def check_provider_readiness(
    request: ProviderCheckRequest,
    checker: CheckerDep,
) -> ProviderCheckResult:
    return await checker.check(requires_speech=request.requires_speech)

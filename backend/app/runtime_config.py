from dataclasses import dataclass, field
from threading import RLock

DEFAULT_DIRECTOR_MODEL = "qwen3.7-plus"
DEFAULT_REPORT_MODEL = "qwen3.8-max"
DEFAULT_ACTOR_MODEL = "qwen-plus-character"
DEFAULT_ASR_MODEL = "qwen-audio-3.0-asr-flash-streaming"
DEFAULT_TTS_MODEL = "qwen-audio-3.0-tts-plus"
DEFAULT_TTS_VOICE = "longanlingxin"
DEFAULT_DIRECTOR_TEMPERATURE = 0.15
DEFAULT_REPORT_TEMPERATURE = 0.1
DEFAULT_ACTOR_TEMPERATURE = 0.75
DEFAULT_ACTOR_CONTEXT_WINDOW_TOKENS = 32768
DEFAULT_ACTOR_MAX_OUTPUT_TOKENS = 2048


@dataclass(frozen=True, slots=True)
class ActorModelProfile:
    context_window_tokens: int
    maximum_output_tokens: int
    default_output_tokens: int


ACTOR_MODEL_PROFILES: dict[str, ActorModelProfile] = {
    "qwen-plus-character": ActorModelProfile(
        context_window_tokens=32768,
        maximum_output_tokens=4096,
        default_output_tokens=DEFAULT_ACTOR_MAX_OUTPUT_TOKENS,
    ),
}


def text_base_url(workspace_id: str | None) -> str:
    if workspace_id:
        return f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


def realtime_base_url(workspace_id: str | None) -> str:
    if workspace_id:
        return f"wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1"
    return "wss://dashscope.aliyuncs.com/api-ws/v1"


@dataclass(frozen=True, slots=True)
class RuntimeCredentials:
    api_key: str = field(repr=False)
    workspace_id: str | None
    report_model: str
    director_model: str
    actor_model: str
    asr_model: str
    tts_model: str
    tts_voice: str
    director_temperature: float
    report_temperature: float
    actor_temperature: float
    actor_context_window_tokens: int
    actor_max_output_tokens: int


class RuntimeCredentialStore:
    """仅保存当前后端进程需要的百炼运行配置。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._credentials = RuntimeCredentials(
            api_key="",
            workspace_id=None,
            report_model=DEFAULT_REPORT_MODEL,
            director_model=DEFAULT_DIRECTOR_MODEL,
            actor_model=DEFAULT_ACTOR_MODEL,
            asr_model=DEFAULT_ASR_MODEL,
            tts_model=DEFAULT_TTS_MODEL,
            tts_voice=DEFAULT_TTS_VOICE,
            director_temperature=DEFAULT_DIRECTOR_TEMPERATURE,
            report_temperature=DEFAULT_REPORT_TEMPERATURE,
            actor_temperature=DEFAULT_ACTOR_TEMPERATURE,
            actor_context_window_tokens=DEFAULT_ACTOR_CONTEXT_WINDOW_TOKENS,
            actor_max_output_tokens=DEFAULT_ACTOR_MAX_OUTPUT_TOKENS,
        )

    def credentials(self) -> RuntimeCredentials:
        with self._lock:
            return self._credentials

    def update(
        self,
        *,
        api_key: str | None = None,
        workspace_id: str | None = None,
        report_model: str | None = None,
        director_model: str | None = None,
        actor_model: str | None = None,
        asr_model: str | None = None,
        tts_model: str | None = None,
        tts_voice: str | None = None,
        director_temperature: float | None = None,
        report_temperature: float | None = None,
        actor_temperature: float | None = None,
        actor_context_window_tokens: int | None = None,
        actor_max_output_tokens: int | None = None,
    ) -> RuntimeCredentials:
        with self._lock:
            current = self._credentials
            next_actor_model = actor_model or current.actor_model
            actor_model_changed = (
                actor_model is not None and next_actor_model != current.actor_model
            )
            profile = ACTOR_MODEL_PROFILES.get(next_actor_model)
            if actor_model_changed:
                if profile is None and actor_context_window_tokens is None:
                    raise ValueError(
                        "切换到未知来访者对话模型时，必须填写上下文容量"
                    )
                if actor_context_window_tokens is not None:
                    next_actor_context_window_tokens = actor_context_window_tokens
                elif profile is not None:
                    next_actor_context_window_tokens = profile.context_window_tokens
                else:
                    raise ValueError(
                        "切换到未知来访者对话模型时，必须填写上下文容量"
                    )
                next_actor_max_output_tokens = (
                    actor_max_output_tokens
                    if actor_max_output_tokens is not None
                    else (
                        profile.default_output_tokens
                        if profile is not None
                        else min(
                            DEFAULT_ACTOR_MAX_OUTPUT_TOKENS,
                            next_actor_context_window_tokens,
                        )
                    )
                )
            else:
                next_actor_context_window_tokens = (
                    actor_context_window_tokens
                    if actor_context_window_tokens is not None
                    else current.actor_context_window_tokens
                )
                next_actor_max_output_tokens = (
                    actor_max_output_tokens
                    if actor_max_output_tokens is not None
                    else current.actor_max_output_tokens
                )
            self._validate_actor_limits(
                next_actor_model,
                next_actor_context_window_tokens,
                next_actor_max_output_tokens,
            )
            self._credentials = RuntimeCredentials(
                api_key=api_key.strip() if api_key and api_key.strip() else current.api_key,
                workspace_id=workspace_id.strip() if workspace_id else None,
                report_model=report_model or current.report_model,
                director_model=director_model or current.director_model,
                actor_model=next_actor_model,
                asr_model=asr_model or current.asr_model,
                tts_model=tts_model or current.tts_model,
                tts_voice=tts_voice or current.tts_voice,
                director_temperature=(
                    director_temperature
                    if director_temperature is not None
                    else current.director_temperature
                ),
                report_temperature=(
                    report_temperature
                    if report_temperature is not None
                    else current.report_temperature
                ),
                actor_temperature=(
                    actor_temperature
                    if actor_temperature is not None
                    else current.actor_temperature
                ),
                actor_context_window_tokens=next_actor_context_window_tokens,
                actor_max_output_tokens=next_actor_max_output_tokens,
            )
            return self._credentials

    @staticmethod
    def _validate_actor_limits(
        actor_model: str,
        context_window_tokens: int,
        max_output_tokens: int,
    ) -> None:
        if isinstance(context_window_tokens, bool) or context_window_tokens < 1:
            raise ValueError("来访者对话模型上下文容量必须是正整数")
        if isinstance(max_output_tokens, bool) or max_output_tokens < 1:
            raise ValueError("单次回复输出上限必须是正整数")
        profile = ACTOR_MODEL_PROFILES.get(actor_model)
        if profile is not None:
            if context_window_tokens > profile.context_window_tokens:
                raise ValueError(
                    f"{actor_model} 的上下文容量不能超过 "
                    f"{profile.context_window_tokens}"
                )
            if max_output_tokens > profile.maximum_output_tokens:
                raise ValueError(
                    f"{actor_model} 的单次回复输出上限不能超过 "
                    f"{profile.maximum_output_tokens}"
                )
        if max_output_tokens > context_window_tokens:
            raise ValueError("单次回复输出上限不能超过上下文容量")


runtime_credential_store = RuntimeCredentialStore()

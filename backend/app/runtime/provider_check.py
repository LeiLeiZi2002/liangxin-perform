import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.cases.loader import CaseRepository
from app.runtime.character_provider import (
    CharacterDefinition,
    CharacterProvider,
    CharacterRepository,
)
from app.runtime.character_world import (
    build_support_world_view,
    initial_support_world,
    no_external_world_view,
)
from app.runtime.providers import AliyunSpeechProvider
from app.runtime_config import RuntimeCredentialStore
from app.sessions.models import Scene


class ProviderCheckStatus(StrEnum):
    passed = "passed"
    failed = "failed"


class ProviderCheckItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ProviderCheckStatus
    message: str | None = None


class ProviderCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor: ProviderCheckItem
    asr: ProviderCheckItem
    tts: ProviderCheckItem


class ProviderReadinessChecker:
    """实际连接来访者对话与所需语音能力，但不保存测试内容。"""

    def __init__(
        self,
        store: RuntimeCredentialStore,
        *,
        cases: CaseRepository | None = None,
        characters: CharacterRepository | None = None,
    ) -> None:
        self._store = store
        self._cases = cases or CaseRepository()
        self._characters = characters or CharacterRepository()
        self._actor = CharacterProvider(store)
        self._speech = AliyunSpeechProvider(store)

    async def check(self, *, requires_speech: bool) -> ProviderCheckResult:
        if not self._store.credentials().api_key.strip():
            missing = ProviderCheckItem(
                status=ProviderCheckStatus.failed,
                message="请先在设置页配置阿里云百炼 API Key",
            )
            return ProviderCheckResult(
                actor=missing,
                asr=missing if requires_speech else self._not_required(),
                tts=missing if requires_speech else self._not_required(),
            )

        if requires_speech:
            actor, asr, tts = await asyncio.gather(
                self._run("来访者对话模型暂时无法连接", self._check_actor),
                self._run("实时语音识别暂时无法连接", self._check_asr),
                self._run("来访者语音暂时无法连接", self._check_tts),
            )
        else:
            actor = await self._run(
                "来访者对话模型暂时无法连接",
                self._check_actor,
            )
            asr = self._not_required()
            tts = self._not_required()
        return ProviderCheckResult(
            actor=actor,
            asr=asr,
            tts=tts,
        )

    @staticmethod
    def _not_required() -> ProviderCheckItem:
        return ProviderCheckItem(status=ProviderCheckStatus.passed)

    @staticmethod
    async def _run(
        failure_message: str,
        operation: Callable[[], Awaitable[None]],
    ) -> ProviderCheckItem:
        try:
            async with asyncio.timeout(30):
                await operation()
            return ProviderCheckItem(status=ProviderCheckStatus.passed)
        except Exception:
            return ProviderCheckItem(
                status=ProviderCheckStatus.failed,
                message=failure_message,
            )

    def _probe_character(self) -> CharacterDefinition:
        packages = self._cases.list_published(scene=Scene.hotline)
        for package in packages:
            try:
                return self._characters.get(package.case.case_id)
            except LookupError:
                continue
        raise RuntimeError("仓库中没有已发布的热线角色档案")

    async def _check_actor(self) -> None:
        character = self._probe_character()
        world_view = (
            no_external_world_view()
            if character.world is None
            else build_support_world_view(
                character.world,
                initial_support_world(),
            )
        )
        await self._actor.respond(
            character=character,
            transcript=[],
            current_worker_text="你好。",
            opening=False,
            current_scene=Scene.hotline.value,
            world_reality=world_view.reality,
            allowed_world_actions=world_view.allowed_actions,
            session_id="provider-check",
            client_turn_id="provider-check-worker",
        )

    async def _check_asr(self) -> None:
        stream = await self._speech.open_asr()
        try:
            await stream.finish()
        finally:
            await stream.close()

    async def _check_tts(self) -> None:
        received_audio = False
        async for chunk in self._speech.synthesize(
            "你好。",
            instruction="自然、简短",
        ):
            received_audio = received_audio or bool(chunk)
        if not received_audio:
            raise RuntimeError("语音合成未返回音频")

from dataclasses import dataclass
from enum import StrEnum

from app.runtime.providers import ASRSentence


class BoundaryState(StrEnum):
    speaking = "speaking"
    candidate_pause = "candidate_pause"
    complete = "complete"


@dataclass(frozen=True, slots=True)
class TurnBoundaryConfig:
    complete_silence_ms: int = 2800
    recent_pause_multiplier: float = 1.5
    maximum_silence_ms: int = 5000
    incomplete_sentence_grace_ms: int = 1200


class TurnBoundary:
    """用持续静音确认话轮边界，ASR 断句只作为辅助信号。"""

    def __init__(
        self,
        config: TurnBoundaryConfig | None = None,
        *,
        listening_started_ms: int = 0,
    ) -> None:
        self._config = config or TurnBoundaryConfig()
        self._state = BoundaryState.candidate_pause
        self._listening_started_ms = listening_started_ms
        self._first_speech_ms: int | None = None
        self._speech_started_ms: int | None = None
        self._pause_started_ms: int | None = None
        self._pause_durations_ms: list[int] = []
        self._speech_duration_ms = 0
        self._supplement_count = 0
        self._has_speech = False
        self._sentence_ended = False
        self._asr_sentence_open = False

    @property
    def state(self) -> BoundaryState:
        return self._state

    @property
    def sentence_ended(self) -> bool:
        return self._sentence_ended

    @property
    def pause_durations_ms(self) -> tuple[int, ...]:
        return tuple(self._pause_durations_ms)

    @property
    def supplement_count(self) -> int:
        return self._supplement_count

    @property
    def first_response_ms(self) -> int | None:
        if self._first_speech_ms is None:
            return None
        return max(0, self._first_speech_ms - self._listening_started_ms)

    @property
    def speech_duration_ms(self) -> int:
        return self._speech_duration_ms

    def speech_started(self, *, at_ms: int) -> BoundaryState:
        if self._state is BoundaryState.complete:
            return self._state
        if self._first_speech_ms is None:
            self._first_speech_ms = at_ms
        if self._pause_started_ms is not None and self._has_speech:
            pause_ms = max(0, at_ms - self._pause_started_ms)
            self._pause_durations_ms.append(pause_ms)
            self._supplement_count += 1
        self._pause_started_ms = None
        self._speech_started_ms = at_ms
        self._has_speech = True
        self._state = BoundaryState.speaking
        return self._state

    def speech_stopped(
        self,
        *,
        at_ms: int,
        confirmed_silence_ms: int = 0,
    ) -> BoundaryState:
        if self._state is not BoundaryState.speaking:
            return self._state
        confirmed_silence_ms = max(0, confirmed_silence_ms)
        speech_ended_ms = at_ms - confirmed_silence_ms
        if self._speech_started_ms is not None:
            speech_ended_ms = max(self._speech_started_ms, speech_ended_ms)
            self._speech_duration_ms += speech_ended_ms - self._speech_started_ms
        self._speech_started_ms = None
        self._pause_started_ms = speech_ended_ms
        self._state = BoundaryState.candidate_pause
        return self._state

    def observe_asr(self, sentence: ASRSentence) -> None:
        self._sentence_ended = sentence.sentence_end
        if sentence.sentence_begin:
            self._asr_sentence_open = True
        if sentence.sentence_end:
            self._asr_sentence_open = False

    def advance(self, *, at_ms: int) -> BoundaryState:
        if (
            self._state is not BoundaryState.candidate_pause
            or not self._has_speech
            or self._pause_started_ms is None
        ):
            return self._state
        if at_ms - self._pause_started_ms >= self._required_silence_ms():
            self._state = BoundaryState.complete
        return self._state

    def manual_complete(self, *, at_ms: int) -> BoundaryState:
        if not self._has_speech:
            return self._state
        if self._state is BoundaryState.speaking:
            self.speech_stopped(at_ms=at_ms)
        self._state = BoundaryState.complete
        return self._state

    def _required_silence_ms(self) -> int:
        required = self._config.complete_silence_ms
        if self._pause_durations_ms:
            required = max(
                required,
                int(max(self._pause_durations_ms[-3:]) * self._config.recent_pause_multiplier),
            )
        if self._asr_sentence_open:
            required += self._config.incomplete_sentence_grace_ms
        return min(required, self._config.maximum_silence_ms)

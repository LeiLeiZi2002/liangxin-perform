from app.runtime.providers import ASRSentence
from app.runtime.turn_boundary import BoundaryState, TurnBoundary, TurnBoundaryConfig


def sentence(*, ended: bool) -> ASRSentence:
    return ASRSentence(
        text="我想一下",
        sentence_id=1,
        begin_time_ms=100,
        end_time_ms=900 if ended else None,
        sentence_begin=not ended,
        sentence_end=ended,
        words=(),
    )


def test_asr_sentence_end_only_marks_a_sentence_and_does_not_submit_turn() -> None:
    boundary = TurnBoundary()

    boundary.speech_started(at_ms=0)
    boundary.observe_asr(sentence(ended=True))
    boundary.speech_stopped(at_ms=900)

    assert boundary.state is BoundaryState.candidate_pause
    assert boundary.sentence_ended is True


def test_thought_pause_then_more_speech_stays_in_the_same_turn() -> None:
    boundary = TurnBoundary(TurnBoundaryConfig(complete_silence_ms=2800))
    boundary.speech_started(at_ms=0)
    boundary.speech_stopped(at_ms=1000)

    assert boundary.advance(at_ms=3300) is BoundaryState.candidate_pause

    boundary.speech_started(at_ms=3400)

    assert boundary.state is BoundaryState.speaking
    assert boundary.pause_durations_ms == (2400,)
    assert boundary.supplement_count == 1


def test_recent_long_pause_makes_the_next_boundary_more_conservative() -> None:
    boundary = TurnBoundary(
        TurnBoundaryConfig(
            complete_silence_ms=2000,
            recent_pause_multiplier=1.5,
            maximum_silence_ms=5000,
        )
    )
    boundary.speech_started(at_ms=0)
    boundary.speech_stopped(at_ms=1000)
    boundary.speech_started(at_ms=3000)
    boundary.speech_stopped(at_ms=3500)

    assert boundary.advance(at_ms=6000) is BoundaryState.candidate_pause
    assert boundary.advance(at_ms=6600) is BoundaryState.complete


def test_manual_finish_completes_without_waiting_for_silence_threshold() -> None:
    boundary = TurnBoundary(TurnBoundaryConfig(complete_silence_ms=5000))
    boundary.speech_started(at_ms=0)
    boundary.speech_stopped(at_ms=800)

    assert boundary.manual_complete(at_ms=1000) is BoundaryState.complete


def test_manual_finish_before_any_speech_keeps_waiting() -> None:
    boundary = TurnBoundary()

    assert boundary.manual_complete(at_ms=1000) is BoundaryState.candidate_pause
    assert boundary.first_response_ms is None


def test_vad_confirmed_silence_counts_toward_the_existing_total_window() -> None:
    boundary = TurnBoundary(TurnBoundaryConfig(complete_silence_ms=2800))
    boundary.speech_started(at_ms=0)

    boundary.speech_stopped(at_ms=1450, confirmed_silence_ms=450)

    assert boundary.speech_duration_ms == 1000
    assert boundary.advance(at_ms=3799) is BoundaryState.candidate_pause
    assert boundary.advance(at_ms=3800) is BoundaryState.complete

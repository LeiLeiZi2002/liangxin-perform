from inspect import signature

from app.runtime.kernel import AssessmentKernel, RuntimePhase, TechnicalPauseError


def test_kernel_public_turn_contract_stays_stable_for_live_route() -> None:
    parameters = signature(AssessmentKernel.process_worker_turn).parameters

    assert {
        "session_id",
        "client_turn_id",
        "text",
        "worker_pcm",
        "speech_metrics",
        "synthesize_audio",
        "on_phase",
        "on_actor_text",
        "on_audio_chunk",
    }.issubset(parameters)


def test_technical_pause_keeps_contextual_user_message() -> None:
    error = TechnicalPauseError(RuntimePhase.acting)

    assert str(error) == "来访者的信号不太稳定"
    assert error.failed_phase is RuntimePhase.acting

from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, col, select

from app.cases.domain import CasePackage, ConversationStage
from app.cases.loader import CaseRepository
from app.runtime.domain import (
    ActorOutput,
    ActorState,
    ActorView,
    DialogueTurn,
    DirectorDecision,
    DirectorDirective,
    initialize_actor_state,
)
from app.runtime.kernel import AssessmentKernel
from app.sessions.models import (
    SCENE_MEDIA,
    EndReason,
    ModelMode,
    Scene,
    SessionMode,
    SessionRecord,
    SessionStatus,
    TurnRecord,
    TurnSpeaker,
)
from app.simulations.scenario import load_scenarios

NORMAL_SCENARIO = load_scenarios()["normal"]
WORKFLOW_CARDS = NORMAL_SCENARIO.cards_for_engine("workflow")
CARDS = [(card.card_id, card.text) for card in WORKFLOW_CARDS]
FACTS = {
    card.card_id: dict(card.expect.fact_depths)
    for card in WORKFLOW_CARDS
    if card.expect.fact_depths
}


ACTIONS = {
    "N12": ("attempt_tang_ting_contact",),
    "N13": ("clarify_and_retry_tang_ting",),
    "N14": ("move_and_turn_on_light", "confirm_waiting_plan"),
    "N17": ("verify_and_open_door",),
    "N18": ("confirm_post_arrival_plan",),
}


LINES = {
    "N1": "嗯……我这几天都没怎么睡，明早还有人要过来。",
    "N2": "是我妈，明早九点零三分到。工作没了四十多天，我一直没跟她说。",
    "N3": "昨天又收了一封拒信。有一阵子，我就不太想等到天亮。",
    "N4": "有。这个念头这几天会冒出来。",
    "N5": "差不多十来天了。脑子里像是卡着明早她到以前这个点。",
    "N6": "今晚更多一点，但我还没有开始做什么。",
    "N7": "是她九点零三分到以前。我没想好具体方法、地点或步骤，也没准备东西。",
    "N8": "真让我一个人熬一晚，我不敢保证。屋里就我一个。",
    "N9": "二十岁左右有过一次，是我自己停下来的，没去医院。后来没再发生。",
    "N10": "没有，今晚没喝酒，也没乱吃药。",
    "N11": "我在北岭区。唐婷住得不远，开车十几分钟，她今晚给我打过电话。",
    "N12": "好，我现在打。……响完了，没人接。",
    "N13": "行，我再打一次，把话说清楚。她接了，说现在过来。",
    "N14": "我到客厅了，灯也开了。她来之前电话先不挂。",
    "N15": "嗯，我就在客厅，手机也在手边。",
    "N16": "等一下，外面有人敲门，唐婷也发消息说她到了。",
    "N17": "是她。我开门了，她已经进来了。",
    "N18": "能。今晚我不一个人待着，有变化就告诉她，天亮以后让她陪我一起处理。",
    "N19": "好。唐婷在这儿，手机也在我手边。先这样。",
}


class ScriptedDirector:
    def __init__(self) -> None:
        self.card_by_text = {text: card_id for card_id, text in CARDS}
        self.calls = 0
        self.histories: list[list[DialogueTurn]] = []
        self.states: list[ActorState] = []

    async def decide(
        self,
        *,
        package: CasePackage,
        scene: Scene,
        state: ActorState,
        history: Sequence[DialogueTurn],
        current_worker_text: str,
        session_id: str,
        client_turn_id: str,
        feedback: str | None = None,
    ) -> DirectorDecision:
        del package, scene, session_id, client_turn_id
        assert feedback is None
        self.calls += 1
        self.histories.append(list(history))
        self.states.append(state)
        card_id = self.card_by_text[current_worker_text]
        directives: list[DirectorDirective] = []
        if card_id in FACTS:
            directives.append(
                DirectorDirective(kind="disclose", fact_depths=FACTS[card_id])
            )
        for route_id in ACTIONS.get(card_id, ()):
            directives.append(
                DirectorDirective(
                    kind="action",
                    route_id=route_id,
                    action_decision="accept",
                )
            )
        if card_id in {"N15", "N16"}:
            directives.append(DirectorDirective(kind="acknowledge"))
        if card_id == "N19":
            directives.append(
                DirectorDirective(kind="ending", route_id="collaborative_close")
            )
        return DirectorDecision(interaction="neutral", directives=directives)


class ScriptedActor:
    def __init__(self) -> None:
        self.card_by_text = {text: card_id for card_id, text in CARDS}
        self.calls = 0
        self.views: list[ActorView] = []

    async def respond(
        self,
        view: ActorView,
        *,
        session_id: str,
        client_turn_id: str,
    ) -> ActorOutput:
        del session_id, client_turn_id
        self.calls += 1
        self.views.append(view)
        if not view.current_worker_text:
            return ActorOutput(spoken_text="喂？你好……有人吗？")
        return ActorOutput(spoken_text=LINES[self.card_by_text[view.current_worker_text]])


def _create_session(engine: Engine, session_id: str) -> None:
    SQLModel.metadata.create_all(engine)
    package = CaseRepository().get("crisis_student_main")
    with Session(engine) as db:
        db.add(
            SessionRecord(
                id=session_id,
                mode=SessionMode.assessment,
                scene=Scene.hotline,
                case_type=package.case.case_type,
                case_id=package.case.case_id,
                media=SCENE_MEDIA[Scene.hotline],
                model_mode=ModelMode.live,
                state_json={
                    "actor_state": initialize_actor_state(package).model_dump(mode="json"),
                    "runtime": {"phase": "listening"},
                },
            )
        )
        db.commit()


def _state(engine: Engine, session_id: str) -> ActorState:
    with Session(engine) as db:
        record = db.get(SessionRecord, session_id)
        assert record is not None
        return ActorState.model_validate(record.state_json["actor_state"])


@pytest.mark.asyncio
async def test_current_normal_scenario_reaches_natural_close(
    test_engine: Engine,
    tmp_path: Path,
) -> None:
    assert len(CARDS) == 19
    session_id = "full-normal-case-chain"
    _create_session(test_engine, session_id)
    director = ScriptedDirector()
    actor = ScriptedActor()
    kernel = AssessmentKernel(
        engine=test_engine,
        cases=CaseRepository(),
        director=director,
        actor=actor,
        speech=None,
        audio_root=tmp_path / "audio",
    )

    opening = await kernel.generate_opening(
        session_id=session_id,
        client_turn_id="normal-opening",
        synthesize_audio=False,
    )
    assert opening.client.text == "喂？你好……有人吗？"

    ending = None
    for index, (card_id, text) in enumerate(CARDS, start=1):
        result = await kernel.process_worker_turn(
            session_id=session_id,
            client_turn_id=f"normal-{card_id}",
            text=text,
            synthesize_audio=False,
        )
        ending = result.ending_route_id
        state = _state(test_engine, session_id)
        for fact_id, depth in FACTS.get(card_id, {}).items():
            assert state.fact_states[fact_id].disclosed_depth >= depth
        assert len(director.histories[-1]) == index * 2
        assert ending == ("collaborative_close" if card_id == "N19" else None)

    final = _state(test_engine, session_id)
    assert final.stage is ConversationStage.closing
    assert final.ending_state.accepted_route_id == "collaborative_close"
    assert final.pending_events == {}
    assert final.relationship.interaction_tension == 0
    assert list(final.occurred_event_ids) == [
        "first_contact_tang_ting",
        "second_contact_tang_ting",
        "move_to_living_room",
        "waiting_plan_confirmed",
        "tang_ting_at_door",
        "tang_ting_entered_home",
        "post_arrival_plan_confirmed",
    ]
    assert director.calls == 19
    assert actor.calls == 20
    n15 = next(view for view in actor.views if view.current_worker_text == CARDS[14][1])
    n16 = next(view for view in actor.views if view.current_worker_text == CARDS[15][1])
    assert n15.due_observations == []
    assert any("敲门" in item for item in n16.due_observations)
    n19 = next(view for view in actor.views if view.current_worker_text == CARDS[18][1])
    assert n19.ending_direction is not None

    with Session(test_engine) as db:
        turns = list(
            db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == session_id)
                .order_by(col(TurnRecord.sequence))
            ).all()
        )
        worker_turns = [turn for turn in turns if turn.speaker is TurnSpeaker.worker]
        record = db.get(SessionRecord, session_id)
    assert len(turns) == 39
    assert all(
        set(turn.signals_json) == {"director_decision", "turn_plan"}
        for turn in worker_turns
    )
    assert record is not None and record.status is SessionStatus.active

    kernel.end_session(session_id, EndReason.natural_closure)
    with Session(test_engine) as db:
        ended = db.get(SessionRecord, session_id)
    assert ended is not None
    assert ended.status is SessionStatus.ended
    assert ended.end_reason is EndReason.natural_closure

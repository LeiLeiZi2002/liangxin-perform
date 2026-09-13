"""核对双场景人物材料及实际出站输入，不以替身回答证明真人感。"""

import json
from types import SimpleNamespace

import pytest

from app.cases.loader import CaseRepository
from app.runtime.character_provider import (
    CharacterProvider,
    CharacterRepository,
    CharacterTranscriptTurn,
)
from app.runtime_config import RuntimeCredentialStore

CASE_ID = "marriage_boundary_main"


@pytest.mark.parametrize("scene", ("hotline", "online"))
def test_marriage_scene_explains_contact_choice_and_personal_privacy_concern(scene):
    character = CharacterRepository().get_for_case(CaseRepository().get(CASE_ID).case)
    selected = character.scene_profiles[scene]

    assert selected.get("service_context"), "场域材料缺少此次求助与媒介选择的来由"
    assert selected.get("current_concerns"), "隐私问句需要对应人物的具体顾虑"
    assert selected.get("after_boundary"), "隐私说明后需要允许接续而非反复重问"
    assert "许凯" in selected["current_concerns"]
    assert "不要求逐字复述" in selected["after_boundary"]
    if scene == "online":
        assert "登记" in selected["service_context"]
        assert "未发送" in selected["language_requirements"]
        assert "短回复" in selected["language_requirements"]
    else:
        assert "压低音量" in selected["service_context"]
        assert "转写" in selected["language_requirements"]


def test_marriage_common_dilemma_allows_strong_suspicion_without_settled_divorce():
    character = CharacterRepository().get(CASE_ID)
    conflicts = character.profile["inner_conflicts"]
    assert "当没发生" in conflicts["truth_and_tonight"]
    assert "明天" in conflicts["decision_boundary"]
    assert "还没有决定" in conflicts["decision_boundary"]
    assert "很怀疑" in "\n".join(character.rules)
    assert "不能确认许凯是否越界" in "\n".join(character.rules)
    assert "不重新制造同一顾虑" in "\n".join(character.rules)


@pytest.mark.parametrize("scene", ("hotline", "online"))
@pytest.mark.asyncio
async def test_marriage_provider_sends_shared_facts_only_selected_scene_and_full_history(scene):
    character = CharacterRepository().get_for_case(CaseRepository().get(CASE_ID).case)
    transcript = (
        CharacterTranscriptTurn(speaker="worker", text="我们可以先谈今晚怎么过。"),
        CharacterTranscriptTurn(speaker="client", text="今晚先不争了，果果还在睡。"),
    )
    current = "好。\n你还有什么想说的？"
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            id="marriage-material-test", usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "spoken_text": "我还想想明天怎么办。", "delivery_hint": "",
                "end_session": False, "action_request": "none",
            }, ensure_ascii=False)))],
        )

    store = RuntimeCredentialStore()
    store.update(api_key="test-key")
    provider = CharacterProvider(
        store,
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    await provider.respond(
        character=character, transcript=transcript, current_worker_text=current,
        opening=False, current_scene=scene,
        world_reality="本案例没有需要程序推进的外部现实事件。",
        allowed_world_actions=("none",),
    )
    assert len(requests) == 1 and "tools" not in requests[0]
    messages = requests[0]["messages"]
    prompt = messages[0]["content"]
    for key in ("observed_facts", "husband_explanations", "own_inferences", "safety_reality"):
        assert CharacterProvider._render_card(character.profile[key], depth=1) in prompt
    selected = character.scene_profiles[scene]
    for key in ("service_context", "current_concerns", "after_boundary"):
        assert key in selected, f"缺少人物场域材料：{key}"
        assert selected[key] in prompt
        other = "online" if scene == "hotline" else "hotline"
        assert character.scene_profiles[other][key] not in prompt
    assert messages[1:-2] == [
        {"role": "user", "content": transcript[0].text},
        {"role": "assistant", "content": transcript[1].text},
    ]
    assert messages[-1] == {"role": "user", "content": current}
    assert character.world is None

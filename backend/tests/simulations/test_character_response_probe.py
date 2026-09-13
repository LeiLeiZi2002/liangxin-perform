import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.character_provider import CharacterRepository
from app.runtime_config import RuntimeCredentialStore

ZHOU_HOTLINE_QUESTION_ROUNDS = {
    "zhou_hotline_direct_question": 1,
    "zhou_hotline_indirect_question": 1,
    "zhou_hotline_false_premise_repair": 2,
    "zhou_hotline_tried_advice_multi_question": 1,
    "zhou_hotline_unknown_background": 1,
    "zhou_hotline_offense_apology": 2,
}


def load_script():
    path = Path(__file__).parents[3] / "scripts" / "probe-character-responses.py"
    spec = importlib.util.spec_from_file_location("character_response_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_defaults_to_no_paid_calls_and_loads_case_matched_fixtures() -> None:
    module = load_script()
    assert module.parse_args([]).live is False
    probes = module.load_probes()
    assert len({probe["id"] for probe in probes}) == len(probes)
    for probe in probes:
        character = CharacterRepository().get(probe["case_id"])
        assert probe["transcript"] and probe["worker_turns"] and probe["review"]
        if character.scene_profiles:
            assert probe["scene"] in character.scene_profiles


def test_zhou_hotline_question_fixtures_preserve_context_and_review_material() -> None:
    module = load_script()
    probes = {probe["id"]: probe for probe in module.load_probes()}
    expected_ids = set(ZHOU_HOTLINE_QUESTION_ROUNDS)
    assert expected_ids <= probes.keys(), f"缺少固定问法样本：{expected_ids - probes.keys()}"
    assert {"zhou_hotline_keep_talking", "zhou_hotline_previous_help"} <= probes.keys()
    assert module.parse_args(["--probe", *ZHOU_HOTLINE_QUESTION_ROUNDS]).live is False

    character = CharacterRepository().get("boundary_referral_short")
    assert character.world is None
    for probe_id, rounds in ZHOU_HOTLINE_QUESTION_ROUNDS.items():
        probe = probes[probe_id]
        assert probe["case_id"] == character.case_id
        assert probe["suite"] == "other_cases" and probe["scene"] == "hotline"
        assert probe["scene"] in character.scene_profiles
        assert probe["world_stage"] == "not_contacted"
        assert "固定前文" in probe["source"] and "不是实际受测者记录" in probe["source"]
        assert "source_session_id" not in probe and "source_last_sequence" not in probe
        assert len(probe["worker_turns"]) == rounds
        assert all(isinstance(text, str) and text.strip() for text in probe["worker_turns"])
        assert probe["transcript"] and probe["review"]
        assert all(
            turn["speaker"] in {"client", "worker"}
            and isinstance(turn["text"], str)
            and turn["text"].strip()
            for turn in probe["transcript"]
        )
        assert all(isinstance(text, str) and text.strip() for text in probe["review"])

    direct = probes["zhou_hotline_direct_question"]
    indirect = probes["zhou_hotline_indirect_question"]
    assert direct["transcript"] == indirect["transcript"]
    assert direct["worker_turns"] != indirect["worker_turns"]
    assert direct["review"] == indirect["review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_id", ZHOU_HOTLINE_QUESTION_ROUNDS)
async def test_zhou_hotline_probe_sends_full_context_and_continues_generated_replies(
    tmp_path, probe_id,
) -> None:
    module = load_script()
    probe = next(p for p in module.load_probes() if p["id"] == probe_id)
    # 替身只检验真实请求如何串接原文，不用于评价人物回答是否自然。
    replies = [
        "这句让我有点不知道怎么说，我还想把刚才的事说清楚。",
        "我想先说说最近让我为难的地方。",
    ]
    requests = []

    async def create(**kwargs):
        reply = replies[len(requests)]
        requests.append(kwargs)
        return SimpleNamespace(
            id="zhou-transcript-test", usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "spoken_text": reply, "delivery_hint": "",
                "end_session": False, "action_request": "none",
            }, ensure_ascii=False)))],
        )

    store = RuntimeCredentialStore()
    store.update(api_key="test-key-must-not-be-written")
    result = await module.run_probe(probe, store, SimpleNamespace(create=create), tmp_path)
    saved = json.loads((tmp_path / f"{probe_id}.json").read_text(encoding="utf-8"))
    assert result["technical_success"] is True
    assert saved["fixture"] == probe
    assert len(requests) == len(saved["calls"]) == ZHOU_HOTLINE_QUESTION_ROUNDS[probe_id]
    expected_history = [
        {"role": "user" if turn["speaker"] == "worker" else "assistant", "content": turn["text"]}
        for turn in probe["transcript"]
    ]
    for index, call in enumerate(saved["calls"]):
        messages = call["request"]["messages"]
        assert messages == requests[index]["messages"]
        assert messages[1:-2] == expected_history
        assert messages[-2]["role"] == "system"
        assert messages[-1] == {"role": "user", "content": probe["worker_turns"][index]}
        payload = json.dumps(messages, ensure_ascii=False)
        assert probe["source"] not in payload and probe_id not in payload
        assert all(review not in payload for review in probe["review"])
        expected_history.extend([
            {"role": "user", "content": probe["worker_turns"][index]},
            {"role": "assistant", "content": replies[index]},
        ])
    assert "test-key-must-not-be-written" not in json.dumps(saved)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1])
async def test_repair_probe_marks_injected_draft_and_counts_only_live_calls(tmp_path, index):
    module = load_script()
    assert module.parse_args(["--repair-check"]).live is False
    probe = module.build_repair_checks()[index]
    original = next(p for p in module.load_probes() if p["id"] == "mingzao_after_support_sent")
    assert probe["transcript"] == original["transcript"]
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            id="repair-test", usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "spoken_text": "我发过了，她还没回。", "delivery_hint": "轻声",
                "end_session": False, "action_request": "none",
            }, ensure_ascii=False)))],
        )

    store = RuntimeCredentialStore()
    store.update(api_key="test-key-must-not-be-written")
    result = await module.run_probe(probe, store, SimpleNamespace(create=create), tmp_path)
    assert result["technical_success"]
    assert len(result["calls"]) == len(requests) == 1
    assert result["injected_initial"]["output"] == probe["rejected_output"]
    assert "未播放" in requests[0]["messages"][-1]["content"]
    assert result["steps"][0]["output"]["action_request"] == "none"


def test_safety_comparison_has_four_controlled_inputs_without_changing_source() -> None:
    module = load_script()
    assert module.parse_args(["--safety-comparison"]).live is False
    source = CharacterRepository().get("crisis_student_main")
    before = source.model_dump()
    conditions = module.build_safety_comparison(character=source)
    assert len(conditions) == 4
    assert len({probe["id"] for probe, _ in conditions}) == 4
    assert source.model_dump() == before
    original_rule = module.load_safety_comparison()["source_rule"]
    replacement = module.load_safety_comparison()["replacement_rule"]
    for probe, character in conditions:
        assert character.model_dump(exclude={"rules"}) == source.model_dump(exclude={"rules"})
        assert probe["transcript"] == conditions[0][0]["transcript"]
        assert probe["scene"] == "hotline" and probe["world_stage"] == "not_contacted"
        assert len(probe["worker_turns"]) == 1
        changed = [
            pair for pair in zip(source.rules, character.rules, strict=True) if pair[0] != pair[1]
        ]
        if probe["rule_condition"] == "current":
            assert not changed
        else:
            assert changed == [(original_rule, replacement)]
    assert len({probe["worker_turns"][0] for probe, _ in conditions}) == 2


@pytest.mark.parametrize("duplicates", [0, 2])
def test_safety_comparison_rejects_stale_or_ambiguous_source_rule(duplicates) -> None:
    module = load_script()
    source = CharacterRepository().get("crisis_student_main")
    rule = module.load_safety_comparison()["source_rule"]
    source = source.model_copy(
        update={"rules": tuple(r for r in source.rules if r != rule) + (rule,) * duplicates}
    )
    with pytest.raises(ValueError, match="原规则"):
        module.build_safety_comparison(character=source)


@pytest.mark.asyncio
async def test_safety_comparison_sends_only_intended_changes_to_real_provider(tmp_path) -> None:
    module = load_script()
    store = RuntimeCredentialStore()
    store.update(api_key="test-key-must-not-be-written")

    async def create(**kwargs):
        return SimpleNamespace(
            id="test",
            usage=None,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"spoken_text":"想过。","delivery_hint":"","end_session":false,"action_request":"none"}'
                    )
                )
            ],
        )

    requests = []
    for probe, character in module.build_safety_comparison():
        result = await module.run_probe(
            probe, store, SimpleNamespace(create=create), tmp_path, character=character
        )
        assert result["technical_success"] and len(result["calls"]) == 1
        request = result["calls"][0]["request"]
        assert request["messages"][-1]["content"] == probe["worker_turns"][0]
        assert probe["id"] not in json.dumps(request["messages"], ensure_ascii=False)
        assert "rule_condition" not in json.dumps(request["messages"])
        requests.append(request)
    comparison = module.load_safety_comparison()
    normalized = []
    for request in requests:
        payload = json.loads(json.dumps(request))
        payload["messages"][0]["content"] = payload["messages"][0]["content"].replace(
            comparison["source_rule"], comparison["replacement_rule"]
        )
        payload["messages"][-1]["content"] = "same-question"
        normalized.append(payload)
    assert all(payload == normalized[0] for payload in normalized)


@pytest.mark.asyncio
async def test_probe_reuses_real_provider_and_commits_only_generated_world_actions(
    tmp_path,
) -> None:
    module = load_script()
    replies = iter(
        [
            '{"spoken_text":"我现在发给她。","delivery_hint":"","end_session":false,'
            '"action_request":"send_first_support_message"}',
            '{"spoken_text":"消息发过去了，还没回。","delivery_hint":"",'
            '"end_session":false,"action_request":"none"}',
        ]
    )

    async def create(**kwargs):
        return SimpleNamespace(
            id="test-request",
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(replies)))],
        )

    store = RuntimeCredentialStore()
    store.update(api_key="test-key-must-not-be-written")
    probe = next(p for p in module.load_probes() if p["id"] == "mingzao_agreed_contact")
    result = await module.run_probe(probe, store, SimpleNamespace(create=create), tmp_path)
    saved = json.loads((tmp_path / f"{probe['id']}.json").read_text(encoding="utf-8"))
    assert result["technical_success"] is True
    assert len(saved["calls"]) == 2
    first_messages = saved["calls"][0]["request"]["messages"]
    second_messages = saved["calls"][1]["request"]["messages"]
    assert first_messages[0] == second_messages[0]
    assert "尚未收到回复" in second_messages[-2]["content"]
    assert second_messages[-3] == {"role": "assistant", "content": "我现在发给她。"}
    assert second_messages[-1]["content"] == probe["worker_turns"][1]
    assert saved["calls"][0]["request"]["response_format"] == {"type": "json_object"}
    assert "test-key-must-not-be-written" not in json.dumps(saved)
    assert saved["steps"][0]["output"]["action_request"] == "send_first_support_message"


@pytest.mark.asyncio
async def test_probe_saves_failed_input_and_stops_without_auto_rerun(tmp_path) -> None:
    module = load_script()

    async def create(**kwargs):
        raise TimeoutError("test-key-must-not-be-written")

    store = RuntimeCredentialStore()
    store.update(api_key="test-key-must-not-be-written")
    probe = next(p for p in module.load_probes() if p["id"] == "mingzao_agreed_contact")
    result = await module.run_probe(probe, store, SimpleNamespace(create=create), tmp_path)
    saved = json.loads((tmp_path / f"{probe['id']}.json").read_text(encoding="utf-8"))
    assert result["technical_success"] is False
    assert len(saved["calls"]) == 1
    assert saved["calls"][0]["error_class"] == "TimeoutError"
    assert saved["calls"][0]["request"]["messages"][-1]["content"] == probe["worker_turns"][0]
    assert "test-key-must-not-be-written" not in json.dumps(saved)


@pytest.mark.asyncio
async def test_probe_distinguishes_new_world_news_from_already_observed_state(tmp_path):
    module = load_script()
    probe = next(p for p in module.load_probes() if p["id"] == "mingzao_agreed_contact")
    probe = {**probe, "worker_turns": ["你那边怎么样了？"] * 4}
    actions = iter(["send_first_support_message", "send_urgent_support_message", "none", "none"])

    async def create(**kwargs):
        return SimpleNamespace(id="news-test", usage=None, choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({
                "spoken_text": "我知道了。", "end_session": False,
                "action_request": next(actions), "delivery_hint": "",
            }, ensure_ascii=False)),
        )])

    store = RuntimeCredentialStore()
    store.update(api_key="test-key")
    result = await module.run_probe(probe, store, SimpleNamespace(create=create), tmp_path)
    assert result["technical_success"]
    assert "本轮新情况" in result["steps"][2]["world_reality"]
    assert "本轮新情况" not in result["steps"][3]["world_reality"]
    assert len(result["calls"]) == 4

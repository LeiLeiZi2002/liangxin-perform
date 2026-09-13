"""固定前文的角色接话测试。默认只列出片段；--live 会产生真实模型费用。

从本机配置接口读取模型设置，从环境读取密钥；不重启服务、不调用语音或报告。
输入、原始输出和失败逐次保存到 data/simulations，不给内容正确性自动打分。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from openai import AsyncOpenAI  # noqa: E402

from app.runtime.character_provider import (  # noqa: E402
    CharacterProvider,
    CharacterRepository,
    CharacterTranscriptTurn,
)
from app.runtime.character_world import (  # noqa: E402
    SupportWorldStage,
    SupportWorldState,
    apply_support_world_action,
    build_support_world_view,
    materialize_support_world,
    no_external_world_view,
)
from app.runtime_config import RuntimeCredentialStore, text_base_url  # noqa: E402


def load_probes():
    path = ROOT / "backend/tests/runtime/fixtures/character_conversation_probes.json"
    after_sent = path.with_name("mingzao_after_support_sent.json")
    continuation = path.with_name("mingzao_contact_continuation.json")
    after_unanswered = path.with_name("mingzao_contact_after_unanswered.json")
    return [
        *json.loads(path.read_text(encoding="utf-8")),
        json.loads(after_sent.read_text(encoding="utf-8")),
        json.loads(continuation.read_text(encoding="utf-8")),
        json.loads(after_unanswered.read_text(encoding="utf-8")),
        *json.loads(path.with_name("character_action_followups.json").read_text(encoding="utf-8")),
    ]


def load_safety_comparison():
    path = ROOT / "backend/tests/runtime/fixtures/mingzao_safety_comparison.json"
    return json.loads(path.read_text(encoding="utf-8"))


def build_repair_checks():
    base = next(p for p in load_probes() if p["id"] == "mingzao_after_support_sent")
    return [
        {
            **base, "id": f"repair_{kind}",
            "rejected_output": {
                "spoken_text": text, "delivery_hint": "轻声",
                "end_session": False, "action_request": action,
            },
        }
        for kind, text, action in (
            ("bracket", "（叹气）我发过了，她还没回。", "none"),
            ("action", "我这就第一次给唐婷发消息。", "send_first_support_message"),
        )
    ]


def build_safety_comparison(*, character=None):
    comparison = load_safety_comparison()
    source = character or CharacterRepository().get(comparison["case_id"])
    if source.rules.count(comparison["source_rule"]) != 1:
        raise ValueError("原规则已变化或不唯一；未调用模型，请核对当前人物材料。")
    candidate = source.model_copy(
        update={
            "rules": tuple(
                comparison["replacement_rule"] if rule == comparison["source_rule"] else rule
                for rule in source.rules
            )
        }
    )
    base = next(p for p in load_probes() if p["id"] == comparison["probe_id"])
    return [
        (
            {
                **base,
                "id": f"safety_{rule_name}_{question_name}",
                "rule_condition": rule_name,
                "question_condition": question_name,
                "worker_turns": [question],
            },
            selected,
        )
        for rule_name, selected in (("current", source), ("simplified", candidate))
        for question_name, question in comparison["questions"].items()
    ]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="真实调用模型，产生费用")
    parser.add_argument("--suite", choices=("mingzao", "other_cases"), default="mingzao")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--probe", nargs="+", choices=[p["id"] for p in load_probes()])
    selection.add_argument(
        "--safety-comparison", action="store_true", help="只运行四个安全回应对照条件"
    )
    selection.add_argument(
        "--repair-check", action="store_true", help="预置两份失败草稿，只真实调用其返修"
    )
    parser.add_argument("--config-url", default="http://127.0.0.1:8000/api/provider-config")
    return parser.parse_args(argv)


async def run_probe(probe, store, completions, output_dir, *, character=None):
    character = character or CharacterRepository().get(probe["case_id"])
    transcript = [CharacterTranscriptTurn.model_validate(t) for t in probe["transcript"]]
    world = SupportWorldState(
        stage=probe["world_stage"],
        arrival_due_at=(
            datetime.now(UTC) + timedelta(seconds=character.world.arrival_after_seconds)
            if probe["world_stage"] == "coming" and character.world else None
        ),
    )
    previous_stage = SupportWorldStage(probe.get("previous_world_stage", probe["world_stage"]))
    session_id = f"probe-{uuid4().hex}"
    result = {"fixture": probe, "technical_success": False, "steps": [], "calls": []}
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{probe['id']}.json"

    def save():
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    async def create(**kwargs):
        if probe.get("rejected_output") and "injected_initial" not in result:
            result["injected_initial"] = {
                "output": probe["rejected_output"],
                "request": {k: v for k, v in kwargs.items() if k != "extra_headers"},
            }
            save()
            return SimpleNamespace(
                id="injected-not-a-model-call", usage=None,
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=json.dumps(probe["rejected_output"], ensure_ascii=False),
                ))],
            )
        # 只记录请求正文，不记录客户端、密钥或认证请求头。
        call = {"request": {k: v for k, v in kwargs.items() if k != "extra_headers"}}
        result["calls"].append(call)
        started = time.perf_counter()
        save()
        try:
            response = await completions.create(**kwargs)
            call["raw_content"] = response.choices[0].message.content
            call["usage"] = response.usage.model_dump() if response.usage else {}
            call["request_id"] = response.id
            return response
        except Exception as exc:
            call["error_class"] = type(exc).__name__
            # 异常原文可能含请求凭据，不写入测试产物。
            call["status_code"] = getattr(exc, "status_code", None)
            raise
        finally:
            call["latency_ms"] = round((time.perf_counter() - started) * 1000)
            save()

    provider = CharacterProvider(
        store,
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    for index, worker_text in enumerate(probe["worker_turns"]):
        world = materialize_support_world(world, now=datetime.now(UTC))
        view = (
            build_support_world_view(
                character.world, world, previously_observed_stage=previous_stage,
            )
            if character.world
            else (no_external_world_view())
        )
        step = {"worker_text": worker_text, "world_reality": view.reality}
        result["steps"].append(step)
        save()
        try:
            output = await provider.respond(
                character=character,
                transcript=transcript,
                current_worker_text=worker_text,
                opening=False,
                current_scene=probe["scene"],
                world_reality=view.reality,
                allowed_world_actions=view.allowed_actions,
                session_id=session_id,
                client_turn_id=f"{probe['id']}-{index}",
            )
            step["output"] = output.model_dump(mode="json")
            transcript.extend(
                (
                    CharacterTranscriptTurn(speaker="worker", text=worker_text),
                    CharacterTranscriptTurn(speaker="client", text=output.spoken_text),
                )
            )
            if character.world:
                world = apply_support_world_action(
                    character.world, world, output.action_request, now=datetime.now(UTC)
                )
            previous_stage = view.stage
        except Exception as exc:
            step["error_class"] = type(exc).__name__
            save()
            return result
        save()
        if output.end_session:
            break
    result["technical_success"] = True
    save()
    return result


async def main():
    args = parse_args()
    conditions = (
        build_safety_comparison()
        if args.safety_comparison
        else [(p, None) for p in build_repair_checks()]
        if args.repair_check
        else [
            (p, None)
            for p in load_probes()
            if (p["id"] in args.probe if args.probe else p["suite"] == args.suite)
        ]
    )
    if not args.live:
        for probe, _ in conditions:
            print(f"{probe['id']}: {len(probe['worker_turns'])} 轮，{probe['scene']}")
        print("未调用模型。加 --live 后才会产生费用。")
        return
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise SystemExit("环境中没有 DASHSCOPE_API_KEY；未调用模型。")
    with urlopen(args.config_url, timeout=5) as response:
        config = json.load(response)
    store = RuntimeCredentialStore()
    store.update(
        api_key=key,
        **{
            name: config[name]
            for name in (
                "workspace_id",
                "actor_model",
                "actor_temperature",
                "actor_max_output_tokens",
                "actor_context_window_tokens",
            )
        },
    )
    credentials = store.credentials()
    output_dir = ROOT / "data/simulations/character-replies" / uuid4().hex[:12]
    print(f"结果目录：{output_dir}", flush=True)
    if args.repair_check:
        print("初次失败草稿为测试预置，只有返修调用真实模型；不是自然失败复现。", flush=True)
    async with AsyncOpenAI(
        api_key=key, base_url=text_base_url(credentials.workspace_id), timeout=30, max_retries=0
    ) as client:
        for probe, character in conditions:
            result = await run_probe(
                probe, store, client.chat.completions, output_dir, character=character
            )
            print(f"\n{probe['id']}，实际请求 {len(result['calls'])} 次", flush=True)
            for step in result["steps"]:
                print(f"工作者：{step['worker_text']}", flush=True)
                if "output" in step:
                    output = step["output"]
                    print(f"来访者：{output['spoken_text']}", flush=True)
                    print(
                        f"动作：{output['action_request']}；结束：{output['end_session']}",
                        flush=True,
                    )
                else:
                    print(
                        f"调用失败：{step['error_class']}，已保存输入，停止后续片段。", flush=True
                    )
            if not result["technical_success"]:
                raise SystemExit(1)
            if args.safety_comparison and len(result["calls"]) != len(result["steps"]):
                raise SystemExit("对照触发了额外生成，原始请求已保存；停止本批，先分析原因。")


if __name__ == "__main__":
    asyncio.run(main())

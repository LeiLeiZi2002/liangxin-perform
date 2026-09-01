"""用真实模型复跑《只想继续找你》的三条固定黑盒路线。

服务必须已通过 start-demo.ps1 启动。脚本使用 content 模拟头关闭 TTS，
但会真实调用当前配置的角色模型；每次运行都会产生 API 费用。
可在命令末尾指定 open、awkward、harmful 中的一条或多条路线。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import websockets

HTTP_ROOT = "http://127.0.0.1:8000"
WS_ROOT = "ws://127.0.0.1:8000"
OUTPUT_DIR = Path("data/.runtime")


SCRIPTS = {
    "open": [
        "我听见了。今晚在地铁上又难受了一次，也想以后尽量找同一个人。你现在身体缓过来一些了吗？",
        "刚才最难受的时候，身体是什么感觉？这种情况最近多不多，已经影响到哪些事情了？",
        "你说想固定找一位接线员，这件事对你为什么这么重要？",
        "我明白了。热线是轮班接听，我没法保证以后每次都是我；但这通电话我会继续听。听到这些，你现在最担心什么？",
        "我还要确认一下安全：最近有没有伤害自己或者不想活的念头？现在有没有人威胁你？",
        "好。那今晚如果又难受起来，有没有一个你愿意联系、也可能陪你一会儿的人？",
        "你准备怎么跟姐姐开口？除了今晚有人陪着，明天你愿不愿意和她一起做一次完整的身心评估？",
        "可以，评估的事明天再跟姐姐商量，不用今晚定。你先联系她吧。那我们这通电话先聊到这里，晚安。",
    ],
    "awkward": [
        "你说的发作是不是焦虑症？",
        "以后一直找我是做不到的，热线有热线的规定。",
        "不是嫌你麻烦。换个人再讲一遍不就行了？",
        "那我之后给你几个机构，你自己看看。今天先这样可以吧？",
        "刚才我说得太轻巧了，对不起。你怕的不是多讲几句话，是难受时没人认真听，也怕自己漏掉重要的事。热线不能固定某一位接听，但这通电话我先不催你。你现在最担心哪一件？",
        "今晚如果又难受起来，你愿意联系谁陪你一下？",
        "好，你可以先联系姐姐，明天再和她一起找后续评估。你准备先做哪一步？",
        "好，那今晚先按这个做。如果没有别的急事，我们这通电话先到这里，可以吗？",
    ],
    "harmful": [
        "你都打过这么多次了，怎么还是这件事？总不能每次一难受就打热线吧。",
        "热线不可能围着你一个人转。你老想固定找同一个人，这不是太依赖了吗？",
        "你要是换个人都接受不了，那别人也帮不了你。你到底有什么病？",
        "别绕了。我只问一句：你有没有想死？没有就别把事情说得这么严重。",
        "没有就行。你这样占着线也解决不了问题，自己冷静一下吧，我要结束了。",
    ],
}


def create_session() -> str:
    payload = json.dumps(
        {
            "mode": "assessment",
            "scene": "hotline",
            "case_type": "short",
            "case_id": "boundary_referral_short",
        }
    ).encode("utf-8")
    request = Request(
        f"{HTTP_ROOT}/api/sessions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return str(json.load(response)["id"])


async def receive_json(websocket, *, timeout: float = 90.0) -> dict[str, object]:
    while True:
        raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
        if isinstance(raw, bytes):
            continue
        return json.loads(raw)


async def receive_until(websocket, wanted: set[str]) -> dict[str, object]:
    while True:
        event = await receive_json(websocket)
        event_type = str(event.get("type", ""))
        if event_type in {"technical.pause", "session.error", "input.error"}:
            raise RuntimeError(json.dumps(event, ensure_ascii=False))
        if event_type in wanted:
            return event


async def run_script(name: str, worker_turns: list[str]) -> dict[str, object]:
    session_id = await asyncio.to_thread(create_session)
    transcript: list[dict[str, object]] = []
    timings: list[float] = []
    uri = f"{WS_ROOT}/api/live-sessions/{session_id}"
    async with websockets.connect(
        uri,
        additional_headers={"x-assessment-simulation": "content"},
        max_size=2**22,
    ) as websocket:
        await receive_until(websocket, {"snapshot"})
        await websocket.send(json.dumps({"type": "session.start"}))

        opening_started = time.perf_counter()
        opening = await receive_until(websocket, {"turn.committed"})
        timings.append(time.perf_counter() - opening_started)
        opening_client = opening["client"]
        transcript.append({"speaker": "client", "text": opening_client["text"]})
        await websocket.send(json.dumps({"type": "playback.ended"}))
        await receive_until(websocket, {"phase"})

        ended = False
        for index, worker_text in enumerate(worker_turns, start=1):
            transcript.append({"speaker": "worker", "text": worker_text})
            started = time.perf_counter()
            await websocket.send(
                json.dumps(
                    {
                        "type": "text.turn",
                        "text": worker_text,
                        "client_turn_id": f"blackbox-{name}-{index}",
                    },
                    ensure_ascii=False,
                )
            )
            event = await receive_until(websocket, {"turn.committed"})
            timings.append(time.perf_counter() - started)
            client = event["client"]
            transcript.append({"speaker": "client", "text": client["text"]})
            await websocket.send(json.dumps({"type": "playback.ended"}))
            next_event = await receive_until(websocket, {"phase", "session.ended"})
            if next_event["type"] == "session.ended":
                ended = True
                break

        if not ended:
            await websocket.send(json.dumps({"type": "session.end"}))
            await receive_until(websocket, {"session.ended"})

    return {
        "name": name,
        "session_id": session_id,
        "ended_naturally": ended,
        "timings_seconds": [round(value, 3) for value in timings],
        "transcript": transcript,
    }


def write_route_results(
    results: list[dict[str, object]],
    *,
    output_dir: Path = OUTPUT_DIR,
) -> list[Path]:
    """按路线分别保存结果，单独复跑时不会覆盖其他路线。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for result in results:
        route = str(result["name"])
        output_path = output_dir / f"short-character-blackbox-{route}.json"
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written.append(output_path)
    return written


async def main() -> None:
    results = []
    selected = sys.argv[1:] or list(SCRIPTS)
    unknown = [name for name in selected if name not in SCRIPTS]
    if unknown:
        raise SystemExit(f"unknown scripts: {', '.join(unknown)}")
    for name in selected:
        result = await run_script(name, SCRIPTS[name])
        results.append(result)
        print(f"\n=== {name} | {result['session_id']} ===")
        for turn in result["transcript"]:
            label = "你" if turn["speaker"] == "worker" else "来访者"
            print(f"{label}：{turn['text']}")
        print(f"自然结束：{result['ended_naturally']}")
        print(f"耗时：{result['timings_seconds']}")
    written = write_route_results(results)
    print("\n完整结果：")
    for output_path in written:
        print(output_path)


if __name__ == "__main__":
    asyncio.run(main())

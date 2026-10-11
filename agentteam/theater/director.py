"""导演：派轮次、执行界面操作（含读档的全局栅栏）、按观察者的判据暂停。

导演是唯一持有"客观时间线"的岗位。角色各自只记得自己那份上下文，所以
"到底发生了什么"必须由导演从事件日志里读出来——它不是任何角色的记忆。

一轮的次序是固定的：
  1. advance + 写一条 scene 事件（本轮的情境，只有在场者能看到）
  2. 在场角色依次行动（演员循环），行动写成 turn 事件
  3. 演员提出的界面操作在这里执行（开界面 → 操作 → 关界面）
  4. 结构层查泄漏
  5. 每轮结尾给每个角色注入状态块
  6. 观察者报分叉与暂停判据
"""
from __future__ import annotations

import json
from typing import Callable

from ..config import PROMPTS_DIR, Config
from ..llm import chat, check_cancelled
from ..recorder import Recorder
from . import actor as actor_mod
from . import observer as observer_mod
from .context import build_messages, inject_state_all
from .world import TheaterError, World


def default_beats(world: World, world_date: str | None = None) -> list[dict]:
    return [{"world_date": world_date or world.world_date,
             "stage": "（本场开始。每个人只知道自己记得的事。）",
             "cast": world.characters}]


def render_prose(cfg: Config, rec: Recorder, world: World, request: str,
                 mock_text: str | None = None) -> str:
    """写手只做渲染：把事件日志变成散文。它不决定剧情，也不能补设定。"""
    check_cancelled(cfg)
    card = (PROMPTS_DIR / "theater_writer.md").read_text(encoding="utf-8").strip()
    log = []
    for event in world.path_events():
        if event.kind in {"state", "interface"}:
            continue
        log.append({"turn": event.turn, "world_date": event.world_date, "kind": event.kind,
                    "actor": event.actor, "public": event.public,
                    "present": list(event.present)})
    user = ("# 本场目标\n" + request
            + "\n\n# 客观事件日志（唯一依据，不得增删）\n"
            + json.dumps(log, ensure_ascii=False, indent=2))
    messages = [{"role": "system", "content": card}, {"role": "user", "content": user}]
    fallback = "\n\n".join(f"【第{e['turn']}轮 · {e['world_date']}】{e['public']}" for e in log)
    text, usage, elapsed = chat(cfg, messages, mock_text=mock_text or fallback)
    rec.record(role="写手", model=cfg.model, messages=messages, response=text,
               usage=usage, elapsed=elapsed)
    return text


def run_scene(
    cfg: Config,
    rec: Recorder,
    world: World,
    *,
    request: str,
    rules: str,
    bibles: dict[str, str],
    beats: list[dict] | None = None,
    observe: bool = True,
    on_event: Callable[[str, object], None] | None = None,
) -> dict:
    emit = on_event or (lambda kind, payload=None: None)
    beats = beats or default_beats(world)
    prompts = {name: actor_mod.actor_prompt(name, bibles.get(name, ""), rules)
               for name in world.characters}
    inject_state_all(world)

    issues: list[dict] = []
    ops: list[dict] = []
    reports: list[dict] = []
    paused = None
    played = 0

    emit("start", {"characters": world.characters, "beats": len(beats)})
    for index, beat in enumerate(beats):
        cast = [n for n in beat.get("cast", world.characters) if n in world.characters]
        world.advance(beat.get("world_date"))
        played += 1
        # stage 是字符串 → 一条人人可见的世界事件；是 {角色: 文字} → 各人只看到自己那一段。
        # 双线戏就靠后者：两人在两个地方，各自的情境不进对方的上下文。
        stage = beat.get("stage")
        if isinstance(stage, dict):
            for name, text in stage.items():
                if name in world.characters:
                    world.append("scene", public=text, present=(name,))
        elif stage:
            world.append("scene", public=stage, present=cast)
        emit("turn", {"index": played, "world_date": world.world_date, "cast": cast,
                      "branch": world.live[0]})

        rewound = False
        for name in cast:
            if not world.alive(name):
                emit("skip", {"actor": name, "reason": "已死亡，没有自动回档"})
                continue
            if world.paused_by is not None:
                # 别人正拿着操作窗口；这个打算连同旧现场一起被覆盖（R03）。
                world.declare_intent(name, "本轮想行动")
                emit("skip", {"actor": name, "reason": "世界正被他人暂停"})
                continue
            reply = actor_mod.take_turn(
                cfg, rec, world, name, prompts[name],
                actor_mod.mock_reply(world.live[1], name, _mock_op(world, name, played)))
            emit("actor", {"actor": name, "action": reply["action"],
                           "inner": reply["inner"], "interface": reply["interface_raw"]})
            try:
                world.append("turn", actor=name, public=reply["action"],
                             private=reply["inner"], present=cast)
            except TheaterError as exc:
                issues.append({"kind": "illegal_action", "actor": name, "message": str(exc)})
                continue
            hits = observer_mod.leak_check(world, name, reply["action"] + "\n" + reply["inner"])
            if hits:
                issues.extend(hits)
                emit("leak", {"actor": name, "hits": hits})
            op = actor_mod.apply_interface(world, name, reply["op"])
            if op:
                ops.append(op)
                emit("op", op)
                if op.get("op") == "load" and op.get("ok"):
                    # 读档把世界换掉了：本轮剩下的行动者已经不在这个世界里，
                    # 他们的行动从未发生（R03）。立刻重建所有人的上下文与状态块。
                    inject_state_all(world)
                    emit("rewind", op["detail"])
                    rewound = True
                    break

        inject_state_all(world)
        if rewound:
            emit("turn_ended", {"reason": "读档改写了世界，本轮就此结束"})
        if observe:
            try:
                report = observer_mod.observe(cfg, rec, world, request, issues)
            except (ValueError, json.JSONDecodeError) as exc:
                report = {"status": "ok", "divergences": [], "reason": f"观察者输出无效：{exc}",
                          "structural": []}
            reports.append(report)
            emit("observer", report)
            if report["status"] == "pause":
                paused = report["reason"]
                emit("pause", {"reason": paused})
                break

    prose = render_prose(cfg, rec, world, request)
    return {
        "request": request,
        "prose": prose,
        "played": played,
        "live": {"branch": world.live[0], "turn": world.live[1], "world_date": world.world_date},
        "paused": paused,
        "issues": issues,
        "interface_ops": ops,
        "observer_reports": reports,
        "graph": world.snapshot_graph(),
        "state": world.character_state(),
        "history": world.history(),
        "discarded": world.discarded(),
        "belief": actor_mod.belief_matrix(world),
        "contexts": {n: world.turn_blocks(n) for n in world.characters},
    }


def _mock_op(world: World, name: str, turn: int) -> dict | None:
    """mock 模式：让流程能走到存/读档，而不必真的问模型。

    第 1、3 轮存槽，第 4 轮读槽 1——正好把"全局回退 + 读档者豁免"走一遍。
    """
    empty = [i for i, s in enumerate(world.slots(name), start=1) if s is None]
    if turn in (1, 3) and empty:
        return {"op": "save", "slot": empty[0]}
    if turn == 4 and world.slots(name)[0]:
        return {"op": "load", "slot": 1}
    return None


def beats_for(world: World, turns: int, cast: list[str] | None = None,
              world_date: str | None = None) -> list[dict]:
    """把 N 轮摊成 N 个 beat。想让世界日期跳跃，直接改 beats 里的 world_date。"""
    names = cast or world.characters
    date = world_date or world.world_date
    return [{"world_date": date, "cast": names,
             "stage": f"（第{i + 1}轮。各人只依据自己记得的事行动。）"} for i in range(max(1, turns))]

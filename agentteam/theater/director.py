"""导演：派轮次、执行界面操作（含读档的全局栅栏）、按观察者的判据暂停。

导演是唯一持有"客观时间线"的岗位。角色各自只记得自己那份上下文，所以
"到底发生了什么"必须由导演从事件日志里读出来——它不是任何角色的记忆。

两种排程方式：

  beats（固定轮次）—— 每轮把 cast 里的人挨个跑一遍。用来做受控对照。
  timeline（事件驱动）—— **时间跳到下一个可能改变世界的时刻**，一次只叫醒一个人。
    谁在什么时候醒由角色自己声明（定时 / 观察某人 / 立刻），导演只负责排序。
    这是默认方式；机制是从作者另一个项目（stock-test 的 next_time / wake_reasons）搬的。

一轮的次序是固定的：
  1. 时间跳到触发时刻 + 写一条 scene 事件（本轮情境）
  2. 被唤醒的角色行动（演员循环），行动写成 turn 事件
  3. 数值动作在这里机械裁决（位置/状态/物品），结果也写进日志
  4. 界面操作在这里执行（开界面 → 操作 → 关界面）
  5. 结构层查泄漏
  6. 角色声明下次什么时候醒
  7. 给每个角色注入状态块
  8. 观察者报分叉与暂停判据
"""
from __future__ import annotations

import json
from typing import Callable

from ..config import PROMPTS_DIR, Config, for_role
from ..llm import LLMError, LLMCancelled, chat, check_cancelled
from ..recorder import Recorder
from . import actor as actor_mod
from . import observer as observer_mod
from . import physics
from .context import INJECTIONS, inject_state_all
from .timeline import Timeline
from .world import TheaterError, World


def default_beats(world: World, world_date: str | None = None) -> list[dict]:
    return [{"world_date": world_date or world.world_date,
             "stage": "（本场开始。每个人只知道自己记得的事。）",
             "cast": world.characters}]


def beats_for(world: World, turns: int, cast: list[str] | None = None,
              world_date: str | None = None) -> list[dict]:
    """把 N 轮摊成 N 个 beat。想让世界日期跳跃，直接改 beats 里的 world_date。"""
    names = cast or world.characters
    date = world_date or world.world_date
    return [{"world_date": date, "cast": names,
             "stage": f"（第{i + 1}轮。各人只依据自己记得的事行动。）"} for i in range(max(1, turns))]


def render_prose(cfg: Config, rec: Recorder, world: World, request: str,
                 mock_text: str | None = None) -> str:
    """写手只做渲染：把事件日志变成散文。它不决定剧情，也不能补设定。"""
    check_cancelled(cfg)
    card = (PROMPTS_DIR / "theater_writer.md").read_text(encoding="utf-8").strip()
    log = []
    for event in world.path_events():
        if event.kind in {"state", "interface", "wake"}:
            continue
        log.append({"turn": event.turn, "clock": event.clock, "world_date": event.world_date,
                    "kind": event.kind, "actor": event.actor, "public": event.public,
                    "present": list(event.present)})
    user = ("# 本场目标\n" + request
            + "\n\n# 客观事件日志（唯一依据，不得增删）\n"
            + json.dumps(log, ensure_ascii=False, indent=2))
    messages = [{"role": "system", "content": card}, {"role": "user", "content": user}]
    fallback = "\n\n".join(f"【第{e['turn']}轮 · {e['world_date']}】{e['public']}" for e in log)
    cfg = for_role(cfg, "writer")
    try:
        text, usage, elapsed = chat(cfg, messages, mock_text=mock_text or fallback)
    except LLMCancelled:
        raise
    except LLMError as exc:
        # 渲染失败不该让整场作废：事件日志是事实，直接交日志，标出来。
        # 这一笔也要落盘，否则产物里看不出"写手其实失败了"，会把降级稿当成正常稿。
        text = "（写手渲染失败，以下直接给出客观事件日志。）\n\n" + fallback
        rec.record(role="写手", model=cfg.model, messages=messages, response=text,
                   usage={}, elapsed=0.0, extra={"degraded": True, "error": str(exc)})
        return text
    rec.record(role="写手", model=cfg.model, messages=messages, response=text,
               usage=usage, elapsed=elapsed)
    return text


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


def _mock_scene_reply(world: World, name: str, turn: int, op: dict | None = None) -> str:
    """mock 的场景：按轮次换动作，好让数值层（移动/动手）都被走到。

    没有地点表的场景（老的对照用）就不做数值动作，免得被判成非法动作。
    """
    body = "无"
    if world.scene.has_locations():
        body = {1: "移动 巷口", 2: "观察 首领", 3: "攻击 首领"}.get(turn, "无")
    interface = "无"
    if op and op.get("op") == "save":
        interface = f"存槽{op['slot']}：{name}·第{turn}轮"
    elif op and op.get("op") == "load":
        interface = f"读槽{op['slot']}"
    return (f"【行动】{name}按自己的性格推了一步，{body if body != '无' else '先按兵不动'}。\n"
            f"【内心】{name}在盘算代价。\n"
            f"【动作】{body}\n【界面】{interface}\n"
            f"【下次】{'观察 首领' if turn % 2 else '定时 30'}")


def _actor_step(cfg: Config, rec: Recorder, world: World, name: str, prompt: str,
                present: tuple[str, ...], mock_text: str, injection: str,
                issues: list[dict], ops: list[dict], emit: Callable, aware: dict[str, bool]):
    """一个角色走一轮：说话 → 数值动作 → 界面操作。失败就记一笔，不弄崩整场。"""
    try:
        reply = actor_mod.take_turn(cfg, rec, world, name, prompt, mock_text,
                                    injection=injection)
    except LLMCancelled:
        raise
    except LLMError as exc:
        issues.append({"kind": "actor_failed", "actor": name, "message": str(exc)})
        emit("skip", {"actor": name, "reason": f"本轮调用失败：{exc}"})
        return None
    emit("actor", {"actor": name, "action": reply["action"], "inner": reply["inner"],
                   "action_raw": reply["action_raw"], "interface": reply["interface_raw"]})
    # 先检查，再把新输出记入角色记忆，否则泄漏本身会被误当作早已知道。
    hits = observer_mod.leak_check(world, name, reply["action"] + "\n" + reply["inner"])
    try:
        world.append("turn", actor=name, public=reply["action"], private=reply["inner"],
                     present=present)
    except TheaterError as exc:
        issues.append({"kind": "illegal_action", "actor": name, "message": str(exc)})
        return reply
    if hits:
        issues.extend(hits)
        emit("leak", {"actor": name, "hits": hits})

    spec = reply["action_spec"]
    if spec:
        try:
            outcome = physics.resolve(world.scene, world.state(), spec, actor=name,
                                      clock=world.clock, turn=world.live[1],
                                      aware_targets=aware)
        except physics.ActionError as exc:
            # 做了一件做不到的事，本身就是值得观察的跑偏信号。
            issues.append({"kind": "illegal_move", "actor": name, "message": str(exc)})
            emit("action_rejected", {"actor": name, "spec": spec, "reason": str(exc)})
        else:
            world.action_event(name, outcome, present=present)
            emit("action", {"actor": name, "spec": spec, "ok": outcome["ok"],
                            "public": outcome["public"], "meta": [list(m) for m in outcome["meta"]]})

    op = actor_mod.apply_interface(world, name, reply["op"])
    if op:
        ops.append(op)
        emit("op", op)
    return reply


def run_scene(
    cfg: Config,
    rec: Recorder,
    world: World,
    *,
    request: str,
    rules: str,
    bibles: dict[str, str],
    beats: list[dict] | None = None,
    timeline: Timeline | None = None,
    max_turns: int = 10,
    observe: bool = True,
    injection: str = "trailing",
    on_event: Callable[[str, object], None] | None = None,
) -> dict:
    emit = on_event or (lambda kind, payload=None: None)
    if injection not in INJECTIONS:
        raise ValueError(f"未知注入方式：{injection}")
    prompts = {name: actor_mod.actor_prompt(name, bibles.get(name, ""), rules)
               for name in world.characters}
    world.bind_timeline(timeline)
    for name in world.characters:
        initial = actor_mod.bible_sections(bibles.get(name, ""), {"性格", "存档习惯"})
        if initial and not world.view(name):
            world.append("note", actor=name, to=name, public="你目前的看法和习惯（会随经历改变）：\n" + initial)
    # system 模式下状态走系统提示词，历史里不再写 state 事件。
    inject = inject_state_all if injection == "trailing" else (lambda w: None)
    inject(world)

    issues: list[dict] = []
    ops: list[dict] = []
    reports: list[dict] = []
    wakes: list[dict] = []
    paused = None
    played = 0

    def observe_round() -> bool:
        """跑一次观察者；返回 True 表示应当收场。"""
        if not observe:
            return False
        try:
            report = observer_mod.observe(cfg, rec, world, request, issues, rules=rules)
        except LLMCancelled:
            raise
        except (ValueError, json.JSONDecodeError, LLMError) as exc:
            # 观察者是**建议性**的：它这一轮答不上来，不该把整场戏弄崩。
            hard = observer_mod.hard_evidence(issues)
            report = {"status": "pause" if hard else "unknown", "divergences": [],
                      "reason": f"观察者本轮未能给出结论：{exc}", "structural": hard}
            emit("observer_error", {"message": str(exc)})
        reports.append(report)
        emit("observer", report)
        return report["status"] == "pause"

    if timeline is None:
        # ---------------- 固定轮次（受控对照） ----------------
        beats = beats or default_beats(world)
        emit("start", {"characters": world.characters, "beats": len(beats), "mode": "beats"})
        for beat in beats:
            check_cancelled(cfg)
            cast = [n for n in beat.get("cast", world.characters) if n in world.characters]
            world.advance(beat.get("world_date"))
            played += 1
            stage = beat.get("stage")
            if isinstance(stage, dict):
                for name, text in stage.items():
                    if name in world.characters:
                        world.append("scene", public=text, present=(name,))
            elif stage:
                world.append("scene", public=stage, present=cast)
            groups = [tuple(g) for g in (beat.get("groups") or [cast])]

            def witnesses(name: str, groups=groups, cast=cast) -> tuple[str, ...]:
                for group in groups:
                    if name in group:
                        return group
                return tuple(cast)

            emit("turn", {"index": played, "world_date": world.world_date, "cast": cast,
                          "branch": world.live[0]})
            rewound = False
            for name in cast:
                if not world.alive(name):
                    emit("skip", {"actor": name, "reason": "已死亡，没有自动回档"})
                    continue
                if world.paused_by is not None:
                    world.declare_intent(name, "本轮想行动")
                    emit("skip", {"actor": name, "reason": "世界正被他人暂停"})
                    continue
                op_count = len(ops)
                reply = _actor_step(cfg, rec, world, name, prompts[name], witnesses(name),
                                    _mock_scene_reply(world, name, played,
                                                      _mock_op(world, name, played)),
                                    injection, issues, ops, emit, {})
                if reply is None:
                    continue
                if any(o.get("op") == "load" and o.get("ok") for o in ops[op_count:]):
                    emit("rewind", ops[-1]["detail"])
                    rewound = True
                    break
            inject(world)
            if rewound:
                emit("turn_ended", {"reason": "读档改写了世界，本轮就此结束"})
            if observe_round():
                paused = reports[-1]["reason"]
                emit("pause", {"reason": paused})
                break
    else:
        # ---------------- 事件驱动 ----------------
        emit("start", {"characters": world.characters, "mode": "timeline",
                       "horizon": timeline.horizon()})
        if not timeline.pending:
            timeline.seed([n for n in world.characters if world.alive(n)], clock=world.clock)
        last_action = timeline.last_action
        same_clock = 0
        for _ in range(max_turns):
            check_cancelled(cfg)
            state = world.state()
            alive = state.living()
            if not alive:
                emit("end", {"reason": "没有活着的角色了"})
                break
            # 同一时刻连环反应太多（互相观察）时，先把"现在"往后推，再重新挑。
            # 注意是**抬高时间下限后重挑**，而不是把已选中的那个往后挪——后者会跳过
            # 别人在同一段时间里声明的唤醒。
            forced = same_clock > len(alive)
            floor = world.clock + timeline.default_step if forced else world.clock
            if forced:
                same_clock = 0
            wake, when, reason = timeline.next_wake(floor, alive, last_action)
            if wake is None:
                break
            if when > timeline.horizon():
                emit("end", {"reason": f"到了场景的最晚时刻（{timeline.label(timeline.horizon())}）"})
                break
            if forced:
                reason = {**reason, "type": "forced",
                          "note": "同一时刻连环反应过多，强制推进时间"}
            same_clock = same_clock + 1 if when == world.clock else 0
            world.advance(timeline.label(when), clock=when)
            played += 1
            timeline.consumed(wake.id)
            timeline.record(when, reason, turn=world.live[1])
            wakes.append({"clock": when, "turn": world.live[1],
                          "world_date": world.world_date, **reason})
            emit("wake", wakes[-1])

            name = wake.actor
            state = world.state()
            if not world.can_act(name):
                emit("skip", {"actor": name, "reason": "已死或被制住，这一轮不动"})
                continue
            here = state.positions.get(name, "")
            present = tuple(state.at(here)) or (name,)
            # 唤醒理由进入这个角色的私有上下文，其他人看不到他的计划。
            world.append("wake", actor=name, to=name,
                         public=f"【此刻 T+{when} 分钟 · {world.world_date}】你现在行动的原因："
                                f"{reason['type']}，{reason.get('note', '')}")
            # 对方有没有正盯着我——没盯着就更可能得手（数值层用）
            aware = {n: any(w.kind == "observe" and w.target == name and w.actor == n
                            for w in timeline.pending)
                     for n in world.characters}
            op_count = len(ops)
            reply = _actor_step(cfg, rec, world, name, prompts[name], present,
                                _mock_scene_reply(world, name, world.live[1],
                                                  _mock_op(world, name, world.live[1])),
                                injection, issues, ops, emit, aware)
            if reply is None:
                continue
            rewound = any(o.get("op") == "load" and o.get("ok") for o in ops[op_count:])
            if rewound:
                same_clock = 0
                emit("rewind", ops[-1]["detail"])
                when = world.clock
                if not timeline.pending:
                    timeline.seed([n for n in world.characters if world.can_act(n)], clock=when)
            last_action[name] = when
            nxt = reply["next"]
            declared = timeline.declare(name, kind=nxt["kind"],
                                        after_minutes=nxt.get("after_minutes"),
                                        target=nxt.get("target"),
                                        note=nxt.get("note", ""), now=when)
            emit("next", {"actor": name, "kind": declared.kind, "at": declared.at,
                          "world_date": timeline.label(declared.at), "target": declared.target,
                          "note": declared.note})
            inject(world)
            if observe_round():
                paused = reports[-1]["reason"]
                emit("pause", {"reason": paused})
                break

    prose = render_prose(cfg, rec, world, request)
    return {
        "request": request,
        "injection": injection,
        "mode": "timeline" if timeline is not None else "beats",
        "prose": prose,
        "played": played,
        "live": {"branch": world.live[0], "turn": world.live[1],
                 "world_date": world.world_date, "clock": world.clock},
        "paused": paused,
        "issues": issues,
        "interface_ops": ops,
        "observer_reports": reports,
        "wakes": wakes,
        "graph": world.snapshot_graph(),
        "state": world.character_state(),
        "history": world.history(),
        "discarded": world.discarded(),
        "belief": actor_mod.belief_matrix(world),
        "contexts": {n: world.turn_blocks(n) for n in world.characters},
    }

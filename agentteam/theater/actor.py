"""演员：一个角色一个只追加的扮演循环。

一轮的流程是固定的三步：
  1. 用 context.build_messages 装配上下文（静态系统提示 + 只追加的历史 + 上轮尾块）
  2. 调模型，拿到「行动 / 内心 / 界面」三段
  3. 把行动与内心写成一条 turn 事件；界面操作交给导演执行

演员**不做任何剧情判断**，也不知道别人的状态和未来。它唯一的信息来源就是
自己的上下文，这是结构性的隔离，不靠提示词承诺。
"""
from __future__ import annotations

import json
import re

from ..config import PROMPTS_DIR, Config, for_role
from ..llm import chat, check_cancelled
from ..recorder import Recorder
from .context import build_messages
from .world import SLOT_COUNT, TheaterError, World

# 界面操作的解析。留出宽松格式，模型写错也不会把整轮弄崩。
_OP_PATTERNS = (
    ("save", re.compile(r"存槽\s*([123])\s*[:：]?\s*(.*)")),
    ("load", re.compile(r"读槽\s*([123])")),
    ("delete", re.compile(r"删槽\s*([123])")),
    ("note", re.compile(r"备注\s*[:：]\s*(.+)")),
)
_FIELD = re.compile(r"【(行动|内心|界面)】\s*([\s\S]*?)(?=\n?【(?:行动|内心|界面)】|$)")


def actor_prompt(name: str, bible: str, rules: str) -> str:
    """这个角色的静态系统提示：一次构造，整场不变（前缀缓存的前提）。"""
    card = (PROMPTS_DIR / "theater_actor.md").read_text(encoding="utf-8").strip()
    return (card.replace("{{name}}", name).replace("{{bible}}", bible or "（缺人物卡）")
            + "\n\n# 你确知的机制（这是你自己会用的东西，不是别人的秘密）\n" + rules)


def parse_reply(text: str) -> dict:
    """把模型的回复拆成三段。解析失败就整段当行动，保证循环不崩。"""
    fields = {k: v.strip() for k, v in _FIELD.findall(text)}
    action = fields.get("行动") or (text.strip() if not fields else "")
    interface = fields.get("界面", "")
    op = None
    if interface and interface.strip() not in {"无", "（无）", "-", "none", "None"}:
        for kind, pattern in _OP_PATTERNS:
            match = pattern.search(interface)
            if not match:
                continue
            if kind == "save":
                op = {"op": "save", "slot": int(match.group(1)), "note": (match.group(2) or "").strip()}
            elif kind == "note":
                op = {"op": "note", "text": match.group(1).strip()}
            else:
                op = {"op": kind, "slot": int(match.group(1))}
            break
        else:
            op = {"op": "unknown", "raw": interface.strip()}
    return {"action": action.strip(), "inner": fields.get("内心", "").strip(),
            "interface_raw": interface.strip(), "op": op}


def take_turn(cfg: Config, rec: Recorder, world: World, name: str, system_prompt: str,
              mock_text: str, injection: str = "trailing") -> dict:
    """走完一轮。返回解析结果；界面操作由调用方（导演）执行。"""
    check_cancelled(cfg)
    messages = build_messages(world, name, system_prompt, injection=injection)
    scoped = for_role(cfg, name)
    text, usage, elapsed = chat(scoped, messages, mock_text=mock_text)
    rec.record(role=f"演员_{name}", model=scoped.model, messages=messages,
               response=text, usage=usage, elapsed=elapsed,
               extra={"turn": world.live[1], "branch": world.live[0],
                      "context_messages": len(messages), "injection": injection})
    return parse_reply(text)


def apply_interface(world: World, name: str, op: dict | None) -> dict | None:
    """执行演员提出的界面操作。世界在此期间暂停（R03：开界面即暂停）。

    返回给观察台的事件摘要；操作非法（槽空、已死）时记一条 issue 而不抛给上层，
    因为"角色试图做一件做不到的事"本身就是值得观察的跑偏信号。
    """
    if not op or op.get("op") == "unknown":
        return None
    world.open_interface(name)
    try:
        if op["op"] == "save":
            entry = world.save(name, op["slot"], op.get("note", ""))
            return {"actor": name, "op": "save", "slot": op["slot"], "ok": True,
                    "detail": f"存槽{op['slot']}（{entry.world_date}）"}
        if op["op"] == "load":
            summary = world.load(name, op["slot"])
            return {"actor": name, "op": "load", "slot": op["slot"], "ok": True, "detail": summary}
        if op["op"] == "delete":
            world.delete(name, op["slot"])
            return {"actor": name, "op": "delete", "slot": op["slot"], "ok": True,
                    "detail": f"删槽{op['slot']}"}
        if op["op"] == "note":
            world.note(name, op["text"])
            return {"actor": name, "op": "note", "ok": True, "detail": op["text"][:60]}
        return None
    except TheaterError as exc:
        return {"actor": name, "op": op.get("op"), "ok": False, "detail": str(exc)}
    finally:
        if world.paused_by == name:
            world.close_interface(name)


def mock_reply(turn: int, name: str, op: dict | None = None) -> str:
    """mock 模式下按轮次给不同的假回复，让流程（含存/读档）都能被走到。"""
    interface = "无"
    if op and op.get("op") == "save":
        interface = f"存槽{op['slot']}：{name}·第{turn}轮"
    elif op and op.get("op") == "load":
        interface = f"读槽{op['slot']}"
    return (f"【行动】{name}环顾四周，把刚才听到的话在心里过了一遍，然后开口。\n"
            f"【内心】{name}在盘算：这件事能不能用已有认知解释。\n【界面】{interface}")


def belief_matrix(world: World) -> dict:
    """信念 vs 事实：谁看得见哪些客观事件。给观察者和观察台用。"""
    objective = world.path_events()
    ids = {id(e): i + 1 for i, e in enumerate(objective)}
    return {
        "objective": [
            {"id": ids[id(e)], "turn": e.turn, "kind": e.kind, "actor": e.actor,
             "public": e.public[:120], "present": list(e.present)}
            for e in objective
        ],
        "knows": {name: sorted(ids[id(e)] for e in world.view(name)) for name in world.characters},
        "blind_spots": {name: sorted(set(ids.values()) - {ids[id(e)] for e in world.view(name)})
                        for name in world.characters},
    }


def dump_matrix(matrix: dict) -> str:
    return json.dumps(matrix, ensure_ascii=False, indent=2)


__all__ = ["actor_prompt", "parse_reply", "take_turn", "apply_interface", "mock_reply",
           "belief_matrix", "dump_matrix", "SLOT_COUNT"]

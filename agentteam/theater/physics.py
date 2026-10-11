"""最小数值层：位置、状态、手里的东西，以及一个**确定性**的冲突裁决器。

为什么必须有这一层：如果"谁赢"由 LLM 决定，那比赛就退化成"谁的叙述更能说服裁决者"。
所以凡是需要判胜负的部分都做成机械的、可复现的；对话与心理完全交给模型。

数值层只保留三样（再少就不足以机械裁决，再多就挤压扮演空间）：

  位置  —— 谁能看见谁，以及能不能到得了
  状态  —— ok / injured / restrained / dead
  物品  —— 手里有什么，影响动手的成功率

裁决用**按内容定种子的随机数**，不用全局 `random`：同样的输入永远得到同样的结果，
而且不依赖调用顺序——这样中断恢复、重放、多次实验都能对得上。
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

CONDITIONS = ("ok", "injured", "restrained", "dead")
OPS = ("move", "attack", "restrain", "take", "use", "observe", "none")


class ActionError(ValueError):
    """非法动作。记下来当跑偏信号，不抛给上层把整场弄崩。"""


@dataclass(frozen=True)
class Location:
    name: str
    label: str = ""
    neighbors: tuple[str, ...] = ()

    def text(self) -> str:
        return self.label or self.name


@dataclass
class Scene:
    """场景的初始状态。角色的位置/状态/物品随后全部由事件派生，这里只给起点。"""

    locations: dict[str, Location] = field(default_factory=dict)
    positions: dict[str, str] = field(default_factory=dict)
    conditions: dict[str, str] = field(default_factory=dict)
    inventory: dict[str, tuple[str, ...]] = field(default_factory=dict)
    items: dict[str, str] = field(default_factory=dict)   # 物品 -> 初始所在地点
    seed: str = "theater"

    @classmethod
    def from_dict(cls, data: dict, characters: list[str]) -> "Scene":
        locations = {name: Location(name, spec.get("label", ""), tuple(spec.get("neighbors", ())))
                     for name, spec in (data.get("locations") or {}).items()}
        for loc in locations.values():
            for neighbor in loc.neighbors:
                if neighbor not in locations:
                    raise ActionError(f"地点 {loc.name} 的相邻地点 {neighbor} 不存在")
        positions, conditions, inventory = {}, {}, {}
        for name in characters:
            spec = (data.get("characters") or {}).get(name, {})
            positions[name] = spec.get("location") or (next(iter(locations), ""))
            conditions[name] = spec.get("condition", "ok")
            inventory[name] = tuple(spec.get("inventory", ()))
            if positions[name] not in locations:
                raise ActionError(f"{name} 的初始地点 {positions[name]} 不在地点表里")
            if conditions[name] not in CONDITIONS:
                raise ActionError(f"{name} 的初始状态 {conditions[name]} 非法")
        items = dict(data.get("items") or {})
        for item, where in items.items():
            if where not in locations:
                raise ActionError(f"物品 {item} 所在地点 {where} 不存在")
        return cls(locations=locations, positions=positions, conditions=conditions,
                   inventory=inventory, items=items, seed=data.get("seed", "theater"))

    def has_locations(self) -> bool:
        return bool(self.locations)


@dataclass(frozen=True)
class State:
    """从事件日志派生出来的数值状态（只读快照）。"""

    positions: dict[str, str]
    conditions: dict[str, str]
    inventory: dict[str, tuple[str, ...]]
    items: dict[str, str]

    def alive(self, name: str) -> bool:
        return self.conditions.get(name, "ok") != "dead"

    def living(self) -> list[str]:
        return [n for n in self.positions if self.alive(n)]

    def at(self, location: str) -> list[str]:
        return [n for n, loc in self.positions.items() if loc == location and self.alive(n)]

    def nearby(self, name: str) -> list[str]:
        """同地点、且活着的人（不含自己）——位置决定谁能看见谁。"""
        return [n for n in self.at(self.positions.get(name, "")) if n != name]


def roll(seed: str, *parts: object) -> float:
    """按内容定种子的随机数：同输入同结果，且与调用顺序无关。"""
    key = ":".join([seed, *(str(p) for p in parts)])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16)).random()


def hit_chance(state: State, actor: str, target: str, *, aware: bool) -> float:
    """动手的成功率。只依赖数值层——不看模型的文笔。"""
    chance = 0.5
    if state.conditions.get(actor) == "injured":
        chance -= 0.20
    if state.inventory.get(actor):
        chance += 0.20                      # 手里有东西
    if state.conditions.get(target) == "injured":
        chance += 0.15
    if state.conditions.get(target) == "restrained":
        chance += 0.50
    if not aware:
        chance += 0.15                      # 对方没在盯着你
    return min(0.95, max(0.05, chance))


def _hurt(condition: str) -> str:
    return {"ok": "injured", "injured": "dead", "restrained": "dead"}.get(condition, condition)


def resolve(scene: Scene, state: State, action: dict, *, actor: str, clock: int, turn: int,
            aware_targets: dict[str, bool] | None = None) -> dict:
    """把一个动作判成结果。返回 {"ok", "public", "private", "meta"}。

    纯函数：不改 state，只给出"该追加哪些事件"。`meta` 里的**三元组**就是数值层的
    状态变更（("position", 角色, 地点) / ("condition", 角色, 状态) / ("item", 角色, 物品)，
    物品前加 "-" 表示失去），二元组只是给人看的审计信息。由调用方（导演）写进日志，
    这样数值层也不会破坏"除三槽外一切由日志派生"的不变式。
    """
    op = action.get("op")
    if op not in OPS:
        raise ActionError(f"未知动作：{op}")
    if not state.alive(actor):
        raise ActionError(f"{actor} 已死，做不了动作")
    aware_targets = aware_targets or {}

    if op == "none":
        return {"ok": True, "public": "", "private": "", "meta": ()}

    if op == "move":
        dest = action.get("dest") or ""
        here = scene.locations.get(state.positions.get(actor, ""))
        if dest not in scene.locations:
            raise ActionError(f"没有这个地点：{dest}")
        if here and dest not in here.neighbors and dest != here.name:
            raise ActionError(f"{here.name} 不通往 {dest}")
        return {"ok": True, "public": f"{actor}去了{scene.locations[dest].text()}。",
                "private": "", "meta": (("move", dest), ("position", actor, dest))}

    if op in ("attack", "restrain"):
        target = action.get("target") or ""
        if target not in state.positions or target == actor:
            raise ActionError(f"没有这个对手：{target}")
        if not state.alive(target):
            raise ActionError(f"{target} 已经死了")
        if state.positions.get(target) != state.positions.get(actor):
            raise ActionError(f"{actor} 和 {target} 不在同一个地方，够不着")
        chance = hit_chance(state, actor, target, aware=aware_targets.get(target, False))
        value = roll(scene.seed, clock, turn, actor, op, target)
        success = value < chance
        verb = "动手" if op == "attack" else "动手控制"
        if success:
            if op == "attack":
                after = _hurt(state.conditions.get(target, "ok"))
            else:
                after = "dead" if state.conditions.get(target) in ("injured", "restrained") else "restrained"
            outcome = {"dead": "死了", "injured": "受了伤", "restrained": "被制住了"}[after]
            return {"ok": True,
                    "public": f"{actor}对{target}{verb}——{target}{outcome}。",
                    "private": "",
                    "meta": (("chance", f"{chance:.2f}"), ("roll", f"{value:.2f}"),
                             ("condition", target, after))}
        return {"ok": False,
                "public": f"{actor}对{target}{verb}，没得手，自己反而吃了亏。",
                "private": "",
                "meta": (("chance", f"{chance:.2f}"), ("roll", f"{value:.2f}"),
                         ("condition", actor, "injured"))}

    if op == "take":
        item = action.get("item") or ""
        if item not in scene.items:
            raise ActionError(f"没有这个物品：{item}")
        if scene.items[item] != state.positions.get(actor):
            raise ActionError(f"{item} 不在这里")
        return {"ok": True, "public": f"{actor}拿起了{item}。",
                "private": "", "meta": (("item", actor, item),)}

    if op == "use":
        item = action.get("item") or ""
        if item not in state.inventory.get(actor, ()):
            raise ActionError(f"{actor} 手里没有{item}")
        return {"ok": True, "public": f"{actor}用掉了{item}。",
                "private": "", "meta": (("item", actor, f"-{item}"),)}

    # observe
    target = action.get("target") or ""
    if target not in state.positions:
        raise ActionError(f"没有这个人：{target}")
    same = state.positions.get(target) == state.positions.get(actor)
    where = scene.locations.get(state.positions.get(target, ""))
    detail = (f"{target}就在{where.text() if where else '这里'}，状态是"
              f"{state.conditions.get(target, 'ok')}"
              + (f"，手里有{'、'.join(state.inventory.get(target, ()))}"
                 if state.inventory.get(target) else "，空着手")
              ) if same else f"{target}不在你能看到的地方"
    return {"ok": True, "public": f"{actor}留意了{target}的动静。",
            "private": detail, "meta": (("observe", target),)}


def condition_text(condition: str) -> str:
    return {"ok": "完好", "injured": "受伤", "restrained": "被制住", "dead": "已死"}.get(condition, condition)

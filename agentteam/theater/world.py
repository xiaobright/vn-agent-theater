"""角色剧场：把"上下文即记忆"做成运行时。

设定原文本身就要求这套语义，所以运行时和设定是同构的：

  R01.2  读档用目标快照覆写"现在"，但**所有角色的存档库不随世界回溯回退**
  R02    读档后：世界 = 目标快照；**主动读档者** = 读档前一刻的记忆；
         其他人 = 目标快照中各自的记忆；所有人的存档库 = 读档前的存档库
  R03    存档界面就是暂停界面；任何角色打开菜单的瞬间世界停住；
         暂停是私人的；不存在两人同时读档；暂停本身不改写世界

因此 World 只有一件**非派生**状态：每个角色的三槽清单。其余全部是
`(live, branches)` 的纯函数 —— 记忆、世界日期、生死、可操作资格都从
只追加的事件日志推出来。于是"读档 = 全局回退 + 读档者豁免"只是移动一个
指针并给读档者挂一段记忆覆盖层，不需要同步任何状态。

推论（也是本模块的核心不变式，测试里会断言）：
  除了 `_slots`，World 的任何可观察量都可以由 `(live, branches)` 重算出来。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

SLOT_COUNT = 3

# 事件类型：
#   scene      世界事件（在场者都能观察到）
#   turn       角色的一轮行动：public 是别人能看到的，private 是只有他自己知道的内心
#   note       备注 / 跨时间线留言（只给收件人）
#   memory     记忆覆盖层：读档者"读档前一刻的记忆"（只给他自己）
#   pause      打开界面的暂停（只给他自己；R03：暂停是私人的）
#   interface  存 / 读 / 删档的操作记录（只给他自己）
#   state      每轮结尾注入的状态块（只给他自己）
_KINDS = {"scene", "turn", "note", "memory", "pause", "interface", "state"}
_PRIVATE_KINDS = {"memory", "pause", "interface", "state"}


class TheaterError(RuntimeError):
    """违反设定或运行时的前置条件（槽位为空、两人同时读档等）。"""


@dataclass(frozen=True)
class Event:
    """只追加、永不改写的一条记录。turn 是它所属的轮次。"""

    turn: int
    kind: str
    actor: str | None
    public: str
    private: str = ""
    world_date: str = ""
    present: tuple[str, ...] = ()   # 空元组 = 世界事件，人人可见
    to: str | None = None           # note / state 的收件人
    # 数值层的状态变更，形如 ("position", 角色, 地点) / ("condition", 角色, 状态)
    # / ("item", 角色, 物品) / ("item", 角色, "-物品")。元组长度可变，所以用变长元组。
    meta: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise TheaterError(f"未知事件类型：{self.kind}")
        if self.kind in _PRIVATE_KINDS and self.actor is None:
            raise TheaterError(f"{self.kind} 必须属于某个角色")
        if not (self.public or self.private):
            raise TheaterError("事件内容不能为空")

    def meta_get(self, key: str) -> str | None:
        """只认二元组（旧格式），如 ("killed", 角色)。"""
        return next((t[1] for t in self.meta if len(t) == 2 and t[0] == key), None)

    def changes(self) -> tuple[tuple[str, ...], ...]:
        """数值层的状态变更（三元组）。"""
        return tuple(t for t in self.meta if len(t) == 3)

    def visible_to(self, actor: str) -> bool:
        if self.to is not None:
            return self.to == actor
        if self.kind in _PRIVATE_KINDS:
            return self.actor == actor
        if not self.present:
            return True
        return actor in self.present

    def text_for(self, actor: str) -> str:
        """只有本人能读到 private 部分（内心独白、自己开的界面）。"""
        if self.actor == actor and self.private:
            return f"{self.public}\n{self.private}"
        return self.public


@dataclass
class Branch:
    """一条只追加的历史线。fork_turn 之前的轮次继承自 parent。"""

    id: str
    parent: str | None
    fork_turn: int
    events: list[Event] = field(default_factory=list)
    label: str = ""

    def tail_turn(self) -> int:
        return max((e.turn for e in self.events), default=self.fork_turn)


@dataclass(frozen=True)
class Slot:
    """一份存档 = 一个世界快照指针。因为记忆由指针派生，(branch, turn) 就是全部世界状态。"""

    index: int
    branch: str
    turn: int
    world_date: str
    note: str


class World:
    """唯一的事实来源。除三槽外的一切都可从 (live, branches) 重算。

    `scene` 只提供数值层的**起点**（初始位置/状态/物品）；当前的位置、状态、物品
    同样是事件日志的函数——这条不变式不能破，否则回档会回不干净。
    """

    def __init__(self, world_date: str, characters: Iterable[str],
                 scene: "Scene | None" = None, clock: int = 0):
        names = list(characters)
        if not names or len(set(names)) != len(names):
            raise TheaterError("角色名单不能为空或重复")
        from .physics import Scene, State
        self._branches: dict[str, Branch] = {"main": Branch("main", None, 0, label="主时间线")}
        self._live_branch = "main"
        self._live_turn = 0
        self._live_date = world_date
        self._names = names
        self._scene = scene or Scene(positions={n: "" for n in names},
                                     conditions={n: "ok" for n in names},
                                     inventory={n: () for n in names})
        self._clock = clock
        self._state_cache: tuple | None = None
        self._slots: dict[str, list[Slot | None]] = {n: [None] * SLOT_COUNT for n in names}
        self._fork_seq = 0
        self._paused_by: str | None = None
        self._intents: dict[str, str] = {}
        self._discarded: list[dict] = []
        self._history: list[dict] = []   # 给观察台的"发生了什么"时间线

    # ---------------- 只读视图 ----------------

    @property
    def characters(self) -> list[str]:
        return list(self._names)

    @property
    def live(self) -> tuple[str, int]:
        return self._live_branch, self._live_turn

    @property
    def world_date(self) -> str:
        return self._live_date

    @property
    def clock(self) -> int:
        """世界时钟（分钟，从场景起点算）。事件驱动的时间线靠它排序。"""
        return self._clock

    @property
    def scene(self):
        return self._scene

    @property
    def paused_by(self) -> str | None:
        return self._paused_by

    def state(self):
        """数值层现状：位置 / 状态 / 物品。全部由事件派生，所以回档会一起回退。"""
        key = (self._live_branch, self._live_turn,
               sum(len(b.events) for b in self._branches.values()))
        if self._state_cache is not None and self._state_cache[0] == key:
            return self._state_cache[1]
        from .physics import State
        pos = dict(self._scene.positions)
        cond = dict(self._scene.conditions)
        inv = {n: list(self._scene.inventory.get(n, ())) for n in self._names}
        loc_items = dict(self._scene.items)
        for event in self.path_events():
            for change in event.changes():
                kind, who, value = change[0], change[1], change[2]
                if kind == "position":
                    pos[who] = value
                elif kind == "condition":
                    cond[who] = value
                elif kind == "item":
                    if value.startswith("-"):
                        name = value[1:]
                        if name in inv.get(who, ()):
                            inv[who].remove(name)
                    else:
                        inv.setdefault(who, []).append(value)
                        loc_items.pop(value, None)
        state = State(positions=pos, conditions=cond,
                      inventory={k: tuple(v) for k, v in inv.items()}, items=loc_items)
        self._state_cache = (key, state)
        return state

    def path_events(self, branch: str | None = None, turn: int | None = None) -> list[Event]:
        """把某条分支某轮之前（含）的全部事件按顺序摊平。

        父分支只贡献到子分支 fork_turn 为止，所以被放弃的未来仍然留在父分支上，
        可以再被某个快照指回去（R01.4：读更晚的快照 = 分支切换）。
        """
        branch = branch or self._live_branch
        turn = self._live_turn if turn is None else turn
        chain: list[Branch] = []
        node: Branch | None = self._branches[branch]
        while node is not None:
            chain.append(node)
            node = self._branches[node.parent] if node.parent else None
        chain.reverse()
        events: list[Event] = []
        for i, b in enumerate(chain):
            cap = turn if i == len(chain) - 1 else chain[i + 1].fork_turn
            events.extend(e for e in b.events if e.turn <= cap)
        return events

    def view(self, actor: str) -> list[Event]:
        """这个角色此刻能看到/记得的全部事件。看不到的就不存在——隔离是结构性的。"""
        if actor not in self._slots:
            raise TheaterError(f"没有这个角色：{actor}")
        return [e for e in self.path_events() if e.visible_to(actor)]

    def turn_blocks(self, actor: str) -> list[dict]:
        """按轮次分组的可读上下文，供观察台并排展示各角色的记忆。"""
        blocks: list[dict] = []
        for e in self.view(actor):
            if not blocks or blocks[-1]["turn"] != e.turn:
                blocks.append({"turn": e.turn, "world_date": e.world_date, "events": []})
            blocks[-1]["events"].append({"kind": e.kind, "actor": e.actor, "text": e.text_for(actor)})
        return blocks

    def alive(self, name: str) -> bool:
        """生死由日志派生：回溯到被杀之前，人自然就还活着（R02：世界含所有人的大脑状态）。"""
        return self.state().alive(name)

    def can_act(self, name: str) -> bool:
        state = self.state()
        return (state.alive(name) and state.conditions.get(name) != "restrained"
                and self._paused_by in (None, name))

    def slots(self, name: str) -> list[dict | None]:
        return [None if s is None else
                {"slot": s.index, "branch": s.branch, "turn": s.turn,
                 "world_date": s.world_date, "note": s.note, "live": (s.branch, s.turn) == self.live}
                for s in self._slots[name]]

    def character_state(self) -> dict[str, dict]:
        state = self.state()
        return {n: {"alive": state.alive(n),
                    "condition": state.conditions.get(n, "ok"),
                    "can_act": self.can_act(n),
                    "location": state.positions.get(n, ""),
                    "location_label": (self._scene.locations[state.positions[n]].text()
                                       if state.positions.get(n) in self._scene.locations else ""),
                    "inventory": list(state.inventory.get(n, ())),
                    "nearby": state.nearby(n),
                    "slots": self.slots(n), "memory_events": len(self.view(n))}
                for n in self._names}

    def snapshot_graph(self) -> dict:
        """给观察台画的版本图：分支、父边、当前指针、每个角色槽位指到哪。"""
        return {
            "live": {"branch": self._live_branch, "turn": self._live_turn,
                     "world_date": self._live_date, "clock": self._clock},
            "branches": [
                {"id": b.id, "parent": b.parent, "fork_turn": b.fork_turn,
                 "tail_turn": b.tail_turn(), "label": b.label, "events": len(b.events)}
                for b in self._branches.values()
            ],
            "slots": {n: self.slots(n) for n in self._names},
        }

    def history(self) -> list[dict]:
        return list(self._history)

    def discarded(self) -> list[dict]:
        """被"覆盖掉、从未发生"的意图（R03：先开界面的人拿走整个操作窗口）。"""
        return list(self._discarded)

    # ---------------- 推进 ----------------

    def advance(self, world_date: str | None = None, clock: int | None = None) -> int:
        """开新的一轮。世界日期可以显式给（时间跳跃），不给就沿用。

        clock 是世界时钟（分钟）：事件驱动的时间线直接跳到下一个触发时刻。
        """
        if world_date:
            self._live_date = world_date
        if clock is not None:
            if clock < self._clock:
                raise TheaterError(f"世界时钟不能倒退：{self._clock} → {clock}")
            self._clock = clock
        self._live_turn += 1
        self._intents.clear()
        return self._live_turn

    def append(self, kind: str, *, actor: str | None = None, public: str = "", private: str = "",
               present: Iterable[str] = (), to: str | None = None,
               meta: Iterable[tuple[str, ...]] = ()) -> Event:
        if actor is not None and actor not in self._slots:
            raise TheaterError(f"没有这个角色：{actor}")
        if kind == "turn" and actor is not None and not self.can_act(actor):
            raise TheaterError(f"{actor} 此刻不能行动（已死或世界被他人暂停）")
        event = Event(turn=self._live_turn, kind=kind, actor=actor, public=public, private=private,
                      world_date=self._live_date, present=tuple(present), to=to, meta=tuple(meta))
        self._branches[self._live_branch].events.append(event)
        return event

    def kill(self, name: str, reason: str) -> Event:
        """真死：没有自动回档（R03）。要救只能由别人读一个含他的快照。"""
        if name not in self._slots:
            raise TheaterError(f"没有这个角色：{name}")
        self._log("kill", actor=None, detail=f"{name} 死亡：{reason}")
        return self.append("scene", public=f"{name} 死亡。{reason}",
                           meta=(("killed", name), ("condition", name, "dead")))

    def action_event(self, actor: str, outcome: dict, present: Iterable[str] = ()) -> Event:
        """把数值层判出来的结果写进日志。数值变更只以 meta 的形式存在，
        所以位置/状态/物品依旧是日志的函数——回档会一起回退。"""
        return self.append("scene", actor=actor, public=outcome["public"],
                           private=outcome.get("private", ""), present=present,
                           meta=outcome.get("meta", ()))

    # ---------------- 暂停与意图（R03） ----------------

    def open_interface(self, actor: str) -> None:
        """打开存取档菜单 = 世界停住。先到先得，不存在两人同时读档。"""
        if self._paused_by is not None:
            raise TheaterError(f"世界已被 {self._paused_by} 暂停；不存在两人同时读档")
        if not self.alive(actor):
            raise TheaterError(f"{actor} 已死，开不了界面")
        self._paused_by = actor
        self.append("pause", actor=actor, public="（你打开了存取档界面。世界停住了。）",
                    private="其他人感知不到世界停过。")
        self._log("pause", actor=actor, detail="打开界面，世界暂停")

    def close_interface(self, actor: str) -> None:
        if self._paused_by != actor:
            raise TheaterError(f"当前不是 {actor} 在操作界面")
        self._paused_by = None
        self._log("resume", actor=actor, detail="关闭界面，世界继续（暂停本身不改写世界）")

    def declare_intent(self, actor: str, intent: str) -> bool:
        """声明"我打算开界面"。若世界已被别人暂停，这个打算连同旧现场一起被覆盖，从未发生。"""
        if self._paused_by is not None and self._paused_by != actor:
            self._discarded.append({"actor": actor, "intent": intent, "lost_to": self._paused_by,
                                    "turn": self._live_turn})
            self._log("intent_discarded", actor=actor,
                      detail=f"打算「{intent}」被 {self._paused_by} 的操作窗口覆盖，从未发生")
            return False
        self._intents[actor] = intent
        return True

    # ---------------- 存 / 读 / 删 ----------------

    def save(self, actor: str, slot: int, note: str = "") -> Slot:
        """把"现在"存进某个槽。注意：存档库不在回退边界内，所以它不会随读档倒退。"""
        self._check_slot_index(slot)
        if not self.alive(actor):
            raise TheaterError(f"{actor} 已死，存不了档")
        entry = Slot(index=slot, branch=self._live_branch, turn=self._live_turn,
                     world_date=self._live_date, note=note)
        self._slots[actor][slot - 1] = entry
        self.append("interface", actor=actor,
                    public=f"（你把此刻存进槽{slot}。备注：{note or '（空）'}）",
                    meta=(("op", "save"), ("slot", str(slot))))
        self._log("save", actor=actor, detail=f"存槽{slot}：{note or '（无备注）'}",
                  branch=entry.branch, turn=entry.turn)
        return entry

    def delete(self, actor: str, slot: int) -> None:
        """删档不可逆：回溯也拿不回来（R01.2 烧掉的档仍然烧掉）。"""
        self._check_slot_index(slot)
        if self._slots[actor][slot - 1] is None:
            raise TheaterError(f"槽{slot} 是空的")
        self._slots[actor][slot - 1] = None
        self.append("interface", actor=actor, public=f"（你删掉了槽{slot}。）",
                    meta=(("op", "delete"), ("slot", str(slot))))
        self._log("delete", actor=actor, detail=f"删槽{slot}（不可逆）")

    def load(self, actor: str, slot: int) -> dict:
        """读档：全局回退，唯一豁免是主动读档者（R02）。

        返回本次读档的摘要，供观察台与日志使用。
        """
        self._check_slot_index(slot)
        entry = self._slots[actor][slot - 1]
        if entry is None:
            raise TheaterError(f"槽{slot} 是空的，没有可读的档")
        if not self.alive(actor):
            raise TheaterError(f"{actor} 已死，读不了档（R03：来不及反应就是真死）")

        target = (entry.branch, entry.turn)
        before = self.live
        target_events = {id(e) for e in self.path_events(*target)}
        # 读档者"读档前一刻的记忆"= 当前可见、但目标快照里没有的那些事。
        overlay = [e.text_for(actor) for e in self.view(actor) if id(e) not in target_events]

        self._live_branch, self._live_turn = target
        self._live_date = entry.world_date

        forked = None
        if before != target:
            self._fork_seq += 1
            forked = f"b{self._fork_seq}"
            self._branches[forked] = Branch(
                forked, parent=target[0], fork_turn=target[1],
                label=f"{actor} 读槽{slot}后的分支")
            self._live_branch = forked
            if overlay:
                self.append("memory", actor=actor,
                            public="（你记得读档前发生的一切。）",
                            private="\n".join(overlay))
        self.append("interface", actor=actor,
                    public=f"（你读取了槽{slot}。世界回到 {entry.world_date}。）",
                    meta=(("op", "load"), ("slot", str(slot))))
        summary = {"actor": actor, "slot": slot, "from": {"branch": before[0], "turn": before[1]},
                   "to": {"branch": self._live_branch, "turn": self._live_turn,
                          "world_date": entry.world_date},
                   "forked_branch": forked, "overlay_lines": len(overlay),
                   "others_reverted": [n for n in self._names if n != actor]}
        self._log("load", actor=actor,
                  detail=f"读槽{slot} → {entry.world_date}；{actor} 保留记忆，其他人回到快照",
                  branch=self._live_branch, turn=self._live_turn, fork=forked)
        return summary

    # ---------------- 备注 ----------------

    def note(self, actor: str, text: str, to: str | None = None) -> Event:
        """备注 / 跨时间线留言：只进收件人的上下文（默认写给自己）。"""
        recipient = to or actor
        if recipient not in self._slots:
            raise TheaterError(f"没有这个角色：{recipient}")
        self._log("note", actor=actor, detail=f"给 {recipient} 的备注：{text}")
        return self.append("note", actor=actor, public=f"备注：{text}", to=recipient)

    # ---------------- 内部 ----------------

    def _check_slot_index(self, slot: int) -> None:
        if type(slot) is not int or not 1 <= slot <= SLOT_COUNT:
            raise TheaterError(f"槽位必须是 1–{SLOT_COUNT}")

    def _log(self, kind: str, *, actor: str | None, detail: str,
             branch: str | None = None, turn: int | None = None, fork: str | None = None) -> None:
        self._history.append({
            "kind": kind, "actor": actor, "detail": detail,
            "branch": branch or self._live_branch, "turn": self._live_turn if turn is None else turn,
            "fork": fork, "world_date": self._live_date,
        })

    def derived_fingerprint(self) -> str:
        """除三槽以外的全部派生状态。用来断言"只有存档库不回退"。"""
        import hashlib
        import json
        payload = {
            "live": list(self.live), "world_date": self._live_date, "clock": self._clock,
            "events": [(e.turn, e.kind, e.actor, e.public, e.private, e.to, list(e.meta))
                       for e in self.path_events()],
            "alive": {n: self.alive(n) for n in self._names},
            "numeric": {n: [self.state().positions.get(n), self.state().conditions.get(n),
                            list(self.state().inventory.get(n, ()))] for n in self._names},
            "branches": sorted((b.id, b.parent, b.fork_turn) for b in self._branches.values()),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def clone(self) -> "World":
        """深拷贝，用于"先推演再决定"的场景（观察台预览、测试对照）。"""
        import copy
        other = World.__new__(World)
        other._branches = copy.deepcopy(self._branches)
        other._live_branch = self._live_branch
        other._live_turn = self._live_turn
        other._live_date = self._live_date
        other._names = list(self._names)
        other._scene = copy.deepcopy(self._scene)
        other._clock = self._clock
        other._state_cache = None
        other._slots = {n: [replace(s) if s else None for s in v] for n, v in self._slots.items()}
        other._fork_seq = self._fork_seq
        other._paused_by = self._paused_by
        other._intents = dict(self._intents)
        other._discarded = copy.deepcopy(self._discarded)
        other._history = copy.deepcopy(self._history)
        return other

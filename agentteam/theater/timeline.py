"""事件驱动的时间线：时间跳到"下一个可能改变世界的时刻"。

这是从作者另一个项目（stock-test 的 `engine.next_time` / `wake_reasons`）搬过来的机制，
不是搬代码。那边是股票市场，这边是被叙述出来的世界，但时间线的形状是同一个：

  1. 时间**不是一格一格走**，而是直接跳到下一个可能改变世界的时刻。
     候选 = 各角色的定时唤醒 ∪ 观察唤醒。
  2. 角色**自己声明下次什么时候醒**（定时 / 观察某人 / 立刻），
     导演只负责排序，不替它决定。
  3. 唤醒理由是**结构化的**，并且会告诉角色——它知道自己"为什么轮到"，
     而不是笼统的"该你了"。
  4. 没有任何人声明下一步时**兜底推进**，否则整场会卡死。

世界时钟是**分钟数**（从场景起点算），可比较、可排序；`world_date` 只是给人看的标签，
由场景的 schedule 映射出来。两者分开，因为标签不保证可排序。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# 没人声明下一步时，兜底推进多少分钟
DEFAULT_STEP = 60

WAKE_KINDS = ("timer", "observe", "immediate")


@dataclass(frozen=True)
class Wake:
    """一个角色声明的一次唤醒条件。

    at     —— 最早可以触发的世界时刻（分钟）。即使 observe 也不会早于它。
    kind   —— timer（到点就醒）/ observe（某人行动后醒）/ immediate（现在就醒）
    target —— observe 时观察谁
    note   —— 角色自己给的理由，会原样告诉它
    since  —— 声明时的时钟，用于判断"目标自那以后是否行动过"
    """

    id: int
    actor: str
    at: int
    kind: str = "timer"
    target: str | None = None
    note: str = ""
    since: int = 0

    def __post_init__(self) -> None:
        if self.kind not in WAKE_KINDS:
            raise ValueError(f"未知唤醒类型：{self.kind}")
        if self.kind == "observe" and not self.target:
            raise ValueError("observe 唤醒必须指定观察对象")


@dataclass
class Timeline:
    schedule: list[tuple[int, str]] = field(default_factory=lambda: [(0, "第一天")])
    default_step: int = DEFAULT_STEP
    pending: list[Wake] = field(default_factory=list)
    fired: list[dict] = field(default_factory=list)
    _seq: int = 0

    # ---------------- 世界时刻 ----------------

    def label(self, clock: int) -> str:
        """把分钟数映射成给人看的标签：取 schedule 里最后一个不晚于它的。"""
        label = self.schedule[0][1] if self.schedule else "未定"
        for at, text in self.schedule:
            if at <= clock:
                label = text
            else:
                break
        return label

    def horizon(self) -> int:
        """场景的最晚时刻；到了就该收场。"""
        return max((at for at, _ in self.schedule), default=0)

    # ---------------- 声明 ----------------

    def declare(self, actor: str, *, kind: str = "timer", at: int | None = None,
                after_minutes: int | None = None, target: str | None = None,
                note: str = "", now: int = 0) -> Wake:
        """角色声明下次唤醒。同一个角色旧的条件会被替换（它改主意了）。"""
        if at is None:
            # observe 默认"目标一动就反应"；timer 才用 default_step 兜底。
            fallback = 0 if kind == "observe" else self.default_step
            at = now + (after_minutes if after_minutes is not None else fallback)
        at = max(at, now)
        self.cancel(actor)
        self._seq += 1
        wake = Wake(id=self._seq, actor=actor, at=at, kind=kind, target=target,
                    note=note, since=now)
        self.pending.append(wake)
        return wake

    def seed(self, actors: list[str], *, clock: int = 0) -> list[Wake]:
        """开场：每个角色都该有一次先手，而不是干等一个兜底。

        对应 stock-test 里 `revision == 0` 时的 initial 唤醒——没有这一步，
        整场戏会从"等 default_step 分钟"开始，很别扭。
        """
        return [self.declare(name, kind="immediate", at=clock, now=clock,
                             note="开场：本场开始，你先动") for name in actors]

    def cancel(self, actor: str) -> None:
        self.pending = [w for w in self.pending if w.actor != actor]

    def fire_time(self, wake: Wake, last_action: dict[str, int]) -> int | None:
        """这个条件实际会在什么时刻触发；返回 None 表示它永远不会自己触发。"""
        if wake.kind == "timer":
            return wake.at
        if wake.kind == "immediate":
            return wake.at
        # observe：目标"自声明以来"行动过才触发，触发时刻取目标行动的时刻
        acted = last_action.get(wake.target or "")
        if acted is None or acted < wake.since:
            return None
        return max(wake.at, acted)

    def next_wake(self, clock: int, alive: list[str],
                  last_action: dict[str, int]) -> tuple[Wake | None, int, dict]:
        """挑出下一个要触发的唤醒。

        返回 (唤醒, 触发时刻, 理由)。没有任何条件可触发时返回兜底唤醒——
        **不能返回 None 让主循环空转**，那会让整场戏卡死。
        """
        best: tuple[Wake, int] | None = None
        for wake in self.pending:
            if wake.actor not in alive:
                continue
            raw = self.fire_time(wake, last_action)
            if raw is None:
                continue
            # 迟到的唤醒**立刻补上，不能丢**。时间可能被"同一时刻连环反应"的守卫推过
            # 某个人的声明时刻；那时他的时机已经过去，但意图不该消失——丢掉它会让
            # 这个人从此再也醒不过来（实测：主角动过一次就被首领一直压着）。
            when = max(raw, clock)
            if best is None or (when, wake.id) < (best[1], best[0].id):
                best = (wake, when)
        if best is not None:
            wake, when = best
            return wake, when, {"type": wake.kind, "actor": wake.actor,
                                "note": wake.note, "at": when,
                                "target": wake.target}
        # 兜底：谁最久没动，就在 default_step 之后叫醒他。
        if not alive:
            return None, clock, {"type": "none", "note": "没有活着的角色"}
        stalest = min(alive, key=lambda n: (last_action.get(n, -1), n))
        self._seq += 1
        wake = Wake(id=self._seq, actor=stalest, at=clock + self.default_step,
                    kind="timer", note="系统兜底：没有任何人声明下一步")
        self.pending.append(wake)
        return wake, wake.at, {"type": "fallback", "actor": stalest, "at": wake.at,
                               "note": "没有任何人声明下一步，系统兜底推进"}

    # ---------------- 记录 ----------------

    def record(self, clock: int, reason: dict, *, turn: int) -> None:
        self.fired.append({"clock": clock, "turn": turn, "world_date": self.label(clock),
                           **reason})

    def consumed(self, wake_id: int) -> None:
        self.pending = [w for w in self.pending if w.id != wake_id]

"""上下文装配：静态系统提示 + 只追加的历史 + 每轮结尾的状态注入。

硬约束（前缀缓存与回滚语义都靠它）：
  1. 系统提示词一字不变，**不含任何随轮次变化的状态**。
  2. 历史消息只追加，永不改写。每个事件渲染成恰好一条消息，不做合并/重排。
  3. "现在的状态"作为**每轮结尾追加的一个尾块**注入，并且在写入时就被冻结成
     一条 state 事件 —— 不能事后重算。

第 3 条的理由值得写下来：状态块里含**三槽清单**，而三槽是 World 里唯一不随
回溯回退的东西（R01.2）。所以"第 T 轮的状态块"不是 (live, branches) 的函数，
事后重算会拿到当前的槽位而不是当时的槽位，前缀就不再逐字节一致，缓存失效、
回滚语义也错。写一次、永不重写，两件事同时成立。

于是有一条可直接断言的性质：
    messages(T) 是 messages(T+1) 的**前缀**。
"""
from __future__ import annotations

from .world import World

# 每轮结尾注入的状态块。改这里就能改角色"开界面时能看到什么"的措辞。
STATE_TEMPLATE = """【状态注入 · 第{turn}轮 · 世界日期：{world_date}】
{situation}
你的状态：{vitality}
你的三槽（只有你自己看得到，也只看得到自己的）：
{slots}"""


def _situation(world: World, actor: str) -> str:
    """数值层的现状：你在哪、能去哪、这里有什么、谁在。没有地点表就返回空。

    这一段是必须的——不写它，角色根本不知道自己在哪，【动作】就没法用。
    实测的教训：模型会直接说「我连自己在哪都说不清」，然后一轮轮原地不动。
    """
    from .physics import condition_text
    if not world.scene.has_locations():
        return ""
    state = world.state()
    here = world.scene.locations.get(state.positions.get(actor, ""))
    if here is None:
        return ""
    neighbors = "、".join(world.scene.locations[n].name for n in here.neighbors) or "（无处可去）"
    items = [i for i, at in state.items.items() if at == here.name]
    near = state.nearby(actor)
    lines = [
        f"你在：{here.name}（{here.text()}）",
        f"能去：{neighbors}　　← 只能写这些地名",
        f"这里的东西：{'、'.join(items) if items else '（没有）'}",
        f"同地点的人：{'、'.join(near) if near else '（没有别人）'}",
        f"你的伤况：{condition_text(state.conditions.get(actor, 'ok'))}"
        f"　　手里：{'、'.join(state.inventory.get(actor, ())) or '（空手）'}",
    ]
    return "\n" + "\n".join(lines)


def state_block(world: World, actor: str) -> str:
    """此刻该角色掌握的状态。只含他自己的槽位——别人的存档列表谁都看不到（R06）。"""
    lines = []
    for index, slot in enumerate(world.slots(actor), start=1):
        if slot is None:
            lines.append(f"  槽{index}：（空）")
        else:
            lines.append(f"  槽{index}：{slot['world_date']} — 「{slot['note'] or '（无备注）'}」")
    vitality = "存活，可操作。" if world.can_act(actor) else (
        "已死亡。" if not world.alive(actor) else "存活，但世界正被他人暂停。")
    return STATE_TEMPLATE.format(turn=world.live[1], world_date=world.world_date,
                                 situation=_situation(world, actor),
                                 vitality=vitality, slots="\n".join(lines))


def inject_state(world: World, actor: str) -> None:
    """在**本轮结尾**为某个角色追加状态块。写入即冻结。"""
    world.append("state", actor=actor, public=state_block(world, actor), to=actor)


def inject_state_all(world: World) -> None:
    """每轮结束时给每个角色各注入一份（各自只看到自己的）。"""
    for name in world.characters:
        inject_state(world, name)


# 两种状态注入法，用来对比缓存命中与最终效果：
#   trailing（默认）—— 状态作为每轮结尾的尾块追加，历史只追加不改写，前缀稳定。
#   system          —— 状态写进系统提示词，每轮重算；第一条消息就变了，前缀必然断。
INJECTIONS = ("trailing", "system")


def build_messages(world: World, actor: str, system_prompt: str,
                   injection: str = "trailing") -> list[dict]:
    """这个角色此刻该看到的完整 messages。

    渲染规则固定：自己的 turn 事件 = assistant，其余 = user；一个事件一条消息。
    不合并连续同角色消息，否则字节布局会随上下文变化，前缀就不再稳定。

    trailing：状态块是**每轮结尾写入并冻结**的 state 事件，按轮次原地渲染。
              历史只追加、永不重写，所以 messages(T) 是 messages(T+1) 的前缀。
    system：  状态拼进系统提示词、每轮重算。代价是第一条消息就变了，前缀必然断。
    """
    if injection not in INJECTIONS:
        raise ValueError(f"未知注入方式：{injection}")
    head = (system_prompt + "\n\n" + state_block(world, actor)
            if injection == "system" else system_prompt)
    messages = [{"role": "system", "content": head}]
    for event in world.view(actor):
        if event.kind == "state" and injection == "system":
            continue          # 这条路上状态走系统提示词，历史里不该再出现
        role = "assistant" if event.kind == "turn" and event.actor == actor else "user"
        messages.append({"role": role, "content": event.text_for(actor)})
    return messages


def prefix_len(a: list[dict], b: list[dict]) -> int:
    """两个 messages 列表从头开始逐字节相同的消息条数。"""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def is_prefix(earlier: list[dict], later: list[dict]) -> bool:
    return prefix_len(earlier, later) == len(earlier) and len(earlier) <= len(later)

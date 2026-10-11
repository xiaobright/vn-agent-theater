"""Extract a small, quoted rule pack from the author's unchanged v7 document."""
from __future__ import annotations

from pathlib import Path


SOURCE = Path(__file__).resolve().parent.parent / "全员存取档Meta系统设定_v7_整合版.md"
# Boundaries are author text, not a second, paraphrased set of game rules.
_SECTIONS = (
    ("R01", "1. 世界只有一个正在运行的", "7. **\"觉醒\"专用定义**"),
    ("R02", "角色发起的世界内读档完成后：", "- 死亡者的恢复见第五节"),
    ("R03", "- 存档界面就是暂停界面。", "- 只要活着、有反应时间"),
    ("R04", "- 死者的档不消失", "- 存档不能预览快照内容"),
    ("R05", "- 每档可附自填备注。", "- 备注没有语境保证"),
    ("R06", "- 任何人都看不到别人的存档列表。", "- 知道\"别人也能读档\"的角色"),
    ("R07", "| | 游戏外普通存取档", "- 普通加载的唯一反馈例外"),
    ("R08", "- 主角额外连接场外玩家", "- 玩家不是全知"),
    ("R09", "- **觉醒状态与记忆完全同轨**", "- **被恢复到觉醒前是绝对遗忘**"),
)


def load_rules(source: Path | None = None) -> str:
    """Return exact source lines with stable rule ids and source locations.

    A missing boundary is an error: silently using outdated or guessed rules
    would make the checker less reliable than not running it.
    """
    path = source or SOURCE
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks = [f"权威机制摘录（原文逐字引用；来源：{path.name}）"]
    for rule_id, start, end in _SECTIONS:
        starts = [i for i, line in enumerate(lines) if line.lstrip().startswith(start)]
        if len(starts) != 1:
            raise ValueError(f"规则来源锚点缺失或重复：{rule_id} / {start}")
        first = starts[0]
        last = next((i for i in range(first, len(lines))
                     if lines[i].lstrip().startswith(end)), None)
        if last is None:
            raise ValueError(f"规则来源结束锚点缺失：{rule_id}")
        blocks.append(f"## {rule_id} · 原文 L{first + 1}–L{last + 1}\n"
                      + "\n".join(lines[first:last + 1]))
    return "\n\n".join(blocks)

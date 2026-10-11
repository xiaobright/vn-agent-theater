"""角色剧场：把"上下文即记忆"做成运行时。

与 `agentteam/pipeline.py` 的多模式流水线是两套不同的拓扑：
流水线里每个岗位是**无状态的一次调用**，上下文由代码按需切片装配；
剧场里每个角色是**一个有状态的扮演循环**，它的上下文就是它的全部记忆，
存档是上下文检查点，读档是全局回退而读档者豁免。

设定原文（R01/R02/R03）本身就要求"记忆可回退、存档库不回退"，所以两者同构。
"""
from .world import Event, Slot, TheaterError, World

__all__ = ["World", "Event", "Slot", "TheaterError"]

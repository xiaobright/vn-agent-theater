"""角色剧场验收：把设定原文条款写成断言，不联网、不调用模型。

对应关系：
  R01.2  存档库不随世界回溯回退 / 烧掉的档仍然烧掉
  R01.3  同一世界日期可以有多个版本
  R01.4  读更晚的快照 = 分支切换
  R02    读档后：读档者保留读档前一刻的记忆，其他人回到目标快照中的记忆
  R03    暂停是私人的 / 不存在两人同时读档 / 暂停本身不改写世界 /
         来不及反应就是真死，没有死亡自动读档
  R06    任何人都看不到别人的存档列表
  R09    觉醒与记忆完全同轨；被恢复到觉醒前是绝对遗忘
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from agentteam.config import load_config
from agentteam.recorder import Recorder
from agentteam.theater import observer as observer_mod
from agentteam.theater.context import build_messages, inject_state_all, is_prefix, state_block
from agentteam.theater.director import beats_for, run_scene
from agentteam.theater.world import SLOT_COUNT, TheaterError, World

SYS = "你是剧中角色，只依据自己记得的事行动。"


def new_world(names=("主角", "首领")) -> World:
    return World("第一天·黄昏", names)


def play(world: World, date: str, events: list[tuple[str, str, str]]) -> None:
    """推进一轮：世界事件 + 各角色行动，最后给每个人注入状态块。"""
    world.advance(date)
    for actor, public, private in events:
        world.append("turn", actor=actor, public=public, private=private)
    inject_state_all(world)


def seen(world: World, actor: str) -> str:
    """这个角色此刻上下文里的全部文字。"""
    return "\n".join(e.text_for(actor) for e in world.view(actor))


class SlotTests(unittest.TestCase):
    def test_three_slots_only(self):
        w = new_world()
        w.advance("第一天·黄昏")
        w.save("主角", 1, "起点")
        for bad in (0, 4, -1):
            with self.assertRaises(TheaterError):
                w.save("主角", bad, "越界")
        self.assertEqual(len(w.slots("主角")), SLOT_COUNT)

    def test_save_is_a_world_snapshot_pointer(self):
        """一份存档 = 一个 (branch, turn)；因为记忆由指针派生，指针就是全部世界状态。"""
        w = new_world()
        w.advance("第一天·黄昏")
        entry = w.save("主角", 1, "对峙前")
        self.assertEqual((entry.branch, entry.turn), w.live)
        self.assertEqual(entry.world_date, "第一天·黄昏")

    def test_same_world_date_can_hold_two_versions(self):
        """R01.3：世界日期不唯一，(branch, turn) 才唯一。"""
        w = new_world()
        play(w, "第三天·夜", [("主角", "第一轮夜", "")])
        first = w.save("主角", 1, "夜·甲")
        play(w, "第三天·夜", [("主角", "第二轮夜", "")])
        second = w.save("主角", 2, "夜·乙")
        self.assertEqual(first.world_date, second.world_date)
        self.assertNotEqual((first.branch, first.turn), (second.branch, second.turn))

    def test_slot_inventory_never_rewinds(self):
        """R01.2：读档不回退存档库——回溯之后，后来的档仍然指在被放弃的未来上。"""
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "起点", "")])
        w.save("主角", 1, "早")
        play(w, "第二天", [("主角", "第二天的事", "")])
        late = w.save("主角", 2, "晚")
        play(w, "第三天", [("主角", "第三天的事", "")])

        w.load("主角", 1)                       # 回到"早"
        self.assertNotIn("第三天", seen(w, "首领"))
        kept = w.slots("主角")[1]               # 槽2 没跟着回退
        self.assertIsNotNone(kept)
        self.assertEqual((kept["branch"], kept["turn"]), (late.branch, late.turn))
        self.assertEqual((kept["branch"], kept["turn"]), w.live) if False else None
        # 而且它仍然指在"早"之后的那条未来上，不是现在的指针
        self.assertNotEqual((kept["branch"], kept["turn"]), w.live)

    def test_delete_is_irreversible(self):
        """R01.2：烧掉的档仍然烧掉——回溯也拿不回来。"""
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "起点", "")])
        w.save("主角", 1, "回溯点")
        w.save("主角", 3, "会被删")
        play(w, "第二天", [("主角", "之后", "")])
        w.delete("主角", 3)
        self.assertIsNone(w.slots("主角")[2])
        w.load("主角", 1)
        self.assertIsNone(w.slots("主角")[2])

    def test_deleting_an_empty_slot_fails(self):
        w = new_world()
        w.advance("第一天·黄昏")
        with self.assertRaises(TheaterError):
            w.delete("主角", 2)

    def test_others_slots_are_invisible(self):
        """R06：任何人都看不到别人的存档列表。"""
        w = new_world()
        w.advance("第一天·黄昏")
        w.save("主角", 1, "主角的档")
        w.save("首领", 2, "首领的档")
        self.assertNotIn("主角的档", seen(w, "首领"))
        self.assertNotIn("主角的档", state_block(w, "首领"))
        self.assertIn("首领的档", state_block(w, "首领"))


class LoadTests(unittest.TestCase):
    def test_loader_keeps_memory_others_revert(self):
        """R02：主动读档者保留读档前一刻的记忆；其他人回到目标快照中的记忆。"""
        w = new_world()
        w.advance("第一天·黄昏")
        w.append("scene", public="码头上，首领说出了那件还没发生的事。")
        w.save("首领", 1, "对峙前")
        inject_state_all(w)

        play(w, "第一天·夜", [("主角", "主角当夜回到住处。", "他记住了「别相信他」。"),
                              ("首领", "首领开始复盘。", "")])
        play(w, "第二天", [("首领", "首领查了电台长。", "")])
        self.assertIn("主角当夜回到住处", seen(w, "首领"))          # 公开部分可见
        self.assertNotIn("别相信他", seen(w, "首领"))              # 主角的内心不可见

        w.load("首领", 1)
        leader, protagonist = seen(w, "首领"), seen(w, "主角")
        # 读档者记得自己刚经历的一切
        self.assertIn("首领开始复盘", leader)
        self.assertIn("首领查了电台长", leader)
        self.assertIn("读档前发生的一切", leader)
        # 其他人回到快照中的记忆：那两轮从未发生
        self.assertNotIn("首领开始复盘", protagonist)
        self.assertNotIn("首领查了电台长", protagonist)
        self.assertNotIn("主角当夜回到住处", protagonist)

    def test_load_forks_and_abandoned_future_stays_reachable(self):
        """R01.4：被放弃的未来留在原分支上，可以被某个快照再指回去（分支切换）。"""
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "起点", "")])
        w.save("首领", 1, "起点档")
        play(w, "第二天", [("首领", "第二天的事", "")])
        play(w, "第三天", [("首领", "第三天的事", "")])
        w.save("首领", 2, "第三天档")

        summary = w.load("首领", 1)
        self.assertIsNotNone(summary["forked_branch"])
        self.assertEqual(w.live, (summary["forked_branch"], 1))
        self.assertNotIn("第三天的事", seen(w, "主角"))      # 世界回退了
        self.assertIn("第三天的事", seen(w, "首领"))          # 但读档者记得

        w.load("首领", 2)                                    # 往前切回那条未来
        self.assertEqual(w.live[1], 3)
        self.assertIn("第三天的事", seen(w, "主角"))

    def test_load_to_current_point_does_not_fork(self):
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "起点", "")])
        w.save("主角", 1, "此刻")
        self.assertIsNone(w.load("主角", 1)["forked_branch"])

    def test_death_is_reverted_by_someone_elses_load(self):
        """R02：世界含所有人的大脑状态——回溯到被杀之前，人自然还活着。"""
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "起点", "")])
        w.save("首领", 1, "他还活着的时候")
        play(w, "第二天", [("主角", "第二天", "")])
        w.kill("主角", "被首领下手。")
        self.assertFalse(w.alive("主角"))
        w.load("首领", 1)
        self.assertTrue(w.alive("主角"))

    def test_dead_cannot_act_or_load(self):
        """R03：来不及反应就是真死，没有死亡自动读档。"""
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "起点", "")])
        w.save("主角", 1, "生前")
        before = w.live
        w.kill("主角", "没反应过来。")
        self.assertEqual(w.live, before)                     # 没有自动回档
        with self.assertRaises(TheaterError):
            w.append("turn", actor="主角", public="还想动")
        with self.assertRaises(TheaterError):
            w.load("主角", 1)

    def test_empty_slot_cannot_be_loaded(self):
        w = new_world()
        w.advance("第一天·黄昏")
        with self.assertRaises(TheaterError):
            w.load("主角", 1)


class PauseTests(unittest.TestCase):
    def test_no_simultaneous_loads(self):
        """R03：先打开界面的人拿走整个操作窗口。"""
        w = new_world()
        w.advance("第一天·黄昏")
        w.open_interface("主角")
        with self.assertRaises(TheaterError):
            w.open_interface("首领")

    def test_losers_intent_is_discarded_not_queued(self):
        """R03：另一个人的打算连同旧现场一起被覆盖，从未发生。"""
        w = new_world()
        w.advance("第一天·黄昏")
        w.open_interface("主角")
        self.assertFalse(w.declare_intent("首领", "我也要开界面"))
        self.assertEqual(len(w.discarded()), 1)
        self.assertEqual(w.discarded()[0]["lost_to"], "主角")

    def test_pause_is_private(self):
        """R03：暂停是私人的，其他人感知不到世界停过。"""
        w = new_world()
        w.advance("第一天·黄昏")
        w.open_interface("主角")
        self.assertNotIn("世界停住", seen(w, "首领"))
        self.assertNotIn("暂停", seen(w, "首领"))
        self.assertIn("世界停住", seen(w, "主角"))

    def test_pause_alone_does_not_rewrite_the_world(self):
        """R03：只开界面思考、随后关掉，世界原样继续。"""
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "起点", "")])
        w.save("主角", 1, "起点档")
        leader_before, live_before, slots_before = seen(w, "首领"), w.live, w.slots("主角")
        w.open_interface("主角")
        w.close_interface("主角")
        self.assertEqual(w.live, live_before)
        self.assertEqual(w.slots("主角"), slots_before)
        self.assertEqual(seen(w, "首领"), leader_before)
        self.assertTrue(w.alive("主角"))


class AwakeningTests(unittest.TestCase):
    """R09：觉醒与记忆完全同轨；被恢复到觉醒前是绝对遗忘。"""

    AWAKEN = "【觉醒】你意识到屏幕外有人在看着你。"

    def test_awakening_travels_with_memory(self):
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "起点", "")])
        w.save("首领", 1, "觉醒前")
        play(w, "第二天", [("主角", "第二天", "")])
        w.append("note", actor=None, public=self.AWAKEN, to="主角")
        play(w, "第三天", [("主角", "第三天", "")])
        w.save("首领", 2, "觉醒后")
        self.assertIn("觉醒", seen(w, "主角"))

        w.load("首领", 1)
        self.assertNotIn("觉醒", seen(w, "主角"))    # 被恢复到觉醒前是绝对遗忘
        w.load("首领", 2)
        self.assertIn("觉醒", seen(w, "主角"))        # 恢复到觉醒后则原样维持


class IsolationTests(unittest.TestCase):
    def test_absence_hides_events_structurally(self):
        """隔离是结构性的：不在场的事件根本不在他的上下文里。"""
        w = new_world()
        w.advance("第一天·黄昏")
        w.append("scene", public="只有主角在场看到的暗号。", present=("主角",))
        self.assertIn("暗号", seen(w, "主角"))
        self.assertNotIn("暗号", seen(w, "首领"))

    def test_private_thought_stays_private(self):
        w = new_world()
        w.advance("第一天·黄昏")
        w.append("turn", actor="主角", public="主角说了一句话。", private="他心里其实在盘算读档。")
        self.assertIn("盘算读档", seen(w, "主角"))
        self.assertNotIn("盘算读档", seen(w, "首领"))


class PrefixCacheTests(unittest.TestCase):
    """用户定的硬约束：系统提示词不变、历史只追加、状态在每轮结尾注入。"""

    def test_messages_are_strictly_extended_each_turn(self):
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "第一轮行动", "第一轮内心")])
        snaps = [build_messages(w, "主角", SYS)]
        for turn in range(2, 6):
            play(w, f"第{turn}天",
                 [("主角", f"第{turn}轮行动", f"第{turn}轮内心"), ("首领", f"第{turn}轮回应", "")])
            snaps.append(build_messages(w, "主角", SYS))
        for earlier, later in zip(snaps, snaps[1:]):
            self.assertTrue(is_prefix(earlier, later),
                            "上一轮的 messages 必须是下一轮的前缀，否则前缀缓存失效")
        self.assertGreater(len(snaps[-1]), len(snaps[0]))

    def test_system_prompt_is_byte_identical_across_turns(self):
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "第一轮", "")])
        first = build_messages(w, "主角", SYS)[0]
        play(w, "第二天", [("主角", "第二轮", "")])
        self.assertEqual(build_messages(w, "主角", SYS)[0], first)
        self.assertEqual(first["content"], SYS)

    def test_state_injection_is_frozen_at_write_time(self):
        """状态块含三槽（唯一不回退的状态），所以必须写一次就冻结、不能事后重算。"""
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "第一轮", "")])
        frozen = build_messages(w, "主角", SYS)[-1]["content"]
        play(w, "第二天", [("主角", "第二轮", "")])
        w.save("主角", 1, "新存的档")             # 槽位变了
        w.advance("第三天")
        inject_state_all(w)
        later = build_messages(w, "主角", SYS)
        self.assertIn(frozen, [m["content"] for m in later])    # 旧状态块原样还在
        self.assertIn("新存的档", later[-1]["content"])          # 新状态块反映现状
        self.assertNotEqual(frozen, later[-1]["content"])

    def test_rollback_restores_a_byte_identical_prefix(self):
        """回滚后，非读档者的上下文回到与当初逐字节相同的样子——缓存仍然命中。"""
        w = new_world()
        play(w, "第一天·黄昏", [("主角", "第一轮", "")])
        w.save("首领", 1, "回溯点")
        at_save = build_messages(w, "主角", SYS)
        play(w, "第二天", [("主角", "第二轮", ""), ("首领", "首领也动了", "")])
        play(w, "第三天", [("主角", "第三轮", "")])
        self.assertNotEqual(build_messages(w, "主角", SYS), at_save)

        w.load("首领", 1)
        self.assertEqual(build_messages(w, "主角", SYS), at_save)
        # 读档者自己的上下文则多了记忆覆盖层
        self.assertIn("读档前发生的一切", seen(w, "首领"))


class ObserverTests(unittest.TestCase):
    """结构层泄漏检测：隔离是结构性的，所以泄漏只可能是模型自己编出来的。"""

    SECRET = "首领的电台长把半句话卖了出去"

    def _world(self) -> World:
        w = new_world()
        w.advance("第一天·黄昏")
        w.append("turn", actor="首领", public="首领说了句场面话。", private=self.SECRET)
        w.save("首领", 1, "只有首领知道的档")
        return w

    def test_clean_output_reports_nothing(self):
        w = self._world()
        self.assertEqual(observer_mod.leak_check(w, "主角", "主角只是站在原地没有说话。"), [])

    def test_reproduced_secret_is_caught(self):
        w = self._world()
        hits = observer_mod.leak_check(w, "主角", f"主角忽然开口：{self.SECRET}")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["kind"], "leak")
        self.assertEqual(hits[0]["secret_owner"], "首领")

    def test_others_slot_note_is_a_secret(self):
        w = self._world()
        hits = observer_mod.leak_check(w, "主角", "主角念出了「只有首领知道的档」。")
        self.assertTrue(any(h["source_kind"] == "slot_note" for h in hits))

    def test_own_secret_is_not_a_leak(self):
        w = self._world()
        self.assertEqual(observer_mod.leak_check(w, "首领", f"首领心想：{self.SECRET}"), [])

    def test_structural_hit_forces_pause(self):
        """硬证据优先于模型意见：结构层命中时，观察者不能报"一切正常"。"""
        report = observer_mod.validate_report(
            {"status": "ok", "divergences": [], "reason": "看起来没问题。"},
            [{"kind": "leak", "actor": "主角", "matched": "…"}])
        self.assertEqual(report["status"], "pause")
        self.assertIn("硬证据", report["reason"])

    def test_report_validation_rejects_bad_shapes(self):
        with self.assertRaises(ValueError):
            observer_mod.validate_report({"status": "maybe"}, [])
        with self.assertRaises(ValueError):
            observer_mod.validate_report({"status": "ok", "divergences": "不是数组"}, [])


class DirectorTests(unittest.TestCase):
    """整场跑通（mock，不联网）：读档确实会全局回退并分出新支。"""

    def test_mock_scene_reaches_a_rollback(self):
        world = new_world()
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(pathlib.Path(tmp))
            result = run_scene(
                load_config(mock=True), rec, world,
                request="测试一场戏", rules="（测试用规则）", bibles={},
                beats=beats_for(world, 4, world.characters, "第三天·夜"), observe=True)

        self.assertEqual(result["played"], 4)
        loads = [op for op in result["interface_ops"] if op.get("op") == "load" and op.get("ok")]
        self.assertTrue(loads, "mock 流程应当走到一次读档")
        self.assertNotEqual(result["live"]["branch"], "main", "读档后应当分出新分支")
        self.assertTrue(result["prose"])
        # 各角色的上下文必须互不相同，否则隔离就是假的
        self.assertNotEqual(json.dumps(result["contexts"]["主角"], ensure_ascii=False),
                            json.dumps(result["contexts"]["首领"], ensure_ascii=False))
        self.assertEqual(result["paused"], None)

    def test_state_is_injected_once_per_character_per_turn(self):
        world = new_world()
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(pathlib.Path(tmp))
            result = run_scene(
                load_config(mock=True), rec, world,
                request="测试", rules="（测试用规则）", bibles={},
                beats=beats_for(world, 2, world.characters, "第三天·夜"), observe=False)
        states = [e for e in world.path_events() if e.kind == "state"]
        self.assertEqual(len(states), 2 * 3)   # 开场一次 + 每轮一次，两个角色
        self.assertEqual(result["observer_reports"], [])

    def test_per_character_staging_keeps_the_two_tracks_apart(self):
        """stage 写成 {角色: 文字} 时，各人只看到自己那一段——双线戏靠这个。"""
        world = new_world()
        beats = [{"world_date": "第三天·夜", "cast": world.characters,
                  "stage": {"主角": "你回到住处，屋里没开灯。",
                            "首领": "你在据点里复盘码头那场对峙。"}}]
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(pathlib.Path(tmp))
            run_scene(load_config(mock=True), rec, world, request="双线",
                      rules="（测试用规则）", bibles={}, beats=beats, observe=False)
        self.assertIn("你回到住处", seen(world, "主角"))
        self.assertNotIn("你在据点里复盘", seen(world, "主角"))
        self.assertIn("你在据点里复盘", seen(world, "首领"))
        self.assertNotIn("你回到住处", seen(world, "首领"))


if __name__ == "__main__":
    unittest.main()

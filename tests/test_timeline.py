"""事件驱动时间线 + 最小数值层的验收（不联网、不调用模型）。

对应关系：
  时间线 —— 时间跳到下一个可能改变世界的时刻；角色自己声明何时醒；
            唤醒理由结构化；没人声明时兜底推进（否则整场卡死）。
  数值层 —— 位置 / 状态 / 物品；冲突由种子随机机械裁决，不看文笔。
  不变式 —— 数值层同样由事件日志派生，所以读档会把位置和状态一起回退。
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from agentteam.config import load_config
from agentteam.recorder import Recorder
from agentteam.theater import physics
from agentteam.theater.context import state_block
from agentteam.theater.director import run_scene
from agentteam.theater.physics import Scene
from agentteam.theater.timeline import Timeline
from agentteam.theater.world import World

SCENE = {
    "locations": {
        "住处": {"label": "主角的住处", "neighbors": ["巷口"]},
        "巷口": {"label": "巷口", "neighbors": ["住处", "码头"]},
        "码头": {"label": "码头", "neighbors": ["巷口"]},
        "据点": {"label": "首领的据点", "neighbors": ["巷口"]},
    },
    "characters": {
        "主角": {"location": "住处", "inventory": ["短刀"]},
        "首领": {"location": "据点"},
    },
    "items": {"灯油": "巷口"},
    "seed": "test",
}


def numeric_world(clock: int = 0) -> World:
    return World("第三天·夜", ["主角", "首领"], Scene.from_dict(SCENE, ["主角", "首领"]),
                 clock=clock)


class TimelineTests(unittest.TestCase):
    def test_picks_the_earliest_wake(self):
        tl = Timeline(schedule=[(0, "夜"), (240, "深夜")])
        tl.declare("主角", after_minutes=120, now=0)
        tl.declare("首领", after_minutes=30, now=0)
        wake, when, reason = tl.next_wake(0, ["主角", "首领"], {})
        self.assertEqual((wake.actor, when), ("首领", 30))
        self.assertEqual(reason["type"], "timer")

    def test_declaring_again_replaces_the_old_wake(self):
        tl = Timeline()
        tl.declare("主角", after_minutes=10, now=0)
        tl.declare("主角", after_minutes=999, now=0)
        self.assertEqual([w.actor for w in tl.pending], ["主角"])
        self.assertEqual(tl.pending[0].at, 999)

    def test_observe_fires_only_after_the_target_acts(self):
        tl = Timeline()
        tl.declare("首领", kind="observe", target="主角", now=10)
        self.assertIsNone(tl.fire_time(tl.pending[0], {}))            # 目标还没动过
        self.assertIsNone(tl.fire_time(tl.pending[0], {"主角": 5}))    # 声明之前动的不算
        self.assertEqual(tl.fire_time(tl.pending[0], {"主角": 40}), 40)

    def test_observe_respects_its_earliest_time(self):
        tl = Timeline()
        tl.declare("首领", kind="observe", target="主角", at=100, now=0)
        self.assertEqual(tl.fire_time(tl.pending[0], {"主角": 40}), 100)

    def test_never_returns_nothing_to_do(self):
        """没有任何人声明下一步时必须有兜底，否则整场会卡死。"""
        tl = Timeline(default_step=60)
        wake, when, reason = tl.next_wake(100, ["主角", "首领"], {"主角": 10})
        self.assertIsNotNone(wake)
        self.assertEqual(reason["type"], "fallback")
        self.assertEqual(when, 160)
        self.assertEqual(wake.actor, "首领")     # 最久没动的那个

    def test_overdue_wakes_are_not_dropped(self):
        """时间被"连环反应"守卫推过某个人的声明时刻时，他的意图不能消失。

        这是实测出来的 bug：主角声明 T+30 再动，首领一直声明"立刻"，守卫把时间推到
        T+60 之后，主角那个 T+30 的唤醒被 `when < clock` 过滤掉，从此再也醒不过来。
        """
        tl = Timeline(schedule=[(0, "夜"), (600, "次日")], default_step=60)
        tl.declare("主角", after_minutes=30, now=0)
        tl.declare("首领", kind="immediate", at=0, now=0)
        wake, when, _ = tl.next_wake(60, ["主角", "首领"], {"首领": 60})
        self.assertEqual(wake.actor, "主角")
        self.assertEqual(when, 60)          # 补在当前时刻，而不是被丢掉

    def test_label_and_horizon(self):
        tl = Timeline(schedule=[(0, "第三天·夜"), (240, "第三天·深夜"), (600, "第四天")])
        self.assertEqual(tl.label(0), "第三天·夜")
        self.assertEqual(tl.label(239), "第三天·夜")
        self.assertEqual(tl.label(240), "第三天·深夜")
        self.assertEqual(tl.label(9999), "第四天")
        self.assertEqual(tl.horizon(), 600)

    def test_dead_actors_are_not_woken(self):
        tl = Timeline()
        tl.declare("主角", after_minutes=1, now=0)
        wake, _, _ = tl.next_wake(0, ["首领"], {})
        self.assertEqual(wake.actor, "首领")


class PhysicsTests(unittest.TestCase):
    def test_move_only_to_a_neighbour(self):
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        with self.assertRaises(physics.ActionError):
            physics.resolve(w.scene, w.state(), {"op": "move", "dest": "码头"},
                            actor="主角", clock=0, turn=1)
        self.assertTrue(physics.resolve(w.scene, w.state(), {"op": "move", "dest": "巷口"},
                                        actor="主角", clock=0, turn=1)["ok"])

    def test_attack_requires_being_in_the_same_place(self):
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        with self.assertRaises(physics.ActionError):
            physics.resolve(w.scene, w.state(), {"op": "attack", "target": "首领"},
                            actor="主角", clock=0, turn=1)

    def test_resolution_is_deterministic(self):
        """裁决必须可复现：同输入同结果，不依赖调用顺序。"""
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        w.action_event("主角", physics.resolve(w.scene, w.state(), {"op": "move", "dest": "巷口"},
                                               actor="主角", clock=0, turn=1))
        w.advance("第三天·夜", clock=5)
        w.action_event("首领", physics.resolve(w.scene, w.state(), {"op": "move", "dest": "巷口"},
                                               actor="首领", clock=5, turn=2))
        state = w.state()
        first = physics.resolve(w.scene, state, {"op": "attack", "target": "首领"},
                                actor="主角", clock=10, turn=3)
        again = physics.resolve(w.scene, state, {"op": "attack", "target": "首领"},
                                actor="主角", clock=10, turn=3)
        self.assertEqual(first["public"], again["public"])
        self.assertTrue(any(t[0] == "chance" for t in first["meta"]))

    def test_being_armed_and_unseen_raises_the_odds(self):
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        state = w.state()
        armed = physics.hit_chance(state, "主角", "首领", aware=True)
        blind = physics.hit_chance(state, "主角", "首领", aware=False)
        self.assertGreater(blind, armed)

    def test_condition_progresses_toward_death(self):
        self.assertEqual(physics._hurt("ok"), "injured")
        self.assertEqual(physics._hurt("injured"), "dead")
        self.assertEqual(physics._hurt("restrained"), "dead")

    def test_take_requires_the_item_to_be_there(self):
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        with self.assertRaises(physics.ActionError):
            physics.resolve(w.scene, w.state(), {"op": "take", "item": "灯油"},
                            actor="主角", clock=0, turn=1)

    def test_scene_rejects_broken_geometry(self):
        with self.assertRaises(physics.ActionError):
            Scene.from_dict({"locations": {"a": {"neighbors": ["不存在"]}},
                             "characters": {"x": {"location": "a"}}}, ["x"])
        with self.assertRaises(physics.ActionError):
            Scene.from_dict({"locations": {"a": {}},
                             "characters": {"x": {"location": "别处"}}}, ["x"])


class NumericDerivationTests(unittest.TestCase):
    """数值层也必须由日志派生——否则回档会回不干净。"""

    def test_position_condition_and_inventory_come_from_the_log(self):
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        w.action_event("主角", physics.resolve(w.scene, w.state(), {"op": "move", "dest": "巷口"},
                                               actor="主角", clock=0, turn=1))
        w.action_event("主角", physics.resolve(w.scene, w.state(), {"op": "take", "item": "灯油"},
                                               actor="主角", clock=0, turn=1))
        state = w.state()
        self.assertEqual(state.positions["主角"], "巷口")
        self.assertIn("灯油", state.inventory["主角"])
        self.assertEqual(state.at("巷口"), ["主角"])
        self.assertEqual(state.nearby("主角"), [])

    def test_rollback_reverts_position_and_condition(self):
        """关键不变式：读档回退的不只是记忆，位置和状态也一起回退。

        注意读档者不能是死人（R03：来不及反应就是真死），所以由**别人**读一个
        含他的快照把他救回来——这正是 R02 说的"被救回者恢复的是目标快照中的自己"。
        """
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        w.action_event("主角", physics.resolve(w.scene, w.state(), {"op": "move", "dest": "巷口"},
                                               actor="主角", clock=0, turn=1))
        w.save("首领", 1, "主角还在巷口的时候")
        before = (w.state().positions["主角"], w.state().conditions["主角"])

        w.advance("第三天·夜", clock=5)
        w.action_event("主角", physics.resolve(w.scene, w.state(), {"op": "move", "dest": "码头"},
                                               actor="主角", clock=5, turn=2))
        w.advance("第三天·夜", clock=10)
        w.kill("主角", "被伏击。")
        self.assertEqual(w.state().positions["主角"], "码头")
        self.assertFalse(w.alive("主角"))

        w.load("首领", 1)
        self.assertEqual((w.state().positions["主角"], w.state().conditions["主角"]), before)
        self.assertTrue(w.alive("主角"))
        self.assertEqual(w.state().positions["首领"], "据点")

    def test_dead_cannot_load_but_others_can_revive_them(self):
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        w.save("首领", 1, "他还活着")
        w.save("主角", 1, "自己的档")
        w.advance("第三天·夜", clock=5)
        w.kill("主角", "没反应过来。")
        with self.assertRaises(Exception):
            w.load("主角", 1)          # 死人读不了档
        w.load("首领", 1)              # 别人可以把他恢复
        self.assertTrue(w.alive("主角"))

    def test_state_block_tells_the_actor_where_it_is(self):
        """状态注入必须带上位置/能去哪/这里有什么/谁在——否则【动作】根本没法用。

        实测的教训：模型会直接说「我连自己在哪都说不清」，然后一轮轮原地不动。
        """
        w = numeric_world()
        block = state_block(w, "主角")
        self.assertIn("你在：住处", block)
        self.assertIn("巷口", block)              # 能去的地方
        self.assertIn("止血药" if False else "这里的东西", block)
        self.assertIn("同地点的人", block)
        self.assertIn("手里", block)

    def test_state_block_shows_nearby_after_moving_together(self):
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        w.action_event("主角", physics.resolve(w.scene, w.state(), {"op": "move", "dest": "巷口"},
                                               actor="主角", clock=0, turn=1))
        w.advance("第三天·夜", clock=1)
        w.action_event("首领", physics.resolve(w.scene, w.state(), {"op": "move", "dest": "巷口"},
                                               actor="首领", clock=1, turn=2))
        self.assertIn("同地点的人：首领", state_block(w, "主角"))
        self.assertIn("同地点的人：主角", state_block(w, "首领"))

    def test_state_block_stays_silent_without_a_scene(self):
        """没有地点表的老场景不该凭空长出这一段。"""
        w = World("第一天", ["主角", "首领"])
        self.assertNotIn("你在：", state_block(w, "主角"))

    def test_restrained_cannot_act_but_is_alive(self):
        w = numeric_world()
        w.advance("第三天·夜", clock=0)
        w.action_event("主角", {"public": "被制住",
                                "meta": (("condition", "主角", "restrained"),)})
        self.assertFalse(w.can_act("主角"))
        self.assertTrue(w.alive("主角"))

    def test_clock_cannot_go_backwards(self):
        w = numeric_world(clock=100)
        with self.assertRaises(Exception):
            w.advance("第三天·夜", clock=50)


class TimelineDirectorTests(unittest.TestCase):
    def test_event_driven_run_wakes_people_and_advances_the_clock(self):
        world = numeric_world()
        timeline = Timeline(schedule=[(0, "第三天·夜"), (240, "第三天·深夜"), (600, "第四天")])
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(pathlib.Path(tmp))
            result = run_scene(load_config(mock=True), rec, world, request="大乱斗",
                               rules="（测试用规则）", bibles={}, timeline=timeline,
                               observe=False, max_turns=6)
        self.assertEqual(result["mode"], "timeline")
        self.assertGreater(result["played"], 0)
        self.assertTrue(result["wakes"])
        self.assertTrue(all("type" in w for w in result["wakes"]))
        clocks = [w["clock"] for w in result["wakes"]]
        self.assertEqual(clocks, sorted(clocks))          # 时钟单调不减
        self.assertTrue(timeline.pending)                 # 每轮都有人声明了下一步

    def test_actors_schedule_their_own_next_wake(self):
        world = numeric_world()
        timeline = Timeline(schedule=[(0, "第三天·夜"), (600, "第四天")])
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(pathlib.Path(tmp))
            run_scene(load_config(mock=True), rec, world, request="大乱斗",
                      rules="（测试用规则）", bibles={}, timeline=timeline,
                      observe=False, max_turns=3)
        self.assertTrue({w.kind for w in timeline.pending} <= {"timer", "observe", "immediate"})

    def test_no_one_declares_still_progresses(self):
        """极端情况：谁都不声明下一步，靠兜底也要能往前走。"""
        world = numeric_world()
        timeline = Timeline(schedule=[(0, "第三天·夜"), (600, "第四天")], default_step=60)
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(pathlib.Path(tmp))
            result = run_scene(load_config(mock=True), rec, world, request="大乱斗",
                               rules="（测试用规则）", bibles={}, timeline=timeline,
                               observe=False, max_turns=2)
        self.assertGreater(result["played"], 0)

    def test_no_actor_gets_starved(self):
        """整场跑下来，两边都得真的动过手——一个人被另一个人一直压着就是 bug。"""
        world = numeric_world()
        timeline = Timeline(schedule=[(0, "第三天·夜"), (600, "第四天")], default_step=60)
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(pathlib.Path(tmp))
            result = run_scene(load_config(mock=True), rec, world, request="大乱斗",
                               rules="（测试用规则）", bibles={}, timeline=timeline,
                               observe=False, max_turns=8)
        actors = [e["actor"] for e in result["belief"]["objective"] if e.get("actor")]
        self.assertGreaterEqual(actors.count("主角"), 2, f"主角只动了 {actors.count('主角')} 次")
        self.assertGreaterEqual(actors.count("首领"), 2, f"首领只动了 {actors.count('首领')} 次")

    def test_horizon_stops_the_scene(self):
        world = numeric_world()
        timeline = Timeline(schedule=[(0, "第三天·夜"), (1, "第三天·深夜")])
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(pathlib.Path(tmp))
            result = run_scene(load_config(mock=True), rec, world, request="大乱斗",
                               rules="（测试用规则）", bibles={}, timeline=timeline,
                               observe=False, max_turns=20)
        self.assertLessEqual(result["played"], 4)   # 到点就该收场，不能一直演


if __name__ == "__main__":
    unittest.main()

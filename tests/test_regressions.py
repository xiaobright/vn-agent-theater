"""存读档组合场景回归：只用内存世界和模型替身，不调用外部接口。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentteam.config import Config
from agentteam.recorder import Recorder
from agentteam.rules import load_rules
from agentteam.theater.actor import actor_prompt, apply_interface, belief_matrix, parse_reply
from agentteam.theater.context import build_messages, state_block
from agentteam.theater.director import run_scene
from agentteam.theater.physics import ActionError, Scene, resolve
from agentteam.theater.timeline import Timeline
from agentteam.theater.world import TheaterError, World


def known(world, name):
    return "\n".join(e.text_for(name) for e in world.view(name))


def config():
    return Config("test", None, None, "fake", 0, None, True)


class SnapshotRegressionTests(unittest.TestCase):
    def test_same_turn_events_do_not_change_saved_world(self):
        w = World("起点", ["甲", "乙"])
        w.advance("过去", clock=10)
        saved = w.save("甲", 1)
        w.append("scene", public="快照之后发生的秘密事件", present=("乙",))
        w.kill("乙", "同轮存档之后死亡")
        w.advance("未来", clock=20)
        w.load("甲", 1)
        self.assertTrue(w.alive("乙"))
        self.assertEqual(w.clock, 10)
        self.assertNotIn("秘密事件", known(w, "乙"))
        self.assertEqual(len(w.path_events(saved.branch, saved.turn, saved.event_count)), saved.event_count)

    def test_two_saves_in_same_turn_have_distinct_frozen_boundaries(self):
        w = World("起点", ["甲", "乙"])
        first = w.save("甲", 1)
        w.kill("乙", "之后")
        second = w.save("甲", 2)
        self.assertEqual(first.turn, second.turn)
        self.assertLess(first.event_count, second.event_count)
        w.load("甲", 1)
        self.assertTrue(w.alive("乙"))
        w.load("甲", 2)
        self.assertFalse(w.alive("乙"))

    def test_own_future_load_cannot_recover_forgotten_memory(self):
        w = World("起点", ["甲", "乙"])
        w.save("乙", 1)
        w.advance("后来")
        w.note("甲", "秘密口令 7461")
        w.save("甲", 1)
        w.save("乙", 2)
        w.load("乙", 1)
        self.assertNotIn("7461", known(w, "甲"))
        w.load("甲", 1)
        self.assertNotIn("7461", known(w, "甲"))
        # 别人的快照才能恢复那个记得秘密的甲。
        w.load("乙", 2)
        self.assertIn("7461", known(w, "甲"))

    def test_retained_memory_survives_another_person_saving_and_loading(self):
        w = World("起点", ["甲", "乙"])
        w.save("甲", 1)
        w.advance("未来")
        w.note("甲", "只有这条分支有的口令 8382")
        w.load("甲", 1)
        w.save("乙", 1)
        w.advance("再往后")
        w.load("乙", 1)
        self.assertIn("8382", known(w, "甲"))
        self.assertNotIn("8382", known(w, "乙"))
        matrix = belief_matrix(w)
        self.assertTrue(matrix["remembered_other_branches"]["甲"])

    def test_slot_note_survives_others_load_without_overwriting_snapshot(self):
        w = World("起点", ["甲", "乙"])
        saved = w.save("甲", 1, "旧备注")
        w.save("乙", 1)
        w.advance("以后")
        op = parse_reply("【行动】甲停下。\n【界面】备注槽1：跨分支口令8839")["op"]
        self.assertTrue(apply_interface(w, "甲", op)["ok"])
        w.load("乙", 1)
        slot = w.slots("甲")[0]
        self.assertEqual(slot["event_count"], saved.event_count)
        self.assertEqual(slot["note"], "跨分支口令8839")
        self.assertIn("8839", state_block(w, "甲"))
        self.assertNotIn("8839", state_block(w, "乙"))
        w.delete("甲", 1)
        with self.assertRaises(TheaterError):
            w.edit_note("甲", 1, "不能复活空槽")


class ActorRegressionTests(unittest.TestCase):
    def test_director_checks_leaks_before_committing_new_memory(self):
        w = World("起点", ["甲", "乙"])
        w.append("turn", actor="乙", public="乙沉默。", private="不能泄露的暗号 991832", present=("乙",))
        reply = parse_reply("【行动】不能泄露的暗号 991832\n【界面】无")
        with TemporaryDirectory() as tmp, patch("agentteam.theater.director.actor_mod.take_turn", return_value=reply):
            result = run_scene(config(), Recorder(Path(tmp)), w, request="测试", rules="", bibles={},
                               beats=[{"cast": ["甲"]}], observe=True)
        self.assertTrue(any(i["kind"] == "leak" for i in result["issues"]))
        self.assertIsNotNone(result["paused"])

    def test_static_prompt_has_no_author_secrets_or_mutable_beliefs(self):
        bible = "## 性格\n我确信只有自己有能力。\n## 口吻\n短句。\n## 存档习惯\n害怕覆盖。"
        prompt = actor_prompt("主角", bible, load_rules())
        for secret in ("主角额外连接场外玩家", "你和所有人一样", "被恢复到觉醒前是绝对遗忘", "我确信只有自己有能力"):
            self.assertNotIn(secret, prompt)
        self.assertIn("短句", prompt)

    def test_initial_beliefs_are_rewindable_memory(self):
        w = World("起点", ["甲", "乙"])
        with TemporaryDirectory() as tmp:
            run_scene(config(), Recorder(Path(tmp)), w, request="测试", rules="作者秘密",
                      bibles={"甲": "## 性格\n初始误判 2391\n## 口吻\n短句"},
                      beats=[{"cast": []}], observe=False)
        self.assertIn("2391", known(w, "甲"))
        self.assertNotIn("2391", known(w, "乙"))


class TimelineRegressionTests(unittest.TestCase):
    def test_load_restores_clock_and_other_actors_plans(self):
        w = World("过去", ["甲", "乙"])
        tl = Timeline(schedule=[(0, "过去"), (100, "未来")])
        w.bind_timeline(tl)
        tl.declare("乙", at=10, now=0, note="原计划")
        tl.last_action["乙"] = 0
        w.save("甲", 1)
        w.advance("未来", clock=40)
        tl.declare("乙", at=80, now=40, note="被放弃分支的计划")
        tl.last_action["乙"] = 40
        w.load("甲", 1)
        self.assertEqual(w.clock, 0)
        self.assertEqual([(x.actor, x.at, x.note) for x in tl.pending], [("乙", 10, "原计划")])
        self.assertEqual(tl.last_action["乙"], 0)

    def test_director_delivers_private_wake_reason_and_replans_at_restored_time(self):
        w = World("过去", ["甲", "乙"])
        tl = Timeline(schedule=[(0, "过去"), (100, "未来")])
        w.bind_timeline(tl)
        tl.declare("乙", at=60, now=0, note="原计划")
        w.save("甲", 1)
        w.advance("未来", clock=20)
        tl.declare("乙", at=80, now=20, note="未来的计划")
        tl.declare("甲", at=20, now=20, note="私有唤醒原因 9971")
        calls = []

        def reply(cfg, rec, world, name, prompt, *args, **kwargs):
            calls.append(name)
            self.assertIn("9971", str(build_messages(world, "甲", prompt)))
            self.assertNotIn("9971", known(world, "乙"))
            return parse_reply("【行动】甲停下。\n【界面】读槽1\n【下次】定时 30")

        with TemporaryDirectory() as tmp, patch("agentteam.theater.director.actor_mod.take_turn", side_effect=reply):
            run_scene(config(), Recorder(Path(tmp)), w, request="测试", rules="", bibles={},
                      timeline=tl, max_turns=1, observe=False)
        self.assertEqual(calls, ["甲"])
        self.assertEqual(w.clock, 0)
        self.assertEqual({x.actor: x.at for x in tl.pending}, {"甲": 30, "乙": 60})


class PhysicsRegressionTests(unittest.TestCase):
    def test_consumed_item_cannot_be_taken_twice_and_load_restores_it(self):
        scene = Scene.from_dict({"locations": {"屋子": {}}, "items": {"钥匙": "屋子"}}, ["甲", "乙"])
        w = World("现在", ["甲", "乙"], scene)
        w.save("乙", 1)
        take = {"op": "take", "item": "钥匙"}
        w.action_event("甲", resolve(scene, w.state(), take, actor="甲", clock=0, turn=0))
        for actor in ("甲", "乙"):
            with self.assertRaises(ActionError):
                resolve(scene, w.state(), take, actor=actor, clock=0, turn=0)
        w.action_event("甲", resolve(scene, w.state(), {"op": "use", "item": "钥匙"}, actor="甲", clock=0, turn=0))
        with self.assertRaises(ActionError):
            resolve(scene, w.state(), take, actor="甲", clock=0, turn=0)
        w.load("乙", 1)
        self.assertTrue(resolve(scene, w.state(), take, actor="乙", clock=0, turn=0)["ok"])


if __name__ == "__main__":
    unittest.main()

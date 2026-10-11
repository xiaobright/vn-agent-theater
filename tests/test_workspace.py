"""工作台存储与 API 验收；仅使用独立临时项目。"""
import asyncio
import json
from pathlib import Path
import shutil
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx2

from agentteam.config import Config, ROOT
from agentteam.ledger import empty_proposal
from agentteam.server import create_app
from agentteam.workspace import ConflictError, Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "data/bible", self.root / "data/bible")
        shutil.copy(ROOT / "data/ledger.json", self.root / "data/ledger.json")
        self.store = Workspace(self.root / "data", self.root / "runs", rules_loader=lambda: "冻结规则")

    def ready(self, mock=False):
        draft = self.store.create_run(request="连续场景", mode="workshop", mock=mock)
        proposal = empty_proposal()
        proposal["events"] = ["主角回到住处。"]
        proposal["player_seen_add"] = ["主角回到住处。"]
        return self.store.finish_run(draft["id"], {
            "scene": "主角回到住处，坐下。", "proposal": proposal,
            "workflow_status": "ready", "check": {"status": "pass", "issues": [], "questions": []}})

    def test_atomic_adoption_is_idempotent_and_invalidates_stale_drafts(self):
        a, b = self.ready(), self.ready()
        before = (self.root / "data/ledger.json").read_bytes()
        result = self.store.adopt(a["id"])
        self.assertEqual(result["revision"], 1)
        self.assertTrue(self.store.adopt(a["id"])["idempotent"])
        with self.assertRaises(ConflictError):
            self.store.adopt(b["id"])
        self.assertEqual(self.store.snapshot()["ledger"]["last_scene"]["scene"], a["scene"])
        self.assertEqual((self.root / "data/ledger.json").read_bytes(), before)

    def test_edit_creates_unreviewed_child_and_preserves_original(self):
        source = self.ready()
        child = self.store.edit_run(source["id"], scene="手工改稿", proposal=source["proposal"])
        self.assertNotEqual(source["id"], child["id"])
        self.assertEqual(self.store.get_run(source["id"])["scene"], source["scene"])
        with self.assertRaises(ConflictError):
            self.store.adopt(child["id"])

    def test_mock_and_changed_rules_cannot_be_adopted(self):
        mock = self.ready(mock=True)
        with self.assertRaises(ConflictError):
            self.store.adopt(mock["id"])
        real = self.ready()
        self.store.rules_loader = lambda: "新的规则"
        with self.assertRaises(ConflictError):
            self.store.adopt(real["id"])


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "data", self.root / "data", ignore=shutil.ignore_patterns("*.sqlite3*"))
        self.app = create_app(self.root / "data", self.root / "runs", force_mock=True)
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        cfg = Config("test", None, None, "fake", 0, None, True)
        self.config_patch = patch("agentteam.server.load_config", return_value=cfg)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    async def wait_done(self, endpoint):
        for _ in range(200):
            data = (await self.client.get(endpoint)).json()
            if data["status"] != "running":
                return data
            await asyncio.sleep(0.02)
        self.fail("local mock run did not complete")

    async def test_both_workflows_run_without_real_provider_or_real_data(self):
        response = await self.client.post("/api/theater/run", json={"mock": False, "max_turns": 4})
        self.assertEqual(response.status_code, 200)
        theater = await self.wait_done("/api/theater/state")
        self.assertEqual(theater["status"], "done", theater.get("error"))
        self.assertTrue(Path(theater["run_dir"]).is_relative_to(self.root))
        self.assertTrue((Path(theater["run_dir"]) / "inputs.json").exists())
        response = await self.client.post("/api/run", json={"mode": "workshop", "mock": False})
        self.assertEqual(response.status_code, 200)
        state = await self.wait_done("/api/state")
        self.assertEqual(state["status"], "done", state.get("error"))
        detail = (await self.client.get("/api/runs/" + response.json()["run_id"])).json()
        self.assertTrue(detail["mock"])
        self.assertFalse(detail["can_adopt"])
        edited = await self.client.patch("/api/runs/" + detail["id"], json={"scene": "手工场景", "proposal": empty_proposal()})
        self.assertEqual(edited.status_code, 200)
        adopted = await self.client.post("/api/runs/" + edited.json()["run_id"] + "/adopt")
        self.assertEqual(adopted.status_code, 409)

    async def test_atomic_busy_reservation(self):
        release = threading.Event()
        finished = threading.Event()
        def held_worker(*args):
            release.wait(3)
            finished.set()
        with patch("agentteam.server._pipeline", side_effect=held_worker):
            try:
                first = await self.client.post("/api/run", json={"mock": True})
                second = await self.client.post("/api/run", json={"mock": True})
            finally:
                release.set()
                await asyncio.to_thread(finished.wait, 3)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

    async def test_unknown_mode_is_rejected(self):
        response = await self.client.post("/api/theater/run", json={"mode": "misspelled"})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()

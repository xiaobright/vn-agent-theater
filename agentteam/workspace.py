"""单机工作台存储：SQLite 事务一次采纳正文与状态，不覆盖原始设定及实验目录。"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from .ledger import Ledger, ProposalError, empty_proposal, fingerprint


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:12]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False)


def review_seal(record: dict) -> str:
    return fingerprint({k: record.get(k) for k in ("scene", "proposal", "snapshot")})


class ConflictError(ValueError):
    pass


class Workspace:
    def __init__(self, data_dir: Path, runs_dir: Path, *, rules_loader: Callable[[], str] | None = None,
                 allow_mock_adoption: bool = False):
        self.data_dir, self.runs_dir = Path(data_dir), Path(runs_dir)
        self.db_path = self.data_dir / "workshop.sqlite3"
        self.rules_loader = rules_loader
        self.allow_mock_adoption = allow_mock_adoption
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        seed = Ledger(self.data_dir / "ledger.json").context_snapshot()
        seed.setdefault("revision", 0)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS canon (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS accepted (run_id TEXT PRIMARY KEY, revision INTEGER UNIQUE NOT NULL, scene TEXT NOT NULL, proposal TEXT NOT NULL, accepted_at TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO canon VALUES (1, ?)", (_json(seed),))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def rules(self) -> str:
        if self.rules_loader is not None:
            return self.rules_loader()
        from .rules import load_rules
        return load_rules()

    def bibles(self) -> dict[str, str]:
        return {p.stem: p.read_text(encoding="utf-8") for p in sorted((self.data_dir / "bible").glob("*.md"))}

    def ledger(self) -> dict:
        with self._db() as db:
            return json.loads(db.execute("SELECT payload FROM canon WHERE id=1").fetchone()[0])

    def snapshot(self) -> dict:
        data = self.ledger()
        return {"ledger": data, "rules": self.rules(), "bibles": self.bibles(), "fingerprint": fingerprint(data)}

    def create_run(self, *, request: str, mode: str, mock: bool, parent_run_id: str | None = None,
                   snapshot: dict | None = None, **options) -> dict:
        snap = deepcopy(snapshot) if snapshot is not None else self.snapshot()
        record = {
            "id": new_run_id(), "created_at": now(), "status": "running", "request": request,
            "mode": mode, "mock": mock, "parent_run_id": parent_run_id, "adopted_at": None,
            "snapshot": snap, "baseline_revision": snap["ledger"].get("revision", 0),
            "baseline_fingerprint": snap["fingerprint"], "scene": "", "proposal": empty_proposal(),
            "check": None, "workflow_status": "unreviewed", "review": "", "error": None,
            "revisions": 0, "calls": 0, "usage": {}, "stages": [], "options": options,
        }
        with self._db() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?)", (record["id"], record["created_at"], _json(record)))
        return record

    @staticmethod
    def _get(db, run_id: str) -> dict:
        row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("没有找到这份草稿")
        return json.loads(row[0])

    def get_run(self, run_id: str) -> dict:
        with self._db() as db:
            record = self._get(db, run_id)
        record["can_adopt"] = self.adoption_problem(record) is None
        record["adoption_error"] = self.adoption_problem(record)
        return record

    def list_runs(self) -> list[dict]:
        keys = ("id", "created_at", "status", "workflow_status", "mode", "request", "mock", "adopted_at", "parent_run_id")
        with self._db() as db:
            rows = db.execute("SELECT payload FROM runs ORDER BY created_at DESC LIMIT 100").fetchall()
        return [{k: data.get(k) for k in keys} for row in rows for data in [json.loads(row[0])]]

    def finish_run(self, run_id: str, result: dict, *, status: str = "done", error: str | None = None) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._get(db, run_id)
            if record["status"] != "running":
                raise ConflictError("已经结束的草稿不可覆盖，请创建新版本")
            allowed = {"scene", "review", "brief", "snapshot_text", "check", "proposal", "workflow_status",
                       "revisions", "calls", "usage", "stages", "characters", "attempts", "approved"}
            record.update({k: v for k, v in result.items() if k in allowed})
            record.update(status=status, error=error, finished_at=now())
            if record["workflow_status"] == "ready" and record.get("check", {}).get("status") == "pass":
                try:
                    Ledger.from_data(record["snapshot"]["ledger"]).validate_proposal(record["proposal"])
                except (ProposalError, TypeError, KeyError) as exc:
                    record["workflow_status"] = "blocked"
                    record["check"] = {"status": "revise", "issues": [{"kind": "rule", "message": str(exc)}], "questions": []}
                else:
                    record["reviewed_fingerprint"] = review_seal(record)
            db.execute("UPDATE runs SET payload=? WHERE id=?", (_json(record), run_id))
        return record

    def edit_run(self, run_id: str, *, scene: str, proposal: dict) -> dict:
        source = self.get_run(run_id)
        if source["status"] == "running":
            raise ConflictError("请等待当前运行结束再编辑")
        Ledger.from_data(source["snapshot"]["ledger"]).validate_proposal(proposal)
        record = self.create_run(request=source["request"], mode=source["mode"], mock=source["mock"],
                                 parent_run_id=run_id, snapshot=source["snapshot"], manual=True)
        return self.finish_run(record["id"], {"scene": scene, "proposal": proposal, "workflow_status": "unreviewed"})

    def adoption_problem(self, record: dict, current: dict | None = None) -> str | None:
        if record.get("adopted_at"):
            return "这份稿件已经采纳"
        if record.get("mock") and not self.allow_mock_adoption:
            return "mock 稿件仅供演示，不能采纳到正式账本"
        check = record.get("check") or {}
        if record.get("status") != "done" or record.get("workflow_status") != "ready" or check.get("status") != "pass":
            return "请先完成结构化检查；实验稿及手工修改稿不可直接采纳"
        if any(issue.get("kind") in ("rule", "missing") for issue in check.get("issues", [])):
            return "检查仍有规则问题或待补信息"
        if not record.get("scene", "").strip():
            return "场景正文为空"
        if record.get("reviewed_fingerprint") != review_seal(record):
            return "正文或提案在检查后发生变化，需要重新检查"
        current = self.ledger() if current is None else current
        if record["baseline_fingerprint"] != fingerprint(current):
            return "账本已更新，请用当前账本重新检查或定向修改这份草稿"
        if record["snapshot"]["rules"] != self.rules() or record["snapshot"]["bibles"] != self.bibles():
            return "规则或人物资料已更新，请重新检查这份草稿"
        return None

    def adopt(self, run_id: str) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = self._get(db, run_id)
            accepted = db.execute("SELECT revision FROM accepted WHERE run_id=?", (run_id,)).fetchone()
            if accepted:
                return {"ok": True, "revision": accepted[0], "idempotent": True}
            current = json.loads(db.execute("SELECT payload FROM canon WHERE id=1").fetchone()[0])
            problem = self.adoption_problem(record, current)
            if problem:
                raise ConflictError(problem)
            data = Ledger.from_data(current).apply_proposal(record["proposal"], run_id, record["scene"])
            record["adopted_at"] = now()
            db.execute("INSERT INTO accepted VALUES (?, ?, ?, ?, ?)",
                       (run_id, data["revision"], record["scene"], _json(record["proposal"]), record["adopted_at"]))
            db.execute("UPDATE canon SET payload=? WHERE id=1", (_json(data),))
            db.execute("UPDATE runs SET payload=? WHERE id=?", (_json(record), run_id))
        return {"ok": True, "revision": data["revision"], "idempotent": False}

    def recover_interrupted(self) -> None:
        """单进程工作台启动时标记中断；保留已落盘的输入、逐步调用与草稿。"""
        with self._db() as db:
            for run_id, payload in db.execute("SELECT id, payload FROM runs").fetchall():
                record = json.loads(payload)
                if record.get("status") == "running":
                    record.update(status="error", error="上次运行被中断；可以从历史稿重试", workflow_status="unreviewed")
                    db.execute("UPDATE runs SET payload=? WHERE id=?", (_json(record), run_id))

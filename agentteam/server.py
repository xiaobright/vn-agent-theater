"""本地单进程创作工作台。启动：uv run uvicorn agentteam.server:app --port 8765"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
import threading

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import pipeline
from .config import DATA_DIR, PROVIDER_PRESETS, RUNS_DIR, load_config
from .ledger import Ledger, ProposalError
from .llm import LLMCancelled, check_cancelled, scheduler_snapshot
from .main import DEFAULT_REQUEST
from .pipeline import MODE_LABELS, MODES
from .recorder import Recorder
from .workspace import ConflictError, Workspace

WEB_DIR = Path(__file__).resolve().parent / "web"


class RunRequest(BaseModel):
    request: str | None = Field(default=None, max_length=30000)
    provider: str | None = Field(default=None, max_length=80)
    reasoning: str | None = Field(default=None, max_length=30)
    mode: str = "team"
    mock: bool = True
    closed_loop: bool = True
    max_revisions: int = Field(default=1, ge=0, le=2)
    global_concurrency: int | None = Field(default=None, ge=1, le=16)
    source_run_id: str | None = None
    feedback: str | None = Field(default=None, max_length=30000)


class EditRequest(BaseModel):
    scene: str = Field(min_length=1, max_length=150000)
    proposal: dict


class RunState:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.reset()

    def reset(self):
        with self.lock:
            self.payload = {
                "status": "idle", "request": "", "provider": "", "mode": "team",
                "stage_index": -1, "stages": [], "characters": [], "brief": None,
                "snapshot": None, "scene": "", "review": None, "error": None,
                "run_dir": None, "run_id": None, "started_at": None, "calls": 0, "usage": {},
                "check": None, "proposal": None, "workflow_status": "unreviewed",
                "revisions": 0, "mock": True, "request_events": [],
            }
            self.cancel_event = threading.Event()

    def to_dict(self):
        with self.lock:
            return deepcopy(self.payload)


def _handle_event(state: RunState, kind: str, payload: object) -> None:
    with state.lock:
        p = state.payload
        if kind == "stages":
            p["stages"] = [asdict(s) if is_dataclass(s) else deepcopy(s) for s in payload]
            p["stage_index"] = -1
        elif kind == "stage":
            i, status, output = payload
            p["stage_index"] = i
            p["stages"][i]["status"] = status
            if output is not None:
                p["stages"][i]["output"] = output
            key = p["stages"][i]["key"]
            if status == "done" and key in {"writer", "write", "single", "scene", "revise", "revision"}:
                p["scene"] = output or p["scene"]
        elif kind == "cast":
            p["characters"] = [{"name": c, "status": "pending", "output": None} for c in payload]
        elif kind in {"char_start", "char_done"}:
            name = payload if kind == "char_start" else payload[0]
            for char in p["characters"]:
                if char["name"] == name:
                    char["status"] = "active" if kind == "char_start" else "done"
                    if kind == "char_done":
                        char["output"] = payload[1]
        elif kind == "speaker":
            for char in p["characters"]:
                if char["status"] == "active":
                    char["status"] = "done"
            found = next((char for char in p["characters"] if char["name"] == payload), None)
            if found:
                found["status"] = "active"
            else:
                p["characters"].append({"name": payload, "status": "active", "output": None})


def _request_event(state: RunState, event: dict):
    with state.lock:
        state.payload["request_events"].append(deepcopy(event))
        state.payload["request_events"] = state.payload["request_events"][-50:]


def _plain(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _pipeline(state: RunState, store: Workspace, record: dict, req: RunRequest, source: dict | None) -> None:
    rec = None
    try:
        cfg = load_config(mock=req.mock, provider=req.provider, reasoning=req.reasoning)
        runtime = {"cancel_event": state.cancel_event, "on_request_event": lambda e: _request_event(state, e),
                   "max_revisions": req.max_revisions}
        if req.global_concurrency is not None:
            runtime["global_concurrency"] = req.global_concurrency
        cfg = replace(cfg, **runtime)
        check_cancelled(cfg)
        run_dir = store.runs_dir / record["id"]
        rec = Recorder(run_dir)
        rec.write_json("inputs.json", record["snapshot"])
        with state.lock:
            state.payload.update(provider=cfg.describe(), run_dir=str(run_dir))
        snap = record["snapshot"]
        ledger = Ledger.from_data(snap["ledger"], rules_text=snap["rules"])
        result = pipeline.run(
            req.mode, cfg, rec, record["request"], ledger, snap["bibles"],
            on_event=lambda kind, payload=None: _handle_event(state, kind, payload),
            closed_loop=req.closed_loop, max_revisions=req.max_revisions,
            feedback=req.feedback,
            initial_scene=source.get("scene") or None if source else None,
            initial_proposal=source.get("proposal") if source and source.get("scene") else None,
        )
        check_cancelled(cfg)
        result = _plain(result)
        result["snapshot_text"] = result.pop("snapshot", None)
        rec.write_json("result.json", result)
        for field, name in (("scene", "scene.md"), ("review", "review.md"), ("brief", "brief.md"), ("snapshot_text", "snapshot.md")):
            content = result.get(field) or ""
            rec.write_text(name, content if isinstance(content, str) else str(content))
        rec.write_summary([
            f"- 模式：{MODE_LABELS[req.mode]}", f"- 模型：{cfg.describe()}",
            f"- 状态：{result.get('workflow_status')}", f"- 调用次数：{rec.seq}",
            f"- 基线账本修订：{record['baseline_revision']}", "",
            "## 场景稿", result.get("scene") or "", "", "## 审读意见", result.get("review") or "",
        ])
        # 取消与任务完成共享同一把锁，避免取消后仍变成可采纳。
        with state.lock:
            check_cancelled(cfg)
            saved = store.finish_run(record["id"], result)
            for key in ("scene", "review", "brief", "calls", "usage", "check", "proposal", "workflow_status", "revisions"):
                state.payload[key] = saved.get(key)
            state.payload.update(status="done", snapshot=result.get("snapshot_text"),
                                 stage_index=len(state.payload["stages"]))
            for char in state.payload["characters"]:
                if char["status"] == "active":
                    char["status"] = "done"
    except Exception as exc:
        status = "cancelled" if isinstance(exc, LLMCancelled) or state.cancel_event.is_set() else "error"
        # 请求层异常经过脱敏；其他本地校验只包含路径/字段，不返回 traceback 或配置对象。
        error = "已取消本次运行" if status == "cancelled" else str(exc)
        with state.lock:
            partial = {k: deepcopy(state.payload[k]) for k in ("scene", "stages", "characters")}
            partial.update(workflow_status="unreviewed", calls=rec.seq if rec else 0,
                           usage=dict(rec.usage_total) if rec else {})
            try:
                store.finish_run(record["id"], partial, status=status, error=error)
            finally:
                state.payload.update(status=status, error=error, **partial)


def create_app(data_dir: Path = DATA_DIR, runs_dir: Path = RUNS_DIR, *, rules_loader=None,
               allow_mock_adoption: bool = False, force_mock: bool = False) -> FastAPI:
    app = FastAPI(title="全员存取档 · 创作工作台")
    state = RunState()
    app.state.run_state = state

    @lru_cache(maxsize=1)
    def workspace():
        store = Workspace(data_dir, runs_dir, rules_loader=rules_loader, allow_mock_adoption=allow_mock_adoption)
        store.recover_interrupted()
        return store

    app.state.workspace = workspace

    def error_response(exc):
        code = 404 if isinstance(exc, KeyError) else 409 if isinstance(exc, ConflictError) else 422
        return JSONResponse({"ok": False, "error": str(exc).strip("'")}, status_code=code)

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/state")
    def get_state():
        payload = state.to_dict()
        payload.update(
            ledger=workspace().ledger(), scheduler=scheduler_snapshot(),
            providers=[{"name": k, "label": v["label"]} for k, v in PROVIDER_PRESETS.items()],
            modes=[{"name": m, "label": MODE_LABELS[m]} for m in MODES],
            force_mock=force_mock,
        )
        return payload

    @app.get("/api/runs")
    def get_runs():
        return {"runs": workspace().list_runs()}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        try:
            return workspace().get_run(run_id)
        except KeyError as exc:
            return error_response(exc)

    @app.post("/api/run")
    def start_run(req: RunRequest):
        if req.mode not in MODES:
            return error_response(ValueError("未知协作模式"))
        if force_mock:
            req = req.model_copy(update={"mock": True})
        with state.lock:
            if state.payload["status"] == "running":
                return error_response(ConflictError("已有一次运行在进行中"))
            try:
                store = workspace()
                source = store.get_run(req.source_run_id) if req.source_run_id else None
                request = (req.request or (source["request"] if source else DEFAULT_REQUEST)).strip()
                if not request:
                    raise ValueError("请填写本场创作要求")
                if source and source.get("adopted_at"):
                    raise ConflictError("已采纳稿已成为事实，请新建下一场；不要将同一场再次采纳")
                record = store.create_run(
                    request=request, mode=req.mode, mock=req.mock, parent_run_id=req.source_run_id,
                    closed_loop=req.closed_loop, max_revisions=req.max_revisions, feedback=req.feedback,
                )
            except (KeyError, ValueError) as exc:
                return error_response(exc)
            state.reset()
            state.payload.update(status="running", request=request, mode=req.mode, mock=req.mock,
                                 run_id=record["id"], started_at=datetime.now().strftime("%H:%M:%S"))
            # 在启动线程前占位；两个同时 POST 不会都被接受。
            try:
                threading.Thread(target=_pipeline, args=(state, store, record, req, source), daemon=True).start()
            except Exception:
                store.finish_run(record["id"], {}, status="error", error="无法启动后台任务")
                state.payload.update(status="error", error="无法启动后台任务")
                return error_response(ConflictError("无法启动后台任务"))
        return {"ok": True, "run_id": record["id"]}

    @app.post("/api/cancel")
    def cancel_run():
        with state.lock:
            if state.payload["status"] != "running":
                return error_response(ConflictError("没有正在运行的任务"))
            state.cancel_event.set()
        return {"ok": True}

    @app.patch("/api/runs/{run_id}")
    def edit_run(run_id: str, req: EditRequest):
        try:
            result = workspace().edit_run(run_id, scene=req.scene, proposal=req.proposal)
            return {"ok": True, "run_id": result["id"]}
        except (KeyError, ValueError) as exc:
            return error_response(exc)

    @app.post("/api/runs/{run_id}/adopt")
    def adopt_run(run_id: str):
        try:
            return workspace().adopt(run_id)
        except (KeyError, ValueError) as exc:
            return error_response(exc)

    return app


app = create_app()
STATE = app.state.run_state


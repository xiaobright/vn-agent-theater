"""把一次创作跑成流水线，并支持多种"协作模式"，方便横向对比。

模式：
  team          总编 → 时间线主管 → 角色编剧（切片事实包·隔离）→ 主笔 → 审读
  team_fullctx  同上，但角色编剧看到全量账本（故意关掉隔离，做对照）
  single        一个智能体拿到全部资料，直接写稿（单 agent 基线）
  groupchat     五个岗位在同一讨论串里依次发言，再由主笔成稿（群聊拓扑）

on_event(kind, payload) 是给调用方（网页/命令行）的进度回调：
  "stages"     payload = list[Stage]        —— 声明本次有哪些阶段
  "stage"      payload = (index, status, output)
  "cast"       payload = list[str]          —— 本场出场角色
  "char_start" payload = 角色名
  "char_done"  payload = (角色名, 文本)
  "speaker"    payload = 角色名             —— 群聊模式：轮到谁发言
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import agents
from .config import Config
from .ledger import Ledger
from .recorder import Recorder
from .llm import check_cancelled

MODES = ["workshop", "team", "team_fullctx", "single", "groupchat", "groupchat_guarded"]

MODE_LABELS = {
    "workshop": "编剧室 · 审查与采纳闭环",
    "team": "团队 · 切片隔离（默认）",
    "team_fullctx": "团队 · 全量账本（隔离关闭，对照）",
    "single": "单 agent 基线",
    "groupchat": "群聊拓扑（对照）",
    "groupchat_guarded": "群聊 + 规则守门（对照）",
}


@dataclass
class Stage:
    key: str
    label: str
    desc: str
    status: str = "pending"
    output: str | None = None


def _stage_set(mode: str) -> list[Stage]:
    if mode in ("workshop", "team", "team_fullctx"):
        return [
            Stage("supervisor", "总编", "把要求变成本场目标"),
            Stage("timeline", "时间线主管", "生成场前状态快照"),
            Stage("characters", "角色编剧",
                  "并发分析各角色" + ("（全量账本·隔离关闭）" if mode == "team_fullctx" else "（切片事实包）")),
            Stage("writer", "主笔", "写完整场景稿"),
            Stage("review", "审读", "独立视角找问题"),
        ]
    if mode == "single":
        return [Stage("single", "单智能体", "拿到全部资料直接写稿")]
    if mode == "groupchat":
        return [
            Stage("groupchat", "群聊讨论", "五个岗位在同一讨论串里依次发言"),
            Stage("writer", "主笔", "据讨论串写完整场景稿"),
        ]
    if mode == "groupchat_guarded":
        return [
            Stage("groupchat", "群聊讨论", "五个岗位在同一讨论串里依次发言"),
            Stage("gatekeeper", "规则守门", "有否决权：批准可用点子、否决违规内容"),
            Stage("writer", "主笔", "只据批准清单写稿"),
        ]
    raise ValueError(f"未知模式：{mode}")


def run(
    mode: str,
    cfg: Config,
    rec: Recorder,
    request: str,
    ledger: Ledger,
    bibles: dict[str, str],
    on_event: Callable[[str, object], None] | None = None,
    closed_loop: bool = True,
    max_revisions: int = 1,
    feedback: str | None = None,
    initial_scene: str | None = None,
    initial_proposal: dict | None = None,
) -> dict:
    if mode not in MODES:
        raise ValueError(f"未知模式：{mode}")
    if not isinstance(max_revisions, int) or not 0 <= max_revisions <= 2:
        raise ValueError("自动修订次数必须为 0–2")
    if initial_proposal is not None and initial_scene is None:
        raise ValueError("提供状态提案时必须同时提供对应稿件")
    check_cancelled(cfg)
    emit = on_event or (lambda kind, payload=None: None)
    stages = ([Stage("revision", "已有稿件", "定向修改或重新审查")]
              if initial_scene is not None else _stage_set(mode))
    base_stages = len(stages)
    if closed_loop:
        stages.extend([Stage("proposal", "状态提案", "只提出候选变化"),
                       Stage("check", "机制审查", "审核正文与同一份状态提案"),
                       Stage("revisions", "修订复验", "有限次修改，等待人工采纳")])
    emit("stages", stages)

    def set_stage(i: int, status: str, output: str | None = None) -> None:
        check_cancelled(cfg)
        stages[i].status = status
        if output is not None:
            stages[i].output = output
        emit("stage", (i, status, output))

    result: dict = {
        "mode": mode,
        "brief": None,
        "snapshot": None,
        "characters": [],
        "scene": "",
        "review": None,
        "check": None,
        "proposal": None,
        "workflow_status": "experiment",
        "revisions": 0,
        "revision_history": [],
    }

    def finish() -> dict:
        result["stages"] = stages
        result["calls"] = rec.seq
        result["usage"] = dict(rec.usage_total)
        return result

    def block(message: str) -> dict:
        result["check"] = {"status": "revise", "issues": [{"kind": "missing", "message": message}],
                           "questions": []}
        result["workflow_status"] = "blocked"
        result["review"] = message
        emit("check", result["check"])
        return finish()

    if initial_scene is not None:
        set_stage(0, "active")
        if feedback and feedback.strip():
            result["scene"] = agents.revise_scene(cfg, rec, initial_scene, feedback, ledger, request)
            result["revisions"] = 1
            initial_proposal = None  # Changed prose requires a new, matching proposal.
        else:
            result["scene"] = initial_scene
        set_stage(0, "done", result["scene"])

    elif mode in ("workshop", "team", "team_fullctx"):
        set_stage(0, "active")
        brief = agents.supervisor_brief(cfg, rec, request, ledger)
        result["brief"] = brief
        set_stage(0, "done", brief)

        set_stage(1, "active")
        snap = agents.timeline_snapshot(cfg, rec, ledger)
        result["snapshot"] = snap
        set_stage(1, "done", snap)

        cast = agents.parse_cast(brief, list(ledger.data["characters"].keys()))
        emit("cast", cast)
        result["characters"] = [{"name": c, "text": None} for c in cast]

        def on_start(cid: str) -> None:
            emit("char_start", cid)

        def on_done(cid: str, text: str) -> None:
            for c in result["characters"]:
                if c["name"] == cid:
                    c["text"] = text
            emit("char_done", (cid, text))

        set_stage(2, "active")
        ctx = "full" if mode == "team_fullctx" else "sliced"
        results = agents.analyze_characters(
            cfg, rec, ledger, bibles, cast, brief,
            on_start=on_start, on_done=on_done, context_mode=ctx,
        )
        set_stage(2, "done", f"{len(results)} 个角色完成")

        set_stage(3, "active")
        scene = agents.write_scene(cfg, rec, brief, snap, results, ledger=ledger)
        result["scene"] = scene
        set_stage(3, "done", scene)

        set_stage(4, "active")
        review = agents.review_scene(cfg, rec, scene, ledger)
        result["review"] = review
        set_stage(4, "done", review)

    elif mode == "single":
        set_stage(0, "active")
        scene = agents.single_shot(cfg, rec, request, ledger, bibles)
        result["scene"] = scene
        set_stage(0, "done", scene)

    elif mode == "groupchat":
        set_stage(0, "active")

        def on_speaker(name: str) -> None:
            emit("speaker", name)

        thread = agents.groupchat_discussion(cfg, rec, request, ledger, bibles, on_speaker=on_speaker)
        result["brief"] = thread
        set_stage(0, "done", thread)

        set_stage(1, "active")
        scene = agents.write_scene(cfg, rec, thread, "（群聊模式没有单独的场前快照）", [], ledger=ledger)
        result["scene"] = scene
        set_stage(1, "done", scene)

    elif mode == "groupchat_guarded":
        set_stage(0, "active")

        def on_speaker(name: str) -> None:
            emit("speaker", name)

        thread = agents.groupchat_discussion(cfg, rec, request, ledger, bibles, on_speaker=on_speaker)
        result["brief"] = thread
        set_stage(0, "done", thread)

        set_stage(1, "active")
        try:
            gate = agents.gatekeeper_review(cfg, rec, thread, ledger, request)
        except ValueError as exc:
            set_stage(1, "error", str(exc))
            return block(f"规则守门格式无效：{exc}")
        result["gate"] = gate
        result["approved"] = gate["approved"]
        set_stage(1, "done", str(gate))
        if gate["status"] != "pass":
            result["check"] = {k: gate[k] for k in ("status", "issues", "questions")}
            result["workflow_status"] = "needs_input" if gate["status"] == "needs_input" else "blocked"
            result["review"] = "\n".join(i["message"] for i in gate["issues"])
            emit("check", result["check"])
            return finish()

        set_stage(2, "active")
        scene = agents.write_scene(cfg, rec, gate["approved"],
                                   "（本场没有单独的快照，判定依据见批准清单）", [], ledger=ledger)
        result["scene"] = scene
        set_stage(2, "done", scene)

    if not closed_loop:
        return finish()
    if not result["scene"].strip():
        return block("稿件为空，不能通过审查。")

    proposal = initial_proposal
    while True:
        check_cancelled(cfg)
        try:
            set_stage(base_stages, "active")
            if proposal is None:
                proposal = agents.propose_changes(cfg, rec, result["scene"], ledger, request)
            proposal = agents.validate_proposal(proposal, ledger)
            result["proposal"] = proposal
            emit("proposal", proposal)
            set_stage(base_stages, "done", str(proposal))
            set_stage(base_stages + 1, "active")
            check = agents.check_scene(cfg, rec, result["scene"], ledger, request, proposal=proposal)
            check = agents.validate_check(check)
        except ValueError as exc:
            set_stage(base_stages + 1, "error", str(exc))
            return block(f"结构化审查未完成：{exc}")
        result["check"] = check
        result["revision_history"].append({"scene": result["scene"], "check": check,
                                           "proposal": proposal})
        emit("check", check)
        set_stage(base_stages + 1, "done", str(check))
        result["review"] = "\n".join([check["status"]]
            + [i["message"] for i in check["issues"]] + check["questions"])
        if check["status"] == "pass":
            result["workflow_status"] = "ready"
            break
        if check["status"] == "needs_input":
            result["workflow_status"] = "needs_input"
            break
        if result["revisions"] >= max_revisions:
            result["workflow_status"] = "blocked"
            break
        set_stage(base_stages + 2, "active")
        result["scene"] = agents.revise_scene(cfg, rec, result["scene"], check, ledger, request)
        result["revisions"] += 1
        emit("revision", {"count": result["revisions"], "scene": result["scene"]})
        proposal = None
    set_stage(base_stages + 2, "done", f"修订 {result['revisions']} 次；{result['workflow_status']}")
    return finish()

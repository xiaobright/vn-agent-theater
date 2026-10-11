"""创作事实账本；候选修改只有经工作台采纳后才进入下一场。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path


class ProposalError(ValueError):
    pass


def fingerprint(data: object) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def empty_proposal() -> dict:
    return {"events": [], "character_updates": {}, "foreshadowing_updates": [], "player_seen_add": []}


def _strings(value: object, label: str) -> None:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ProposalError(f"{label} 必须是非空字符串组成的数组")


class Ledger:
    def __init__(self, path: Path, rules_text: str | None = None):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self._rules_text = rules_text
        self.validate()

    @classmethod
    def from_data(cls, data: dict, rules_text: str | None = None) -> "Ledger":
        ledger = cls.__new__(cls)
        ledger.path = None
        ledger.data = deepcopy(data)
        ledger._rules_text = rules_text
        ledger.validate()
        return ledger

    def validate(self) -> None:
        versions = self.data.get("versions")
        characters = self.data.get("characters")
        if not isinstance(versions, list) or not versions or not isinstance(characters, dict):
            raise ProposalError("账本缺少版本或角色数据")
        ids = [v.get("id") for v in versions]
        if any(not isinstance(vid, str) or not vid for vid in ids) or len(ids) != len(set(ids)):
            raise ProposalError("账本版本 ID 缺失或重复")
        if self.data.get("current_version") not in ids:
            raise ProposalError("当前版本不存在")
        for v in versions:
            if v.get("parent") is not None and v["parent"] not in ids:
                raise ProposalError(f"版本 {v['id']} 的父版本不存在")
        for name, char in characters.items():
            slots = char.get("slots", [])
            if len(slots) != 3 or {s.get("slot") for s in slots} != {1, 2, 3}:
                raise ProposalError(f"{name} 必须有固定的三个槽位")
            for s in slots:
                if s.get("version") is not None and s["version"] not in ids:
                    raise ProposalError(f"{name} 的槽位引用了不存在的版本")

    def current_version_id(self) -> str:
        return self.data["current_version"]

    def version(self, vid: str | None = None) -> dict:
        vid = vid or self.current_version_id()
        for v in self.data["versions"]:
            if v["id"] == vid:
                return deepcopy(v)
        raise KeyError(f"账本里没有版本 {vid}")

    def author_view(self) -> dict:
        return self.context_snapshot()

    def timeline_view(self) -> dict:
        return self.context_snapshot()

    def context_snapshot(self) -> dict:
        return deepcopy(self.data)

    def fingerprint(self) -> str:
        return fingerprint(self.data)

    def rules_text(self) -> str:
        if self._rules_text is None:
            from .rules import load_rules
            self._rules_text = load_rules()
        return self._rules_text

    def slice_for(self, char_id: str) -> dict:
        char = self.data["characters"].get(char_id)
        if char is None:
            raise KeyError(f"账本里没有角色 {char_id}")
        # 作者的事件列表包含隐藏支线；角色认知只从各自的 knows 等字段读取。
        return {
            "version": {"world_date": self.version().get("world_date")},
            "character": deepcopy(char),
        }

    def validate_proposal(self, proposal: dict) -> None:
        required = set(empty_proposal())
        if not isinstance(proposal, dict) or not required <= proposal.keys():
            raise ProposalError("状态提案缺少 events / character_updates / foreshadowing_updates / player_seen_add")
        if set(proposal) - required - {"summary", "world_date"}:
            raise ProposalError("本轮只支持连续场景；存档、读档及版本跳跃需要完整目标快照，暂不可采纳")
        _strings(proposal["events"], "events")
        _strings(proposal["player_seen_add"], "player_seen_add")
        for key in ("summary", "world_date"):
            if key in proposal and (not isinstance(proposal[key], str) or not proposal[key].strip()):
                raise ProposalError(f"{key} 必须为非空文字")
        updates = proposal["character_updates"]
        if not isinstance(updates, dict):
            raise ProposalError("character_updates 必须按角色名索引")
        allowed = {"alive", "can_act", "knows", "believes", "misjudges", "relations", "mechanism_awareness", "goal"}
        for name, patch in updates.items():
            if name not in self.data["characters"]:
                raise ProposalError(f"未知角色：{name}")
            if not isinstance(patch, dict) or set(patch) - allowed:
                raise ProposalError(f"{name} 包含不支持的字段（本轮不可改动槽位）")
            for key, value in patch.items():
                if key in {"alive", "can_act"} and not isinstance(value, bool):
                    raise ProposalError(f"{name}.{key} 必须为布尔值")
                if key in {"knows", "believes", "misjudges"}:
                    _strings(value, f"{name}.{key}")
                if key in {"goal", "mechanism_awareness"} and not isinstance(value, str):
                    raise ProposalError(f"{name}.{key} 必须为文字")
                if key == "relations":
                    if not isinstance(value, dict) or any(k not in self.data["characters"] or not isinstance(v, str) for k, v in value.items()):
                        raise ProposalError(f"{name}.relations 的目标必须是已知角色，关系必须为文字")
        clues = proposal["foreshadowing_updates"]
        if not isinstance(clues, list):
            raise ProposalError("foreshadowing_updates 必须为数组")
        existing = {f["id"] for f in self.data.get("foreshadowing", [])}
        seen = set()
        for clue in clues:
            if not isinstance(clue, dict) or set(clue) - {"id", "desc", "status"}:
                raise ProposalError("伏笔只接受 id / desc / status")
            if any(not isinstance(clue.get(k), str) or not clue[k].strip() for k in ("id", "status")):
                raise ProposalError("伏笔需要非空 id 和 status")
            if clue["id"] in seen:
                raise ProposalError("同一提案的伏笔 ID 不可重复")
            seen.add(clue["id"])
            if clue["id"] not in existing or "desc" in clue:
                if not isinstance(clue.get("desc"), str) or not clue["desc"].strip():
                    raise ProposalError("新增伏笔需要 desc")

    def apply_proposal(self, proposal: dict, run_id: str, scene: str) -> dict:
        self.validate_proposal(proposal)
        data = self.context_snapshot()
        vid = f"scene_{run_id}"
        if any(v["id"] == vid for v in data["versions"]):
            raise ProposalError("此场景已经进入账本")
        data["versions"].append({
            "id": vid, "parent": self.current_version_id(), "transition": "normal", "actor": None,
            "world_date": proposal.get("world_date", self.version().get("world_date", "未定")),
            "label": proposal.get("summary", "已采纳场景"), "events": deepcopy(proposal["events"]),
        })
        data["current_version"] = vid
        data["revision"] = int(data.get("revision", 0)) + 1
        for name, patch in proposal["character_updates"].items():
            data["characters"][name].update(deepcopy(patch))
        clues = {f["id"]: f for f in data.get("foreshadowing", [])}
        for patch in proposal["foreshadowing_updates"]:
            clues.setdefault(patch["id"], {}).update(deepcopy(patch))
        data["foreshadowing"] = list(clues.values())
        data["player_seen"] = list(dict.fromkeys(data.get("player_seen", []) + proposal["player_seen_add"]))
        data["last_scene"] = {"id": run_id, "scene": scene, "summary": proposal.get("summary", "已采纳场景")}
        self.from_data(data, self._rules_text)
        return data


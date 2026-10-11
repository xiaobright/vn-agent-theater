"""观察者：信念与事实的差分，以及"严重跑偏就暂停"的判据。

分两层，故意分开：

  结构层（不调模型，确定性的）
    隔离是结构性的，所以泄漏只可能来自模型自己"编"出来。做法是把这个角色
    **不该有**的那些字符串收集起来（别人的内心、别人的备注、别人的记忆覆盖层），
    在他的输出里做包含检查。命中就是硬证据，不需要评审口味。

  语义层（调一次模型）
    把客观事件日志 + 谁看得见哪些事件 + 结构层发现的问题交给观察者，让它报
    「谁在哪一点上与事实分叉」，并判断要不要暂停整场。

暂停判据只认**机制违反**（用了不该有的知识、存读档与快照图矛盾），不认审美分歧——
审美分歧不可判定，停太早戏就长不出来。
"""
from __future__ import annotations

import json
import re

from ..config import PROMPTS_DIR, Config, for_role
from ..llm import chat, check_cancelled
from ..recorder import Recorder
from .actor import belief_matrix, dump_matrix
from .world import World

MIN_SECRET_LEN = 6

# 观察者要动脑（对照日志、盲区表和结构层证据），而**推理 token 也算进 completion**
# 预算（stepfun 实测：2366 completion 只吐出 93 字正文）。用默认 4096 会写到一半被截断，
# 所以这个岗位单独给一个大预算。
OBSERVER_MAX_TOKENS = 8192


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def secret_strings(world: World, actor: str) -> list[dict]:
    """这个角色此刻**不该知道**的具体字符串，连同来源。

    要分两种情况：整条事件他都不该看见（不在场、别人的备注/记忆），
    以及事件他看得见、但其中的 private 部分只属于当事人（别人的内心）。
    """
    secrets: list[dict] = []
    remembered = world.view(actor)
    known_ids = {e.event_id for e in remembered}
    known_text = _normalize("\n".join(e.text_for(actor) for e in remembered))
    for event in world.path_events():
        owner = event.actor or event.to
        source = {"turn": event.turn, "kind": event.kind, "owner": owner}
        if event.event_id not in known_ids:
            candidates = (event.public, event.private)
        elif event.actor != actor and event.private:
            candidates = (event.private,)
        else:
            candidates = ()
        for text in candidates:
            clean = _normalize(text)
            if len(clean) >= MIN_SECRET_LEN and clean not in known_text:
                secrets.append({**source, "text": clean})
    for name in world.characters:
        if name == actor:
            continue
        for slot in world.slots(name):
            if slot and slot["note"] and len(_normalize(slot["note"])) >= MIN_SECRET_LEN and _normalize(slot["note"]) not in known_text:
                secrets.append({"turn": slot["turn"], "kind": "slot_note", "owner": name,
                                "text": _normalize(slot["note"])})
    return secrets


def leak_check(world: World, actor: str, output: str) -> list[dict]:
    """在角色输出里找他不可能知道的字符串。命中即硬证据。"""
    clean = _normalize(output)
    hits = []
    for secret in secret_strings(world, actor):
        if secret["text"] in clean:
            hits.append({"kind": "leak", "actor": actor, "secret_owner": secret["owner"],
                         "source_kind": secret["kind"], "source_turn": secret["turn"],
                         "matched": secret["text"][:80]})
    return hits


def observe(cfg: Config, rec: Recorder, world: World, request: str,
            structural: list[dict], mock_text: str | None = None, *, rules: str = "") -> dict:
    """语义层观察：分叉与暂停判据。输出结构化，供导演决定继续还是停。"""
    check_cancelled(cfg)
    card = (PROMPTS_DIR / "theater_observer.md").read_text(encoding="utf-8").strip()
    user = "\n".join([
        "# 作者机制规则（不得当作角色已经知道的知识）\n" + rules,
        "# 本场目标\n" + request,
        "# 客观事件日志（事实，按轮次）\n" + json.dumps(
            [{"turn": e.turn, "kind": e.kind, "actor": e.actor, "public": e.public[:200],
              "present": list(e.present), "world_date": e.world_date}
             for e in world.path_events()], ensure_ascii=False, indent=2),
        "# 信念与事实的差分（谁看得见哪些事件；blind_spots = 他没看见的）\n"
        + dump_matrix(belief_matrix(world)),
        "# 结构层已发现的泄漏或非法操作\n"
        + (json.dumps(structural, ensure_ascii=False, indent=2) if structural else "（无）"),
        "# 当前世界状态\n" + json.dumps(world.character_state(), ensure_ascii=False, indent=2),
    ])
    messages = [{"role": "system", "content": card}, {"role": "user", "content": user}]
    cfg = for_role(cfg, "continuity")
    text, usage, elapsed = chat(cfg, messages, max_tokens=max(cfg.max_output_tokens, OBSERVER_MAX_TOKENS),
                                mock_text=mock_text or json.dumps(
                                    {"status": "ok", "divergences": [], "reason": "未发现机制违反。"},
                                    ensure_ascii=False))
    rec.record(role="观察者", model=cfg.model, messages=messages, response=text,
               usage=usage, elapsed=elapsed, extra={"turn": world.live[1]})
    return validate_report(parse_object(text), structural)


def parse_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
        if not match:
            raise ValueError("观察者输出不是完整 JSON")
        text = match.group(1)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("观察者输出必须是 JSON 对象")
    return value


def hard_evidence(structural: list[dict]) -> list[dict]:
    """只有**泄漏**是硬证据。非法动作是玩法失误，调用失败是基础设施问题，
    都不该让观察者替作者按下暂停——那两类记进报告由人看。"""
    return [i for i in structural if i.get("kind") == "leak"]


def validate_report(value: dict, structural: list[dict]) -> dict:
    status = value.get("status")
    if status not in {"ok", "pause"}:
        raise ValueError("观察者状态必须是 ok 或 pause")
    divergences = value.get("divergences", [])
    if not isinstance(divergences, list):
        raise ValueError("divergences 必须是数组")
    reason = value.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("reason 必须是文字")
    # 结构层命中泄漏时不允许"一切正常"——硬证据优先于模型意见。
    if hard_evidence(structural) and status == "ok":
        status = "pause"
        reason = (reason + " 结构层发现泄漏硬证据，已强制暂停。").strip()
    return {"status": status, "divergences": divergences, "reason": reason,
            "structural": structural}

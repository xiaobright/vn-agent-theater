"""五个岗位的调用：总编 / 时间线主管 / 角色编剧 / 主笔 / 审读。

每个函数只做三件事：装配消息 → 调 llm.chat → 记录。
装配消息的地方，就是"上下文工程"发生的地方——想看清谁被喂了什么，看这里。
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .config import PROMPTS_DIR, Config, for_role
from .ledger import Ledger
from .llm import chat, check_cancelled
from .recorder import Recorder
from .rules import load_rules


def _card(name: str) -> str:
    """读岗位卡（prompts/<name>.md）。改岗位说明不用碰代码。"""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def _run(cfg: Config, rec: Recorder, role: str, system: str, user: str, mock_text: str) -> str:
    check_cancelled(cfg)
    cfg = for_role(cfg, role)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    text, usage, dt = chat(cfg, messages, mock_text=mock_text)
    rec.record(role=role, model=cfg.model, messages=messages, response=text, usage=usage, elapsed=dt)
    return text


def _rules(ledger: Ledger) -> str:
    method = getattr(ledger, "rules_text", None)
    return method() if method else load_rules()


def _state(ledger: Ledger) -> dict:
    method = getattr(ledger, "context_snapshot", None)
    return method() if method else ledger.timeline_view()


def _reference(ledger: Ledger) -> str:
    return ("# 权威规则（作者知识，不代表角色已经知道）\n" + _rules(ledger)
            + "\n\n# 当前账本\n```json\n"
            + json.dumps(_state(ledger), ensure_ascii=False, indent=2) + "\n```\n")


# ---------------- 总编 ----------------

def supervisor_brief(cfg: Config, rec: Recorder, request: str, ledger: Ledger) -> str:
    system = _card("supervisor")
    user = (
        "# 作者的要求\n" + request + "\n\n"
        + _reference(ledger)
    )
    return _run(cfg, rec, "总编", system, user, _MOCK_BRIEF)


def parse_cast(brief: str, known: list[str]) -> list[str]:
    """从总编的目标里抠出"出场角色"。抠不到就默认全员出场。"""
    for line in brief.splitlines():
        if "出场角色" in line:
            tail = line.split("：", 1)[-1] if "：" in line else line.split(":", 1)[-1]
            names = [n.strip(" 　*、") for n in re.split(r"[、,，\s]+", tail)]
            picked = [n for n in names if n in known]
            if picked:
                return picked
    return list(known)


# ---------------- 时间线主管 ----------------

def timeline_snapshot(cfg: Config, rec: Recorder, ledger: Ledger) -> str:
    system = _card("timeline_keeper")
    user = _reference(ledger)
    return _run(cfg, rec, "时间线主管", system, user, _MOCK_SNAPSHOT)


# ---------------- 角色编剧（并发扇出）----------------

def analyze_characters(
    cfg: Config,
    rec: Recorder,
    ledger: Ledger,
    bibles: dict[str, str],
    cast: list[str],
    brief: str,
    on_start: Callable[[str], None] | None = None,
    on_done: Callable[[str, str], None] | None = None,
    context_mode: str = "sliced",
) -> list[dict]:
    """并发调用各角色编剧。

    on_start / on_done 是给网页观察台用的实时回调——让它能看到谁在跑、谁跑完了。
    context_mode="sliced" 是正常模式（只给切片事实包）；"full" 是故意关掉隔离，
    把全量账本塞给每个角色，用来做对照实验。
    """

    def one(char_id: str) -> dict:
        if on_start:
            on_start(char_id)
        system = _card("character").replace("{{name}}", char_id)
        if context_mode == "full":
            fact_pack = ledger.timeline_view()  # ← 隔离关闭（对照用）
            pack_title = "本场资料（全量账本 · 隔离已关闭）"
            goal = f"# 作者的全场安排（全量对照）\n{brief}\n\n"
        else:
            raw = ledger.slice_for(char_id)
            version = raw.get("version") or ledger.version()
            # Never forward the complete brief or global version.events here.
            fact_pack = {"version": {k: version[k] for k in ("id", "world_date") if k in version},
                         "character": raw["character"]}
            pack_title = "本场事实包（只含你在当前版本下应该知道的）"
            goal = "# 本次任务\n依据当前目标、认知与人物卡，推演下一步行动和对白倾向；不预设幕后事件。\n\n"
        user = (
            goal +
            f"# 人物圣经（{char_id}）\n{bibles.get(char_id, '（缺，未提供）')}\n\n"
            f"# {pack_title}\n```json\n"
            + json.dumps(fact_pack, ensure_ascii=False, indent=2)
            + "\n```\n"
        )
        text = _run(cfg, rec, f"角色_{char_id}", system, user, _MOCK_CHAR.format(name=char_id))
        if on_done:
            on_done(char_id, text)
        return {"character": char_id, "text": text}

    with ThreadPoolExecutor(max_workers=max(1, min(len(cast), getattr(cfg, "global_concurrency", 2)))) as pool:
        return list(pool.map(one, cast))


# ---------------- 主笔 ----------------

def write_scene(
    cfg: Config, rec: Recorder, brief: str, snapshot: str, char_results: list[dict],
    ledger: Ledger | None = None,
) -> str:
    system = _card("writer")
    parts = ["# 本场目标\n" + brief, "# 场前状态快照\n" + snapshot, "# 各角色分析"]
    for r in char_results:
        parts.append(f"## {r['character']}\n{r['text']}")
    if ledger is not None:
        parts.append(_reference(ledger))
    user = "\n\n".join(parts) + "\n"
    return _run(cfg, rec, "主笔", system, user, _MOCK_SCENE)


# ---------------- 审读（独立视角）----------------

def review_scene(cfg: Config, rec: Recorder, scene: str, ledger: Ledger) -> str:
    system = _card("reviewer")
    seen = ledger.data.get("player_seen") or []
    user = (
        "# 已写好的场景稿\n" + scene + "\n\n"
        "# 此前玩家已经看到过的内容\n"
        + ("\n".join(f"- {s}" for s in seen) if seen else "（这是第一场戏，玩家此前什么都没看过）")
        + "\n"
    )
    return _run(cfg, rec, "审读", system, user, _MOCK_REVIEW)


# ---------------- 评审（对比实验用）----------------

def judge_scenes(cfg: Config, rec: Recorder, request: str, items: list[dict],
                 context: dict | None = None) -> str:
    """items: [{"mode": 模式名, "scene": 稿件}]，让模型当评审横向打分。"""
    if not context or not all(k in context for k in ("rules", "ledger", "bibles")):
        raise ValueError("评审缺少已冻结的规则、账本或人物卡；不能把无依据评分当作机制检查。")
    system = context.get("judge_prompt") or _card("judge")
    parts = [f"# 创作要求\n{request}\n",
             "# 权威规则\n" + context["rules"],
             "# 账本与人物卡\n" + json.dumps({"ledger": context["ledger"],
              "bibles": context["bibles"]}, ensure_ascii=False, indent=2)]
    for i, it in enumerate(items):
        label = chr(ord("A") + i)
        parts.append(f"# 稿件 {label}\n{it['scene']}\n")
    return _run(cfg, rec, "评审", system, "\n".join(parts), "[mock] 评审：各稿各有长短……")


def evaluation_context(ledger: Ledger, bibles: dict[str, str]) -> dict:
    return {"rules": _rules(ledger), "ledger": _state(ledger), "bibles": dict(bibles),
            "judge_prompt": _card("judge")}


# ---------------- 单 agent 基线（对照用）----------------

def single_shot(cfg: Config, rec: Recorder, request: str, ledger: Ledger,
                bibles: dict[str, str]) -> str:
    """一个智能体拿到全部资料，直接写稿。用来和"团队版"做对照。"""
    system = _card("single")
    user = (
        "# 作者的要求\n" + request + "\n\n"
        + _reference(ledger) + "\n"
        "# 人物圣经\n"
        + "\n\n".join(f"## {k}\n{v}" for k, v in bibles.items())
        + "\n"
    )
    return _run(cfg, rec, "单智能体", system, user, _MOCK_SCENE)


# ---------------- 群聊拓扑（对照用）----------------

GROUP_ROLES: list[tuple[str, str]] = [
    ("总编", "把作者的要求变成本场目标，把握整体方向"),
    ("时间线主管", "维护账本、校验读档逻辑，指出连续性风险"),
    ("主角", "只从主角的立场出发，给出他的动机与行动"),
    ("首领", "只从首领的立场出发，给出他的动机与行动"),
    ("审读", "挑问题：人设混同、反转无据、玩家看不懂"),
]


def groupchat_discussion(cfg: Config, rec: Recorder, request: str, ledger: Ledger,
                         bibles: dict[str, str],
                         on_speaker: Callable[[str], None] | None = None) -> str:
    """五个岗位在同一个共享讨论串里依次发言，后面的人能看到前面所有人的话。

    这就是"群聊式"拓扑——也是谄媚级联最容易出现的地方：后发言的人倾向于附和多数的意见。
    """
    thread = ""
    reference = (_reference(ledger) + "\n# 人物卡（作者资料）\n"
                 + "\n\n".join(f"## {name}\n{text}" for name, text in bibles.items()))
    for name, duty in GROUP_ROLES:
        if on_speaker:
            on_speaker(name)
        system = _card("groupchat") + f"\n\n你的岗位：{name}。职责：{duty}。"
        user = (
            f"# 作者的要求\n{request}\n\n"
            + reference + "\n"
            "# 讨论串（按时间顺序，你只能看到这些）\n"
            + (thread if thread.strip() else "（还没有人发言，你先说。）")
            + f"\n\n现在轮到你（{name}）发言。"
        )
        reply = _run(cfg, rec, f"群聊_{name}", system, user,
                     f"[mock] {name}：同意前面的方向，我只补充一点——……")
        thread += f"\n【{name}】{reply}\n"
    return thread.strip()


# ---------------- 规则守门（群聊守卫版用）----------------

def gatekeeper_review(cfg: Config, rec: Recorder, thread: str, ledger: Ledger,
                       request: str) -> dict:
    """有否决权的规则守门人：在成稿前把违反设定/机制的创意筛掉。

    群聊的问题不是附和，是"跑偏"——团队会发明账本里没有的设定，而没人拦。
    这一步就是给它加一道否决闸。
    """
    system = _card("gatekeeper")
    user = (
        f"# 创作要求\n{request}\n\n"
        "# 创作团队的讨论串\n" + thread + "\n\n"
        + _reference(ledger)
    )
    result = _object(_run(cfg, rec, "规则守门", system, user,
        json.dumps({"status": "pass", "issues": [], "questions": [],
                    "approved": _MOCK_BRIEF}, ensure_ascii=False)))
    check = validate_check({k: result.get(k) for k in ("status", "issues", "questions")})
    approved = result.get("approved")
    if not isinstance(approved, str) or (check["status"] == "pass" and not approved.strip()):
        raise ValueError("守门输出缺少可用的 approved 文本")
    return {**check, "approved": approved}


def _object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
        if not match:
            raise ValueError("结构化输出不是完整 JSON")
        text = match.group(1)
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("结构化输出无法解析为 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("结构化输出必须是 JSON 对象")
    return value


def validate_check(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("status") not in {"pass", "revise", "needs_input"}:
        raise ValueError("审查状态缺失或非法")
    issues, questions = value.get("issues"), value.get("questions")
    if not isinstance(issues, list) or not isinstance(questions, list):
        raise ValueError("审查必须包含 issues 和 questions 数组")
    for issue in issues:
        if (not isinstance(issue, dict) or issue.get("kind") not in {"rule", "missing", "style"}
                or not isinstance(issue.get("message"), str) or not issue["message"].strip()):
            raise ValueError("审查问题格式非法")
    if any(not isinstance(q, str) or not q.strip() for q in questions):
        raise ValueError("审查问题必须是非空文字")
    if value["status"] == "pass" and (questions or any(i["kind"] in {"rule", "missing"} for i in issues)):
        raise ValueError("审查同时声称通过和存在硬伤/缺失信息")
    return {"status": value["status"], "issues": issues, "questions": questions}


_CHAR_FIELDS = {"knows", "believes", "misjudges", "relations", "goal", "mechanism_awareness",
                "alive", "can_act", "awake"}
_PROPOSAL_FIELDS = {"summary", "world_date", "events", "character_updates",
                    "foreshadowing_updates", "player_seen_add"}


def validate_proposal(value: dict, ledger: Ledger) -> dict:
    if not isinstance(value, dict) or set(value) - _PROPOSAL_FIELDS:
        raise ValueError("提案含未知字段；存读删档必须另行确认完整状态")
    for key in ("events", "player_seen_add"):
        if not isinstance(value.get(key), list) or any(not isinstance(x, str) for x in value[key]):
            raise ValueError(f"提案 {key} 必须是文字数组")
    for key in ("summary", "world_date"):
        if key in value and not isinstance(value[key], str):
            raise ValueError(f"提案 {key} 必须是文字")
    changes = value.get("character_updates")
    if not isinstance(changes, dict):
        raise ValueError("character_updates 必须是对象")
    for name, fields in changes.items():
        if name not in ledger.data["characters"] or not isinstance(fields, dict) or set(fields) - _CHAR_FIELDS:
            raise ValueError(f"非法角色状态更新：{name}（不能更新 slots 或创建角色）")
        for field, item in fields.items():
            if field in {"knows", "believes", "misjudges"}:
                good = isinstance(item, list) and all(isinstance(x, str) for x in item)
            elif field == "relations":
                good = isinstance(item, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in item.items())
            elif field in {"alive", "can_act", "awake"}:
                good = isinstance(item, bool)
            else:
                good = isinstance(item, str)
            if not good:
                raise ValueError(f"角色 {name} 的 {field} 类型非法")
    seeds = value.get("foreshadowing_updates")
    if not isinstance(seeds, list):
        raise ValueError("foreshadowing_updates 必须是数组")
    for seed in seeds:
        if (not isinstance(seed, dict) or set(seed) != {"id", "desc", "status"}
                or not all(isinstance(seed[k], str) and seed[k].strip() for k in seed)):
            raise ValueError("伏笔更新需要 id、desc、status 三个文字字段")
    return value


def check_scene(cfg: Config, rec: Recorder, scene: str, ledger: Ledger, request: str,
                proposal: dict | None = None) -> dict:
    if proposal is not None:
        validate_proposal(proposal, ledger)
    user = (f"# 作者要求\n{request}\n\n# 待审稿\n{scene}\n\n"
            + _reference(ledger) + "\n# 同时待审的状态提案\n"
            + json.dumps(proposal, ensure_ascii=False, indent=2))
    return validate_check(_object(_run(cfg, rec, "规则审查", _card("checker"), user,
                         '{"status":"pass","issues":[],"questions":[]}')))


def revise_scene(cfg: Config, rec: Recorder, scene: str, feedback: dict | str,
                 ledger: Ledger, request: str) -> str:
    notes = feedback if isinstance(feedback, str) else json.dumps(feedback, ensure_ascii=False)
    user = (f"# 作者要求\n{request}\n\n# 原稿\n{scene}\n\n# 修改意见\n{notes}\n\n"
            + _reference(ledger))
    return _run(cfg, rec, "定向修改", _card("revise"), user, scene)


def propose_changes(cfg: Config, rec: Recorder, scene: str, ledger: Ledger, request: str) -> dict:
    user = f"# 作者要求\n{request}\n\n# 场景稿\n{scene}\n\n" + _reference(ledger)
    empty = {"events": [], "character_updates": {}, "foreshadowing_updates": [], "player_seen_add": []}
    return validate_proposal(_object(_run(cfg, rec, "状态提议", _card("proposal"), user,
                                         json.dumps(empty))), ledger)


# ---------------- mock 输出（--mock 时用）----------------
# 每个岗位给一段有代表性的假输出，让不花钱也能看出流程长什么样。

_MOCK_BRIEF = """本场目标：第一幕双线夜戏，让"陌生档"第一次出现。

核心事件：
- 主线：主角回到住处，打开存取档界面，发现三槽里有一个自己不记得存过的档，备注是自己的字迹，写着「别相信他」。
- 副线：同一时刻，首领在另一处复盘码头对峙，怀疑主角提前知道了他要说的话。

本场结束时世界状态：主角仍未确认"别人也能读档"，但已经出现第一处陌生档疑云；首领对主角的怀疑升级但无证据。

出场角色：主角、首领"""

_MOCK_SNAPSHOT = """场前状态快照（版本 v2 · 第三天·夜）

主角：存活，可操作。知道自己能存取档、每人三槽、开界面时世界会停住；知道首领在码头说过一件"还没发生"的事。相信自己是唯一读档者，把首领的预知归因于巧合或泄密。与首领关系：警惕。三槽：①进城前存的（v1）②空 ③空。当前目标：弄清首领为什么能提前知道未来。

首领：存活，可操作。知道主角在码头的反应"像提前防了一手"。相信主角可能也是读档者但证据不足，更倾向是有人泄密。与主角关系：试探中。三槽：①对峙前（v2）②空 ③空。当前目标：确认主角知道多少，同时保住退路。

连续性约束：
- 主角三槽里的第三个档，在本场开始前是空的；"陌生档"是本场事件，不是场前事实。
- 两人都不知道对方也能读档，本场不得出现任何一方的"读档感应"。

矛盾：无。"""

_MOCK_CHAR = """本场动机（{name}）：{name}此刻想弄清楚眼前这件事到底能不能用已有认知解释。

关键行动：{name}按自己的性格推进本场——先观察，再决定要不要动槽位。

对白要点：{name}的话不多，越是不确定越不肯把话说满。

存档动作：本场{name}不主动存、也不读档——在没弄清状况前动槽位，代价太大。"""

_MOCK_SCENE = """# 第一幕 · 陌生档

（主角回到住处。灯没开，他把门反锁，靠在门上听了几秒走廊的动静。）

主角（低声）：……他怎么会知道。

（他坐下，指尖悬在桌面之上，像是在犹豫要不要叫出那个界面。最后他叫了。世界停住。三个槽位浮在眼前。）

主角：第一个，进城前存的。第二个，空的。

（他的目光停在第三个上。）

主角：……第三个？

（备注栏里有一行字。是他自己的字迹。）

> 别相信他。

主角（很久没有动）：……我没存过这个。"""

_MOCK_REVIEW = """审读意见：

1. 问题：主角看到陌生档后的反应略显平静。
   位置：「主角（很久没有动）：……我没存过这个。」
   建议：他是"记忆即自我"的人设，发现自己有个不记得存过的档，应该有一瞬的失控——哪怕只是一个动作细节。

2. 问题：首领副线在本场完全没落地。
   位置：全场
   建议：本场目标写的是双线，但稿子只写了主角线。要么补一小段首领侧写，要么把目标改成单线。

3. 玩家可读性：没有发现问题。陌生档与备注的悬念对玩家是可见的。"""

_MOCK_GATEKEEPER = """批准清单：

**批准**
1. 主线：主角回住处，打开界面，发现不记得存过的档，备注是自己字迹「别相信他」。
2. 副线：首领复盘码头对峙，怀疑主角提前知情。
3. 主角本场不读该档，另存当前状态到空槽（符合他"重记忆、怕自我消失"的习惯）。

**否决**
1. 讨论里"界面浮现同步源提示"——账本里界面不提供任何来源提示，属于系统递答案，否决。
2. 讨论里"两个写入时间/覆盖中状态"——违反三槽机制（存档无预览、无冲突状态），否决。

**遗漏**
1. 首领本场应有存档动作（按他惜档的性格，会留退路），讨论未提到，需补。"""

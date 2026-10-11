"""对比两种状态注入法：trailing（每轮结尾追加尾块）vs system（写进系统提示词）。

同一场戏、同一批角色、同一温度，只改状态怎么交给角色。看两件事：

  缓存命中 —— 直接读 provider 报的 cached_tokens。trailing 下历史只追加，
              前缀应当被复用；system 下系统提示词每轮改写，前缀从第一条就断。
  最终效果 —— 跨族评审横向打分（默认 longcat 评 stepfun 的产出），
              并把盲区表一起交给评审，让它按事件编号判"有没有未卜先知"。

用法：
  uv run python -m agentteam.theater.compare_injection --turns 3 --temperature 0
  uv run python -m agentteam.theater.compare_injection --beats data/beats_双线夜戏.json \\
      --provider stepfun --judge-provider longcat
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .. import agents
from ..config import PROMPTS_DIR, RUNS_DIR, load_config
from ..main import DEFAULT_REQUEST, load_bibles
from ..recorder import Recorder, cached_tokens
from ..rules import load_rules
from .director import beats_for, run_scene
from .world import World

INJECTIONS = ("trailing", "system")
LABELS = {"trailing": "每轮结尾追加尾块（保前缀）", "system": "写进系统提示词（每轮改写）"}


def run_one(injection: str, cfg, request: str, rules: str, bibles: dict, beats: list[dict],
            cast: list[str], world_date: str, out_dir: Path, observe: bool = True) -> dict:
    world = World(world_date, cast)
    rec = Recorder(out_dir)
    result = run_scene(cfg, rec, world, request=request, rules=rules, bibles=bibles,
                       beats=beats, observe=observe, injection=injection)
    rec.write_text("prose.md", result["prose"])
    rec.write_json("result.json", result)
    stats = per_call(out_dir)
    stats["prose"] = result["prose"]
    stats["issues"] = result["issues"]
    stats["observer"] = result["observer_reports"]
    stats["belief"] = result["belief"]
    stats["state"] = result["state"]
    return stats


def per_call(run_dir: Path) -> dict:
    """从落盘的每次调用里按岗位汇总 token 与缓存命中。"""
    groups: dict[str, dict] = {}
    for path in sorted(run_dir.glob("*.json")):
        if path.name in {"result.json", "inputs.json"}:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        role = payload.get("role", "?")
        bucket = ("演员" if role.startswith("演员_") else
                  "观察者" if role == "观察者" else
                  "写手" if role == "写手" else "其它")
        usage = payload.get("usage") or {}
        g = groups.setdefault(bucket, {"calls": 0, "prompt": 0, "cached": 0, "output": 0, "elapsed": 0.0})
        g["calls"] += 1
        g["prompt"] += usage.get("prompt_tokens", 0) or 0
        g["output"] += usage.get("completion_tokens", 0) or 0
        g["cached"] += cached_tokens(usage)
        g["elapsed"] += payload.get("elapsed_sec", 0) or 0
    total = {"calls": 0, "prompt": 0, "cached": 0, "output": 0, "elapsed": 0.0}
    for g in groups.values():
        for k in total:
            total[k] += g[k]
    for bucket in list(groups.values()) + [total]:
        prompt = bucket["prompt"]
        bucket["hit"] = round(bucket["cached"] / prompt * 100, 1) if prompt else 0.0
    return {"groups": groups, "total": total}


def judge(cfg, rec, request: str, context: dict, items: list[dict]) -> str:
    return agents.judge_scenes(cfg, rec, request, items, context=context)


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="对比两种状态注入法")
    ap.add_argument("--provider", default=None, help="写作模型（默认读 .env）")
    ap.add_argument("--reasoning", default=None)
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--cast", nargs="+", default=["主角", "首领"])
    ap.add_argument("--world-date", default="第三天·夜")
    ap.add_argument("--beats", default=None, help="场景节拍 JSON；给了就以它为准")
    ap.add_argument("--request", default=None)
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="默认 0，减少采样噪声；两稿必须同温才可比")
    ap.add_argument("--judge", action="store_true", default=True)
    ap.add_argument("--no-judge", dest="judge", action="store_false")
    ap.add_argument("--judge-provider", default="longcat", help="跨族评审（默认 longcat）")
    ap.add_argument("--observe", dest="observe", action="store_true", default=False,
                    help="打开语义层观察者。默认关闭：观察者每轮另发一次请求、且可能暂停整场，"
                         "会让两种方法的轮数不一致，污染缓存对比")
    ap.add_argument("--order", default="trailing,system",
                    help="两种方法的先后顺序。**必须两个方向各跑一次**："
                         "两者的系统提示词大部分相同，后跑的那一种会继承先跑者的暖缓存。")
    args = ap.parse_args()

    cfg = load_config(mock=False, provider=args.provider, reasoning=args.reasoning)
    cfg = replace(cfg, temperature=args.temperature)
    request = args.request or DEFAULT_REQUEST
    rules, bibles = load_rules(), load_bibles()
    cast = list(args.cast)
    world_date = args.world_date
    if args.beats:
        beats = json.loads(Path(args.beats).read_text(encoding="utf-8"))
        for beat in beats:
            beat.setdefault("cast", cast)
    else:
        beats = beats_for(World(world_date, cast), args.turns, cast, world_date)

    root = RUNS_DIR / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_injection")
    print(f"模型：{cfg.describe()} · 温度 {cfg.temperature}")
    print(f"角色：{'、'.join(cast)} · {len(beats)} 轮 · 要求：{request[:40]}…\n")

    order = [m.strip() for m in args.order.split(",") if m.strip()]
    if sorted(order) != sorted(INJECTIONS):
        raise SystemExit(f"--order 必须正好包含 {INJECTIONS}")
    results = {}
    for injection in order:
        print(f"▶ {LABELS[injection]}")
        try:
            results[injection] = run_one(injection, cfg, request, rules, bibles, beats,
                                        cast, world_date, root / injection, observe=args.observe)
        except Exception as exc:  # noqa: BLE001 — 一种方法失败不该让对比整个作废
            print(f"  失败：{type(exc).__name__}: {exc}\n")
            results[injection] = {"total": {"calls": 0, "prompt": 0, "cached": 0, "output": 0,
                                            "elapsed": 0.0, "hit": 0.0},
                                  "groups": {}, "prose": f"（{LABELS[injection]} 运行失败：{exc}）",
                                  "issues": [], "observer": [], "belief": None,
                                  "state": None, "error": str(exc)}
            continue
        t = results[injection]["total"]
        print(f"  完成：{t['calls']} 次调用 · 输入 {t['prompt']} tok"
              f"（缓存命中 {t['cached']}，{t['hit']}%） · 输出 {t['output']} tok"
              f" · 耗时 {t['elapsed']:.0f}s\n")

    # ---- 评审（跨族，只看效果）----
    judge_text = ""
    if args.judge:
        # 评审的输入最大（规则 + 日志 + 盲区表 + 两篇正文）。注意单次尝试的上限是
        # request_timeout（默认 90s），**不是** request_deadline——两个都要放宽，
        # 否则调大 deadline 也没用（实测 longcat 评这一次超过 90s）。
        jcfg = replace(load_config(mock=False, provider=args.judge_provider),
                       temperature=args.temperature,
                       request_timeout=300.0, request_deadline=900.0)
        print(f"▶ 评审：{jcfg.describe()}（不知道哪份是哪种注入法）")
        # 评审要的"事实"从结果里取（belief 就是客观日志 + 谁看得见哪些事件），
        # 不依赖活的 World 对象，某一种方法失败也不会让评审跟着崩。
        ref = next((r for r in results.values() if r.get("belief")), None)
        context = {
            "rules": rules,
            "ledger": {"objective": ref["belief"]["objective"] if ref else [],
                       "blind_spots": ref["belief"]["blind_spots"] if ref else {},
                       "state": ref["state"] if ref else {}},
            "bibles": bibles,
            "judge_prompt": (PROMPTS_DIR / "theater_judge.md").read_text(encoding="utf-8").strip(),
        }
        items = [{"mode": m, "scene": results[m]["prose"]} for m in order]
        jrec = Recorder(root / "_judge")
        try:
            judge_text = judge(jcfg, jrec, request, context, items)
        except Exception as exc:  # noqa: BLE001 — 评审失败不该让数据对比作废
            judge_text = f"（评审失败：{type(exc).__name__}: {exc}）"
        print("  完成\n")

    # ---- 报告 ----
    lines = [
        f"# 状态注入法对比 {root.name}", "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 模型：{cfg.describe()} · 温度 {cfg.temperature}",
        f"- 角色：{'、'.join(cast)} · 轮数：{len(beats)}",
        f"- 要求：{request}",
        f"- 运行顺序：{' → '.join(LABELS[m] for m in order)}（顺序会影响缓存，两个方向都要跑）", "",
        "> 两稿唯一的差别是「当前状态」怎么交给角色；其余全部相同。",
        "> 缓存命中取自 provider 报的 `cached_tokens`；0 表示未命中（不是未报告）。", "",
        "## 数据对照", "",
        "| 注入法 | 调用 | 输入tok | 缓存命中tok | 命中率 | 输出tok | 耗时(s) | 正文(字) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for m in order:
        t = results[m]["total"]
        lines.append(f"| {LABELS[m]} | {t['calls']} | {t['prompt']} | {t['cached']} | "
                     f"{t['hit']}% | {t['output']} | {t['elapsed']:.0f} | {len(results[m]['prose'])} |")
    lines += ["", "## 分岗位明细", ""]
    for m in order:
        lines += [f"### {LABELS[m]}", "",
                  "| 岗位 | 调用 | 输入tok | 缓存命中tok | 命中率 | 输出tok |", "| --- | --- | --- | --- | --- | --- |"]
        for name, g in sorted(results[m]["groups"].items()):
            lines.append(f"| {name} | {g['calls']} | {g['prompt']} | {g['cached']} | "
                         f"{g['hit']}% | {g['output']} |")
        lines.append("")
    for m in order:
        lines += [f"## 正文 · {LABELS[m]}", "", results[m]["prose"], ""]
        issues = results[m]["issues"]
        lines += [f"（结构层问题 {len(issues)} 条："
                  + ("、".join(f"{i.get('kind')}/{i.get('actor','')}" for i in issues) or "无") + "）", ""]
    if judge_text:
        lines += ["---", "", "# 评审意见（跨族，匿名）", "", judge_text, ""]
        lines += ["匿名映射：" + "；".join(f"{chr(65 + i)}={LABELS[m]}" for i, m in enumerate(order)), ""]

    report = root / "compare_injection.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告：{report}")


if __name__ == "__main__":
    main()

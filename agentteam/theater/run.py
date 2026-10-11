"""跑一场角色剧场（命令行版）。

用法：
  uv run python -m agentteam.theater.run --mock                 # 假模型，不花钱，走完存读档
  uv run python -m agentteam.theater.run --turns 6              # 用 .env 的默认 provider
  uv run python -m agentteam.theater.run --provider longcat --turns 4
  uv run python -m agentteam.theater.run --cast 主角 首领 --turns 5

产物落在 runs/<时间戳>_theater/：正文、客观日志、信念差分、每个角色的完整上下文。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..config import RUNS_DIR, load_config
from ..main import DEFAULT_REQUEST, load_bibles
from ..recorder import Recorder
from ..rules import load_rules
from .director import beats_for, run_scene
from .world import World


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="角色剧场：每个角色一个只追加的扮演循环")
    ap.add_argument("--mock", action="store_true", help="假模型，不联网不花钱")
    ap.add_argument("--provider", default=None, help="stepfun（默认）或 longcat")
    ap.add_argument("--reasoning", default=None, help="推理档位 disabled/low/medium/high")
    ap.add_argument("--turns", type=int, default=4, help="跑几轮")
    ap.add_argument("--cast", nargs="+", default=["主角", "首领"], help="出场角色")
    ap.add_argument("--world-date", default="第三天·夜", help="起始世界日期")
    ap.add_argument("--beats", default=None,
                    help="场景节拍 JSON 文件：每项 {world_date, stage, cast}；"
                         "stage 写成 {角色: 文字} 就是双线（各人只看到自己那一段）")
    ap.add_argument("--request", default=None, help="本场目标（不填用内置示例）")
    ap.add_argument("--no-observer", action="store_true", help="不跑语义层观察者")
    ap.add_argument("--injection", default="trailing", choices=["trailing", "system"],
                    help="状态注入法：trailing=每轮结尾追加尾块（保前缀缓存）；"
                         "system=写进系统提示词（每轮改写，前缀必断）")
    ap.add_argument("--temperature", type=float, default=None,
                    help="覆盖 .env 的温度；对比实验建议用 0 减少采样噪声")
    args = ap.parse_args()

    cfg = load_config(mock=args.mock, provider=args.provider, reasoning=args.reasoning)
    if args.temperature is not None:
        cfg = replace(cfg, temperature=args.temperature)
    request = args.request or DEFAULT_REQUEST
    rules = load_rules()
    bibles = load_bibles()
    world = World(args.world_date, args.cast)
    if args.beats:
        beats = json.loads(Path(args.beats).read_text(encoding="utf-8"))
        if not isinstance(beats, list) or not beats:
            raise SystemExit("beats 文件必须是一个非空数组")
        for beat in beats:
            beat.setdefault("cast", args.cast)
    else:
        beats = beats_for(world, args.turns, args.cast, args.world_date)

    run_dir = RUNS_DIR / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_theater")
    rec = Recorder(run_dir)

    print(f"模型：{cfg.describe()}")
    print(f"角色：{'、'.join(args.cast)} · {args.turns} 轮 · 起始 {args.world_date}")
    print(f"状态注入：{args.injection} · 温度 {cfg.temperature}")
    print(f"目标：{request[:60]}…\n")

    def on_event(kind: str, payload=None) -> None:
        if kind == "turn":
            print(f"▶ 第{payload['index']}轮 · {payload['world_date']} · 在场：{'、'.join(payload['cast'])}")
        elif kind == "actor":
            print(f"    · {payload['actor']}：{payload['action'][:60]}")
            if payload["interface"] and payload["interface"] != "无":
                print(f"      【界面】{payload['interface']}")
        elif kind == "op":
            mark = "✓" if payload.get("ok") else "✗"
            print(f"      {mark} {payload['op']} → {payload['detail']}")
        elif kind == "rewind":
            print("      ⟲ 世界回退：所有角色上下文按快照重建，读档者保留记忆")
        elif kind == "turn_ended":
            print(f"      — {payload['reason']}")
        elif kind == "leak":
            print(f"      ⚠ 结构层发现泄漏：{payload['hits']}")
        elif kind == "observer":
            print(f"    观察者：{payload['status']} —— {payload['reason'][:80]}")
        elif kind == "pause":
            print(f"    ⏸ 暂停：{payload['reason']}")
        elif kind == "skip":
            print(f"    · {payload['actor']} 本轮不动（{payload['reason']}）")

    result = run_scene(cfg, rec, world, request=request, rules=rules, bibles=bibles,
                       beats=beats, observe=not args.no_observer,
                       injection=args.injection, on_event=on_event)

    # ---- 落盘 ----
    rec.write_json("inputs.json", {"request": request, "cast": args.cast, "beats": beats,
                                   "rules": rules, "bibles": bibles})
    rec.write_json("result.json", result)
    rec.write_text("prose.md", result["prose"])
    rec.write_json("log.json", [{"turn": e.turn, "kind": e.kind, "actor": e.actor,
                                 "public": e.public, "private": e.private,
                                 "world_date": e.world_date, "present": list(e.present),
                                 "to": e.to} for e in world.path_events()])
    rec.write_json("belief.json", result["belief"])
    rec.write_json("graph.json", result["graph"])
    rec.write_text("contexts.md", _contexts_md(world))
    rec.write_summary([
        f"- 模型：{cfg.describe()}",
        f"- 角色：{'、'.join(args.cast)}",
        f"- 轮次：{result['played']}（当前指针 {result['live']['branch']}·第{result['live']['turn']}轮）",
        f"- 暂停：{result['paused'] or '（未暂停）'}",
        f"- 结构层问题：{len(result['issues'])}",
        f"- 界面操作：{len(result['interface_ops'])}",
        f"- 调用次数：{rec.seq}",
        f"- 状态注入：{args.injection}",
        f"- 输入 token：{rec.usage_total['prompt_tokens']}（其中缓存命中 {rec.usage_total['cached_tokens']}）",
        f"- 输出 token：{rec.usage_total['completion_tokens']}",
        "",
        "## 正文", result["prose"], "",
        "## 客观日志", "```json", json.dumps(result["belief"]["objective"], ensure_ascii=False, indent=2), "```", "",
        "## 信念与事实的差分", "```json", json.dumps(result["belief"]["blind_spots"], ensure_ascii=False, indent=2), "```", "",
        "## 观察者报告", *[f"- {r['status']}：{r['reason']}" for r in result["observer_reports"]],
    ])

    u = rec.usage_total
    hit = (u["cached_tokens"] / u["prompt_tokens"] * 100) if u["prompt_tokens"] else 0
    print(f"\n完成：{result['played']} 轮 · {rec.seq} 次调用 · 暂停={result['paused'] or '无'}"
          f" · 当前指针 {result['live']['branch']}·第{result['live']['turn']}轮")
    print(f"     输入 {u['prompt_tokens']} tok（缓存命中 {u['cached_tokens']}，{hit:.0f}%）"
          f" · 输出 {u['completion_tokens']} tok")
    if result["issues"]:
        print(f"结构层问题 {len(result['issues'])} 条：")
        for issue in result["issues"]:
            print(f"  - {issue}")
    print(f"\n产物在：{run_dir}")
    print("  - prose.md       正文（写手渲染）")
    print("  - log.json       客观事件日志（事实）")
    print("  - belief.json    谁看得见哪些事件（信念 vs 事实）")
    print("  - contexts.md    每个角色的完整上下文（隔离是否真的成立，看这个）")


def _contexts_md(world: World) -> str:
    lines = ["# 各角色的完整上下文", "",
             "同一轮里，不同角色的上下文应该明显不同——相同就说明隔离漏了。", ""]
    for name in world.characters:
        lines += [f"## {name}", ""]
        for block in world.turn_blocks(name):
            lines.append(f"### 第{block['turn']}轮 · {block['world_date']}")
            for event in block["events"]:
                lines.append(f"- `{event['kind']}` {event['text']}")
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()

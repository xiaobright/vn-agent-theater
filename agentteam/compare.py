"""对比实验：同一场戏，跑多种协作模式，出一份对照报告。

用法：
  uv run python -m agentteam.compare --modes team single
  uv run python -m agentteam.compare --modes team team_fullctx single groupchat
  uv run python -m agentteam.compare --modes team single --mock      # 不花钱，先验证流程
  uv run python -m agentteam.compare --modes team single --judge     # 让模型当评审横向打分

产物：runs/<时间戳>_compare/
  compare.md        对照报告（表格 + 各稿全文 + 评审意见）
  <mode>/scene.md   各模式的稿件
  <mode>/NN_*.json  各模式每次调用的完整输入输出
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime

from . import agents, pipeline
from .config import DATA_DIR, RUNS_DIR, load_config
from .ledger import Ledger
from .main import DEFAULT_REQUEST, load_bibles
from .pipeline import MODE_LABELS, MODES
from .recorder import Recorder


def run_one(mode: str, cfg, request: str, ledger, bibles, out_dir) -> dict:
    rec = Recorder(out_dir)
    stages_box: dict = {"stages": []}

    def on_event(kind: str, payload=None) -> None:
        if kind == "stages":
            stages_box["stages"] = payload
        elif kind == "stage":
            i, status, _ = payload
            if status == "active" and i < len(stages_box["stages"]):
                print(f"      · {stages_box['stages'][i].label} …")
        elif kind == "char_start":
            print(f"        - {payload} 分析中")
        elif kind == "speaker":
            print(f"        - {payload} 发言")

    t0 = time.time()
    result = pipeline.run(mode, cfg, rec, request, ledger, bibles, on_event=on_event,
                          closed_loop=False)
    result["elapsed"] = time.time() - t0

    scene = result.get("scene") or ""
    review = result.get("review") or ""
    rec.write_text("scene.md", scene)
    rec.write_text("review.md", review)
    rec.write_text("brief.md", result.get("brief") or "")
    rec.write_text("snapshot.md", result.get("snapshot") or "")
    rec.write_text("meta.json", json.dumps(
        {"mode": mode, "calls": result["calls"], "elapsed": round(result["elapsed"], 1),
         "usage": result["usage"], "scene_chars": len(scene)},
        ensure_ascii=False, indent=2))
    return result


def new_run_dir(parent, suffix: str):
    path = parent / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6] + suffix)
    path.mkdir(parents=True, exist_ok=False)
    return path


def judge_bundle(request: str, context: dict, items: list[dict]) -> dict:
    """Persist exact ordered text and references; mode mapping stays outside model input."""
    return {"schema_version": 1, "request": request, "context": context,
            "items": [{"label": chr(65 + i), "mode": item["mode"], "scene": item["scene"]}
                      for i, item in enumerate(items)]}


def main() -> None:
    # 重定向到文件时 Python 默认块缓冲，导致看不到实时进度；改成行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="多模式对比实验")
    ap.add_argument("--modes", nargs="+", default=["team", "single"],
                    help=f"要跑的模式，可选：{' '.join(MODES)}")
    ap.add_argument("--request", default=None, help="本场创作要求")
    ap.add_argument("--provider", default=None, help="stepfun（默认）或 longcat")
    ap.add_argument("--reasoning", default=None, help="推理档位 disabled/low/medium/high")
    ap.add_argument("--mock", action="store_true", help="假模型，不联网不花钱")
    ap.add_argument("--judge", action="store_true", help="额外让模型当评审打分")
    ap.add_argument("--judge-provider", default="longcat",
                    help="评审用的 provider（默认 longcat，跨族避免同族自评）")
    ap.add_argument("--judge-reasoning", default=None, help="评审的推理档位")
    args = ap.parse_args()

    modes = list(dict.fromkeys(m for m in args.modes if m in MODES))
    bad = [m for m in args.modes if m not in MODES]
    if bad:
        print(f"忽略未知模式：{bad}")
    if not modes:
        print(f"没有可跑的模式。可选：{MODES}")
        return

    cfg = load_config(mock=args.mock, provider=args.provider, reasoning=args.reasoning)
    request = args.request or DEFAULT_REQUEST
    ledger = Ledger(DATA_DIR / "ledger.json")
    bibles = load_bibles()

    root = new_run_dir(RUNS_DIR, "_compare")
    context = agents.evaluation_context(ledger, bibles)

    print(f"模型：{cfg.describe()}")
    print(f"要求：{request[:60]}…")
    print(f"模式：{modes}\n")

    results: dict[str, dict] = {}
    for mode in modes:
        print(f"▶ [{mode}] {MODE_LABELS[mode]}")
        try:
            results[mode] = run_one(mode, cfg, request, ledger, bibles, root / mode)
            r = results[mode]
            print(f"  完成：{r['calls']} 次调用 · {r['elapsed']:.0f}s · "
                  f"{len(r.get('scene') or '')} 字\n")
        except Exception as e:  # noqa: BLE001
            print(f"  失败：{e}\n")
            results[mode] = {"mode": mode, "error": str(e), "scene": "",
                             "calls": 0, "elapsed": 0.0, "usage": {}}

    # ---- 评审 ----
    judge_text = ""
    ok = [m for m in modes if results[m].get("scene")]
    items = [{"mode": m, "scene": results[m]["scene"]} for m in ok]
    bundle = judge_bundle(request, context, items)
    (root / "judge_input.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.judge and ok:
        judge_cfg = load_config(mock=args.mock, provider=args.judge_provider,
                                reasoning=args.judge_reasoning)
        print(f"▶ 评审：横向打分（评审模型 {judge_cfg.describe()}）…")
        rec = Recorder(root / "_judge")
        judge_text = agents.judge_scenes(judge_cfg, rec, request, items, context=context)
        (root / "judge.md").write_text(judge_text, encoding="utf-8")
        print("  完成\n")

    # ---- 报告 ----
    lines = [
        f"# 多模式对比实验 {root.name}",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 模型：{cfg.describe()}",
        f"- 要求：{request}",
        "- 评审输入：judge_input.json（冻结规则、人物、账本、提示词、稿件和顺序；模型只见字母标签）",
        "- 匿名映射：" + "；".join(f"{item['label']}={item['mode']}" for item in bundle["items"]),
        "",
        "## 数据对照",
        "",
        "| 模式 | 调用次数 | 耗时(s) | 输入token | 输出token | 稿件字数 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for m in modes:
        r = results[m]
        u = r.get("usage") or {}
        lines.append(
            f"| {MODE_LABELS[m]} | {r.get('calls', 0)} | {r.get('elapsed', 0):.0f} | "
            f"{u.get('prompt_tokens', 0)} | {u.get('completion_tokens', 0)} | "
            f"{len(r.get('scene') or '')} |"
        )
    lines.append("")
    for m in modes:
        r = results[m]
        lines.append(f"## {MODE_LABELS[m]}")
        lines.append("")
        if r.get("error"):
            lines.append(f"**失败**：{r['error']}")
        else:
            lines.append(r.get("scene") or "（空）")
            if r.get("review"):
                lines += ["", "### 该模式的审读意见", "", r["review"]]
        lines.append("")
    if judge_text:
        lines += ["---", "", "# 评审意见", "", judge_text, ""]

    (root / "compare.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"报告：{root / 'compare.md'}")


if __name__ == "__main__":
    main()

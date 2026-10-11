"""入口（命令行版）：跑一段剧情。

用法：
  uv run python -m agentteam.main --mock            # 假模型，验证流程
  uv run python -m agentteam.main                   # 用 .env 里的默认 provider（stepfun）
  uv run python -m agentteam.main --provider longcat # 换 longcat
  uv run python -m agentteam.main --request "……"     # 自定义本场要求

想看网页版（时间线跳跃 + 实时进度），用：
  uv run uvicorn agentteam.server:app --port 8765
"""
from __future__ import annotations

import argparse
from datetime import datetime

from . import agents
from .config import DATA_DIR, RUNS_DIR, load_config
from .ledger import Ledger
from .recorder import Recorder

DEFAULT_REQUEST = (
    "写第一幕的一场戏，双线：主线是第三天夜里主角回到住处、打开存取档界面，"
    "发现三槽里有一个自己不记得存过的档、备注是自己的字迹写着「别相信他」；"
    "副线是同一时刻首领在自己那边复盘码头对峙，怀疑主角提前知道了他要说什么。"
)


def load_bibles() -> dict[str, str]:
    bibles = {}
    for p in (DATA_DIR / "bible").glob("*.md"):
        bibles[p.stem] = p.read_text(encoding="utf-8")
    return bibles


def main() -> None:
    ap = argparse.ArgumentParser(description="《全员存取档》智能体团队实验台")
    ap.add_argument("--mock", action="store_true", help="用假模型跑，不联网、不花钱")
    ap.add_argument("--provider", default=None, help="stepfun（默认）或 longcat")
    ap.add_argument("--reasoning", default=None,
                    help="推理档位 disabled/low/medium/high（默认读 .env，现为 medium）")
    ap.add_argument("--request", default=None, help="本场创作要求（不填用内置示例）")
    args = ap.parse_args()

    cfg = load_config(mock=args.mock, provider=args.provider, reasoning=args.reasoning)
    request = args.request or DEFAULT_REQUEST

    run_dir = RUNS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    rec = Recorder(run_dir)
    ledger = Ledger(DATA_DIR / "ledger.json")
    bibles = load_bibles()

    print(f"模型：{cfg.describe()}")
    print(f"本场要求：{request}\n")

    print("[1/5] 总编：把要求变成本场目标 …")
    brief = agents.supervisor_brief(cfg, rec, request, ledger)

    print("[2/5] 时间线主管：生成场前状态快照 …")
    snapshot = agents.timeline_snapshot(cfg, rec, ledger)

    known = list(ledger.data["characters"].keys())
    cast = agents.parse_cast(brief, known)
    print(f"[3/5] 角色分析（并发扇出）：{'、'.join(cast)} …")
    char_results = agents.analyze_characters(cfg, rec, ledger, bibles, cast, brief)

    print("[4/5] 主笔：写完整场景稿 …")
    scene = agents.write_scene(cfg, rec, brief, snapshot, char_results)

    print("[5/5] 审读（独立上下文）：找具体问题 …")
    review = agents.review_scene(cfg, rec, scene, ledger)

    # ---- 落盘 ----
    rec.write_text("brief.md", brief)
    rec.write_text("snapshot.md", snapshot)
    rec.write_text("scene.md", scene)
    rec.write_text("review.md", review)
    rec.write_summary([
        f"- 模型：{cfg.describe()}",
        f"- 出场角色：{'、'.join(cast)}",
        f"- 调用次数：{rec.seq}",
        "",
        "## 本场目标",
        brief,
        "",
        "## 各角色分析",
        *[f"### {r['character']}\n{r['text']}" for r in char_results],
        "",
        "## 场景稿",
        scene,
        "",
        "## 审读意见",
        review,
    ])

    print(f"\n完成。全部产物在：{run_dir}")
    print("  - scene.md       最终场景稿")
    print("  - _summary.md    本次运行总览")
    print("  - NN_*.json      每次 agent 调用的完整输入/输出")


if __name__ == "__main__":
    main()

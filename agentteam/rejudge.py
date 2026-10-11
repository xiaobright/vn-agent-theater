"""用另一个模型重新评审已有的稿件——把"评审模型"这个变量单独隔离出来。

同一批稿子、同一份评审提示词，只换评审模型，就能看出排序变化是稿子造成的
还是评审造成的。

用法：
  uv run python -m agentteam.rejudge runs/20261010_150247_compare --provider stepfun
  uv run python -m agentteam.rejudge runs/20261010_130032_compare --provider longcat
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import agents
from .config import RUNS_DIR, load_config
from .pipeline import MODE_LABELS
from .recorder import Recorder


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="换评审模型重新打分")
    ap.add_argument("run_dir", help="已有的实验目录，例如 runs/xxx_compare")
    ap.add_argument("--provider", required=True, help="评审用的 provider")
    ap.add_argument("--reasoning", default=None)
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    root = Path(args.run_dir)
    if not root.is_absolute():
        root = RUNS_DIR.parent / root
    if not root.exists():
        print(f"目录不存在：{root}")
        return

    request = ""
    cmp_md = root / "compare.md"
    if cmp_md.exists():  # 从原报告里抠出创作要求
        for line in cmp_md.read_text(encoding="utf-8").splitlines():
            if line.startswith("- 要求："):
                request = line.replace("- 要求：", "").strip()
                break

    items = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        scene = sub / "scene.md"
        if scene.exists() and scene.read_text(encoding="utf-8").strip():
            items.append({"mode": MODE_LABELS.get(sub.name, sub.name),
                          "scene": scene.read_text(encoding="utf-8")})
    if not items:
        print("没找到任何 scene.md")
        return

    cfg = load_config(mock=args.mock, provider=args.provider, reasoning=args.reasoning)
    print(f"评审模型：{cfg.describe()}")
    print(f"稿件：{[i['mode'] for i in items]}\n")

    rec = Recorder(root / f"_rejudge_{args.provider}")
    text = agents.judge_scenes(cfg, rec, request, items)
    out = root / f"judge_{args.provider}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n写入：{out}")


if __name__ == "__main__":
    main()

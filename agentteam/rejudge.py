"""用另一个模型重新评审已有的稿件——把"评审模型"这个变量单独隔离出来。

同一批稿子、同一份评审提示词、同一份冻结资料，只换评审模型，排序变化才能
归因给评审，而不是归因给"这次喂进去的规则不一样"。

优先复用实验目录里的 judge_input.json（compare.py 落盘的冻结输入）；没有它
才回退到当前账本 + 人物卡重建，并在报告里标明用的是哪一份。

用法：
  uv run python -m agentteam.rejudge runs/20261010_150247_compare --provider stepfun
  uv run python -m agentteam.rejudge runs/20261010_130032_compare --provider longcat
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import agents
from .compare import judge_bundle
from .config import DATA_DIR, RUNS_DIR, load_config
from .ledger import Ledger
from .main import load_bibles
from .pipeline import MODE_LABELS
from .recorder import Recorder


def load_frozen(root: Path) -> dict | None:
    """读 compare.py 落盘的冻结输入；缺失或版本不符就返回 None，由调用方回退。"""
    path = root / "judge_input.json"
    if not path.exists():
        return None
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if bundle.get("schema_version") != 1 or not bundle.get("items"):
        return None
    context = bundle.get("context")
    if not isinstance(context, dict) or not all(k in context for k in ("rules", "ledger", "bibles")):
        return None
    return bundle


def request_from_report(root: Path) -> str:
    """从原报告里抠出创作要求（回退路径用）。"""
    report = root / "compare.md"
    if not report.exists():
        return ""
    for line in report.read_text(encoding="utf-8").splitlines():
        if line.startswith("- 要求："):
            return line.replace("- 要求：", "").strip()
    return ""


def scenes_from_disk(root: Path) -> list[dict]:
    """按目录名排序收集各模式的 scene.md，顺序稳定，标签由 judge_scenes 统一分配。"""
    items = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        scene = sub / "scene.md"
        if scene.exists() and scene.read_text(encoding="utf-8").strip():
            items.append({"mode": sub.name, "scene": scene.read_text(encoding="utf-8")})
    return items


def main() -> None:
    # 重定向到文件时 Python 默认块缓冲，进度会看不到。
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

    frozen = load_frozen(root)
    if frozen is not None:
        request, context = frozen["request"], frozen["context"]
        items = list(frozen["items"])
        source = "judge_input.json（冻结资料，与首次评审完全一致）"
    else:
        request = request_from_report(root)
        items = scenes_from_disk(root)
        context = agents.evaluation_context(Ledger(DATA_DIR / "ledger.json"), load_bibles())
        source = "当前账本 + 人物卡（该目录没有 judge_input.json，规则可能与首次评审不同）"
    if not items:
        print("没找到任何稿件")
        return

    cfg = load_config(mock=args.mock, provider=args.provider, reasoning=args.reasoning)
    print(f"评审模型：{cfg.describe()}")
    print(f"评审资料：{source}")
    print(f"稿件：{len(items)} 份\n")

    rec = Recorder(root / f"_rejudge_{args.provider}")
    text = agents.judge_scenes(cfg, rec, request, items, context=context)

    # 匿名映射只落在产物里，不进入模型输入（judge_scenes 只发字母标签和正文）。
    mapping = {chr(65 + i): it["mode"] for i, it in enumerate(items)}
    bundle = judge_bundle(request, context, items)
    bundle["judge_provider"] = cfg.describe()
    bundle["context_source"] = source
    (root / f"judge_{args.provider}_input.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    header = [
        f"<!-- 评审模型：{cfg.describe()} -->",
        f"<!-- 评审资料：{source} -->",
        "<!-- 匿名映射：" + "；".join(f"{k}={v}" for k, v in mapping.items()) + " -->",
        "",
    ]
    out = root / f"judge_{args.provider}.md"
    out.write_text("\n".join(header) + text + "\n", encoding="utf-8")
    print(text)
    print("\n匿名映射：" + "；".join(f"{k}={MODE_LABELS.get(v, v)}" for k, v in mapping.items()))
    print(f"\n写入：{out}")


if __name__ == "__main__":
    main()

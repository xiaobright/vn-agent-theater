"""记录器：把每一次 agent 调用的完整输入/输出落盘，方便事后读。

这是"实验台"最重要的部分——没有它，你只能看到最终稿，看不到
每个 agent 到底被喂了什么、又吐了什么。要看懂 agent team，就看这里。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def cached_tokens(usage: dict | None) -> int:
    """从 usage 里取缓存命中的输入 token。

    不同 provider 放的位置不一样（顶层 cached_tokens，或 prompt_tokens_details 里）。
    拿不到就返回 0——"没报告"和"没命中"在这里不做区分，报告里会说明。
    """
    if not isinstance(usage, dict):
        return 0
    for value in (usage.get("cached_tokens"),
                  (usage.get("prompt_tokens_details") or {}).get("cached_tokens")):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


class Recorder:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        self.index: list[dict] = []
        self.usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        self._lock = threading.Lock()  # 角色分析是并发调用，编号要加锁

    def record(
        self,
        *,
        role: str,
        model: str,
        messages: list[dict],
        response: str,
        usage: dict,
        elapsed: float,
        extra: dict | None = None,
    ) -> None:
        with self._lock:
            self.seq += 1
            seq = self.seq
            for k in ("prompt_tokens", "completion_tokens"):
                self.usage_total[k] += (usage or {}).get(k, 0) or 0
        safe_role = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", role)
        tag = f"{seq:02d}_{safe_role}"
        payload = {
            "seq": seq,
            "role": role,
            "model": model,
            "elapsed_sec": round(elapsed, 2),
            "usage": usage,
            "messages": messages,   # 完整输入：谁被喂了什么，全在这里
            "response": response,   # 完整输出
            "extra": extra or {},
        }
        self.write_json(f"{tag}.json", payload)
        with self._lock:
            self.index.append({"tag": tag, "role": role, "elapsed_sec": round(elapsed, 2)})

    def write_text(self, name: str, text: str) -> None:
        target = self.run_dir / name
        if target.resolve().parent != self.run_dir.resolve():
            raise ValueError("记录文件必须位于本次运行目录内")
        temp = target.with_name(target.name + "." + uuid4().hex + ".tmp")
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()

    def write_json(self, name: str, payload: object) -> None:
        self.write_text(name, json.dumps(payload, ensure_ascii=False, indent=2))

    def write_summary(self, lines: list[str]) -> None:
        header = [
            f"# 本次运行 {self.run_dir.name}",
            "",
            f"生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            "",
        ]
        self.write_text("_summary.md", "\n".join(header + lines) + "\n")

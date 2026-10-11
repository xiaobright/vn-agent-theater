"""可审阅的模型路由与请求预算；密钥只从环境或本地 .env 读取。"""
from __future__ import annotations

import math
import os
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROMPTS_DIR = ROOT / "prompts"
RUNS_DIR = ROOT / "runs"

PROVIDER_PRESETS: dict[str, dict] = {
    "stepfun": {
        "label": "StepFun · step-5-preview",
        "base_url": "https://api.stepfun.com/step_plan/v1",
        "model": "step-5-preview",
        "key_env": "STEPFUN_API_KEY",
        "reasoning_levels": ["disabled", "low", "medium", "high"],
    },
    "longcat": {
        "label": "LongCat · LongCat-2.5-Preview",
        "base_url": "https://api.longcat.chat/openai/v1",
        "model": "LongCat-2.5-Preview",
        "key_env": "LONGCAT_API_KEY",
        "reasoning_levels": [],
    },
}
REASONING_LEVELS = ["disabled", "low", "medium", "high"]


@dataclass
class Config:
    # 保留原有位置参数顺序，旧 CLI / 实验脚本仍可构造 Config。
    provider: str
    api_key: str | None = field(repr=False)
    base_url: str | None
    model: str
    temperature: float
    reasoning: str | None
    mock: bool
    global_concurrency: int = 2
    provider_concurrency: int = 2
    quota_group: str = ""
    quota_concurrency: int = 2
    request_timeout: float = 90.0
    request_deadline: float = 180.0
    max_retries: int = 2
    retry_base: float = 1.0
    retry_cap: float = 30.0
    max_output_tokens: int = 16384
    max_revisions: int = 1
    cancel_event: threading.Event | None = field(default=None, repr=False, compare=False)
    on_request_event: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name, low, high in (
            ("global_concurrency", 1, 64), ("provider_concurrency", 1, 64),
            ("quota_concurrency", 1, 64), ("max_retries", 0, 5),
            ("max_output_tokens", 1, 131072), ("max_revisions", 0, 2),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} 必须是 {low}–{high} 的整数")
        for name, low, high in (
            ("temperature", 0, 2), ("request_timeout", 0.01, 600),
            ("request_deadline", 0.01, 1800), ("retry_base", 0.01, 60),
            ("retry_cap", 0.01, 300),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} 必须在 {low}–{high} 之间")
        if self.retry_cap < self.retry_base:
            raise ValueError("retry_cap 不得小于 retry_base")
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("provider 和 model 不能为空")
        if self.base_url:
            try:
                parsed = urlsplit(self.base_url)
                valid = parsed.scheme in {"https", "http"} and parsed.hostname and not (
                    parsed.username or parsed.password or parsed.query or parsed.fragment
                )
                parsed.port  # 非法端口也提前报配置错误。
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("base_url 必须是无凭据、查询参数或片段的 HTTP(S) 地址")
        if self.reasoning and self.reasoning not in REASONING_LEVELS:
            raise ValueError("reasoning 必须为 disabled / low / medium / high")

    def describe(self) -> str:
        if self.mock:
            return "mock（假模型）"
        suffix = f" · reasoning={self.reasoning}" if self.reasoning else ""
        result = f"{self.provider} · {self.model}{suffix}"
        return result.replace(self.api_key, "[redacted]") if self.api_key else result


def _prefix(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def _env(name: str) -> str | None:
    return (os.getenv(name) or "").strip() or None


def _number(name: str, fallback: int | float, kind: type = int) -> int | float:
    try:
        return kind(_env(name) or fallback)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是有效数字") from None


def _provider_values(name: str, use_generic: bool) -> dict:
    preset = PROVIDER_PRESETS.get(name, {})
    prefix = _prefix(name)
    def read(suffix: str) -> str | None:
        return _env(f"{prefix}_{suffix}") or (_env(f"LLM_{suffix}") if use_generic else None)
    return {
        "provider": name,
        "api_key": read("API_KEY"),
        "base_url": read("BASE_URL") or preset.get("base_url"),
        "model": read("MODEL") or preset.get("model", "gpt-4o-mini"),
        "quota_group": read("QUOTA_GROUP") or "",
    }


def _reasoning(name: str, level: str | None) -> str | None:
    preset = PROVIDER_PRESETS.get(name)
    if preset is not None and not preset["reasoning_levels"]:
        return None
    return level


def load_config(mock: bool = False, provider: str | None = None, reasoning: str | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    primary = (_env("LLM_PROVIDER") or "stepfun").lower()
    name = (provider or primary).strip().lower()
    values = _provider_values(name, name == primary)
    prefix = _prefix(name)
    level = reasoning or _env(f"{prefix}_REASONING") or (_env("LLM_REASONING") if name == primary else None)
    return Config(
        **values,
        temperature=_number("LLM_TEMPERATURE", 0.8, float),
        reasoning=_reasoning(name, level), mock=mock,
        global_concurrency=_number("LLM_GLOBAL_CONCURRENCY", 2),
        provider_concurrency=_number(f"{prefix}_CONCURRENCY", _number("LLM_PROVIDER_CONCURRENCY", 2)),
        quota_concurrency=_number(f"{prefix}_QUOTA_CONCURRENCY", _number("LLM_QUOTA_CONCURRENCY", 2)),
        request_timeout=_number("LLM_REQUEST_TIMEOUT", 90, float),
        request_deadline=_number("LLM_REQUEST_DEADLINE", 180, float),
        max_retries=_number("LLM_MAX_RETRIES", 2),
        retry_base=_number("LLM_RETRY_BASE", 1, float),
        retry_cap=_number("LLM_RETRY_CAP", 30, float),
        # 注意：这里的 fallback 必须跟 Config 的字段默认值一致，否则改了 dataclass 也没用。
        max_output_tokens=_number("LLM_MAX_OUTPUT_TOKENS", Config.__dataclass_fields__["max_output_tokens"].default),
        max_revisions=_number("LLM_MAX_REVISIONS", 1),
    )


ROLE_KEYS = {
    "总编": "DIRECTOR", "时间线主管": "CONTINUITY", "规则守门": "CONTINUITY",
    "状态提取": "CONTINUITY", "状态整理": "CONTINUITY", "连续性审校": "CONTINUITY",
    "规则审查": "CONTINUITY", "状态提议": "CONTINUITY",
    "主笔": "WRITER", "定向修订": "WRITER", "定向修改": "WRITER", "修订": "WRITER",
    "审读": "REVIEWER", "评审": "JUDGE", "单智能体": "SINGLE",
    "主角": "PROTAGONIST", "首领": "LEADER", "摇摆者": "SWING",
}


def for_role(cfg: Config, role: str) -> Config:
    """岗位覆盖：ROLE_WRITER_PROVIDER/MODEL/BASE_URL/API_KEY/QUOTA_GROUP。

    群聊与人物推演复用对应岗位路由。切换 provider 不继承原 provider 的
    全局 URL / key / model；同一账户的不同入口应显式填相同 QUOTA_GROUP。
    """
    role = role.removeprefix("群聊_").removeprefix("角色_")
    key = ROLE_KEYS.get(role, _prefix(role))
    if not key:
        return cfg
    prefix = f"ROLE_{key}"
    target = (_env(f"{prefix}_PROVIDER") or cfg.provider).lower()
    result = cfg
    if target != cfg.provider:
        primary = (_env("LLM_PROVIDER") or "stepfun").lower()
        result = replace(
            cfg, **_provider_values(target, target == primary),
            reasoning=_reasoning(target, _env(f"{_prefix(target)}_REASONING")),
            provider_concurrency=_number(f"{_prefix(target)}_CONCURRENCY", cfg.provider_concurrency),
            quota_concurrency=_number(f"{_prefix(target)}_QUOTA_CONCURRENCY", cfg.quota_concurrency),
        )
    overrides = {}
    for field_name in ("model", "base_url", "api_key", "quota_group"):
        value = _env(f"{prefix}_{field_name.upper()}")
        if value:
            overrides[field_name] = value
    return replace(result, **overrides) if overrides else result

"""统一请求入口：进程内配额、有限重试、可取消等待。无真实请求的 mock 也走限流。"""
from __future__ import annotations

import asyncio
import hashlib
import math
import random
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .config import Config


class LLMError(RuntimeError):
    pass


class LLMCancelled(LLMError):
    pass


def check_cancelled(cfg: Config) -> None:
    if cfg.cancel_event is not None and cfg.cancel_event.is_set():
        raise LLMCancelled("本次创作已取消")


def _check(cfg: Config, deadline: float) -> float:
    check_cancelled(cfg)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LLMError("请求总时限已到；未自动重发")
    return remaining


def _emit(cfg: Config, event: str, **fields) -> None:
    if cfg.on_request_event:
        try:
            cfg.on_request_event({"event": event, **fields})
        except Exception:
            # 观察台回调失败不能把已完成的请求变成可重试错误。
            pass


def _wait(cfg: Config, seconds: float, deadline: float) -> None:
    until = time.monotonic() + seconds
    while True:
        remaining = _check(cfg, deadline)
        left = until - time.monotonic()
        if left <= 0:
            return
        delay = min(left, remaining, 0.1)
        if cfg.cancel_event is not None:
            cfg.cancel_event.wait(delay)
        else:
            time.sleep(delay)


@dataclass(eq=False)
class _Ticket:
    cfg: Config
    endpoint: str
    group: str


class _Scheduler:
    """一次性原子获取三种许可，不会持有一个许可等待另一个。"""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.active: list[_Ticket] = []
        self.waiting: list[_Ticket] = []
        self.cooldowns: dict[str, float] = {}
        self.counts = Counter()

    def ticket(self, cfg: Config) -> _Ticket:
        # 相同地址的不同模型共用 provider 配额；键不含 API key。
        url = urlsplit(cfg.base_url or "https://api.openai.com/v1")
        port = url.port or (443 if url.scheme == "https" else 80)
        endpoint = f"{url.scheme.lower()}://{url.hostname.lower()}:{port}{url.path.rstrip('/')}"
        group = f"named:{cfg.quota_group}" if cfg.quota_group else f"endpoint:{endpoint}"
        return _Ticket(cfg, endpoint, group)

    def increment(self, key: str) -> None:
        with self.condition:
            self.counts[key] += 1

    def _available(self, ticket: _Ticket) -> bool:
        all_tickets = self.active + self.waiting
        # 配置不一致时遵守同一作用域中更保守的上限。
        global_limit = min(t.cfg.global_concurrency for t in all_tickets)
        endpoint_tickets = [t for t in all_tickets if t.endpoint == ticket.endpoint]
        group_tickets = [t for t in all_tickets if t.group == ticket.group]
        return (
            len(self.active) < global_limit
            and sum(t.endpoint == ticket.endpoint for t in self.active) < min(t.cfg.provider_concurrency for t in endpoint_tickets)
            and sum(t.group == ticket.group for t in self.active) < min(t.cfg.quota_concurrency for t in group_tickets)
            and self.cooldowns.get(ticket.group, 0) <= time.monotonic()
        )

    def acquire(self, ticket: _Ticket, deadline: float) -> None:
        with self.condition:
            self.waiting.append(ticket)
            try:
                while True:
                    remaining = _check(ticket.cfg, deadline)
                    if self._available(ticket):
                        self.waiting.remove(ticket)
                        self.active.append(ticket)
                        self.counts["peak_active"] = max(self.counts["peak_active"], len(self.active))
                        return
                    self.condition.wait(min(remaining, 0.1))
            finally:
                if ticket in self.waiting:
                    self.waiting.remove(ticket)
                self.condition.notify_all()

    def release(self, ticket: _Ticket, cooldown: float = 0) -> None:
        with self.condition:
            if cooldown:
                self.cooldowns[ticket.group] = max(self.cooldowns.get(ticket.group, 0), time.monotonic() + cooldown)
            self.active.remove(ticket)
            self.condition.notify_all()

    def snapshot(self) -> dict:
        with self.condition:
            now = time.monotonic()
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if v > now}
            all_tickets = self.active + self.waiting
            def count(items, attr, key):
                return sum(getattr(t, attr) == key for t in items)
            providers = []
            for endpoint in sorted({t.endpoint for t in all_tickets}):
                # URL 路径也可能含私密配置，观察台只显示摘要 ID。
                label = "endpoint-" + hashlib.sha256(endpoint.encode()).hexdigest()[:8]
                providers.append({"provider": label, "active": count(self.active, "endpoint", endpoint), "queued": count(self.waiting, "endpoint", endpoint)})
            groups = []
            for group in sorted({t.group for t in all_tickets} | set(self.cooldowns)):
                label = "quota-" + hashlib.sha256(group.encode()).hexdigest()[:8]
                groups.append({"group": label, "active": count(self.active, "group", group), "queued": count(self.waiting, "group", group), "cooldown_seconds": round(max(0, self.cooldowns.get(group, 0) - now), 2)})
            return {
                "active": len(self.active), "queued": len(self.waiting),
                **{k: self.counts[k] for k in ("peak_active", "total_requests", "succeeded", "failed", "cancelled", "retries")},
                "providers": providers, "quota_groups": groups,
            }


_SCHEDULER = _Scheduler()


def scheduler_snapshot() -> dict:
    """仅当前进程的计数；多个服务进程不会共享额度锁。"""
    return _SCHEDULER.snapshot()


async def _network_attempt(cfg: Config, messages: list[dict], temperature: float, deadline: float):
    from openai import AsyncOpenAI

    timeout = min(cfg.request_timeout, _check(cfg, deadline))
    # 禁用 SDK 自动重试；总时限/取消由本层控制，避免嵌套重试翻倍。
    async with AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url, max_retries=0, timeout=timeout) as client:
        args = {"model": cfg.model, "messages": messages, "temperature": temperature, "max_tokens": cfg.max_output_tokens}
        if cfg.reasoning:
            args["extra_body"] = {"reasoning_effort": cfg.reasoning}
        task = asyncio.create_task(client.chat.completions.create(**args))
        attempt_deadline = min(deadline, time.monotonic() + timeout)
        try:
            while True:
                remaining = _check(cfg, deadline)
                left = attempt_deadline - time.monotonic()
                if left <= 0:
                    raise LLMError("请求超时；服务端可能仍在生成，未自动重发")
                done, _ = await asyncio.wait({task}, timeout=min(remaining, left, 0.1))
                if done:
                    check_cancelled(cfg)
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _retry_after(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", {})
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delay = dt.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, delay) if math.isfinite(delay) else None


def _failure(exc: Exception) -> tuple[bool, int | None, str]:
    """仅返回安全分类；原始异常可能包含 Authorization、请求体或代理地址。"""
    from openai import APIConnectionError, APITimeoutError

    status = getattr(exc, "status_code", None)
    status = status if type(status) is int else None
    body = getattr(exc, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(detail, dict):
        detail = {}
    raw = " ".join(str(value) for value in (
        getattr(exc, "code", ""), getattr(exc, "type", ""),
        detail.get("code", ""), detail.get("type", ""), detail.get("message", ""),
    )).lower()
    if any(word in raw for word in ("insufficient_quota", "quota_exceeded", "quota exhausted", "billing", "credit balance", "insufficient balance", "payment_required", "余额不足", "额度耗尽")):
        return False, status, "账户额度或账单限制，请检查套餐后手动重试"
    if isinstance(exc, APITimeoutError):
        cause = exc.__cause__
        if cause is not None and type(cause).__name__ in {"ConnectTimeout", "PoolTimeout"}:
            return True, status, "建立连接超时"
        return False, status, "读取超时；服务端可能仍在生成，未自动重发"
    if isinstance(exc, APIConnectionError):
        # ReadError / WriteError 等无法确认服务端是否收到请求，保守停止。
        cause = exc.__cause__
        if cause is None or type(cause).__name__ in {"ConnectError", "ConnectTimeout", "PoolTimeout"}:
            return True, status, "连接失败"
        return False, status, "传输中断；服务端结果不确定，未自动重发"
    if status in {429, 500, 502, 503, 504}:
        return True, status, f"服务暂不可用（HTTP {status}）"
    if status is not None:
        return False, status, f"服务拒绝请求（HTTP {status}），请检查模型、接口和授权配置"
    return False, None, "模型响应格式或客户端调用异常；未自动重发"


def _response_text(resp) -> tuple[str, dict]:
    try:
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("输出达到 token 上限，稿件不完整；请调整长度后手动重试")
        content = choice.message.content
        if not isinstance(content, str) or not content.strip():
            raise LLMError("模型未返回有效正文；未自动重发")
        if choice.finish_reason not in {None, "stop"}:
            raise LLMError("模型未正常完成正文；未自动重发")
        usage = resp.usage.model_dump() if resp.usage else {}
        return content.strip(), usage
    except LLMError:
        raise
    except Exception:
        raise LLMError("模型返回了无效响应结构；未自动重发") from None


def chat(cfg: Config, messages: list[dict], *, temperature: float | None = None, mock_text: str = "[mock] 假输出。") -> tuple[str, dict, float]:
    """排队、请求、退避均计入总时限，返回 (文本, usage, 耗时秒)。"""
    started = time.monotonic()
    deadline = started + cfg.request_deadline
    ticket = _SCHEDULER.ticket(cfg)
    _SCHEDULER.increment("total_requests")
    try:
        _check(cfg, deadline)
        if not cfg.mock and not cfg.api_key:
            raise LLMError("没有读到 API key。请在 .env 中配置，或使用 mock。")
        for attempt in range(1, cfg.max_retries + 2):
            _emit(cfg, "queued", attempt=attempt)
            _SCHEDULER.acquire(ticket, deadline)
            cooldown = 0.0
            retry = False
            delay = 0.0
            try:
                _check(cfg, deadline)
                _emit(cfg, "started", attempt=attempt)
                if cfg.mock:
                    _wait(cfg, 0.05, deadline)
                    text, usage = mock_text, {"prompt_tokens": 0, "completion_tokens": 0}
                else:
                    try:
                        resp = asyncio.run(_network_attempt(cfg, messages, cfg.temperature if temperature is None else temperature, deadline))
                    except LLMError:
                        raise
                    except Exception as exc:
                        retry, status, message = _failure(exc)
                        if not retry:
                            raise LLMError(message) from None
                        hint = _retry_after(exc)
                        fallback = min(cfg.retry_cap, cfg.retry_base * (2 ** (attempt - 1)))
                        delay = max(hint or 0, random.uniform(fallback / 2, fallback))
                        # 释放许可时同时发布冷却，同组下一请求不会抢跑。
                        if status == 429:
                            cooldown = delay
                        if attempt > cfg.max_retries:
                            raise LLMError(f"{message}；已用完 {cfg.max_retries} 次重试") from None
                        if delay >= _check(cfg, deadline):
                            raise LLMError("服务要求的等待超过剩余总时限；已停止，不会提前重试") from None
                        _emit(cfg, "retry", attempt=attempt, status_code=status, retry_in=round(delay, 3))
                    else:
                        _check(cfg, deadline)
                        text, usage = _response_text(resp)
            finally:
                _SCHEDULER.release(ticket, cooldown)
            if retry:
                _SCHEDULER.increment("retries")
                _wait(cfg, delay, deadline)
                continue
            _SCHEDULER.increment("succeeded")
            _emit(cfg, "succeeded", attempt=attempt)
            return text, usage, time.monotonic() - started
        raise LLMError("请求未完成")
    except LLMCancelled:
        _SCHEDULER.increment("cancelled")
        _emit(cfg, "cancelled")
        raise
    except LLMError:
        _SCHEDULER.increment("failed")
        _emit(cfg, "failed")
        raise

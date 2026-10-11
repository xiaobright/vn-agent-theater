"""请求层验收：只使用内存 SDK 替身，不读取 .env、不调用真实服务。"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import patch

from openai import APIConnectionError, APITimeoutError

from agentteam.config import Config, for_role, load_config
from agentteam import llm


def config(**kwargs):
    return replace(Config("test", "never-a-real-key", "https://test.invalid/v1", "fake", 0.8, None, False),
                   retry_base=0.01, retry_cap=0.01, **kwargs)


def response(text="完成", finish="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 3, "completion_tokens": 5}),
    )


class StatusError(Exception):
    def __init__(self, status=429, retry_after=None, code=None):
        super().__init__("sensitive upstream body never-a-real-key")
        self.status_code = status
        self.body = {"error": {"code": code}} if code else {}
        self.response = SimpleNamespace(headers={} if retry_after is None else {"retry-after": str(retry_after)})


class SDK:
    """每次 attempt 新建 client，内部 async create 可等待、失败或取消。"""
    def __init__(self, action):
        self.action = action
        self.clients = []
        self.calls = []
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def __call__(self, **kwargs):
        outer = self

        class Client:
            def __init__(self):
                self.closed = False
                self.options = kwargs
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.closed = True

            async def create(self, **args):
                with outer.lock:
                    outer.calls.append(args)
                    number = len(outer.calls)
                    outer.active += 1
                    outer.peak = max(outer.peak, outer.active)
                try:
                    return await outer.action(number, args)
                finally:
                    with outer.lock:
                        outer.active -= 1
        result = Client()
        self.clients.append(result)
        return result


class LLMTests(unittest.TestCase):
    def setUp(self):
        # 每个用例隔离调度器统计；生产仅有模块单例。
        self.scheduler = patch.object(llm, "_SCHEDULER", llm._Scheduler())
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)

    def assert_idle(self):
        state = llm.scheduler_snapshot()
        self.assertEqual((state["active"], state["queued"]), (0, 0))

    def test_success_closes_client_and_disables_sdk_retries(self):
        async def action(n, args):
            return response()
        sdk = SDK(action)
        with patch("openai.AsyncOpenAI", sdk):
            text, usage, elapsed = llm.chat(config(), [])
        self.assertEqual(text, "完成")
        self.assertEqual(usage["completion_tokens"], 5)
        self.assertGreaterEqual(elapsed, 0)
        self.assertTrue(all(c.closed for c in sdk.clients))
        self.assertEqual(sdk.clients[0].options["max_retries"], 0)
        self.assertGreater(sdk.clients[0].options["timeout"], 0)
        self.assertEqual(sdk.calls[0]["max_tokens"], Config.__dataclass_fields__["max_output_tokens"].default)
        self.assert_idle()

    def test_max_tokens_can_be_overridden_per_call(self):
        """推理 token 也算在 completion 里，所以要动脑的岗位可以单独要更大的预算。"""
        async def action(n, args):
            return response()
        sdk = SDK(action)
        with patch("openai.AsyncOpenAI", sdk):
            llm.chat(config(), [], max_tokens=9999)
        self.assertEqual(sdk.calls[0]["max_tokens"], 9999)
        self.assert_idle()

    def test_global_cap_across_independent_configs(self):
        async def action(n, args):
            await asyncio.sleep(0.03)
            return response()
        sdk = SDK(action)
        configurations = [config(base_url=f"https://endpoint-{i}.invalid/v1") for i in range(6)]
        with patch("openai.AsyncOpenAI", sdk), ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda c: llm.chat(c, []), configurations))
        self.assertEqual(len(results), 6)
        self.assertEqual(sdk.peak, 2)
        self.assert_idle()

    def test_shared_account_group_across_urls(self):
        async def action(n, args):
            await asyncio.sleep(0.03)
            return response()
        sdk = SDK(action)
        configurations = [config(global_concurrency=6, provider_concurrency=6, quota_concurrency=1,
                                 quota_group="same-account", base_url=f"https://endpoint-{i}.invalid/v1") for i in range(5)]
        with patch("openai.AsyncOpenAI", sdk), ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda c: llm.chat(c, []), configurations))
        self.assertEqual(sdk.peak, 1)
        self.assert_idle()

    def test_endpoint_cap_normalizes_host_and_default_port(self):
        async def action(n, args):
            await asyncio.sleep(0.03)
            return response()
        sdk = SDK(action)
        urls = ["https://TEST.invalid/v1", "https://test.invalid:443/v1/"]
        configurations = [config(global_concurrency=6, provider_concurrency=1, quota_concurrency=6,
                                 base_url=url) for url in urls]
        with patch("openai.AsyncOpenAI", sdk), ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda c: llm.chat(c, []), configurations))
        self.assertEqual(sdk.peak, 1)
        self.assert_idle()

    def test_retry_after_is_not_capped_and_backoff_releases_permit(self):
        times = []
        async def action(n, args):
            times.append(time.monotonic())
            if n == 1:
                raise StatusError(503, 0.08)
            return response()
        sdk = SDK(action)
        snapshot_at_backoff = []
        original_wait = llm._wait
        def wait(cfg, seconds, deadline):
            snapshot_at_backoff.append(llm.scheduler_snapshot())
            return original_wait(cfg, seconds, deadline)
        with patch("openai.AsyncOpenAI", sdk), patch.object(llm, "_wait", wait):
            llm.chat(config(), [])
        self.assertGreaterEqual(times[1] - times[0], 0.075)
        self.assertEqual(snapshot_at_backoff[0]["active"], 0)
        self.assertEqual(len(sdk.calls), 2)
        self.assertTrue(all(c.closed for c in sdk.clients))
        self.assert_idle()

    def test_retry_after_http_date_and_bad_values(self):
        target = datetime.fromtimestamp(2_000_000_120, timezone.utc)
        with patch.object(llm.time, "time", return_value=2_000_000_000):
            self.assertEqual(llm._retry_after(StatusError(retry_after=format_datetime(target))), 120)
        for invalid in ["", "tomorrow", "nan", "inf"]:
            self.assertIsNone(llm._retry_after(StatusError(retry_after=invalid)))

    def test_retry_after_beyond_deadline_stops_without_early_retry(self):
        async def action(n, args):
            raise StatusError(429, 10)
        sdk = SDK(action)
        with patch("openai.AsyncOpenAI", sdk), self.assertRaisesRegex(llm.LLMError, "等待超过"):
            llm.chat(config(request_deadline=0.5), [])
        self.assertEqual(len(sdk.calls), 1)
        self.assertGreater(llm.scheduler_snapshot()["quota_groups"][0]["cooldown_seconds"], 9)
        self.assert_idle()

    def test_429_cools_entire_group_but_not_another_account(self):
        times = {}
        first_failed = threading.Event()
        async def action(n, args):
            model = args["model"]
            times[model] = time.monotonic()
            if model == "limited":
                first_failed.set()
                raise StatusError(429, 0.15)
            return response()
        sdk = SDK(action)
        a = config(model="limited", max_retries=0, quota_group="shared")
        b = config(model="same", quota_group="shared", base_url="https://other.invalid/v1")
        c = config(model="independent", quota_group="independent")
        with patch("openai.AsyncOpenAI", sdk):
            with self.assertRaises(llm.LLMError):
                llm.chat(a, [])
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda cfg: llm.chat(cfg, []), [b, c]))
        self.assertGreaterEqual(times["same"] - times["limited"], 0.14)
        self.assertLess(times["independent"] - times["limited"], 0.14)
        self.assert_idle()

    def test_permanent_errors_never_retry_and_do_not_expose_keys(self):
        for error in [StatusError(400), StatusError(401), StatusError(403), StatusError(429, code="insufficient_quota")]:
            async def action(n, args, error=error):
                raise error
            sdk = SDK(action)
            events = []
            with self.subTest(error=error.status_code), patch("openai.AsyncOpenAI", sdk):
                with self.assertRaises(llm.LLMError) as caught:
                    llm.chat(config(on_request_event=events.append), [])
                self.assertNotIn("never-a-real-key", str(caught.exception))
                self.assertNotIn("never-a-real-key", str(events))
                self.assertEqual(len(sdk.calls), 1)
                self.assertTrue(sdk.clients[0].closed)
                self.assert_idle()

    def test_retries_only_safe_connection_failures_and_stops_on_read_timeout(self):
        connection = APIConnectionError(request=SimpleNamespace())
        read_timeout = APITimeoutError(request=SimpleNamespace())
        for error, expected in [(connection, 3), (read_timeout, 1)]:
            async def action(n, args, error=error):
                raise error
            sdk = SDK(action)
            with self.subTest(error=type(error).__name__), patch("openai.AsyncOpenAI", sdk):
                with self.assertRaises(llm.LLMError):
                    llm.chat(config(), [])
                self.assertEqual(len(sdk.calls), expected)
                self.assertTrue(all(c.closed for c in sdk.clients))
                self.assert_idle()

    def test_empty_malformed_and_truncated_outputs_are_permanent(self):
        for result in [response(" "), response("一半", "length"), SimpleNamespace(choices=[])]:
            async def action(n, args, result=result):
                return result
            sdk = SDK(action)
            with self.subTest(result=result), patch("openai.AsyncOpenAI", sdk):
                with self.assertRaises(llm.LLMError):
                    llm.chat(config(), [])
                self.assertEqual(len(sdk.calls), 1)
                self.assert_idle()

    def test_request_timeout_is_hard_bounded_and_client_closes(self):
        async def action(n, args):
            await asyncio.sleep(10)
            return response()
        sdk = SDK(action)
        started = time.monotonic()
        with patch("openai.AsyncOpenAI", sdk), self.assertRaisesRegex(llm.LLMError, "超时"):
            llm.chat(config(request_timeout=0.05), [])
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(sdk.calls), 1)
        self.assertTrue(sdk.clients[0].closed)
        self.assert_idle()

    def test_cancel_inflight_closes_client_without_retry(self):
        event = threading.Event()
        entered = threading.Event()
        async def action(n, args):
            entered.set()
            await asyncio.sleep(10)
            return response()
        sdk = SDK(action)
        with patch("openai.AsyncOpenAI", sdk), ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(llm.chat, config(cancel_event=event), [])
            self.assertTrue(entered.wait(1))
            event.set()
            with self.assertRaises(llm.LLMCancelled):
                task.result(1)
        self.assertEqual(len(sdk.calls), 1)
        self.assertTrue(sdk.clients[0].closed)
        self.assert_idle()

    def test_cancel_queued_request_never_enters_sdk(self):
        gate = threading.Event()
        entered = threading.Event()
        cancel = threading.Event()
        async def action(n, args):
            entered.set()
            while not gate.is_set():
                await asyncio.sleep(0.01)
            return response()
        sdk = SDK(action)
        try:
            with patch("openai.AsyncOpenAI", sdk), ThreadPoolExecutor(max_workers=2) as pool:
                one = pool.submit(llm.chat, config(global_concurrency=1), [])
                self.assertTrue(entered.wait(1))
                two = pool.submit(llm.chat, config(global_concurrency=1, cancel_event=cancel), [])
                cancel.set()
                with self.assertRaises(llm.LLMCancelled):
                    two.result(1)
                gate.set()
                one.result(1)
        finally:
            gate.set()
        self.assertEqual(len(sdk.calls), 1)
        self.assert_idle()

    def test_cancel_during_retry_backoff(self):
        event = threading.Event()
        async def action(n, args):
            raise StatusError(503, 10)
        sdk = SDK(action)
        def callback(item):
            if item["event"] == "retry":
                event.set()
        with patch("openai.AsyncOpenAI", sdk), self.assertRaises(llm.LLMCancelled):
            llm.chat(config(cancel_event=event, on_request_event=callback), [])
        self.assertEqual(len(sdk.calls), 1)
        self.assert_idle()

    def test_mock_uses_limiter_without_openai_client(self):
        with patch("openai.AsyncOpenAI", side_effect=AssertionError("must not create client")):
            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(lambda _: llm.chat(config(mock=True, global_concurrency=1), []), range(5)))
        self.assertEqual(len(results), 5)
        self.assertEqual(llm.scheduler_snapshot()["peak_active"], 1)
        self.assert_idle()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dotenv = patch("agentteam.config.load_dotenv")
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_explicit_alternate_does_not_inherit_generic_default(self):
        os.environ.update(LLM_PROVIDER="stepfun", LLM_BASE_URL="https://default.invalid/v1",
                          LLM_MODEL="default-model", LLM_API_KEY="default-key", LONGCAT_API_KEY="other-key")
        alternate = load_config(provider="longcat")
        self.assertEqual(alternate.base_url, "https://api.longcat.chat/openai/v1")
        self.assertEqual(alternate.model, "LongCat-2.5-Preview")
        self.assertEqual(alternate.api_key, "other-key")
        self.assertEqual(load_config().base_url, "https://default.invalid/v1")

    def test_role_mapping_preserves_runtime_budget_and_mock(self):
        event = threading.Event()
        callback = lambda data: None
        cfg = config(mock=True, cancel_event=event, on_request_event=callback, global_concurrency=4)
        os.environ.update(ROLE_WRITER_PROVIDER="longcat", LONGCAT_API_KEY="other-key",
                          ROLE_WRITER_MODEL="special", ROLE_WRITER_QUOTA_GROUP="shared")
        mapped = for_role(cfg, "定向修改")
        self.assertEqual((mapped.provider, mapped.model, mapped.quota_group), ("longcat", "special", "shared"))
        self.assertEqual(mapped.global_concurrency, 4)
        self.assertIs(mapped.cancel_event, event)
        self.assertIs(mapped.on_request_event, callback)
        self.assertTrue(mapped.mock)
        os.environ["ROLE_PROTAGONIST_MODEL"] = "character"
        self.assertEqual(for_role(cfg, "角色_主角").model, "character")
        self.assertEqual(for_role(cfg, "群聊_主角").model, "character")

    def test_custom_provider_prefix_and_role_url(self):
        os.environ.update(ROLE_CONTINUITY_PROVIDER="other", OTHER_API_KEY="other-key",
                          OTHER_BASE_URL="https://other.invalid/v1", OTHER_MODEL="other-model")
        mapped = for_role(config(), "规则审查")
        self.assertEqual(mapped.base_url, "https://other.invalid/v1")
        self.assertEqual(mapped.model, "other-model")
        self.assertEqual(mapped.api_key, "other-key")

    def test_validation_and_safe_descriptions(self):
        for kwargs in [dict(global_concurrency=0), dict(max_retries=100), dict(request_timeout=float("nan")),
                       dict(base_url="https://user:password@example.com"), dict(base_url="https://test.invalid/?key=secret"),
                       dict(max_revisions=3)]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                config(**kwargs)
        cfg = config(model="never-a-real-key")
        self.assertNotIn(cfg.api_key, cfg.describe())
        self.assertNotIn(cfg.api_key, repr(config()))
        os.environ["LLM_GLOBAL_CONCURRENCY"] = "sensitive-value"
        with self.assertRaises(ValueError) as caught:
            load_config()
        self.assertNotIn("sensitive-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

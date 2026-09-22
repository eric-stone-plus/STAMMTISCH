"""AI streaming (SSE) + provider fallback chain — offline unit tests."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from services import ai_driver
from services.ai_driver import AIDriver, ChatResponse, fallback_profiles_from_config


class _FakeStream:
    """Minimal SSE response: readline() over pre-encoded lines."""

    def __init__(self, lines: list[str]):
        self._lines = [line.encode("utf-8") for line in lines]

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class OpenAIStreamTest(unittest.TestCase):
    def _lines(self) -> list[str]:
        return [
            'data: {"model":"glm-5.3","choices":[{"delta":{"role":"assistant"}}]}',
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]

    def test_content_streams_and_assembles(self):
        tokens: list[str] = []
        driver = AIDriver(api_key="k")
        data, error = driver._consume_openai_stream(
            _FakeStream(self._lines()), tokens.append)
        self.assertIsNone(error)
        self.assertEqual(tokens, ["Hel", "lo"])
        message = data["choices"][0]["message"]
        self.assertEqual(message["content"], "Hello")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        self.assertEqual(data["model"], "glm-5.3")

    def test_tool_call_deltas_accumulate(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call1",'
            '"function":{"name":"market_context","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"{\\"symbol\\": \\"AAP"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"L\\"}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        driver = AIDriver(api_key="k")
        data, error = driver._consume_openai_stream(_FakeStream(lines), None)
        self.assertIsNone(error)
        choice = data["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["id"], "call1")
        self.assertEqual(call["function"]["name"], "market_context")
        self.assertEqual(call["function"]["arguments"], '{"symbol": "AAPL"}')

    def test_truncated_stream_fails_closed(self):
        lines = ['data: {"choices":[{"delta":{"content":"partial"}}]}']
        driver = AIDriver(api_key="k")
        data, error = driver._consume_openai_stream(_FakeStream(lines), None)
        self.assertIsNone(data)
        self.assertIn("Stream error", error)


class AnthropicStreamTest(unittest.TestCase):
    def test_text_and_tool_use_assemble(self):
        lines = [
            'data: {"type":"message_start","message":{"model":"glm-5.3",'
            '"usage":{"input_tokens":10}}}',
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text"}}',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Hi"}}',
            'data: {"type":"content_block_stop","index":0}',
            'data: {"type":"content_block_start","index":1,'
            '"content_block":{"type":"tool_use","id":"t1","name":"fetch_data"}}',
            'data: {"type":"content_block_delta","index":1,'
            '"delta":{"type":"input_json_delta","partial_json":"{\\"symbol\\":"}}',
            'data: {"type":"content_block_delta","index":1,'
            '"delta":{"type":"input_json_delta","partial_json":"\\"AAPL\\"}"}}',
            'data: {"type":"content_block_stop","index":1}',
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
            '"usage":{"output_tokens":5}}',
            'data: {"type":"message_stop"}',
        ]
        tokens: list[str] = []
        driver = AIDriver(api_key="k")
        data, error = driver._consume_anthropic_stream(
            _FakeStream(lines), tokens.append)
        self.assertIsNone(error)
        self.assertEqual(tokens, ["Hi"])
        choice = data["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        message = choice["message"]
        self.assertEqual(message["content"], "Hi")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "fetch_data")
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]),
                         {"symbol": "AAPL"})

    def test_thinking_lands_as_reasoning_content(self):
        lines = [
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"thinking_delta","thinking":"ponder"}}',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"answer"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
        ]
        driver = AIDriver(api_key="k")
        data, error = driver._consume_anthropic_stream(_FakeStream(lines), None)
        self.assertIsNone(error)
        message = data["choices"][0]["message"]
        self.assertEqual(message["content"], "answer")
        self.assertEqual(message["reasoning_content"], "ponder")


class ProviderChainTest(unittest.TestCase):
    def test_retryable_failure_falls_back_to_next_profile(self):
        calls: list[str] = []

        def fake_post(payload, base_url, key):
            calls.append(base_url)
            if base_url.endswith("/anthropic"):
                return None, "Network error: refused"
            return {"model": "deepseek-v4-pro", "choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }], "usage": {}}, None

        driver = AIDriver(api_key="k",
                          base_url="https://open.bigmodel.cn/api/anthropic",
                          model="glm-5.3")
        driver.fallback_profiles = [("k2", "https://api.deepseek.com/v1",
                                     "deepseek-v4-pro")]
        with mock.patch.object(driver, "_post", side_effect=fake_post):
            result = driver.chat("ping")
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "ok")
        self.assertEqual(calls[0], "https://open.bigmodel.cn/api/anthropic")
        self.assertEqual(calls[1], "https://api.deepseek.com/v1")
        self.assertTrue(any("provider fallback" in event
                            for event in result.tool_events))

    def test_auth_failure_does_not_fall_back(self):
        calls: list[str] = []

        def fake_post(payload, base_url, key):
            calls.append(base_url)
            return None, "HTTP 401: bad key"

        driver = AIDriver(api_key="k",
                          base_url="https://open.bigmodel.cn/api/anthropic")
        driver.fallback_profiles = [("k2", "https://api.deepseek.com/v1", "m")]
        with mock.patch.object(driver, "_post", side_effect=fake_post):
            result = driver.chat("ping")
        self.assertFalse(result.ok)
        self.assertEqual(calls, ["https://open.bigmodel.cn/api/anthropic"])

    def test_stream_rejection_retries_non_streaming_same_endpoint(self):
        seen_streams: list[bool] = []

        def fake_post(payload, base_url, key, on_token=None):
            seen_streams.append(bool(payload.get("stream")))
            if payload.get("stream"):
                return None, "HTTP 400: stream is not supported"
            return {"model": "m", "choices": [{
                "message": {"role": "assistant", "content": "plain"},
                "finish_reason": "stop",
            }], "usage": {}}, None

        driver = AIDriver(api_key="k")
        with mock.patch.object(driver, "_post", side_effect=fake_post):
            result = driver.chat("ping", on_token=lambda delta: None)
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "plain")
        self.assertEqual(seen_streams, [True, False])

    def test_streaming_round_calls_post_with_on_token(self):
        seen: dict = {}

        def fake_post(payload, base_url, key, on_token=None):
            seen["stream"] = payload.get("stream")
            seen["has_token"] = on_token is not None
            return {"model": "m", "choices": [{
                "message": {"role": "assistant", "content": "s"},
                "finish_reason": "stop",
            }], "usage": {}}, None

        driver = AIDriver(api_key="k")
        with mock.patch.object(driver, "_post", side_effect=fake_post):
            result = driver.chat("ping", on_token=lambda delta: None)
        self.assertTrue(result.ok)
        self.assertTrue(seen["stream"])
        self.assertTrue(seen["has_token"])


class FallbackProfileConfigTest(unittest.TestCase):
    _ENV_KEYS = ("GLM_API_KEY", "ZHIPU_API_KEY", "XIAOMI_API_KEY",
                 "DEEPSEEK_API_KEY", "DEEPSEEK_KEY", "DEEPSEEK_TOKEN",
                 "QIANWEN_TP_PERSONAL_KEY")

    def setUp(self) -> None:
        # Operator shells really export AI keys; chain building must be
        # decided by the fixture config, not the host environment.
        self._saved = {k: os.environ[k] for k in self._ENV_KEYS if k in os.environ}
        for key in self._saved:
            del os.environ[key]

    def tearDown(self) -> None:
        for key in self._saved:
            os.environ[key] = self._saved[key]

    def test_profiles_from_key_memory_and_env(self):
        config = SimpleNamespace()
        config.get = lambda key, default=None: (
            {"deepseek": "stored-key"} if key == "ai_profile_keys" else default)
        os.environ["GLM_API_KEY"] = "env-glm"
        chain = fallback_profiles_from_config(
            config, active_base_url="https://token-plan-cn.xiaomimimo.com/anthropic")
        names = [base_url for _key, base_url, _model in chain]
        # Active mimo endpoint skipped; glm (env key) and deepseek
        # (config key memory) join in AI_PROFILES order.
        self.assertEqual(names, ["https://open.bigmodel.cn/api/anthropic",
                                 "https://api.deepseek.com/v1"])

    def test_profile_without_key_never_joins(self):
        config = SimpleNamespace()
        config.get = lambda key, default=None: default
        chain = fallback_profiles_from_config(
            config, active_base_url="https://open.bigmodel.cn/api/anthropic")
        self.assertEqual(chain, [])


if __name__ == "__main__":
    unittest.main()

"""Tests for per-tier model routing on OpenAI-compatible multi-model servers."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from sonder_runtime.adapters import tool_lane_guard
from sonder_runtime.adapters.inference import openai_compat_gateway as gw
from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.model_gateway import ModelRequest


class OpenAICompatTierModelsTests(unittest.TestCase):
    def test_parse_tier_models_and_model_for_tier(self):
        parsed = gw.parse_tier_models(
            "fast=bonsai-9b-mtp, general=bonsai-27b-pq2, CODE = bonsai-27b-pq2 , invalid"
        )
        self.assertEqual(parsed, {
            "fast": "bonsai-9b-mtp",
            "general": "bonsai-27b-pq2",
            "code": "bonsai-27b-pq2",
        })
        cfg = gw.OpenAICompatibleConfig(
            base_url="http://127.0.0.1:18080",
            model="fallback-model",
            tier_models=parsed,
        )
        self.assertEqual(cfg.model_for_tier("fast"), "bonsai-9b-mtp")
        self.assertEqual(cfg.model_for_tier("code"), "bonsai-27b-pq2")
        self.assertEqual(cfg.model_for_tier("reasoning"), "fallback-model")

    def test_gateway_sends_per_tier_model_id(self):
        sent_models: list[str] = []

        def fake_transport(url, payload, headers, timeout):
            sent_models.append(payload["model"])
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }

        env = {
            "SONDER_OPENAI_BASE_URL": "http://127.0.0.1:18080",
            "SONDER_OPENAI_MODEL": "bonsai-27b-pq2",
            "SONDER_OPENAI_TIER_MODELS": "fast=bonsai-9b-mtp,general=bonsai-27b-pq2,code=bonsai-27b-pq2",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            gateway = gw.OpenAICompatibleGateway(transport=fake_transport)
            ctx = OperationContext("req-1", "local", "owner", "test", None, None)
            r_fast = gateway.generate(ModelRequest(prompt="hi", tier="fast"), ctx)
            r_code = gateway.generate(ModelRequest(prompt="code", tier="code"), ctx)
            self.assertEqual(r_fast.model, "bonsai-9b-mtp")
            self.assertEqual(r_code.model, "bonsai-27b-pq2")
            self.assertEqual(sent_models, ["bonsai-9b-mtp", "bonsai-27b-pq2"])

    def test_tool_lane_guard_reads_openai_tier_models(self):
        env = {
            "SONDER_FAST_PROVIDER": "openai-compatible",
            "SONDER_GENERAL_PROVIDER": "openai-compatible",
            "SONDER_CODE_PROVIDER": "openai-compatible",
            "SONDER_REASONING_PROVIDER": "ollama",
            "SONDER_VISION_PROVIDER": "ollama",
            "SONDER_OPENAI_MODEL": "bonsai-27b-pq2",
            "SONDER_OPENAI_TIER_MODELS": "fast=bonsai-9b-mtp,code=bonsai-27b-pq2",
        }
        self.assertEqual(tool_lane_guard.served_model("fast", env), ("openai_compatible", "bonsai-9b-mtp"))
        self.assertEqual(tool_lane_guard.served_model("code", env), ("openai_compatible", "bonsai-27b-pq2"))
        self.assertEqual(tool_lane_guard.served_model("general", env), ("openai_compatible", "bonsai-27b-pq2"))


if __name__ == "__main__":
    unittest.main()

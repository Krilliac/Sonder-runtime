"""Tests for optional security, creative, and draft specialist tiers."""
from __future__ import annotations

import unittest

from sonder_runtime.adapters import model_lists
from sonder_runtime.adapters.provider_bindings import (
    PROVIDER_TIERS,
    ProviderBindings,
    provider_bindings_from_env,
)
from sonder_runtime.domain.routing import capability_router


class SpecialistTiersTests(unittest.TestCase):
    def setUp(self):
        self.router_cfg = model_lists.load().values["capability_router"]

    def test_provider_bindings_supports_specialist_tiers_and_legacy_five(self):
        for tier in ("security", "creative", "draft"):
            self.assertIn(tier, PROVIDER_TIERS)
        bindings = provider_bindings_from_env({
            "SONDER_MODEL_BACKEND": "sonder-inference",
            "SONDER_SECURITY_PROVIDER": "sonder-inference",
            "SONDER_CREATIVE_PROVIDER": "openai-compatible",
            "SONDER_DRAFT_PROVIDER": "openai-compatible",
        })
        self.assertEqual(bindings.tier_providers["security"], "sonder_inference")
        self.assertEqual(bindings.tier_providers["creative"], "openai_compatible")
        self.assertEqual(bindings.tier_providers["draft"], "openai_compatible")
        # Legacy 5-tier direct construction remains accepted and defaults new tiers.
        legacy = ProviderBindings(
            "ollama",
            {t: "ollama" for t in ("fast", "general", "code", "reasoning", "vision")},
            "ollama",
        )
        self.assertEqual(legacy.tier_providers["security"], "ollama")

    def test_capability_router_routes_and_degrades_specialist_tiers(self):
        full = {"fast", "general", "code", "reasoning", "vision", "security", "creative", "draft"}
        base_only = {"fast", "general", "code"}

        task, _ = capability_router.classify_task(
            "Perform a threat model and vulnerability security audit on this auth flow",
            config=self.router_cfg,
        )
        self.assertEqual(task, "security")
        self.assertEqual(capability_router.recommend_tier(task, full, config=self.router_cfg), "security")
        self.assertEqual(capability_router.recommend_tier(task, base_only, config=self.router_cfg), "general")

        task_c, _ = capability_router.classify_task(
            "Write a story and character backstory with rich lore for a ruined citadel",
            config=self.router_cfg,
        )
        self.assertEqual(task_c, "creative")
        self.assertEqual(capability_router.recommend_tier(task_c, full, config=self.router_cfg), "creative")
        self.assertEqual(capability_router.recommend_tier(task_c, base_only, config=self.router_cfg), "general")

        task_d, _ = capability_router.classify_task(
            "Draft a quick commit message and boilerplate changelog entry",
            config=self.router_cfg,
        )
        self.assertEqual(task_d, "draft")
        self.assertEqual(capability_router.recommend_tier(task_d, full, config=self.router_cfg), "draft")
        self.assertEqual(capability_router.recommend_tier(task_d, base_only, config=self.router_cfg), "general")


if __name__ == "__main__":
    unittest.main()

"""WP3 SEAM-015: scoped replacement policy tests."""
from __future__ import annotations

import pytest

from sonder_runtime.application.provider_overrides import ProviderOverrideService
from sonder_runtime.domain.provider_override_policy import ProviderOverridePolicy


def test_override_replaces_only_inside_its_scope():
    policy = ProviderOverridePolicy({"default": "ollama", "hosted": "openai"})
    scoped = policy.with_override("coding", "default", "local-code")

    assert scoped.resolve("default", ["coding"]) == "local-code"
    assert scoped.resolve("default", ["chat"]) == "ollama"
    assert scoped.resolve("hosted", ["coding"]) == "openai"


def test_policy_changes_do_not_mutate_source_or_create_global_state():
    source = ProviderOverridePolicy({"default": "ollama"})
    changed = source.with_override("agent-a", "default", "test-double")

    assert source.resolve("default", ["agent-a"]) == "ollama"
    assert changed.resolve("default", ["agent-a"]) == "test-double"
    assert ProviderOverridePolicy({"default": "ollama"}).resolve(
        "default", ["agent-a"]
    ) == "ollama"


def test_resolution_uses_explicit_most_specific_scope_order():
    policy = (
        ProviderOverridePolicy({"default": "ollama"})
        .with_override("preset", "default", "preset-provider")
        .with_override("agent", "default", "agent-provider")
    )

    assert policy.resolve("default", ("agent", "preset")) == "agent-provider"
    assert policy.resolve("default", ("preset", "agent")) == "preset-provider"


def test_duplicate_override_is_rejected_and_unknown_provider_fails_closed():
    policy = ProviderOverridePolicy({"default": "ollama"}).with_override(
        "agent", "default", "test-double"
    )
    with pytest.raises(ValueError, match="already exists"):
        policy.with_override("agent", "default", "other")
    with pytest.raises(KeyError, match="unknown provider"):
        policy.resolve("missing", ["agent"])


def test_application_service_returns_new_local_service():
    service = ProviderOverrideService(ProviderOverridePolicy({"default": "ollama"}))
    scoped = service.replace("agent-a", "default", "test-double")

    assert service.resolve("default", ["agent-a"]) == "ollama"
    assert scoped.resolve("default", ["agent-a"]) == "test-double"

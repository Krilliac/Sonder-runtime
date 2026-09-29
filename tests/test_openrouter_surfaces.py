"""OpenRouter operator surfaces: runtime-policy tier mapping, CLI, MCP tools,
composition and the "unconfigured changes nothing" parity guarantees."""
from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters.model_gateway_factory import build_model_gateway
from sonder_runtime.adapters.inference.openrouter_gateway import OpenRouterGateway
from sonder_runtime.adapters.provider_bindings import (
    PROVIDER_LABEL_IDS,
    ProviderBindings,
    normalize_provider,
    provider_bindings_from_env,
)
from sonder_runtime.bootstrap import openrouter_cli, openrouter_tools
from sonder_runtime.domain.runtime_identity import runtime_identity_fields


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(path))
    return path


def test_aliases_select_openrouter_only_when_named():
    assert normalize_provider("openrouter") == "openrouter"
    assert normalize_provider("open-router") == "openrouter"
    assert PROVIDER_LABEL_IDS["openrouter"] == "openrouter"
    default = provider_bindings_from_env({})
    assert "openrouter" not in default.bound_providers
    bound = provider_bindings_from_env({"SONDER_CODE_PROVIDER": "open-router"})
    assert bound.tier_providers["code"] == "openrouter"
    assert bound.tier_providers["fast"] == "ollama"


def test_unconfigured_composition_never_constructs_openrouter(monkeypatch):
    built = []

    class Spy:
        def __init__(self, *args, **kwargs):
            built.append(1)

    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.openrouter_gateway.OpenRouterGateway", Spy,
    )
    gateway = build_model_gateway(ProviderBindings.uniform("ollama"))
    assert built == [] and type(gateway).__name__ == "OllamaGateway"


def test_bound_tier_composes_openrouter_behind_dispatch():
    bindings = provider_bindings_from_env({"SONDER_CODE_PROVIDER": "openrouter"})
    gateway = build_model_gateway(bindings)
    assert type(gateway).__name__ == "ProviderDispatchGateway"
    assert isinstance(gateway._providers["openrouter"], OpenRouterGateway)


def test_policy_provider_models_round_trip_and_shape(policy_file):
    runtime_policy.update(provider_models={"openrouter": {"code": "qwen/qwen3-coder"}})
    on_disk = json.loads(policy_file.read_text(encoding="utf-8"))
    assert on_disk["provider_models"] == {"openrouter": {"code": "qwen/qwen3-coder"}}
    # Local tiers are untouched and still refuse cloud names.
    assert on_disk["local_models"]["code"] == "sonder:latest"
    with pytest.raises(ValueError, match="cloud"):
        runtime_policy.update(local_models={"code": "qwen3-coder:480b-cloud"})
    runtime_policy.update(provider_models={"openrouter": {"fast": "google/gemini-2.5-flash"}})
    loaded = runtime_policy.load(create=False)
    assert loaded["provider_models"]["openrouter"] == {
        "fast": "google/gemini-2.5-flash", "code": "qwen/qwen3-coder",
    }
    runtime_policy.update(provider_models={"openrouter": {"code": "", "fast": ""}})
    on_disk = json.loads(policy_file.read_text(encoding="utf-8"))
    assert "provider_models" not in on_disk  # cleared maps leave the old shape
    assert "openrouter tier models" not in runtime_policy.format_policy()


def test_policy_refuses_bad_provider_models(policy_file):
    for bad in ({"ollama": {"code": "x/y"}}, {"openrouter": {"turbo": "x/y"}},
                {"openrouter": {"code": "no-slash"}}):
        with pytest.raises(ValueError):
            runtime_policy.update(provider_models=bad)


def test_policy_without_section_keeps_its_existing_shape(policy_file):
    runtime_policy.update(local_models={"fast": "qwen3:4b"})
    on_disk = json.loads(policy_file.read_text(encoding="utf-8"))
    assert set(on_disk) == {"version", "revision", "local_models", "embedding_model",
                            "routing", "npu", "long_context_overflow", "updated_ts", "source"}


def test_cli_use_writes_policy_and_explains_binding(policy_file, capsys):
    parser = __import__("sonder_runtime.__main__", fromlist=["build_parser"]).build_parser()
    args = parser.parse_args(["openrouter", "use", "code", "qwen/qwen3-coder", "--json"])
    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["tier"] == "code" and result["model"] == "qwen/qwen3-coder"
    assert any("SONDER_CODE_PROVIDER=openrouter" in note for note in result["notes"])
    assert runtime_policy.load(create=False)["provider_models"]["openrouter"]["code"] == "qwen/qwen3-coder"
    # The gateway reads the mapping from the shared policy.
    gateway = OpenRouterGateway(env={"SONDER_ALLOW_CLOUD": "1", "OPENROUTER_API_KEY": "k"})
    assert gateway.served_tier_models()["openrouter"] == {"code": "qwen/qwen3-coder"}
    args = parser.parse_args(["openrouter", "use", "code", "none"])
    assert args.func(args) == 0
    assert "(cleared)" in capsys.readouterr().out


def test_cli_use_respects_the_deployment_transition_lock(policy_file, capsys):
    runtime_policy.reserve_transition({"transition_id": "t1", "policy_token": "secret-token"})
    parser = __import__("sonder_runtime.__main__", fromlist=["build_parser"]).build_parser()
    args = parser.parse_args(["openrouter", "use", "code", "qwen/qwen3-coder"])
    assert args.func(args) == 2
    assert "blocked by active model deployment" in capsys.readouterr().err


def test_cli_use_rejects_invalid_model_without_writing(policy_file, capsys):
    parser = __import__("sonder_runtime.__main__", fromlist=["build_parser"]).build_parser()
    args = parser.parse_args(["openrouter", "use", "code", "gpt4"])
    assert args.func(args) == 2
    assert "vendor/model" in capsys.readouterr().err
    assert not policy_file.exists() or "provider_models" not in policy_file.read_text()


def test_cli_reads_refuse_without_cloud(monkeypatch, capsys):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "0")
    parser = __import__("sonder_runtime.__main__", fromlist=["build_parser"]).build_parser()
    for argv in (["openrouter", "models"], ["openrouter", "account"]):
        args = parser.parse_args(argv)
        assert args.func(args) == 2
        assert "SONDER_ALLOW_CLOUD" in capsys.readouterr().err


def test_cli_formatters():
    text = openrouter_cli.format_models({"source": "account", "total": 1, "models": [{
        "id": "a/b", "context_length": 1000, "prompt_usd_per_million": 3.0,
        "completion_usd_per_million": 15.0, "supports_tools": True,
        "supports_structured_outputs": False,
    }]})
    assert "a/b" in text and "3.00" in text and "tools" in text
    text = openrouter_cli.format_account({
        "credits_remaining": 20.5, "total_credits": 50.0, "total_usage": 29.5,
        "limit": None, "limit_remaining": None, "usage": 1.0, "usage_daily": 0.0,
        "usage_weekly": 0.0, "usage_monthly": 1.0, "is_free_tier": False,
    })
    assert "$20.5000" in text and "unlimited" in text


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


def test_mcp_tools_register_and_respect_consent(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "0")
    recorded = []
    mcp = _FakeMcp()
    openrouter_tools.register(mcp, lambda name, args, **kw: recorded.append((name, kw["ok"])))
    assert set(mcp.tools) == {"openrouter_models", "openrouter_account"}
    for name in mcp.tools:
        payload = json.loads(mcp.tools[name]())
        assert payload["ok"] is False and "SONDER_ALLOW_CLOUD" in payload["error"]
    assert recorded == [("openrouter_models", False), ("openrouter_account", False)]


def test_mcp_payloads_bound_the_listing():
    class Gateway:
        def list_models(self, search, tools):
            return {"source": "public", "total": 300, "count": 300,
                    "models": [{"id": "v/m%d" % i} for i in range(300)]}

        def account(self):
            return {"credits_remaining": 1.0}

    listing = openrouter_tools.models_payload(limit=10_000, gateway=Gateway())
    assert len(listing["models"]) == openrouter_tools.MAX_LISTED and listing["truncated"]
    assert openrouter_tools.account_payload(gateway=Gateway()) == {"ok": True, "credits_remaining": 1.0}


def test_new_tools_are_graded_read_only_network_reads():
    import permission_modes

    for name in ("openrouter_models", "openrouter_account"):
        assert permission_modes.risk_of(name) == permission_modes.risk_of("web_fetch") == "safe"


def test_prompt_identity_never_claims_a_local_model_for_openrouter():
    fields = runtime_identity_fields("anthropic/claude-sonnet-4", provider="openrouter")
    assert "OpenRouter" in fields["where"] and "this machine" in fields["where"]
    assert "not on this machine" in fields["where"]

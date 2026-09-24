"""Regressions found by static audit (Sep 2026).

Each case failed with NameError or a wrong planner bucket before its fix.
"""

import builtins

import pytest

import bootstrap_engine
from sonder_runtime.bootstrap import config_loading


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("qwen2.5-coder:1.5b", ("1.5b", 1.5)),
        ("qwen2.5-coder:3b", ("3b", 3.0)),
        ("qwen2.5-coder:7b", ("7b", 7.0)),
        # Substring matching used to plan these as 7b / 3b.
        ("qwen3.8:27b-q6", ("27b", 27.0)),
        ("ornith-1.5:35b-a3b", ("35b", 35.0)),
        ("custom-7b-model:3b", ("3b", 3.0)),
        ("custom-27b-model:3b", ("3b", 3.0)),
        ("custom-7b-model:latest", ("auto", None)),
        ("Qwen/Qwen2.5-Coder-7B-Instruct", ("7b", 7.0)),
        ("registry.example:11434/qwen:3b", ("3b", 3.0)),
        ("qwen:7b-a3b", ("7b", 7.0)),
        ("sonder:latest", ("auto", None)),
        ("", ("auto", None)),
    ],
)
def test_planner_size_uses_total_parameters_from_the_tag(requested, expected):
    assert bootstrap_engine._planner_size_for(requested) == expected


@pytest.mark.parametrize(
    ("requested", "expected_model", "expected_params"),
    [
        ("qwen:7b", "7b", 7.0),
        ("qwen:27b-q6", "27b", 27.0),
        ("ornith:35b-a3b", "35b", 35.0),
        # The repository/tag suffix is authoritative over misleading name text.
        ("custom-27b-model:3b", "3b", 3.0),
        ("Qwen/Qwen2.5-Coder-7B-Instruct", "7b", 7.0),
        ("sonder:latest", "auto", None),
    ],
)
def test_bootstrap_main_passes_exact_model_metadata_to_planner(
    monkeypatch, requested, expected_model, expected_params,
):
    """The CLI must forward parsed total parameters before runtime I/O."""
    class Planned(Exception):
        pass

    hardware = object()
    monkeypatch.setattr(bootstrap_engine, "_load_bundle", lambda args: None)
    monkeypatch.setattr(
        bootstrap_engine.system_profile, "detect_hardware", lambda: hardware,
    )
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "8192")

    def plan(observed_hardware, options):
        assert observed_hardware is hardware
        assert options.model == expected_model
        assert options.requested_model == requested
        assert options.parameter_billions == expected_params
        raise Planned

    monkeypatch.setattr(bootstrap_engine.adaptive_training, "build_plan", plan)
    with pytest.raises(Planned):
        bootstrap_engine.main([
            "--dry-run", "--model", requested,
            "--max-vram", "8", "--max-system-ram", "16",
        ])


def test_check_config_skip_callable_survives_import_failure(monkeypatch):
    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name.startswith("sonder_runtime.adapters.config_validation"):
            raise ImportError("simulated missing validator")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    check = config_loading.check_config()
    monkeypatch.setattr(builtins, "__import__", real_import)

    result = check()
    assert result["status"] == "skipped"
    assert "simulated missing validator" in result["detail"]


def test_goal_adopt_formats_adopted_goal(monkeypatch):
    import goal_store
    import server

    adopted = {"id": "g-1", "objective": "ship it", "status": "active"}
    monkeypatch.setattr(goal_store, "adopt", lambda goal_id, actor: adopted)

    out = server._goal_command("adopt g-1")
    assert out.startswith("adopted\n")
    assert "ship it" in out


def test_ports_package_exports_both_cleanup_results_unambiguously():
    from sonder_runtime.application import ports
    from sonder_runtime.application.ports import execution_world, specialized_lifecycle

    assert ports.CleanupResult is specialized_lifecycle.CleanupResult
    assert ports.ExecutionWorldCleanupResult is execution_world.CleanupResult
    assert ports.__all__.count("CleanupResult") == 1


def test_star_import_surfaces_resolve():
    namespace = {}
    exec("from sonder_runtime.application.agent_registry.unified import *", namespace)
    exec("from sonder_runtime.application.compaction import *", namespace)
    assert "UnifiedAgentRegistryService" in namespace


def test_context_policy_prefers_declared_server_kv_type(monkeypatch):
    from sonder_runtime.platform import context_policy

    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q8_0")
    monkeypatch.delenv("SONDER_KV_CACHE_TYPE", raising=False)
    assert context_policy.kv_cache_type() == ("q8_0", "client-environment")

    monkeypatch.setenv("SONDER_KV_CACHE_TYPE", "f16")
    assert context_policy.kv_cache_type() == ("f16", "declared")
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    assert context_policy.default_context() == context_policy.DEFAULT_CONTEXT_FP16_KV


def test_residency_ceiling_never_overrides_an_operator_pin(monkeypatch):
    from sonder_runtime.platform import context_policy

    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "32k")
    plan = context_policy.auto_context_plan(262144, "7B", residency_ceiling=4096)
    assert plan["context"] == 32000
    monkeypatch.delenv("SONDER_CONTEXT_SIZE")
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    plan = context_policy.auto_context_plan(262144, "7B", residency_ceiling=4096)
    assert plan["context"] == 4096
    assert "observed-spill" in plan["clamps"]

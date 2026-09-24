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
        ("qwen2.5-coder:1.5b", "1.5b"),
        ("qwen2.5-coder:3b", "3b"),
        ("qwen2.5-coder:7b", "7b"),
        # Substring matching used to plan these as 7b / 3b.
        ("qwen3.8:27b-q6", "auto"),
        ("ornith-1.5:35b-a3b", "auto"),
        ("sonder:latest", "auto"),
        ("", "auto"),
    ],
)
def test_planner_size_matches_parameter_token_exactly(requested, expected):
    assert bootstrap_engine._planner_size_for(requested) == expected


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("qwen:7b", "7b"), ("qwen:27b-q6", "auto"), ("ornith:35b-a3b", "auto")],
)
def test_bootstrap_passes_parameter_bucket_to_planner(monkeypatch, requested, expected):
    """The real CLI must send the corrected bucket before any runtime action."""
    class Planned(Exception):
        pass

    hardware = object()
    monkeypatch.setattr(bootstrap_engine, "_load_bundle", lambda args: None)
    monkeypatch.setattr(bootstrap_engine.system_profile, "detect_hardware", lambda: hardware)
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "8192")

    def plan(observed_hardware, options):
        assert observed_hardware is hardware
        assert options.model == expected
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

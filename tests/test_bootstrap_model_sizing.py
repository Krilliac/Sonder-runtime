"""Bootstrap planner sizing reads total parameters from the explicit tag.

Ported from open PRs #558/#560. Before the fix, a substring scan planned
``27b`` as ``7b``, an MoE ``35b-a3b`` as ``3b``, and let a repository name
such as ``custom-7b-model:3b`` override its tag.
"""

import pytest

import bootstrap_engine
from sonder_runtime.application.training.hardware_planning import build_plan
from system_profile import HardwareProfile


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("qwen2.5-coder:1.5b", ("1.5b", 1.5)),
        ("qwen2.5-coder:3b", ("3b", 3.0)),
        ("qwen2.5-coder:7b", ("7b", 7.0)),
        # Substring matching used to plan these as 7b / 3b.
        ("gemma3:27b", ("27b", 27.0)),
        ("qwen3.8:27b-q6", ("27b", 27.0)),
        ("qwen3:30b-a3b", ("30b", 30.0)),
        ("ornith-1.5:35b-a3b", ("35b", 35.0)),
        # The tag after the final ':' is authoritative over repository text.
        ("custom-7b-model:3b", ("3b", 3.0)),
        ("custom-27b-model:3b", ("3b", 3.0)),
        ("custom-7b-model:latest", ("auto", None)),
        # Tagless names keep token-anchored compatibility.
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


def test_bootstrap_dry_run_plans_a_large_tag_by_its_own_size(monkeypatch, capsys):
    """A 27b request reaches the real planner without being read as 7b."""
    hardware = HardwareProfile(
        os_name="Linux", architecture="x86_64",
        system_ram_total_gb=128.0, system_ram_available_gb=128.0,
        gpu_vendor="nvidia", gpu_name="mock", cuda_available=True,
        rocm_available=False, vram_total_gb=48.0, vram_free_gb=48.0,
        compute_capability="8.9", cpu_offload_supported=True,
    )
    captured = {}

    def plan(observed_hardware, options):
        captured["plan"] = build_plan(observed_hardware, options)
        return captured["plan"]

    monkeypatch.setattr(bootstrap_engine, "_load_bundle", lambda args: None)
    monkeypatch.setattr(
        bootstrap_engine.system_profile, "detect_hardware", lambda: hardware,
    )
    monkeypatch.setattr(bootstrap_engine.adaptive_training, "build_plan", plan)
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "8192")

    assert bootstrap_engine.main(["--dry-run", "--model", "gemma3:27b"]) == 0

    inference = captured["plan"].inference
    assert inference.model_size == "27b"
    assert inference.model == "gemma3:27b"
    assert "selected model: gemma3:27b" in capsys.readouterr().out

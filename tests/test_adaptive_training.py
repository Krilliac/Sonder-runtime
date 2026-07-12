import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import adaptive_training
import runtime_policy
from system_profile import HardwareProfile


def profile(vram=0, ram=32, *, free_vram=None, available_ram=None, vendor="nvidia", cuda=True):
    return HardwareProfile(
        os_name="Linux",
        architecture="x86_64",
        system_ram_total_gb=ram,
        system_ram_available_gb=ram if available_ram is None else available_ram,
        gpu_vendor=vendor if vram else "none",
        gpu_name="mock GPU" if vram else "",
        cuda_available=cuda if vram else False,
        rocm_available=vendor == "amd",
        vram_total_gb=vram,
        vram_free_gb=vram if free_vram is None else free_vram,
        compute_capability="8.9" if vram else "",
        cpu_offload_supported=bool(vram and (cuda or vendor == "amd")),
    )


@pytest.mark.parametrize(
    "vram,ram,expected",
    [
        (4, 8, "1.5b"),
        (6, 16, "1.5b"),
        (8, 16, "3b"),
        (12, 32, "7b"),
        (16, 32, "7b"),
        (24, 64, "7b"),
    ],
)
def test_training_matrix(vram, ram, expected):
    plan = adaptive_training.build_plan(profile(vram, ram))
    assert plan.training.enabled
    assert plan.training.model_size == expected
    assert plan.training.method == "QLoRA (4-bit NF4)"


@pytest.mark.parametrize("ram", [8, 16, 32, 64])
def test_cpu_only_allows_inference_but_disables_training(ram):
    plan = adaptive_training.build_plan(profile(0, ram, vendor="none", cuda=False))
    assert plan.inference.enabled
    assert not plan.training.enabled
    assert "CUDA" in " ".join(plan.training.rejected)


def test_low_available_ram_wins_over_high_total():
    plan = adaptive_training.build_plan(profile(16, 64, available_ram=10))
    assert not plan.training.enabled
    assert plan.usable_system_ram_gb == 0
    assert any("RAM" in reason for reason in plan.training.rejected)


def test_low_free_vram_wins_over_high_total():
    plan = adaptive_training.build_plan(profile(24, 64, free_vram=5))
    assert plan.training.model_size == "1.5b"
    assert plan.usable_vram_gb == 3


def test_unsupported_gpu_runtime_disables_training():
    plan = adaptive_training.build_plan(profile(16, 64, vendor="amd", cuda=False))
    assert not plan.training.enabled
    assert "supported NVIDIA CUDA" in plan.training.rejected[0]


def test_explicit_model_and_memory_overrides_are_enforced():
    options = adaptive_training.PlanOptions(model="7b", max_vram_gb=8, max_system_ram_gb=20)
    plan = adaptive_training.build_plan(profile(24, 64), options)
    assert not plan.training.enabled
    assert plan.usable_vram_gb == 8


def test_cpu_offload_requires_both_opt_in_and_support():
    host = profile(12, 64, free_vram=11.5)
    without = adaptive_training.build_plan(host, adaptive_training.PlanOptions(model="7b"))
    with_offload = adaptive_training.build_plan(
        host, adaptive_training.PlanOptions(model="7b", allow_cpu_offload=True)
    )
    unsupported = adaptive_training.build_plan(
        HardwareProfile(**{**host.to_dict(), "cpu_offload_supported": False}),
        adaptive_training.PlanOptions(model="7b", allow_cpu_offload=True),
    )
    assert not without.training.enabled
    assert with_offload.training.enabled and with_offload.training.cpu_offload
    assert not unsupported.training.enabled


def test_dense_training_is_never_automatic_and_must_fit():
    normal = adaptive_training.build_plan(profile(24, 64))
    dense = adaptive_training.build_plan(
        profile(24, 64), adaptive_training.PlanOptions(model="1.5b", full_finetune=True)
    )
    assert normal.training.method.startswith("QLoRA")
    assert not dense.training.enabled
    assert any("Dense" in item for item in dense.training.rejected)


def test_dense_feasibility_report_never_enters_qlora_runner():
    dense = adaptive_training.build_plan(
        profile(48, 64), adaptive_training.PlanOptions(model="1.5b", full_finetune=True)
    )
    calls = []
    ok, message = adaptive_training.start_training(
        dense, confirmed=True, runner=lambda *args, **kwargs: calls.append(args)
    )
    assert dense.training.enabled
    assert dense.training.method.startswith("full-parameter")
    assert dense.training.estimated_vram_gb == 28
    assert not ok and "feasibility report only" in message
    assert calls == []


def _adapter(tmp_path, *, config_base, manifest_base, ollama_base="qwen2.5-coder:1.5b"):
    path = tmp_path / "adapter"
    path.mkdir()
    (path / "adapter_config.json").write_text(json.dumps({
        "base_model_name_or_path": config_base,
    }), encoding="utf-8")
    (path / "training-manifest.json").write_text(json.dumps({
        "base_hf": manifest_base,
        "base_ollama": ollama_base,
        "model_size": "1.5b",
    }), encoding="utf-8")
    return path


def test_invalid_adapter_base_combination_is_rejected(tmp_path):
    adapter = _adapter(
        tmp_path,
        config_base="Qwen/Qwen2.5-Coder-3B-Instruct",
        manifest_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    ok, reason = adaptive_training.validate_adapter(adapter)
    assert not ok
    assert "mismatch" in reason


def test_failed_candidate_deployment_preserves_runtime_policy(monkeypatch, tmp_path):
    policy_path = tmp_path / "runtime-policy.json"
    state_path = tmp_path / "training-state.json"
    monkeypatch.setenv("TRILOBITE_RUNTIME_POLICY", str(policy_path))
    monkeypatch.setenv("TRILOBITE_TRAINING_STATE", str(state_path))
    runtime_policy.load(create=True)
    adapter = _adapter(
        tmp_path,
        config_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
        manifest_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    converter = tmp_path / "convert_lora_to_gguf.py"
    converter.write_text("# mock", encoding="utf-8")

    def runner(command, **kwargs):
        if str(converter) in command:
            output = Path(command[command.index("--outfile") + 1])
            output.write_bytes(b"G" * 2048)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1:3] == ["run", command[2]]:
            return SimpleNamespace(returncode=1, stdout="", stderr="failed")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    ok, message = adaptive_training.deploy(adapter, converter=str(converter), runner=runner)
    assert not ok
    assert "validation failed" in message
    policy = runtime_policy.load(create=False)
    assert policy["local_models"]["code"] == adaptive_training.ROLLBACK_MODEL
    assert policy["local_models"]["general"] == adaptive_training.ROLLBACK_MODEL


def test_successful_deployment_activates_both_tiers_after_inference(monkeypatch, tmp_path):
    monkeypatch.setenv("TRILOBITE_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("TRILOBITE_TRAINING_STATE", str(tmp_path / "training-state.json"))
    monkeypatch.setattr(adaptive_training.shutil, "which", lambda name: None)
    adapter = _adapter(
        tmp_path,
        config_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
        manifest_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    converter = tmp_path / "convert_lora_to_gguf.py"
    converter.write_text("# mock", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if str(converter) in command:
            Path(command[command.index("--outfile") + 1]).write_bytes(b"G" * 2048)
        return SimpleNamespace(returncode=0, stdout="TRILOBITE_VALID", stderr="")

    ok, message = adaptive_training.deploy(adapter, converter=str(converter), runner=runner)
    policy = runtime_policy.load(create=False)
    assert ok and "Validated and deployed" in message
    assert policy["local_models"]["code"] == adaptive_training.PERSONAL_MODEL
    assert policy["local_models"]["general"] == adaptive_training.PERSONAL_MODEL
    assert ["ollama", "show", "qwen2.5-coder:1.5b"] in calls
    assert any(command[1:3] == ["run", adaptive_training.PERSONAL_MODEL] for command in calls)


def test_rollback_updates_both_tiers_without_deleting_personal_model(monkeypatch, tmp_path):
    monkeypatch.setenv("TRILOBITE_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("TRILOBITE_TRAINING_STATE", str(tmp_path / "training-state.json"))
    runtime_policy.update(
        local_models={"code": adaptive_training.PERSONAL_MODEL, "general": adaptive_training.PERSONAL_MODEL}
    )
    ok, message = adaptive_training.rollback()
    policy = runtime_policy.load(create=False)
    assert ok
    assert policy["local_models"]["code"] == adaptive_training.ROLLBACK_MODEL
    assert policy["local_models"]["general"] == adaptive_training.ROLLBACK_MODEL
    assert "not deleted" in message


def test_training_start_requires_confirmation_and_dry_run_never_runs():
    plan = adaptive_training.build_plan(profile(8, 32))
    calls = []
    ok, message = adaptive_training.start_training(plan, runner=lambda *a, **k: calls.append(a))
    assert not ok and "--confirm" in message
    ok, message = adaptive_training.start_training(plan, dry_run=True, runner=lambda *a, **k: calls.append(a))
    assert ok and "no training process started" in message
    assert calls == []


def test_minimal_mocked_training_flow_builds_command_and_validates(monkeypatch, tmp_path):
    data = tmp_path / "training.jsonl"
    data.write_text('{"messages":[{"role":"user","content":"x"},{"role":"assistant","content":"y"}]}\n', encoding="utf-8")
    output = tmp_path / "lora"
    monkeypatch.setenv("TRILOBITE_DATA", str(data))
    monkeypatch.setenv("TRILOBITE_LORA_OUT", str(output))
    monkeypatch.setenv("TRILOBITE_TRAINING_STATE", str(tmp_path / "state.json"))
    seen = {}

    def runner(command, **kwargs):
        seen.update(command=command, env=kwargs["env"], cwd=kwargs["cwd"])
        (output / "adapter_config.json").write_text(json.dumps({
            "base_model_name_or_path": "Qwen/Qwen2.5-Coder-3B-Instruct",
        }), encoding="utf-8")
        plan_manifest = json.loads((output / "training-plan.json").read_text(encoding="utf-8"))
        (output / "training-manifest.json").write_text(json.dumps(plan_manifest), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    plan = adaptive_training.build_plan(profile(8, 32))
    ok, message = adaptive_training.start_training(plan, confirmed=True, runner=runner)
    assert ok and "completed" in message
    assert seen["command"][-1].endswith("qlora_train.py")
    assert seen["env"]["TRILOBITE_BASE"] == "Qwen/Qwen2.5-Coder-3B-Instruct"
    assert seen["env"]["TRILOBITE_ALLOW_CPU_OFFLOAD"] == "0"
    assert json.loads((tmp_path / "state.json").read_text())["status"] == "trained"

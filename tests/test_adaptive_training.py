import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import adaptive_training
import qlora_train
import runtime_policy
import system_profile
from system_profile import HardwareProfile


def test_training_lifecycle_uses_only_sonder_ollama_aliases():
    assert adaptive_training.ROLLBACK_MODEL == "sonder:latest"
    assert adaptive_training.PERSONAL_MODEL == "sonder-personal:latest"


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


def test_cpu_offload_request_fails_closed_for_current_training_backend():
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
    assert not with_offload.training.enabled
    assert adaptive_training.TRAINING_CPU_OFFLOAD_REASON in with_offload.training.rejected
    assert not unsupported.training.enabled

    direct_fit = adaptive_training.build_plan(
        profile(24, 64),
        adaptive_training.PlanOptions(model="1.5b", allow_cpu_offload=True),
    )
    assert not direct_fit.training.enabled
    assert adaptive_training.TRAINING_CPU_OFFLOAD_REASON in direct_fit.training.rejected


def test_direct_qlora_invocation_fails_before_heavy_imports(monkeypatch):
    monkeypatch.delenv("SONDER_TRAINING_MANIFEST", raising=False)
    monkeypatch.delenv("SONDER_TRAINING_LAUNCH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="training start --confirm"):
        qlora_train.main()


def test_authorized_qlora_cpu_offload_request_fails_before_heavy_imports(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SONDER_ALLOW_CPU_OFFLOAD", "1")
    data = tmp_path / "training.jsonl"
    data.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "runs" / "run-1" / "adapter"
    output.mkdir(parents=True)
    token = "test-token"
    manifest = output.parent / "training-plan.json"
    payload = {
        "schema": 2,
        "run_id": "run-1",
        "created_ts": 100,
        "base_hf": qlora_train.BASE,
        "data_path": str(data.resolve()),
        "data_sha256": __import__("hashlib").sha256(data.read_bytes()).hexdigest(),
        "adapter_dir": str(output.resolve()),
        "gpu_index": 0,
        "launch_token_sha256": __import__("hashlib").sha256(token.encode()).hexdigest(),
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SONDER_TRAINING_MANIFEST", str(manifest))
    monkeypatch.setenv("SONDER_TRAINING_LAUNCH_TOKEN", token)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setattr(qlora_train, "DATA_PATH", str(data))
    monkeypatch.setattr(qlora_train, "OUTPUT_DIR", str(output))
    monkeypatch.setattr(qlora_train.time, "time", lambda: 100)
    assert qlora_train.main() == 5
    assert "CPU offload is disabled" in capsys.readouterr().out


def _mock_hardware_detection(monkeypatch):
    monkeypatch.setattr(system_profile, "_system_memory", lambda: (64.0, 48.0, True))
    monkeypatch.setattr(system_profile, "_rocm_profile", lambda: None)
    for name in (
        "SONDER_GPU_VENDOR",
        "SONDER_VRAM_GB",
        "SONDER_FREE_VRAM_GB",
        "SONDER_CUDA_AVAILABLE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_explicit_zero_free_vram_is_preserved_and_disables_training(monkeypatch):
    _mock_hardware_detection(monkeypatch)
    monkeypatch.setattr(system_profile, "_nvidia_profile", lambda: None)
    monkeypatch.setenv("SONDER_GPU_VENDOR", "nvidia")
    monkeypatch.setenv("SONDER_VRAM_GB", "24")
    monkeypatch.setenv("SONDER_FREE_VRAM_GB", "0")
    monkeypatch.setenv("SONDER_CUDA_AVAILABLE", "1")

    detected = system_profile.detect_hardware()

    assert detected.vram_free_gb == 0
    assert detected.vram_availability_live
    assert not adaptive_training.build_plan(detected).training.enabled


def test_live_zero_free_vram_is_not_replaced_by_fallback(monkeypatch):
    _mock_hardware_detection(monkeypatch)
    monkeypatch.setattr(
        system_profile,
        "_nvidia_profile",
        lambda: ("fully occupied GPU", 24.0, 0.0, "8.9"),
    )

    detected = system_profile.detect_hardware()

    assert detected.vram_free_gb == 0
    assert detected.vram_availability_live


def test_total_only_vram_uses_marked_conservative_fallback(monkeypatch):
    _mock_hardware_detection(monkeypatch)
    monkeypatch.setattr(system_profile, "_nvidia_profile", lambda: None)
    monkeypatch.setenv("SONDER_GPU_VENDOR", "nvidia")
    monkeypatch.setenv("SONDER_VRAM_GB", "24")
    monkeypatch.setenv("SONDER_CUDA_AVAILABLE", "1")

    detected = system_profile.detect_hardware()

    assert detected.vram_free_gb == 18
    assert not detected.vram_availability_live


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
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_path))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(state_path))
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
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
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
        return SimpleNamespace(returncode=0, stdout="SONDER_VALID", stderr="")

    ok, message = adaptive_training.deploy(adapter, converter=str(converter), runner=runner)
    policy = runtime_policy.load(create=False)
    assert ok and "Validated and deployed" in message
    assert policy["local_models"]["code"] == adaptive_training.PERSONAL_MODEL
    assert policy["local_models"]["general"] == adaptive_training.PERSONAL_MODEL
    assert ["ollama", "show", "qwen2.5-coder:1.5b"] in calls
    assert any(command[1:3] == ["run", adaptive_training.PERSONAL_MODEL] for command in calls)
    assert any(command[1:3] == ["cp", adaptive_training.PERSONAL_MODEL] for command in calls)
    assert any(
        command[1:2] == ["cp"]
        and "candidate" in command[2]
        and command[3] == adaptive_training.PERSONAL_MODEL
        for command in calls
    )
    removed = {
        command[2] for command in calls if command[1:2] == ["rm"]
    }
    assert "qwen2.5-coder:1.5b" not in removed
    assert adaptive_training.ROLLBACK_MODEL not in removed


def test_deployment_rejects_non_marker_output_and_cleans_candidate(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
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
        output = "wrong marker" if command[1:2] == ["run"] else "ok"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    assert not ok and "exact marker" in message
    assert any(command[1:2] == ["rm"] for command in calls)
    assert not any(command[1:2] == ["cp"] for command in calls)


def test_failed_final_probe_restores_previous_personal_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    adapter = _adapter(
        tmp_path,
        config_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
        manifest_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    converter = tmp_path / "convert_lora_to_gguf.py"
    converter.write_text("# mock", encoding="utf-8")
    calls = []
    run_count = 0

    def runner(command, **kwargs):
        nonlocal run_count
        calls.append(command)
        if str(converter) in command:
            Path(command[command.index("--outfile") + 1]).write_bytes(b"G" * 2048)
        if command[1:2] == ["run"]:
            run_count += 1
            output = "SONDER_VALID" if run_count == 1 else "wrong"
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    assert not ok and "previous personal alias" in message and "restored" in message
    copies = [command for command in calls if command[1:2] == ["cp"]]
    previous = next(command[3] for command in copies if command[2] == adaptive_training.PERSONAL_MODEL)
    assert ["ollama", "cp", previous, adaptive_training.PERSONAL_MODEL] in calls
    assert any(command[1:2] == ["rm"] and "candidate" in command[2] for command in calls)


def test_final_probe_timeout_restores_active_alias_and_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    runtime_policy.update(local_models={
        "code": adaptive_training.PERSONAL_MODEL,
        "general": adaptive_training.PERSONAL_MODEL,
    })
    adapter = _adapter(
        tmp_path,
        config_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
        manifest_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    converter = tmp_path / "convert_lora_to_gguf.py"
    converter.write_text("# mock", encoding="utf-8")
    calls = []
    run_count = 0

    def runner(command, **kwargs):
        nonlocal run_count
        calls.append(command)
        if str(converter) in command:
            Path(command[command.index("--outfile") + 1]).write_bytes(b"G" * 2048)
        if command[1:2] == ["run"]:
            run_count += 1
            if run_count == 2:
                raise adaptive_training.subprocess.TimeoutExpired(command, 120)
            return SimpleNamespace(returncode=0, stdout="SONDER_VALID", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    policy = runtime_policy.load(create=False)
    assert not ok and "restored" in message
    assert policy["local_models"]["code"] == adaptive_training.PERSONAL_MODEL
    assert policy["local_models"]["general"] == adaptive_training.PERSONAL_MODEL
    copies = [command for command in calls if command[1:2] == ["cp"]]
    previous = next(command[3] for command in copies if command[2] == adaptive_training.PERSONAL_MODEL)
    assert ["ollama", "cp", previous, adaptive_training.PERSONAL_MODEL] in calls


def test_deployment_lock_rejects_concurrent_promotion(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    with adaptive_training._deployment_lock():
        ok, message = adaptive_training.deploy(tmp_path)
    assert not ok
    assert "already running" in message


def test_rollback_updates_both_tiers_without_deleting_personal_model(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
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
    monkeypatch.setenv("SONDER_DATA", str(data))
    monkeypatch.setenv("SONDER_LORA_OUT", str(output))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "state.json"))
    seen = {}

    def runner(command, **kwargs):
        seen.update(command=command, env=kwargs["env"], cwd=kwargs["cwd"])
        adapter = Path(kwargs["env"]["SONDER_LORA_OUT"])
        (adapter / "adapter_config.json").write_text(json.dumps({
            "base_model_name_or_path": "Qwen/Qwen2.5-Coder-3B-Instruct",
        }), encoding="utf-8")
        plan_manifest = json.loads(Path(kwargs["env"]["SONDER_TRAINING_MANIFEST"]).read_text(encoding="utf-8"))
        (adapter / "training-manifest.json").write_text(json.dumps(plan_manifest), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    plan = adaptive_training.build_plan(
        profile(8, 32), adaptive_training.PlanOptions(gpu_index=2)
    )
    ok, message = adaptive_training.start_training(plan, confirmed=True, runner=runner)
    assert ok and "completed" in message
    assert seen["command"][-1].endswith("qlora_train.py")
    assert seen["env"]["SONDER_BASE"] == "Qwen/Qwen2.5-Coder-3B-Instruct"
    assert seen["env"]["SONDER_ALLOW_CPU_OFFLOAD"] == "0"
    assert seen["env"]["CUDA_VISIBLE_DEVICES"] == "2"
    assert Path(seen["env"]["SONDER_LORA_OUT"]).parent.parent.parent == output
    assert json.loads((tmp_path / "state.json").read_text())["status"] == "trained"


def test_resume_requires_proven_interrupted_or_failed_run(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "state.json"))
    plan = adaptive_training.build_plan(profile(8, 32))
    ok, message = adaptive_training.start_training(
        plan, confirmed=True, resume=True, runner=lambda *args, **kwargs: None
    )
    assert not ok
    assert "interrupted or failed" in message


def _create_failed_training_run(monkeypatch, tmp_path):
    data = tmp_path / "training.jsonl"
    data.write_text(
        '{"messages":[{"role":"user","content":"x"},{"role":"assistant","content":"y"}]}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SONDER_DATA", str(data))
    monkeypatch.setenv("SONDER_LORA_OUT", str(tmp_path / "lora"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "state.json"))
    plan = adaptive_training.build_plan(profile(8, 32))
    ok, _message = adaptive_training.start_training(
        plan,
        confirmed=True,
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    assert not ok
    assert json.loads((tmp_path / "state.json").read_text())["status"] == "failed"
    return plan, data


def test_resume_rejects_changed_dataset_content(monkeypatch, tmp_path):
    plan, data = _create_failed_training_run(monkeypatch, tmp_path)
    data.write_text(data.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    called = []
    ok, message = adaptive_training.start_training(
        plan, confirmed=True, resume=True,
        runner=lambda *args, **kwargs: called.append(args),
    )
    assert not ok
    assert "dataset changed" in message
    assert called == []


def test_resume_rejects_changed_dataset_path(monkeypatch, tmp_path):
    plan, data = _create_failed_training_run(monkeypatch, tmp_path)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(data.read_bytes())
    monkeypatch.setenv("SONDER_DATA", str(replacement))
    called = []
    ok, message = adaptive_training.start_training(
        plan, confirmed=True, resume=True,
        runner=lambda *args, **kwargs: called.append(args),
    )
    assert not ok
    assert "dataset path" in message
    assert called == []


def test_programmatic_start_rejects_negative_gpu_index():
    plan = adaptive_training.build_plan(
        profile(8, 32), adaptive_training.PlanOptions(gpu_index=-1)
    )
    ok, message = adaptive_training.start_training(plan, confirmed=True)
    assert not ok
    assert "GPU index" in message

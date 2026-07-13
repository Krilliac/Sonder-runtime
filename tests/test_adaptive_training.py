import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from types import SimpleNamespace

import pytest

import adaptive_training
import qlora_train
import runtime_policy
import system_profile
from system_profile import HardwareProfile


STAGE_REVIEWED_CONVERTER = adaptive_training._stage_reviewed_converter
SHARED_ALIAS_PATHS = adaptive_training._shared_alias_paths


def test_ollama_external_runner_uses_canonical_client_environment(monkeypatch):
    captured = []

    def runner(command, **kwargs):
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    result = adaptive_training._run_external(
        runner,
        ["ollama-test", "show", "model"],
        ollama_command=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert captured[0][1]["env"]["OLLAMA_HOST"] == "http://127.0.0.1:11434"


def test_ollama_external_runner_blocks_unapproved_remote_before_subprocess(
    monkeypatch,
):
    calls = []
    monkeypatch.setenv("OLLAMA_HOST", "http://models.example.test:11434")
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_OLLAMA", raising=False)

    result = adaptive_training._run_external(
        lambda *args, **kwargs: calls.append(1),
        ["ollama-test", "show", "model"],
        ollama_command=True,
    )

    assert result.returncode == 125
    assert "blocked" in result.stderr.lower()
    assert calls == []


@pytest.fixture(autouse=True)
def isolated_sonder_home(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "sonder-home"))
    monkeypatch.setattr(
        adaptive_training.promotion_eval,
        "local_model_digest",
        lambda _model: "a" * 64,
    )
    monkeypatch.setattr(
        adaptive_training,
        "_stage_reviewed_converter",
        lambda path, _destination, runner=None: (
            True,
            {
                "converter": str(path),
                "revision": adaptive_training.LLAMA_CPP_REVISION,
                "tree_sha256": "b" * 64,
                "staged_manifest_sha256": "c" * 64,
            },
        ),
    )
    shared_prefix = tmp_path / "shared" / "ollama-test"
    monkeypatch.setattr(
        adaptive_training,
        "_shared_alias_paths",
        lambda: {
            "origin": "loopback:11434",
            "transport_origin": "http://127.0.0.1:11434",
            "lock": shared_prefix.with_name(shared_prefix.name + ".alias.lock"),
            "transition": shared_prefix.with_name(
                shared_prefix.name + ".alias-transition.json"
            ),
            "owner": shared_prefix.with_name(shared_prefix.name + ".alias-owner.json"),
        },
    )


def test_training_lifecycle_uses_only_sonder_ollama_aliases():
    assert adaptive_training.ROLLBACK_MODEL == "sonder:latest"
    assert adaptive_training.PERSONAL_MODEL == "sonder-personal:latest"


def test_relative_training_state_path_is_canonical_across_working_directories(
    monkeypatch, tmp_path,
):
    first = tmp_path / "cwd-a"
    second = tmp_path / "cwd-b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("SONDER_TRAINING_STATE", "relative-state.json")
    monkeypatch.chdir(first)
    recorded = adaptive_training.state_path()
    monkeypatch.chdir(second)

    assert recorded.is_absolute()
    assert recorded == first / "relative-state.json"
    assert Path(str(recorded)).resolve() == recorded


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
    data.write_text(
        '{"messages":[{"role":"user","content":"question"},'
        '{"role":"assistant","content":"answer"}]}\n',
        encoding="utf-8",
    )
    output = tmp_path / "runs" / "run-1" / "adapter"
    output.mkdir(parents=True)
    token = "test-token"
    manifest = output.parent / "training-plan.json"
    payload = {
        "schema": 2,
        "run_id": "run-1",
        "created_ts": 100,
        "base_hf": qlora_train.BASE,
        "hf_revision": qlora_train.HF_REVISION,
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


def _trusted_adapter(
    monkeypatch, tmp_path, *,
    config_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    manifest_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    ollama_base="qwen2.5-coder:1.5b",
):
    run_id = "run-1"
    run_dir = tmp_path / "training" / "runs" / run_id
    adapter = run_dir / "adapter"
    adapter.mkdir(parents=True)
    data = tmp_path / "training.jsonl"
    data.write_text(
        '{"messages":[{"role":"user","content":"question"},'
        '{"role":"assistant","content":"answer"}]}\n',
        encoding="utf-8",
    )
    data_snapshot = run_dir / "training-data.jsonl"
    data_snapshot.write_bytes(data.read_bytes())
    data_inspection = adaptive_training.training_data.inspect_jsonl(data_snapshot)
    config = adapter / "adapter_config.json"
    weights = adapter / "adapter_model.safetensors"
    config.write_text(json.dumps({"base_model_name_or_path": config_base}), encoding="utf-8")
    weights.write_bytes(b"trusted-adapter-weights")
    artifacts = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (config, weights)
    }
    sizes = {path.name: path.stat().st_size for path in (config, weights)}
    plan = {
        "schema": 2,
        "run_id": run_id,
        "base_hf": manifest_base,
        "hf_revision": adaptive_training.MODEL_SPECS["1.5b"]["hf_revision"],
        "base_ollama": ollama_base,
        "model_size": "1.5b",
        "method": "QLoRA (4-bit NF4)",
        "data_path": str(data_snapshot.resolve()),
        "data_sha256": data_inspection.sha256,
        "data_examples": len(data_inspection.examples),
        "data_bytes": data_inspection.file_bytes,
        "data_content_chars": data_inspection.content_chars,
        "data_source": "explicit",
        "source_data_path": str(data.resolve()),
        "source_data_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
        "selection_manifest_path": "",
        "selection_manifest_sha256": "",
        "adapter_dir": str(adapter.resolve()),
        "gpu_index": 0,
        "created_ts": 100,
        "launch_consumed_ts": 101,
    }
    plan_path = run_dir / "training-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    manifest = {
        **plan,
        "completed_ts": 102,
        "artifact_sha256": artifacts,
        "artifact_sizes": sizes,
    }
    manifest_path = adapter / "training-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    state_path = tmp_path / "training-state.json"
    state = {
        "status": "trained",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "adapter_dir": str(adapter),
        "plan_file": str(plan_path),
        "manifest": str(manifest_path),
        "base_hf": manifest_base,
        "hf_revision": plan["hf_revision"],
        "base_ollama": ollama_base,
        "data_sha256": plan["data_sha256"],
        "data_examples": plan["data_examples"],
        "data_bytes": plan["data_bytes"],
        "data_content_chars": plan["data_content_chars"],
        "selection_manifest_sha256": "",
        "source_data_sha256": plan["source_data_sha256"],
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "artifact_sha256": artifacts,
        "artifact_sizes": sizes,
        "rollback_model": adaptive_training.ROLLBACK_MODEL,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(state_path))
    return adapter, state_path


def _converter(tmp_path):
    path = tmp_path / "convert_lora_to_gguf.py"
    path.write_text("# mock", encoding="utf-8")
    return path


def test_converter_is_materialized_from_expected_git_objects(monkeypatch, tmp_path):
    converter = _converter(tmp_path)
    archive_files = {"convert_lora_to_gguf.py": b"# sealed\n"}
    archive_files.update({
        f"conversion/module_{index}.py": b"# sealed\n" for index in range(107)
    })

    def git_blob_id(content):
        return hashlib.sha1(
            f"blob {len(content)}\0".encode("ascii") + content
        ).hexdigest()

    tree_output = "".join(
        f"100644 blob {git_blob_id(content)}\t{name}\n"
        for name, content in sorted(archive_files.items())
    )
    monkeypatch.setattr(
        adaptive_training,
        "LLAMA_CPP_TREE_SHA256",
        hashlib.sha256(tree_output.encode()).hexdigest(),
    )

    seen_env = []

    def runner(command, **kwargs):
        seen_env.append(kwargs["env"])
        arguments = command[3:]
        if arguments == ["rev-parse", "--show-toplevel"]:
            output = str(tmp_path)
        elif arguments[:2] == ["cat-file", "-e"]:
            output = ""
        elif arguments[:3] == ["ls-tree", "-r", "--full-tree"]:
            output = tree_output
        elif arguments[:2] == ["archive", "--format=zip"]:
            archive = Path(next(
                value.split("=", 1)[1]
                for value in arguments if value.startswith("--output=")
            ))
            with zipfile.ZipFile(archive, "w") as bundle:
                for name, content in archive_files.items():
                    bundle.writestr(name, content)
            output = ""
        else:
            return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    ok, provenance = STAGE_REVIEWED_CONVERTER(
        converter, tmp_path / "staged", runner=runner,
    )

    assert ok
    assert provenance["revision"] == adaptive_training.LLAMA_CPP_REVISION
    assert len(provenance["tree_sha256"]) == 64
    assert len(provenance["staged_manifest_sha256"]) == 64
    assert Path(provenance["converter"]).is_file()
    assert all(env["GIT_NO_REPLACE_OBJECTS"] == "1" for env in seen_env)
    assert all(env["GIT_CONFIG_COUNT"] == "1" for env in seen_env)
    assert all(env["GIT_CONFIG_KEY_0"] == "core.autocrlf" for env in seen_env)
    assert all(env["GIT_CONFIG_VALUE_0"] == "false" for env in seen_env)


def _activate_personal_policy(local_models=None):
    token = "test-personal-transition"
    _current, journal = runtime_policy.reserve_transition({
        "schema": 1,
        "deployment_id": "test-personal-policy",
        "policy_token": token,
    })
    try:
        return runtime_policy.update(
            local_models=local_models or {
                "code": adaptive_training.PERSONAL_MODEL,
                "general": adaptive_training.PERSONAL_MODEL,
            },
            transition_token=token,
        )
    finally:
        runtime_policy.finish_transition(journal["deployment_id"], token)


def test_converter_tree_must_match_hardcoded_reviewed_seal(tmp_path):
    converter = _converter(tmp_path)

    def runner(command, **kwargs):
        assert kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
        arguments = command[3:]
        output = ""
        if arguments == ["rev-parse", "--show-toplevel"]:
            output = str(tmp_path)
        elif arguments[:2] == ["ls-tree", "-r"]:
            output = (
                "100644 blob " + "a" * 40
                + "\tconvert_lora_to_gguf.py\n"
            )
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    ok, reason = STAGE_REVIEWED_CONVERTER(
        converter, tmp_path / "staged", runner=runner,
    )

    assert not ok and "seal does not match" in reason


def test_converter_archive_bytes_must_match_sealed_git_blobs(monkeypatch, tmp_path):
    converter = _converter(tmp_path)
    files = {"convert_lora_to_gguf.py": b"# reviewed\n"}
    files.update({f"conversion/module_{index}.py": b"# reviewed\n" for index in range(107)})

    def blob_id(content):
        return hashlib.sha1(
            f"blob {len(content)}\0".encode("ascii") + content
        ).hexdigest()

    tree_output = "".join(
        f"100644 blob {blob_id(content)}\t{name}\n"
        for name, content in sorted(files.items())
    )
    monkeypatch.setattr(
        adaptive_training,
        "LLAMA_CPP_TREE_SHA256",
        hashlib.sha256(tree_output.encode()).hexdigest(),
    )

    def runner(command, **_kwargs):
        arguments = command[3:]
        if arguments == ["rev-parse", "--show-toplevel"]:
            output = str(tmp_path)
        elif arguments[:2] == ["cat-file", "-e"]:
            output = ""
        elif arguments[:3] == ["ls-tree", "-r", "--full-tree"]:
            output = tree_output
        elif arguments[:2] == ["archive", "--format=zip"]:
            archive = Path(next(
                value.split("=", 1)[1]
                for value in arguments if value.startswith("--output=")
            ))
            with zipfile.ZipFile(archive, "w") as bundle:
                for name, content in files.items():
                    bundle.writestr(
                        name,
                        b"# changed after seal\n"
                        if name == "convert_lora_to_gguf.py" else content,
                    )
            output = ""
        else:
            return SimpleNamespace(returncode=1, stdout="", stderr="unexpected")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    ok, reason = STAGE_REVIEWED_CONVERTER(
        converter, tmp_path / "staged", runner=runner,
    )

    assert not ok and "blob does not match sealed Git object" in reason


def _model_report(model, passed_ids):
    task_ids = [task.task_id for task in adaptive_training.promotion_eval.TASKS]
    passed_ids = set(passed_ids)
    return {
        "model": model,
        "score": len(passed_ids),
        "total": len(task_ids),
        "tasks": [
            {
                "id": task_id,
                "passed": task_id in passed_ids,
                "reason": "passed" if task_id in passed_ids else "wrong_result",
                "artifact_sha256": hashlib.sha256(task_id.encode()).hexdigest(),
            }
            for task_id in task_ids
        ],
    }


def _pair_report(candidate, *, candidate_passes=None, challenge=""):
    task_ids = [task.task_id for task in adaptive_training.promotion_eval.TASKS]
    candidate_passes = set(task_ids) if candidate_passes is None else set(candidate_passes)
    base_report = _model_report("qwen2.5-coder:1.5b", set(task_ids) - {task_ids[2]})
    candidate_report = _model_report(candidate, candidate_passes)
    if challenge:
        for report in (base_report, candidate_report):
            report["tasks"].append({
                "id": adaptive_training.promotion_eval.STRUCTURED_TASK_ID,
                "passed": True,
                "reason": "passed",
                "artifact_sha256": "d" * 64,
            })
            report["score"] += 1
            report["total"] += 1
    return {
        "schema": adaptive_training.promotion_eval.REPORT_SCHEMA,
        "suite_version": adaptive_training.promotion_eval.SUITE_VERSION,
        "suite_hash": adaptive_training.promotion_eval.SUITE_HASH,
        "challenge_hash": hashlib.sha256(challenge.encode()).hexdigest(),
        "options": dict(adaptive_training.promotion_eval.INFERENCE_OPTIONS),
        "base": base_report,
        "candidate": candidate_report,
    }


def _stub_promotion(monkeypatch, *, candidate_passes=None, final_pass=True):
    monkeypatch.setattr(
        adaptive_training.promotion_eval,
        "evaluate_pair",
        lambda base, candidate, challenge="": _pair_report(
            candidate, candidate_passes=candidate_passes, challenge=challenge,
        ),
    )

    def evaluate_model(model, *, task_ids=None, challenge="", **_kwargs):
        ids = list(task_ids or [task.task_id for task in adaptive_training.promotion_eval.TASKS])
        if challenge and task_ids is None:
            ids.append(adaptive_training.promotion_eval.STRUCTURED_TASK_ID)
        return {
            "model": model,
            "score": len(ids) if final_pass else 0,
            "total": len(ids),
            "tasks": [
                {"id": task_id, "passed": final_pass, "reason": "passed" if final_pass else "wrong_result",
                 "artifact_sha256": hashlib.sha256(task_id.encode()).hexdigest()}
                for task_id in ids
            ],
        }

    monkeypatch.setattr(adaptive_training.promotion_eval, "evaluate_model", evaluate_model)
    monkeypatch.setattr(
        adaptive_training.promotion_eval,
        "local_model_digest",
        lambda model: (
            "b" * 64 if model == "qwen2.5-coder:1.5b" else "c" * 64
        ),
    )


def _ollama_runner(
    converter, calls, *, personal_exists=True, intercept=None, register_owner=True,
):
    models = {"qwen2.5-coder:1.5b": "base-id"}
    if personal_exists:
        models[adaptive_training.PERSONAL_MODEL] = "previous-personal-id"
    if personal_exists and register_owner:
        adaptive_training._write_shared_alias_owner(
            deployment_id="test-fixture-owner",
            identity=hashlib.sha256(b"previous-personal-id").hexdigest(),
            digest=adaptive_training.promotion_eval.local_model_digest(
                adaptive_training.PERSONAL_MODEL
            ),
        )
    elif not personal_exists:
        with contextlib.suppress(OSError):
            adaptive_training._shared_alias_paths()["owner"].unlink()

    def runner(command, **_kwargs):
        calls.append(command)
        if (
            len(command) >= 7
            and command[1:3] == ["-I", "-c"]
            and "hf_hub_download" in command[3]
        ):
            target = Path(command[6]) / "config.json"
            target.write_text(
                json.dumps({"model_type": "qwen2", "_name_or_path": command[4]}),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "--outfile" in command and any(
            "convert_lora_to_gguf.py" in str(item) for item in command
        ):
            Path(command[command.index("--outfile") + 1]).write_bytes(b"G" * 2048)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if intercept:
            result = intercept(command, models)
            if result is not None:
                return result
        action = command[1] if len(command) > 1 else ""
        if action == "show":
            model = command[2]
            if model in models:
                return SimpleNamespace(returncode=0, stdout=models[model], stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr=f"Error: model '{model}' not found")
        if action == "create":
            models[command[2]] = "candidate-id:" + command[2]
        elif action == "cp":
            if command[2] not in models:
                return SimpleNamespace(returncode=1, stdout="", stderr="source missing")
            models[command[3]] = models[command[2]]
        elif action == "rm":
            models.pop(command[2], None)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    return runner, models


def test_invalid_adapter_base_combination_is_rejected(tmp_path):
    monkeypatch = pytest.MonkeyPatch()
    adapter, _ = _trusted_adapter(
        monkeypatch, tmp_path,
        config_base="Qwen/Qwen2.5-Coder-3B-Instruct",
        manifest_base="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    try:
        ok, reason = adaptive_training.validate_adapter(adapter)
        assert not ok
        assert "mismatch" in reason
    finally:
        monkeypatch.undo()


def test_malformed_adapter_completion_timestamp_fails_closed(monkeypatch, tmp_path):
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    manifest_path = adapter / "training-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["completed_ts"] = {"not": "an integer"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    ok, reason = adaptive_training.validate_adapter(adapter)

    assert not ok and "timestamps are invalid" in reason


def test_deploy_rejects_adapter_not_bound_to_current_training_run(monkeypatch, tmp_path):
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    copied = tmp_path / "copied-adapter"
    __import__("shutil").copytree(adapter, copied)
    calls = []

    ok, message = adaptive_training.deploy(copied, runner=lambda *args, **kwargs: calls.append(args))

    assert not ok and "completed trusted run" in message
    assert calls == []


def test_deploy_rejects_tampered_adapter_weights_before_conversion(monkeypatch, tmp_path):
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    (adapter / "adapter_model.safetensors").write_bytes(b"tampered")
    calls = []

    ok, message = adaptive_training.deploy(adapter, runner=lambda *args, **kwargs: calls.append(args))

    assert not ok and "integrity" in message
    assert calls == []


def test_deploy_rejects_tampered_training_manifest_before_conversion(monkeypatch, tmp_path):
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    manifest_path = adapter / "training-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["data_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    calls = []

    ok, message = adaptive_training.deploy(
        adapter, runner=lambda *args, **kwargs: calls.append(args)
    )

    assert not ok and "provenance integrity" in message
    assert calls == []


def test_failed_candidate_deployment_preserves_runtime_policy(monkeypatch, tmp_path):
    policy_path = tmp_path / "runtime-policy.json"
    state_path = tmp_path / "training-state.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_path))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(state_path))
    runtime_policy.load(create=True)
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(
        monkeypatch,
        candidate_passes={task.task_id for task in adaptive_training.promotion_eval.TASKS[:3]},
    )
    calls = []
    runner, models = _ollama_runner(converter, calls)

    ok, message = adaptive_training.deploy(adapter, converter=str(converter), runner=runner)
    assert not ok
    assert "behavior evaluation rejected" in message
    policy = runtime_policy.load(create=False)
    assert policy["local_models"]["code"] == adaptive_training.ROLLBACK_MODEL
    assert policy["local_models"]["general"] == adaptive_training.ROLLBACK_MODEL


def test_successful_deployment_activates_both_tiers_after_inference(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    monkeypatch.setattr(adaptive_training.shutil, "which", lambda name: None)
    adapter, state_path = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    calls = []
    runner, _ = _ollama_runner(converter, calls)

    ok, message = adaptive_training.deploy(adapter, converter=str(converter), runner=runner)
    policy = runtime_policy.load(create=False)
    assert ok and "Behavior-validated and deployed" in message
    assert policy["local_models"]["code"] == adaptive_training.PERSONAL_MODEL
    assert policy["local_models"]["general"] == adaptive_training.PERSONAL_MODEL
    assert ["ollama", "show", "qwen2.5-coder:1.5b"] in calls
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
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["status"] == "deployed"
    assert saved["last_deployment_eval"]["candidate_score"] == 5
    assert not any("SONDER_VALID" in " ".join(command) for command in calls)
    assert not adaptive_training._deployment_journal_path().exists()


def test_ordinary_policy_update_is_blocked_during_alias_transition(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    _activate_personal_policy()
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    calls = []
    blocked = []
    journal_seen = []

    def intercept(command, _models):
        if (
            command[1:2] == ["cp"]
            and command[2] == adaptive_training.PERSONAL_MODEL
            and command[3].startswith("sonder-personal-previous:")
        ):
            journal = json.loads(
                adaptive_training._deployment_journal_path().read_text(encoding="utf-8")
            )
            journal_seen.append(journal)
            assert journal["previous_alias"] == command[3]
            try:
                runtime_policy.update(
                    local_models={
                        "code": adaptive_training.PERSONAL_MODEL,
                        "general": adaptive_training.PERSONAL_MODEL,
                    },
                    source="concurrent ordinary update",
                )
            except RuntimeError as exc:
                blocked.append(str(exc))
        return None

    runner, _models = _ollama_runner(converter, calls, intercept=intercept)

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    assert ok, message
    assert journal_seen
    assert blocked and "active model deployment" in blocked[0]


def test_second_policy_cannot_replace_global_personal_alias(monkeypatch, tmp_path):
    policy_a = tmp_path / "policy-a.json"
    policy_b = tmp_path / "policy-b.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_a))
    runtime_policy.load(create=True)
    adaptive_training._write_shared_alias_owner(
        deployment_id="policy-a-deployment",
        identity=hashlib.sha256(b"previous-personal-id").hexdigest(),
        digest="c" * 64,
    )

    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_b))
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    calls = []
    runner, models = _ollama_runner(
        converter, calls, register_owner=False,
    )

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner,
    )

    assert not ok and "owned by another runtime policy" in message
    assert models[adaptive_training.PERSONAL_MODEL] == "previous-personal-id"
    assert not any(
        command[1:2] == ["cp"]
        and command[2].startswith("sonder-personal-candidate:")
        and command[3] == adaptive_training.PERSONAL_MODEL
        for command in calls
    )
    assert runtime_policy.load(create=False)["local_models"]["code"] == (
        adaptive_training.ROLLBACK_MODEL
    )


def test_unowned_personal_alias_rejects_policy_and_foreign_state_self_assertion(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "policy-b.json"))
    with pytest.raises(ValueError, match="reserved for an active validated deployment"):
        runtime_policy.update(local_models={
            "code": adaptive_training.PERSONAL_MODEL,
        })
    prior = runtime_policy.load(create=True)

    ok, detail = adaptive_training._validate_shared_alias_owner(
        prior_policy=prior,
        personal_status="exists",
        personal_identity="1" * 64,
        personal_digest="2" * 64,
        state={"status": "deployed", "model": adaptive_training.PERSONAL_MODEL},
    )

    assert not ok
    assert "cannot be adopted implicitly" in detail
    assert not adaptive_training._shared_alias_paths()["owner"].exists()


def test_legacy_personal_alias_adoption_is_explicit_and_digest_bound(
    monkeypatch, tmp_path,
):
    policy = tmp_path / "runtime-policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy))
    calls = []
    converter = _converter(tmp_path)
    runner, _models = _ollama_runner(
        converter, calls, register_owner=False,
    )

    refused, message = adaptive_training.adopt_legacy_personal(
        confirmed=False, ollama="ollama", runner=runner,
    )
    ok, message = adaptive_training.adopt_legacy_personal(
        confirmed=True, ollama="ollama", runner=runner,
    )

    owner = json.loads(
        adaptive_training._shared_alias_paths()["owner"].read_text(encoding="utf-8")
    )
    assert not refused
    assert ok and "Adopted exact legacy" in message
    assert owner["policy_path"] == str(policy.resolve())
    assert owner["identity"] == hashlib.sha256(b"previous-personal-id").hexdigest()
    assert owner["digest"] == "a" * 64
    assert not any(command[1:2] in (["cp"], ["rm"]) for command in calls)


def test_alias_owner_release_requires_policy_to_leave_personal_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    adaptive_training._write_shared_alias_owner(
        deployment_id="owned-model",
        identity="a" * 64,
        digest="b" * 64,
    )
    _activate_personal_policy()

    blocked, detail = adaptive_training.release_personal_owner(confirmed=True)
    assert not blocked and "roll every tier off" in detail
    assert adaptive_training._shared_alias_paths()["owner"].exists()

    runtime_policy.update(
        local_models={
            "code": adaptive_training.ROLLBACK_MODEL,
            "general": adaptive_training.ROLLBACK_MODEL,
        }
    )
    released, detail = adaptive_training.release_personal_owner(confirmed=True)

    assert released and "without deleting or rerouting" in detail
    assert not adaptive_training._shared_alias_paths()["owner"].exists()


def test_first_deployment_requires_proven_missing_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    adapter, state_path = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    calls = []
    runner, models = _ollama_runner(converter, calls, personal_exists=False)

    ok, _message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    assert ok
    assert adaptive_training.PERSONAL_MODEL in models
    assert not any(
        command[1:2] == ["cp"]
        and command[2] == adaptive_training.PERSONAL_MODEL
        and command[3].startswith("sonder-personal-previous:")
        for command in calls
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["previous_personal_model"] == ""


def test_transient_personal_alias_probe_blocks_promotion_without_touching_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    calls = []

    def intercept(command, _models):
        if command[1:3] == ["show", adaptive_training.PERSONAL_MODEL]:
            return SimpleNamespace(returncode=1, stdout="", stderr="connection reset")
        return None

    runner, _ = _ollama_runner(converter, calls, intercept=intercept)

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    assert not ok and "could not determine" in message
    assert any(command[1:2] == ["rm"] and "candidate" in command[2] for command in calls)
    assert not any(command[1:2] == ["cp"] for command in calls)
    assert not any(command[1:] == ["rm", adaptive_training.PERSONAL_MODEL] for command in calls)


def test_successful_copy_with_wrong_published_identity_is_rolled_back(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    calls = []

    def intercept(command, _models):
        if (
            command[1:2] == ["cp"]
            and command[2].startswith("sonder-personal-candidate:")
            and command[3] == adaptive_training.PERSONAL_MODEL
        ):
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return None

    runner, models = _ollama_runner(converter, calls, intercept=intercept)

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    assert not ok and "identity did not match" in message
    assert models[adaptive_training.PERSONAL_MODEL] == "previous-personal-id"
    assert not any(model.startswith("sonder-personal-previous:") for model in models)


def test_failed_final_probe_restores_previous_personal_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch, final_pass=False)
    calls = []
    runner, models = _ollama_runner(converter, calls)

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    assert not ok and "Final behavior evaluation failed" in message and "restored" in message
    copies = [command for command in calls if command[1:2] == ["cp"]]
    previous = next(command[3] for command in copies if command[2] == adaptive_training.PERSONAL_MODEL)
    assert any(
        command[1:] == ["cp", previous, adaptive_training.PERSONAL_MODEL]
        for command in calls
    )
    assert any(command[1:2] == ["rm"] and "candidate" in command[2] for command in calls)
    assert previous not in models
    assert not adaptive_training._deployment_journal_path().exists()


def test_final_probe_timeout_restores_active_alias_and_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    _activate_personal_policy()
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    monkeypatch.setattr(
        adaptive_training.promotion_eval,
        "evaluate_model",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    calls = []
    runner, models = _ollama_runner(converter, calls)

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    policy = runtime_policy.load(create=False)
    assert not ok and "restored" in message
    assert policy["local_models"]["code"] == adaptive_training.PERSONAL_MODEL
    assert policy["local_models"]["general"] == adaptive_training.PERSONAL_MODEL
    copies = [command for command in calls if command[1:2] == ["cp"]]
    previous = next(command[3] for command in copies if command[2] == adaptive_training.PERSONAL_MODEL)
    assert any(
        command[1:] == ["cp", previous, adaptive_training.PERSONAL_MODEL]
        for command in calls
    )
    assert previous not in models


def test_deployment_state_commit_failure_restores_alias_and_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    calls = []
    runner, models = _ollama_runner(converter, calls)
    monkeypatch.setattr(
        adaptive_training, "_write_state",
        lambda payload: (_ for _ in ()).throw(OSError("disk full")),
    )

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    policy = runtime_policy.load(create=False)
    assert not ok and "state commit failed" in message and "restored" in message
    assert policy["local_models"]["code"] == adaptive_training.ROLLBACK_MODEL
    assert policy["local_models"]["general"] == adaptive_training.ROLLBACK_MODEL
    assert models[adaptive_training.PERSONAL_MODEL] == "previous-personal-id"
    assert not any(model.startswith("sonder-personal-previous:") for model in models)
    assert not adaptive_training._deployment_journal_path().exists()


def test_backup_digest_mismatch_blocks_personal_alias_publication(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    expected_digest = adaptive_training.promotion_eval.local_model_digest
    monkeypatch.setattr(
        adaptive_training.promotion_eval,
        "local_model_digest",
        lambda model: (
            "d" * 64
            if model.startswith("sonder-personal-previous:")
            else expected_digest(model)
        ),
    )
    calls = []
    runner, models = _ollama_runner(converter, calls)

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner,
    )

    assert not ok and "could not be preserved and verified" in message
    assert models[adaptive_training.PERSONAL_MODEL] == "previous-personal-id"
    assert not any(
        command[1:2] == ["cp"]
        and command[2].startswith("sonder-personal-candidate:")
        and command[3] == adaptive_training.PERSONAL_MODEL
        for command in calls
    )


def test_partial_candidate_creation_is_always_cleaned(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    calls = []
    cleanup_owned = []

    def intercept(command, models):
        if command[1:2] == ["create"]:
            ledger = json.loads(
                adaptive_training._cleanup_pending_path().read_text(encoding="utf-8")
            )
            cleanup_owned.append(command[2] in ledger["models"])
            models[command[2]] = "partially-created-id"
            return SimpleNamespace(returncode=1, stdout="", stderr="create failed late")
        return None

    runner, models = _ollama_runner(converter, calls, intercept=intercept)

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    assert not ok and "candidate creation failed" in message
    assert cleanup_owned == [True]
    assert not any(model.startswith("sonder-personal-candidate:") for model in models)
    assert any(command[1:2] == ["rm"] and "candidate" in command[2] for command in calls)
    assert adaptive_training.PERSONAL_MODEL in models
    assert not any(command[1:2] == ["cp"] for command in calls)
    assert not any(model.startswith("sonder-personal-previous:") for model in models)
    assert not adaptive_training._deployment_journal_path().exists()


def test_failed_alias_restore_routes_policy_to_safe_rollback(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    adapter, _ = _trusted_adapter(monkeypatch, tmp_path)
    converter = _converter(tmp_path)
    _stub_promotion(monkeypatch)
    calls = []

    def intercept(command, _models):
        if (
            command[1:2] == ["cp"]
            and command[2].startswith("sonder-personal-previous:")
            and command[3] == adaptive_training.PERSONAL_MODEL
        ):
            return SimpleNamespace(returncode=1, stdout="", stderr="restore failed")
        return None

    runner, models = _ollama_runner(converter, calls, intercept=intercept)
    monkeypatch.setattr(
        adaptive_training, "_write_state",
        lambda payload: (_ for _ in ()).throw(OSError("disk full")),
    )

    ok, message = adaptive_training.deploy(
        adapter, converter=str(converter), runner=runner
    )

    policy = runtime_policy.load(create=False)
    assert not ok and "recovery needs attention" in message.lower()
    assert policy["local_models"]["code"] == adaptive_training.ROLLBACK_MODEL
    assert policy["local_models"]["general"] == adaptive_training.ROLLBACK_MODEL
    assert any(model.startswith("sonder-personal-previous:") for model in models)
    assert adaptive_training._deployment_journal_path().exists()


def test_next_lifecycle_command_reconciles_interrupted_deployment(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    initial = runtime_policy.load(create=True)
    active = _activate_personal_policy()
    converter = _converter(tmp_path)
    calls = []
    runner, models = _ollama_runner(converter, calls)
    deployment_id = "crash-1234"
    candidate = f"sonder-personal-candidate:{deployment_id}"
    previous = f"sonder-personal-previous:{deployment_id}"
    models[candidate] = "candidate-id"
    models[adaptive_training.PERSONAL_MODEL] = "candidate-id"
    models[previous] = "previous-personal-id"
    journal = {
        "schema": 1,
        "deployment_id": deployment_id,
        "state_path": str(tmp_path / "missing-state.json"),
        "candidate_model": candidate,
        "candidate_identity": hashlib.sha256(b"candidate-id").hexdigest(),
        "candidate_digest": "a" * 64,
        "previous_alias": previous,
        "previous_identity": hashlib.sha256(b"previous-personal-id").hexdigest(),
        "personal_existed": True,
        "prior_models": initial["local_models"],
        "prior_policy_revision": initial["revision"],
        "last_policy_revision": active["revision"],
        "phase": "activated",
        "policy_path": str(runtime_policy.policy_path().resolve()),
        "policy_token": "test-transition-token",
    }
    adaptive_training._write_deployment_journal(journal)

    ok, message = adaptive_training._reconcile_pending_deployment(
        ollama="ollama", runner=runner
    )

    policy = runtime_policy.load(create=False)
    assert ok and "recovered" in message
    assert models[adaptive_training.PERSONAL_MODEL] == "previous-personal-id"
    assert previous not in models and candidate not in models
    assert policy["local_models"] == initial["local_models"]
    assert not adaptive_training._deployment_journal_path().exists()


def test_interrupted_recovery_hands_failed_alias_removal_to_cleanup_ledger(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    initial = runtime_policy.load(create=True)
    active = _activate_personal_policy()
    deployment_id = "cleanup-handoff"
    candidate = f"sonder-personal-candidate:{deployment_id}"
    previous = f"sonder-personal-previous:{deployment_id}"
    calls = []

    def intercept(command, _models):
        if command[1:2] == ["rm"] and command[2] in {candidate, previous}:
            return SimpleNamespace(returncode=1, stdout="", stderr="backend busy")
        return None

    converter = _converter(tmp_path)
    runner, models = _ollama_runner(converter, calls, intercept=intercept)
    models[candidate] = "candidate-id"
    models[adaptive_training.PERSONAL_MODEL] = "candidate-id"
    models[previous] = "previous-personal-id"
    adaptive_training._write_deployment_journal({
        "schema": 1,
        "operation": "deploy",
        "deployment_id": deployment_id,
        "state_path": str(tmp_path / "missing-state.json"),
        "candidate_model": candidate,
        "candidate_identity": hashlib.sha256(b"candidate-id").hexdigest(),
        "candidate_digest": "a" * 64,
        "previous_alias": previous,
        "previous_identity": hashlib.sha256(b"previous-personal-id").hexdigest(),
        "personal_existed": True,
        "prior_models": initial["local_models"],
        "prior_policy_revision": initial["revision"],
        "last_policy_revision": active["revision"],
        "phase": "activated",
        "policy_path": str(runtime_policy.policy_path().resolve()),
        "policy_token": "cleanup-token",
    })

    ok, message = adaptive_training._reconcile_pending_deployment(
        ollama="ollama", runner=runner,
    )

    ledger = json.loads(
        adaptive_training._cleanup_pending_path().read_text(encoding="utf-8")
    )
    assert not ok and "cleanup remains pending" in message
    assert set(ledger["models"]) == {candidate, previous}
    assert candidate in models and previous in models
    assert not adaptive_training._deployment_journal_path().exists()


def test_reserved_transition_recovers_before_backup_alias_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    initial = runtime_policy.load(create=True)
    deployment_id = "reserved-only"
    previous = f"sonder-personal-previous:{deployment_id}"
    converter = _converter(tmp_path)
    calls = []
    runner, models = _ollama_runner(converter, calls)
    adaptive_training._write_deployment_journal({
        "schema": 1,
        "operation": "deploy",
        "deployment_id": deployment_id,
        "state_path": str(tmp_path / "missing-state.json"),
        "candidate_model": f"sonder-personal-candidate:{deployment_id}",
        "candidate_identity": "c" * 64,
        "candidate_digest": "a" * 64,
        "previous_alias": previous,
        "previous_identity": hashlib.sha256(b"previous-personal-id").hexdigest(),
        "personal_existed": True,
        "prior_models": initial["local_models"],
        "prior_policy_revision": initial["revision"],
        "last_policy_revision": initial["revision"],
        "phase": "reserved",
        "policy_path": str(runtime_policy.policy_path().resolve()),
        "policy_token": "reserved-token",
    })

    ok, message = adaptive_training._reconcile_pending_deployment(
        ollama="ollama", runner=runner,
    )

    assert ok and "recovered" in message
    assert models[adaptive_training.PERSONAL_MODEL] == "previous-personal-id"
    assert not any(
        command[1:2] == ["cp"] and command[2] == previous for command in calls
    )
    assert not adaptive_training._deployment_journal_path().exists()


def test_recovery_does_not_overwrite_candidate_from_digest_mismatched_backup(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    runtime_policy.load(create=True)
    active = _activate_personal_policy()
    deployment_id = "corrupt-backup"
    candidate = f"sonder-personal-candidate:{deployment_id}"
    previous = f"sonder-personal-previous:{deployment_id}"
    converter = _converter(tmp_path)
    calls = []
    runner, models = _ollama_runner(converter, calls)
    models[candidate] = "candidate-id"
    models[adaptive_training.PERSONAL_MODEL] = "candidate-id"
    models[previous] = "previous-personal-id"
    monkeypatch.setattr(
        adaptive_training.promotion_eval,
        "local_model_digest",
        lambda model: "d" * 64 if model == previous else "a" * 64,
    )
    adaptive_training._write_deployment_journal({
        "schema": 1,
        "operation": "deploy",
        "deployment_id": deployment_id,
        "state_path": str(tmp_path / "missing-state.json"),
        "candidate_model": candidate,
        "candidate_identity": hashlib.sha256(b"candidate-id").hexdigest(),
        "candidate_digest": "a" * 64,
        "previous_alias": previous,
        "previous_identity": hashlib.sha256(b"previous-personal-id").hexdigest(),
        "previous_digest": "a" * 64,
        "personal_existed": True,
        "prior_models": active["local_models"],
        "prior_policy_revision": active["revision"],
        "last_policy_revision": active["revision"],
        "phase": "activated",
        "policy_path": str(runtime_policy.policy_path().resolve()),
        "policy_token": "corrupt-backup-token",
    })

    ok, message = adaptive_training._reconcile_pending_deployment(
        ollama="ollama", runner=runner,
    )

    policy = runtime_policy.load(create=False)
    assert not ok and "needs attention" in message
    assert models[adaptive_training.PERSONAL_MODEL] == "candidate-id"
    assert not any(
        command[1:] == ["cp", previous, adaptive_training.PERSONAL_MODEL]
        for command in calls
    )
    assert policy["local_models"]["code"] == adaptive_training.ROLLBACK_MODEL
    assert policy["local_models"]["general"] == adaptive_training.ROLLBACK_MODEL
    assert adaptive_training._deployment_journal_path().exists()


def test_malformed_journal_fails_closed_before_ollama_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    deployment_id = "bad-journal"
    adaptive_training._write_deployment_journal({
        "schema": 1,
        "deployment_id": deployment_id,
        "state_path": [],
        "candidate_model": f"sonder-personal-candidate:{deployment_id}",
        "previous_alias": "",
        "personal_existed": False,
        "prior_models": runtime_policy.DEFAULT_MODELS,
        "prior_policy_revision": "bad",
        "last_policy_revision": 0,
        "phase": "prepared",
        "policy_path": str(runtime_policy.policy_path().resolve()),
        "policy_token": "token",
    })
    calls = []

    ok, message = adaptive_training._reconcile_pending_deployment(
        ollama="ollama", runner=lambda *args, **kwargs: calls.append(args)
    )

    assert not ok and "invalid" in message
    assert calls == []


def test_shared_policy_transition_cannot_be_bypassed_with_different_home(monkeypatch, tmp_path):
    shared_policy = tmp_path / "shared-policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(shared_policy))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home-a"))
    _current, journal = runtime_policy.reserve_transition({
        "schema": 1,
        "deployment_id": "shared-policy-transition",
        "policy_token": "token",
    })
    first_marker = adaptive_training._deployment_journal_path()

    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home-b"))

    assert adaptive_training._deployment_journal_path() == first_marker
    with pytest.raises(RuntimeError, match="active model deployment"):
        runtime_policy.update(local_models={"code": "bypass:latest"})
    assert runtime_policy.finish_transition(
        journal["deployment_id"], journal["policy_token"]
    )


def test_cross_policy_alias_claim_persists_after_process_lock_release(monkeypatch, tmp_path):
    policy_a = tmp_path / "policy-a.json"
    policy_b = tmp_path / "policy-b.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_a))
    claim = adaptive_training._claim_shared_alias_transition(
        "crashed-deployment", "claim-token",
    )

    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_b))
    ready, detail = adaptive_training._prepare_shared_alias_lifecycle()

    assert not ready and "another runtime policy" in detail
    assert adaptive_training._shared_alias_paths()["transition"].exists()

    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_a))
    ready, detail = adaptive_training._prepare_shared_alias_lifecycle()
    assert ready and "orphaned alias claim cleared" in detail
    assert claim["phase"] == "claiming"
    assert not adaptive_training._shared_alias_paths()["transition"].exists()


def test_clearing_alias_marker_is_idempotent_after_policy_marker_finishes(
    monkeypatch, tmp_path,
):
    policy = tmp_path / "policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy))
    claim = adaptive_training._claim_shared_alias_transition(
        "clearing-deployment", "clearing-token",
    )
    _current, journal = runtime_policy.reserve_transition({
        "schema": 1,
        "operation": "deploy",
        "deployment_id": claim["deployment_id"],
        "policy_token": claim["policy_token"],
        "shared_alias_transition": True,
    })
    adaptive_training._advance_shared_alias_transition(claim, "clearing")
    assert runtime_policy.finish_transition(
        journal["deployment_id"], journal["policy_token"],
    )

    ready, detail = adaptive_training._prepare_shared_alias_lifecycle()

    assert ready and "orphaned alias claim cleared" in detail
    assert not adaptive_training._shared_alias_paths()["transition"].exists()


def test_foreign_owner_blocks_deploy_recovery_when_shared_marker_is_missing(
    monkeypatch, tmp_path,
):
    policy_a = tmp_path / "policy-a.json"
    policy_b = tmp_path / "policy-b.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_b))
    adaptive_training._write_shared_alias_owner(
        deployment_id="policy-b-owner",
        identity="a" * 64,
        digest="b" * 64,
    )
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_a))
    _current, journal = runtime_policy.reserve_transition({
        "schema": 1,
        "operation": "deploy",
        "deployment_id": "policy-a-pending",
        "policy_token": "policy-a-token",
        "shared_alias_transition": True,
    })

    ready, detail = adaptive_training._prepare_shared_alias_lifecycle()

    assert not ready and "foreign personal-alias owner" in detail
    assert runtime_policy.finish_transition(
        journal["deployment_id"], journal["policy_token"],
    )


def test_deployment_lock_rejects_concurrent_promotion(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    with adaptive_training._deployment_lock():
        ok, message = adaptive_training.deploy(tmp_path)
    assert not ok
    assert "already running" in message


def test_policy_lifecycle_lock_spans_different_sonder_homes(monkeypatch, tmp_path):
    policy = tmp_path / "shared-policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home-a"))
    script = """
import os
from pathlib import Path
import adaptive_training
adaptive_training._shared_alias_paths = lambda: {"lock": Path(os.environ["TEST_ALIAS_LOCK"])}
try:
    with adaptive_training._deployment_lock():
        print('ACQUIRED')
except RuntimeError as exc:
    print('BLOCKED:' + str(exc))
"""
    env = os.environ.copy()
    env["SONDER_RUNTIME_POLICY"] = str(policy)
    env["SONDER_HOME"] = str(tmp_path / "home-b")
    env["TEST_ALIAS_LOCK"] = str(adaptive_training._shared_alias_paths()["lock"])
    with adaptive_training._deployment_lock():
        child = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(adaptive_training.__file__).parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert child.returncode == 0
    assert "BLOCKED:another training lifecycle operation" in child.stdout
    assert "ACQUIRED" not in child.stdout


def test_alias_lifecycle_lock_spans_different_homes_policies_and_states(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home-a"))
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "policy-a.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "state-a.json"))
    script = """
import os
from pathlib import Path
import adaptive_training
adaptive_training._shared_alias_paths = lambda: {"lock": Path(os.environ["TEST_ALIAS_LOCK"])}
try:
    with adaptive_training._deployment_lock():
        print('ACQUIRED')
except RuntimeError as exc:
    print('BLOCKED:' + str(exc))
"""
    env = os.environ.copy()
    env.update({
        "OLLAMA_HOST": "[::1]:11434",
        "SONDER_HOME": str(tmp_path / "home-b"),
        "SONDER_RUNTIME_POLICY": str(tmp_path / "policy-b.json"),
        "SONDER_TRAINING_STATE": str(tmp_path / "state-b.json"),
        "TEST_ALIAS_LOCK": str(adaptive_training._shared_alias_paths()["lock"]),
    })
    with adaptive_training._deployment_lock():
        child = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(adaptive_training.__file__).parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert child.returncode == 0
    assert "BLOCKED:another training lifecycle operation" in child.stdout
    assert "ACQUIRED" not in child.stdout


def test_ipv4_and_ipv6_loopback_share_alias_namespace(monkeypatch):
    monkeypatch.setattr(adaptive_training, "_shared_alias_paths", SHARED_ALIAS_PATHS)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    ipv4 = adaptive_training._shared_alias_paths()
    monkeypatch.setenv("OLLAMA_HOST", "[::1]:11434")
    ipv6 = adaptive_training._shared_alias_paths()

    assert ipv4["origin"] == ipv6["origin"] == "loopback:11434"
    assert ipv4["lock"] == ipv6["lock"]
    assert ipv4["owner"] == ipv6["owner"]
    assert ipv4["transport_origin"] != ipv6["transport_origin"]


def test_unreadable_owner_metadata_cannot_override_os_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / "training-lifecycle.lock"
    original = adaptive_training.sonder_paths.state_path

    def isolated_state_path(name, env_var=""):
        if name == "training-lifecycle.lock":
            return str(lock_path)
        return original(name, env_var)

    monkeypatch.setattr(adaptive_training.sonder_paths, "state_path", isolated_state_path)
    owner_path = lock_path.with_name(lock_path.name + ".owner.json")
    owner_path.write_bytes(b"broken")

    with adaptive_training._deployment_lock():
        with pytest.raises(RuntimeError, match="already running"):
            with adaptive_training._deployment_lock():
                pass

    assert lock_path.exists()
    assert not owner_path.exists()


def test_training_start_is_blocked_by_active_lifecycle_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    plan = adaptive_training.build_plan(profile(8, 32))
    with adaptive_training._deployment_lock():
        ok, message = adaptive_training.start_training(
            plan,
            confirmed=True,
            runner=lambda *args, **kwargs: pytest.fail("training runner called"),
        )
    assert not ok and "lifecycle operation" in message


def test_rollback_is_blocked_by_active_lifecycle_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    before = _activate_personal_policy()
    with adaptive_training._deployment_lock():
        ok, message = adaptive_training.rollback(runner=lambda *args, **kwargs: pytest.fail("runner called"))
    after = runtime_policy.load(create=False)
    assert not ok and "already running" in message
    assert after["revision"] == before["revision"]


def test_rollback_updates_both_tiers_without_deleting_personal_model(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    _activate_personal_policy()
    ok, message = adaptive_training.rollback(
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr="")
    )
    policy = runtime_policy.load(create=False)
    assert ok
    assert policy["local_models"]["code"] == adaptive_training.ROLLBACK_MODEL
    assert policy["local_models"]["general"] == adaptive_training.ROLLBACK_MODEL
    assert "not deleted" in message


def test_rollback_holds_transition_marker_through_state_commit(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    _activate_personal_policy()
    original_write = adaptive_training._write_state
    blocked = []

    def guarded_write(payload):
        assert adaptive_training._deployment_journal_path().exists()
        try:
            runtime_policy.update(local_models={"code": "concurrent:latest"})
        except RuntimeError as exc:
            blocked.append(str(exc))
        original_write(payload)

    monkeypatch.setattr(adaptive_training, "_write_state", guarded_write)

    ok, message = adaptive_training.rollback(
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="rollback-model-id", stderr="",
        )
    )

    assert ok, message
    assert blocked and "active model deployment" in blocked[0]
    assert not adaptive_training._deployment_journal_path().exists()


def test_rollback_rejects_model_swap_after_policy_reservation(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    before = _activate_personal_policy()
    probes = 0

    def runner(command, **_kwargs):
        nonlocal probes
        if command[1:3] == ["show", adaptive_training.ROLLBACK_MODEL]:
            probes += 1
            identity = "trusted-rollback" if probes == 1 else "swapped-rollback"
            return SimpleNamespace(returncode=0, stdout=identity, stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    ok, message = adaptive_training.rollback(runner=runner)

    after = runtime_policy.load(create=False)
    assert not ok and "changed after transition reservation" in message
    assert after["revision"] == before["revision"]
    assert after["local_models"] == before["local_models"]
    assert not adaptive_training._deployment_journal_path().exists()


def test_rollback_state_commit_failure_restores_prior_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "training-state.json"))
    _activate_personal_policy()
    monkeypatch.setattr(
        adaptive_training, "_write_state",
        lambda payload: (_ for _ in ()).throw(OSError("disk full")),
    )

    ok, message = adaptive_training.rollback(
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr="")
    )

    policy = runtime_policy.load(create=False)
    assert not ok and "policy was restored" in message
    assert policy["local_models"]["code"] == adaptive_training.PERSONAL_MODEL
    assert policy["local_models"]["general"] == adaptive_training.PERSONAL_MODEL


def test_rollback_normalizes_non_object_training_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime-policy.json"))
    state_path = tmp_path / "training-state.json"
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(state_path))
    state_path.write_text("[]", encoding="utf-8")
    _activate_personal_policy()

    ok, message = adaptive_training.rollback(
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr="")
    )

    assert ok, message
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(saved, dict) and saved["status"] == "rolled_back"


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
        (adapter / "adapter_model.safetensors").write_bytes(b"weights")
        plan_path = Path(kwargs["env"]["SONDER_TRAINING_MANIFEST"])
        plan_manifest = json.loads(plan_path.read_text(encoding="utf-8"))
        plan_manifest["launch_consumed_ts"] = plan_manifest["created_ts"] + 1
        plan_manifest.pop("launch_token_sha256", None)
        plan_path.write_text(json.dumps(plan_manifest), encoding="utf-8")
        config = adapter / "adapter_config.json"
        weights = adapter / "adapter_model.safetensors"
        manifest = {
            **plan_manifest,
            "completed_ts": plan_manifest["created_ts"] + 2,
            "artifact_sha256": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (config, weights)
            },
            "artifact_sizes": {
                path.name: path.stat().st_size for path in (config, weights)
            },
        }
        (adapter / "training-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    plan = adaptive_training.build_plan(
        profile(8, 32), adaptive_training.PlanOptions(gpu_index=2)
    )
    ok, message = adaptive_training.start_training(plan, confirmed=True, runner=runner)
    assert ok and "completed" in message
    assert seen["command"][-1].endswith("qlora_train.py")
    assert seen["env"]["SONDER_BASE"] == "Qwen/Qwen2.5-Coder-3B-Instruct"
    assert seen["env"]["SONDER_HF_REVISION"] == adaptive_training.MODEL_SPECS["3b"]["hf_revision"]
    assert seen["env"]["SONDER_ALLOW_CPU_OFFLOAD"] == "0"
    assert seen["env"]["CUDA_VISIBLE_DEVICES"] == "2"
    assert Path(seen["env"]["SONDER_DATA"]).name == "training-data.jsonl"
    assert Path(seen["env"]["SONDER_DATA"]).parent.name == json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    )["run_id"]
    assert Path(seen["env"]["SONDER_LORA_OUT"]).parent.parent.parent == output
    assert json.loads((tmp_path / "state.json").read_text())["status"] == "trained"


def test_new_training_run_freshly_exports_memory_into_immutable_run(
    monkeypatch, tmp_path,
):
    import export_training_data

    monkeypatch.delenv("SONDER_DATA", raising=False)
    monkeypatch.setenv("SONDER_LORA_OUT", str(tmp_path / "lora"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "state.json"))
    exported_paths = []

    def export(out_path):
        destination = Path(out_path)
        exported_paths.append(destination)
        payload = (
            '{"messages":[{"role":"user","content":"fresh"},'
            '{"role":"assistant","content":"snapshot"}]}\n'
        )
        destination.write_bytes(payload.encode("utf-8"))
        Path(str(destination) + ".manifest.json").write_text(json.dumps({
            "schema": 1,
            "format": "sonder-chat-jsonl",
            "accepted": 1,
            "characters": len("freshsnapshot"),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "privacy_policy": "exclude-shared-private-markers",
        }), encoding="utf-8")
        return 1

    monkeypatch.setattr(export_training_data, "main", export)
    plan = adaptive_training.build_plan(profile(8, 32))

    ok, message = adaptive_training.start_training(
        plan,
        confirmed=True,
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    assert not ok and "preserved" in message
    assert len(exported_paths) == 1
    assert exported_paths[0].name == "training-data.jsonl"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    plan_manifest = json.loads(Path(state["plan_file"]).read_text(encoding="utf-8"))
    assert exported_paths[0].parent == Path(state["run_dir"])
    assert plan_manifest["data_source"] == "memory_export"
    assert plan_manifest["source_data_path"] == plan_manifest["data_path"]
    assert plan_manifest["selection_manifest_sha256"]
    assert state["selection_manifest_sha256"] == plan_manifest[
        "selection_manifest_sha256"
    ]


def test_memory_export_resume_reuses_exact_snapshot_without_reexport(
    monkeypatch, tmp_path,
):
    import export_training_data

    monkeypatch.delenv("SONDER_DATA", raising=False)
    monkeypatch.setenv("SONDER_LORA_OUT", str(tmp_path / "lora"))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "state.json"))

    def export(out_path):
        destination = Path(out_path)
        payload = (
            '{"messages":[{"role":"user","content":"fresh"},'
            '{"role":"assistant","content":"snapshot"}]}\n'
        ).encode("utf-8")
        destination.write_bytes(payload)
        Path(str(destination) + ".manifest.json").write_text(json.dumps({
            "schema": 1,
            "format": "sonder-chat-jsonl",
            "accepted": 1,
            "characters": len("freshsnapshot"),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "privacy_policy": "exclude-shared-private-markers",
        }), encoding="utf-8")
        return 1

    monkeypatch.setattr(export_training_data, "main", export)
    plan = adaptive_training.build_plan(profile(8, 32))
    ok, _message = adaptive_training.start_training(
        plan, confirmed=True,
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    assert not ok
    monkeypatch.setattr(
        export_training_data, "main",
        lambda *args, **kwargs: pytest.fail("resume must not re-export memory"),
    )
    calls = []

    ok, message = adaptive_training.start_training(
        plan, confirmed=True, resume=True,
        runner=lambda *args, **kwargs: calls.append(args)
        or SimpleNamespace(returncode=1),
    )

    assert not ok and "preserved" in message
    assert len(calls) == 1


def test_prelaunch_dataset_failures_remove_new_run_directory(monkeypatch, tmp_path):
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text("{}\n", encoding="utf-8")
    output_root = tmp_path / "lora"
    monkeypatch.setenv("SONDER_DATA", str(invalid))
    monkeypatch.setenv("SONDER_LORA_OUT", str(output_root))
    monkeypatch.setenv("SONDER_TRAINING_STATE", str(tmp_path / "state.json"))
    plan = adaptive_training.build_plan(profile(8, 32))

    ok, message = adaptive_training.start_training(plan, confirmed=True)

    assert not ok and "dataset is invalid" in message
    runs = output_root / "runs"
    assert not runs.exists() or list(runs.iterdir()) == []


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
    data.write_text(
        data.read_text(encoding="utf-8")
        + '{"messages":[{"role":"user","content":"new"},'
        '{"role":"assistant","content":"row"}]}\n',
        encoding="utf-8",
    )
    called = []
    ok, message = adaptive_training.start_training(
        plan, confirmed=True, resume=True,
        runner=lambda *args, **kwargs: called.append(args),
    )
    assert not ok
    assert "data changed" in message
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


def test_resume_rejects_tampered_plan_and_out_of_run_snapshot(monkeypatch, tmp_path):
    plan, _data = _create_failed_training_run(monkeypatch, tmp_path)
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    plan_path = Path(state["plan_file"])
    manifest = json.loads(plan_path.read_text(encoding="utf-8"))
    attacker_data = tmp_path / "attacker.jsonl"
    attacker_data.write_text(
        '{"messages":[{"role":"user","content":"untrusted"},'
        '{"role":"assistant","content":"payload"}]}\n',
        encoding="utf-8",
    )
    attacker_hash = hashlib.sha256(attacker_data.read_bytes()).hexdigest()
    manifest.update(
        data_path=str(attacker_data.resolve()),
        data_sha256=attacker_hash,
        data_source="memory_export",
        source_data_path=str(attacker_data.resolve()),
        source_data_sha256=attacker_hash,
    )
    plan_path.write_text(json.dumps(manifest), encoding="utf-8")
    # Even if mutable state is changed alongside the plan, containment remains
    # an independent invariant: training input must be the run-local snapshot.
    state.update(
        plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        data_path=str(attacker_data.resolve()),
        data_sha256=attacker_hash,
        data_source="memory_export",
        source_data_path=str(attacker_data.resolve()),
        source_data_sha256=attacker_hash,
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    called = []

    ok, message = adaptive_training.start_training(
        plan,
        confirmed=True,
        resume=True,
        runner=lambda *args, **kwargs: called.append(args),
    )

    assert not ok and "outside the immutable run directory" in message
    assert called == []


def test_resume_rejects_still_live_prior_training_child(monkeypatch, tmp_path):
    plan, _data = _create_failed_training_run(monkeypatch, tmp_path)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    claim = Path(state["run_dir"]) / ".launch-claimed"
    claim.write_text(str(__import__("os").getpid()), encoding="ascii")
    called = []

    ok, message = adaptive_training.start_training(
        plan,
        confirmed=True,
        resume=True,
        runner=lambda *args, **kwargs: called.append(args),
    )

    assert not ok and "still running" in message
    assert called == []


def test_programmatic_start_rejects_negative_gpu_index():
    plan = adaptive_training.build_plan(
        profile(8, 32), adaptive_training.PlanOptions(gpu_index=-1)
    )
    ok, message = adaptive_training.start_training(plan, confirmed=True)
    assert not ok
    assert "GPU index" in message

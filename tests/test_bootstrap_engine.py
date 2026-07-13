import bootstrap_engine
import engine_bundle
from pathlib import Path
from types import SimpleNamespace


def test_choose_model_by_ram():
    assert bootstrap_engine.choose_model(2) == "qwen2.5-coder:1.5b"
    assert bootstrap_engine.choose_model(4) == "qwen2.5-coder:3b"
    assert bootstrap_engine.choose_model(8) == "qwen2.5-coder:7b"


def test_choose_model_env_override(monkeypatch):
    monkeypatch.setenv("SONDER_BASE_MODEL", "custom:model")
    assert bootstrap_engine.choose_model(1) == "custom:model"


def test_main_runs_setup_alias_from_script_root(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_engine, "total_ram_gb", lambda: 8)
    monkeypatch.setattr(
        bootstrap_engine,
        "ensure_python_deps",
        lambda *args, **kwargs: (True, "ok"),
    )
    monkeypatch.setattr(
        bootstrap_engine,
        "ensure_ollama_running",
        lambda *args, **kwargs: (True, "ok"),
    )
    monkeypatch.setattr(bootstrap_engine, "_load_bundle", lambda args: None)

    def fake_run(cmd, check=False, env=None, cwd=None, **kwargs):
        seen.update(cmd=cmd, check=check, env=env, cwd=cwd, kwargs=kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bootstrap_engine, "_run", fake_run)
    assert bootstrap_engine.main([]) == 0
    assert Path(seen["cmd"][1]) == bootstrap_engine.ROOT / "setup_alias.py"
    assert Path(seen["cwd"]) == bootstrap_engine.ROOT
    assert "SONDER_BASE_MODEL" in seen["env"]


def test_offline_without_bundle_never_installs_dependency(monkeypatch):
    seen = {}
    monkeypatch.setattr(bootstrap_engine, "total_ram_gb", lambda: 2)
    monkeypatch.setattr(bootstrap_engine, "_load_bundle", lambda args: None)

    def fake_deps(python_executable, *, offline, env):
        seen.update(python=python_executable, offline=offline, env=env)
        return False, "missing"

    monkeypatch.setattr(bootstrap_engine, "ensure_python_deps", fake_deps)
    assert bootstrap_engine.main(["--offline"]) == 3
    assert seen["offline"] is True


def test_invalid_bundle_fails_before_runtime_actions(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bootstrap_engine,
        "_load_bundle",
        lambda args: (_ for _ in ()).throw(ValueError("hash mismatch")),
    )
    called = []
    monkeypatch.setattr(
        bootstrap_engine,
        "ensure_ollama_running",
        lambda *args, **kwargs: called.append(True),
    )
    assert bootstrap_engine.main(["--bundle", str(tmp_path)]) == 4
    assert called == []


def test_remote_ollama_is_blocked_before_runtime_probe(monkeypatch):
    called = []
    monkeypatch.setenv("OLLAMA_HOST", "http://models.example.test:11434")
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_OLLAMA", raising=False)
    monkeypatch.setattr(bootstrap_engine, "_load_bundle", lambda args: None)
    monkeypatch.setattr(
        bootstrap_engine,
        "ensure_python_deps",
        lambda *args, **kwargs: (True, "ok"),
    )
    monkeypatch.setattr(
        bootstrap_engine,
        "ensure_ollama_running",
        lambda *args, **kwargs: called.append(True) or (True, "ok"),
    )

    assert bootstrap_engine.main([]) == 4
    assert called == []


def test_ensure_ollama_running_splits_bind_and_client_environments(monkeypatch):
    probes = []
    serves = []
    results = iter([1, 0])
    monkeypatch.setattr(bootstrap_engine, "_ollama_executable", lambda value="": "ollama-test")
    monkeypatch.setattr(bootstrap_engine.time, "sleep", lambda seconds: None)

    def fake_run(command, **kwargs):
        probes.append(kwargs["env"])
        return SimpleNamespace(
            returncode=next(results), stdout="", stderr="",
        )

    monkeypatch.setattr(bootstrap_engine.subprocess, "run", fake_run)
    monkeypatch.setattr(
        bootstrap_engine.subprocess,
        "Popen",
        lambda command, **kwargs: serves.append(kwargs["env"]),
    )

    ok, _ = bootstrap_engine.ensure_ollama_running(
        "ollama-test", env={"OLLAMA_HOST": "0.0.0.0:11434"},
    )

    assert ok is True
    assert all(
        env["OLLAMA_HOST"] == "http://127.0.0.1:11434" for env in probes
    )
    assert serves[0]["OLLAMA_HOST"] == "0.0.0.0:11434"


def test_ensure_ollama_running_does_not_start_daemon_for_remote(monkeypatch):
    serves = []
    monkeypatch.setattr(bootstrap_engine, "_ollama_executable", lambda value="": "ollama-test")
    monkeypatch.setattr(
        bootstrap_engine.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="offline",
        ),
    )
    monkeypatch.setattr(
        bootstrap_engine.subprocess,
        "Popen",
        lambda *args, **kwargs: serves.append(1),
    )

    ok, message = bootstrap_engine.ensure_ollama_running(
        "ollama-test",
        env={
            "OLLAMA_HOST": "http://models.example.test:11434",
            "SONDER_ALLOW_REMOTE_OLLAMA": "1",
        },
    )

    assert ok is False
    assert "Remote Ollama is unavailable" in message
    assert serves == []


def test_bundle_dry_run_is_offline_and_uses_sealed_model(monkeypatch, capsys):
    bundle = SimpleNamespace(
        root=Path("bundle"),
        identity="windows-x86_64",
        base_models=(engine_bundle.BundleModel("sealed:model", Path("models/manifest"), 0),),
    )
    monkeypatch.setattr(bootstrap_engine, "_load_bundle", lambda args: bundle)
    monkeypatch.setattr(bootstrap_engine, "total_ram_gb", lambda: 16)
    assert bootstrap_engine.main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "selected model: sealed:model" in output
    assert "network policy: offline" in output

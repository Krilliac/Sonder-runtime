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


def test_python_dependency_probe_requires_mcpserver_compatibility_api(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="missing MCPServer")

    monkeypatch.setattr(bootstrap_engine.subprocess, "run", fake_run)

    ok, message = bootstrap_engine.ensure_python_deps("python-test", offline=True)

    assert ok is False
    assert "MCPServer compatibility API is unavailable" in message
    assert calls == [
        ["python-test", "-c", bootstrap_engine.MCPSERVER_IMPORT_PROBE]
    ]


def test_python_dependency_install_uses_runtime_contract_and_reprobes(
    monkeypatch, tmp_path
):
    requirements = tmp_path / bootstrap_engine.RUNTIME_REQUIREMENTS_NAME
    requirements.write_text("mcp==2.0.0\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap_engine, "ROOT", tmp_path)
    calls = []
    results = iter((1, 0, 0))

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=next(results), stdout="MCPServer\n", stderr=""
        )

    monkeypatch.setattr(bootstrap_engine.subprocess, "run", fake_run)

    ok, message = bootstrap_engine.ensure_python_deps("python-test")

    assert ok is True
    assert requirements.name in message
    assert calls == [
        ["python-test", "-c", bootstrap_engine.MCPSERVER_IMPORT_PROBE],
        [
            "python-test",
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "-r",
            str(requirements),
        ],
        ["python-test", "-c", bootstrap_engine.MCPSERVER_IMPORT_PROBE],
    ]


def test_python_dependency_install_fails_closed_when_mcpserver_still_missing(
    monkeypatch, tmp_path
):
    requirements = tmp_path / bootstrap_engine.RUNTIME_REQUIREMENTS_NAME
    requirements.write_text("mcp==2.0.0\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap_engine, "ROOT", tmp_path)
    results = iter((1, 0, 1))
    monkeypatch.setattr(
        bootstrap_engine.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=next(results), stdout="", stderr="missing MCPServer"
        ),
    )

    ok, message = bootstrap_engine.ensure_python_deps("python-test")

    assert ok is False
    assert "still unavailable" in message


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
            "OLLAMA_HOST": "https://models.example.test:11434",
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


def test_a_present_bundle_does_not_forbid_dependency_repair(monkeypatch, tmp_path):
    """A bundle supplies models offline; it must not veto repairing the runtime.

    `offline = args.offline or bundle is not None` was passed straight to
    `ensure_python_deps`, so a sealed runtime that could no longer import the
    MCP server API -- exactly what a source update past the runtime pin
    produces -- could not be repaired by pip. The engine failed at startup and
    this bootstrap exited 3 naming no remedy. Only an explicit `--offline` may
    forbid pip now; a healthy bundle never reaches pip anyway, because the
    compatibility probe short-circuits first.
    """
    seen = {}
    bundle = SimpleNamespace(
        root=tmp_path,
        python_executable=tmp_path / "python.exe",
        ollama_executable=tmp_path / "ollama.exe",
        model_store=tmp_path / "models",
        base_models=(
            SimpleNamespace(
                name="qwen2.5-coder:1.5b",
                min_ram_gb=2.0,
                manifest=tmp_path / "models" / "base.json",
            ),
        ),
        embedding_model=SimpleNamespace(name="nomic-embed-text:latest"),
        runtime_contract="0" * 64,
        identity="windows-x86_64",
    )
    monkeypatch.setattr(bootstrap_engine, "_load_bundle", lambda args: bundle)
    monkeypatch.setattr(bootstrap_engine, "total_ram_gb", lambda: 32)
    monkeypatch.setattr(
        engine_bundle, "install_model_store",
        lambda _bundle: (tmp_path / "models", 0, 0),
    )
    monkeypatch.setattr(engine_bundle, "runtime_environment", lambda *a, **k: {})

    def fake_deps(python_executable, *, offline, env):
        seen["offline"] = offline
        return False, "missing"

    monkeypatch.setattr(bootstrap_engine, "ensure_python_deps", fake_deps)

    assert bootstrap_engine.main([]) == 3
    assert seen["offline"] is False, (
        "a present bundle still forbids pip, so a stale sealed runtime has no "
        "repair path"
    )

    seen.clear()
    assert bootstrap_engine.main(["--offline"]) == 3
    assert seen["offline"] is True, "an explicit --offline must still forbid pip"

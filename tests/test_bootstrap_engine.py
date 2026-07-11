import bootstrap_engine
import engine_bundle
from pathlib import Path
from types import SimpleNamespace


def test_choose_model_by_ram():
    assert bootstrap_engine.choose_model(2) == "qwen2.5-coder:1.5b"
    assert bootstrap_engine.choose_model(4) == "qwen2.5-coder:3b"
    assert bootstrap_engine.choose_model(8) == "qwen2.5-coder:7b"


def test_choose_model_env_override(monkeypatch):
    monkeypatch.setenv("TRILOBITE_BASE_MODEL", "custom:model")
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

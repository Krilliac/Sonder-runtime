import bootstrap_engine
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
        lambda: (True, "ok"),
    )
    monkeypatch.setattr(
        bootstrap_engine,
        "ensure_ollama_running",
        lambda: (True, "ok"),
    )

    def fake_run(cmd, check=False, env=None, cwd=None):
        seen.update(cmd=cmd, check=check, env=env, cwd=cwd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bootstrap_engine, "_run", fake_run)
    assert bootstrap_engine.main([]) == 0
    assert Path(seen["cmd"][1]) == bootstrap_engine.ROOT / "setup_alias.py"
    assert Path(seen["cwd"]) == bootstrap_engine.ROOT

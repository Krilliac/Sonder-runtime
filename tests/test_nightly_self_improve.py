from pathlib import Path

import pytest

from scripts import nightly_self_improve


def test_nightly_rehomes_absolute_paths_from_another_checkout(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    other = (tmp_path / "other").resolve()
    other.mkdir()
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", str(other / "emotion_vectors.json"))
    monkeypatch.setenv("SONDER_SYSTEM_PROFILE", str(other / "system_profile.md"))

    rebound = nightly_self_improve._bind_workspace_config_paths(root)

    assert rebound == ("SONDER_EMOTION_VECTORS", "SONDER_SYSTEM_PROFILE")
    assert nightly_self_improve.os.environ["SONDER_EMOTION_VECTORS"] == str(root / "emotion_vectors.json")
    assert nightly_self_improve.os.environ["SONDER_SYSTEM_PROFILE"] == str(root / "system_profile.md")


def test_nightly_preserves_an_in_checkout_override(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    custom = root / "custom-profile.md"
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", "emotion_vectors.json")
    monkeypatch.setenv("SONDER_SYSTEM_PROFILE", str(custom))

    rebound = nightly_self_improve._bind_workspace_config_paths(root)

    assert rebound == ()
    assert nightly_self_improve.os.environ["SONDER_EMOTION_VECTORS"] == str(root / "emotion_vectors.json")
    assert nightly_self_improve.os.environ["SONDER_SYSTEM_PROFILE"] == str(custom)


def test_nightly_rejects_an_escaping_checkout_default(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    default = root / "emotion_vectors.json"
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == default:
            return outside / default.name
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", str(outside / default.name))

    with pytest.raises(ValueError, match="workspace default escapes checkout"):
        nightly_self_improve._bind_workspace_config_paths(root)


def test_nightly_exports_toml_workers_when_env_blank(tmp_path, monkeypatch):
    cfg = tmp_path / "sonder.toml"
    cfg.write_text(
        "\n".join([
            "schema_version = 1",
            'profile = "workstation-local"',
            "[ollama]",
            'url = "http://127.0.0.1:11434"',
            "allow_remote = true",
            'workers = ["https://10.77.0.2:8443"]',
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SONDER_OLLAMA_WORKERS", raising=False)
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_OLLAMA", raising=False)
    monkeypatch.delenv("SONDER_TRUSTED_ORIGINS", raising=False)

    bound = nightly_self_improve._bind_ollama_pool_from_config(cfg)

    assert "SONDER_OLLAMA_WORKERS" in bound
    assert nightly_self_improve.os.environ["SONDER_OLLAMA_WORKERS"] == "https://10.77.0.2:8443"
    assert nightly_self_improve.os.environ["SONDER_ALLOW_REMOTE_OLLAMA"] == "1"


def test_nightly_keeps_explicit_worker_env(tmp_path, monkeypatch):
    cfg = tmp_path / "sonder.toml"
    cfg.write_text(
        "schema_version = 1\n[ollama]\nallow_remote = true\n"
        'workers = ["https://10.77.0.2:8443"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SONDER_OLLAMA_WORKERS", "http://127.0.0.1:11435")

    assert nightly_self_improve._bind_ollama_pool_from_config(cfg) == ()
    assert nightly_self_improve.os.environ["SONDER_OLLAMA_WORKERS"] == "http://127.0.0.1:11435"


def test_nightly_preflight_is_provider_and_workspace_binding_only(monkeypatch):
    calls = []

    def bind_workspace(root):
        calls.append(("workspace", root))
        return ("SONDER_EMOTION_VECTORS",)

    def bind_workers():
        calls.append(("ollama",))
        return ("SONDER_OLLAMA_WORKERS",)

    monkeypatch.setattr(nightly_self_improve, "_bind_workspace_config_paths", bind_workspace)
    monkeypatch.setattr(nightly_self_improve, "_bind_ollama_pool_from_config", bind_workers)

    rebound, workers = nightly_self_improve._preflight(Path("C:/nightly"))

    assert rebound == ("SONDER_EMOTION_VECTORS",)
    assert workers == ("SONDER_OLLAMA_WORKERS",)
    assert calls == [("workspace", Path("C:/nightly")), ("ollama",)]


def test_nightly_stage_records_critical_failure():
    failures = []
    messages = []

    result = nightly_self_improve._stage(
        messages.append, "campaign", lambda: (_ for _ in ()).throw(RuntimeError("down")), failures
    )

    assert result is None
    assert failures == ["campaign"]
    assert "FAILED" in messages[0]


def test_nightly_classifies_known_blocking_results_but_keeps_intentional_skips():
    cases = [
        ("campaign", "ERROR: backend unavailable", True),
        ("repo-repair", "ERROR: no model produced an answer", True),
        ("selfmod", "working tree dirty (4 path(s)); none was started", True),
        ("selfmod", "ERROR: model unavailable: Ollama HTTP 500", True),
        ("selfmod", "selfmod disabled", False),
        ("selfmod", "no actionable objective proposed", False),
    ]
    for name, result, expected in cases:
        assert bool(nightly_self_improve._blocking_result(name, result)) is expected

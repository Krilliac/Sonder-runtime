from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from sonder_runtime.bootstrap import legacy_root
from sonder_runtime.platform.config import load_config


@pytest.fixture
def typed_application(tmp_path):
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    application = build_application(config=SonderConfig(state=StateConfig(home=str(tmp_path))))
    try:
        yield application
    finally:
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


def test_config_retains_exact_private_file_provenance(tmp_path):
    config_file = tmp_path / "explicit.toml"
    secrets_file = tmp_path / "explicit.env"
    config_file.write_text(
        "[child_storage]\nbinding_file = '" + str(tmp_path / "binding.json") + "'\n",
        encoding="utf-8",
    )
    secrets_file.write_text("SONDER_HOST=127.0.0.1\n", encoding="utf-8")
    secrets_file.chmod(0o600)
    config = load_config(config_file, secrets_path=secrets_file, env={})
    assert config.private_source_paths == (
        str(config_file.resolve()),
        str(secrets_file.resolve()),
        str((tmp_path / "binding.json").resolve()),
    )
    assert "private_source_paths" not in config.as_redacted_dict()
    assert "private_source_paths" not in repr(config)


def test_legacy_injection_does_not_replace_caller_owned_application(monkeypatch, typed_application):
    calls = []
    caller = SimpleNamespace(close_providers=lambda **kw: calls.append("closed"))
    pool = SimpleNamespace(drain=lambda **kw: calls.append("drained"))
    runtime = SimpleNamespace(_APP_GRAPH=caller, _APP_GRAPH_LOCK=threading.Lock(),
                              OLLAMA_POOL=pool, BASE="http://127.0.0.1:11434")
    monkeypatch.setattr(legacy_root, "runtime", lambda: runtime)
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    with pytest.raises(RuntimeError, match="caller-owned"):
        legacy_root.configure_application(typed_application)
    assert runtime._APP_GRAPH is caller and calls == []
    assert runtime.OLLAMA_POOL is pool and runtime.BASE == "http://127.0.0.1:11434"


def test_owned_legacy_replacement_requires_successful_bounded_cleanup(monkeypatch, typed_application):
    calls = []
    old = replace(typed_application)
    close = type(old).close_providers

    def fail(self, *, timeout):
        if self is old:
            calls.append(timeout)
            raise RuntimeError("cleanup incomplete")
        return close(self, timeout=timeout)

    monkeypatch.setattr(type(old), "close_providers", fail)
    pool = old.inference_pool
    monkeypatch.setattr(
        pool, "drain", lambda **kw: calls.append("drained") or True,
    )
    runtime = SimpleNamespace(_APP_GRAPH=old, _APP_GRAPH_LOCK=threading.Lock(), OLLAMA_POOL=pool)
    monkeypatch.setattr(legacy_root, "runtime", lambda: runtime)
    monkeypatch.setattr(legacy_root, "_owned_application", old)
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        legacy_root.configure_application(typed_application)
    assert runtime._APP_GRAPH is old and calls == [5]
    assert runtime.OLLAMA_POOL is pool


def test_busy_legacy_composition_is_bounded_and_does_not_replace(monkeypatch, typed_application):
    class BusyLock:
        def acquire(self, timeout):
            assert timeout == 5
            return False

        def release(self):
            pytest.fail("unacquired lock released")

    original = object()
    runtime = SimpleNamespace(_APP_GRAPH=original, _APP_GRAPH_LOCK=BusyLock())
    monkeypatch.setattr(legacy_root, "runtime", lambda: runtime)
    with pytest.raises(RuntimeError, match="composition is busy"):
        legacy_root.configure_application(typed_application)
    assert runtime._APP_GRAPH is original

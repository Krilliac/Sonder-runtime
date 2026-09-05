from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from sonder_runtime.bootstrap import legacy_root
from sonder_runtime.platform.config import load_config


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


def test_legacy_injection_does_not_replace_caller_owned_application(monkeypatch):
    calls = []
    caller = SimpleNamespace(close_providers=lambda **kw: calls.append("closed"))
    runtime = SimpleNamespace(_APP_GRAPH=caller, _APP_GRAPH_LOCK=threading.Lock())
    monkeypatch.setattr(legacy_root, "runtime", lambda: runtime)
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    with pytest.raises(RuntimeError, match="caller-owned"):
        legacy_root.configure_application(SimpleNamespace())
    assert runtime._APP_GRAPH is caller and calls == []


def test_owned_legacy_replacement_requires_successful_bounded_cleanup(monkeypatch):
    calls = []

    def fail(timeout):
        calls.append(timeout)
        raise RuntimeError("cleanup incomplete")

    old = SimpleNamespace(close_providers=fail)
    runtime = SimpleNamespace(_APP_GRAPH=old, _APP_GRAPH_LOCK=threading.Lock())
    monkeypatch.setattr(legacy_root, "runtime", lambda: runtime)
    monkeypatch.setattr(legacy_root, "_owned_application", old)
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        legacy_root.configure_application(SimpleNamespace())
    assert runtime._APP_GRAPH is old and calls == [5]


def test_busy_legacy_composition_is_bounded_and_does_not_replace(monkeypatch):
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
        legacy_root.configure_application(object())
    assert runtime._APP_GRAPH is original

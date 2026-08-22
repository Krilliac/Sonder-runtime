"""Identity and fail-closed coverage for security compatibility aliases."""
from __future__ import annotations

import json
import os

import pytest


def test_served_action_receipts_root_is_the_canonical_module():
    import served_action_receipts as legacy
    from sonder_runtime.adapters.persistence import served_action_receipts as canonical

    assert legacy is canonical
    assert legacy.__name__ == canonical.__name__
    assert legacy.claim is canonical.claim


def test_unsafe_lab_root_is_the_canonical_module():
    import unsafe_lab as legacy
    from sonder_runtime.adapters.security import unsafe_lab as canonical

    assert legacy is canonical
    assert legacy.__name__ == canonical.__name__
    assert legacy.require_startup is canonical.require_startup


def test_unsafe_lab_requires_exact_ack_and_refuses_privileged_execution():
    from sonder_runtime.adapters.security import unsafe_lab

    base = {
        unsafe_lab.ACK_ENV: unsafe_lab.ACKNOWLEDGEMENT,
        "SONDER_HOST": "127.0.0.1",
        "OLLAMA_HOST": "127.0.0.1:11434",
    }

    assert unsafe_lab.inspect(env={}).enabled is False
    with pytest.raises(unsafe_lab.UnsafeLabError, match="exactly match"):
        unsafe_lab.require_startup(
            env={**base, unsafe_lab.ACK_ENV: "1"}, privilege_probe=lambda: False
        )
    with pytest.raises(unsafe_lab.UnsafeLabError, match="root or elevated"):
        unsafe_lab.require_startup(env=base, privilege_probe=lambda: True)


def test_unsafe_lab_rejects_remote_or_cloud_activation():
    from sonder_runtime.adapters.security import unsafe_lab

    base = {
        unsafe_lab.ACK_ENV: unsafe_lab.ACKNOWLEDGEMENT,
        "SONDER_HOST": "127.0.0.1",
        "OLLAMA_HOST": "127.0.0.1:11434",
    }

    remote = unsafe_lab.inspect(env={**base, "SONDER_HOST": "0.0.0.0"}, privilege_probe=lambda: False)
    assert remote.enabled is False
    assert "non-loopback" in remote.error

    cloud = unsafe_lab.inspect(env={**base, "SONDER_ALLOW_CLOUD": "1"}, privilege_probe=lambda: False)
    assert cloud.enabled is False
    assert "cloud" in cloud.error


def test_unsafe_lab_activation_writes_one_audited_warning(tmp_path, monkeypatch):
    from sonder_runtime.adapters.security import unsafe_lab

    audit_path = tmp_path / "audit" / "unsafe-lab.jsonl"
    env = {
        unsafe_lab.ACK_ENV: unsafe_lab.ACKNOWLEDGEMENT,
        "SONDER_HOST": "localhost",
        "OLLAMA_HOST": "http://127.0.0.1:11434",
        unsafe_lab.AUDIT_PATH_ENV: str(audit_path),
    }
    monkeypatch.setattr(unsafe_lab, "_audited_processes", set())

    assert unsafe_lab.require_startup(env=env, privilege_probe=lambda: False) is True
    assert unsafe_lab.require_startup(env=env, privilege_probe=lambda: False) is True

    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["event"] == "unsafe_lab_activated"
    assert records[0]["host"] == "localhost"
    assert records[0]["pid"] == os.getpid()
    assert "UNSAFE LAB MODE" in records[0]["warning"]
    if os.name == "posix":
        assert audit_path.stat().st_mode & 0o777 == 0o600

from types import SimpleNamespace

import sonder_doctor
from sonder_runtime.adapters.config_validation import validated_config_check


def test_config_check_adapter_reports_ollama_url():
    config = SimpleNamespace(ollama=SimpleNamespace(url="http://local"))
    assert validated_config_check(config)() == {
        "status": "ok",
        "detail": "ollama=http://local",
    }


def test_config_check_adapter_handles_missing_ollama_config():
    assert validated_config_check(object())() == {
        "status": "ok",
        "detail": "ollama=?",
    }


def test_doctor_alias_delegates_to_packaged_adapter(monkeypatch):
    calls = []

    def fake(config):
        calls.append(config)
        return lambda: {"status": "ok", "detail": "delegated"}

    monkeypatch.setattr(sonder_doctor, "_validated_config_check", fake)
    config = object()
    assert sonder_doctor.validated_config_check(config)() == {
        "status": "ok",
        "detail": "delegated",
    }
    assert calls == [config]

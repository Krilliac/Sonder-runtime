"""Ownership and failure-policy tests for the bootstrap config boundary."""
from __future__ import annotations

import builtins
import types

from sonder_runtime.bootstrap import config_loading


def test_loader_returns_packaged_config(monkeypatch):
    expected = object()
    module = types.SimpleNamespace(load_config=lambda: expected)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sonder_runtime.platform":
            return types.SimpleNamespace(config=module)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert config_loading.load_config_or_none() is expected


def test_loader_returns_none_when_config_import_fails(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sonder_runtime.platform":
            raise ImportError("config unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert config_loading.load_config_or_none() is None


def test_loader_returns_none_when_config_load_fails(monkeypatch):
    module = types.SimpleNamespace(
        load_config=lambda: (_ for _ in ()).throw(ValueError("invalid"))
    )
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sonder_runtime.platform":
            return types.SimpleNamespace(config=module)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert config_loading.load_config_or_none() is None


def test_doctor_root_helper_delegates_to_bootstrap_owner(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        "sonder_doctor._load_config_or_none_impl",
        lambda: sentinel,
    )

    import sonder_doctor

    assert sonder_doctor._load_config_or_none() is sentinel


def test_packaged_config_check_reports_validated_config(monkeypatch):
    module = types.SimpleNamespace(
        ConfigError=ValueError,
        load_config=lambda: types.SimpleNamespace(
            ollama=types.SimpleNamespace(url="http://local")
        ),
    )
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sonder_runtime.platform":
            return types.SimpleNamespace(config=module)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert config_loading.check_config()() == {
        "status": "ok",
        "detail": "ollama=http://local",
    }


def test_packaged_config_check_preserves_config_error(monkeypatch):
    module = types.SimpleNamespace(
        ConfigError=ValueError,
        load_config=lambda: (_ for _ in ()).throw(ValueError("invalid")),
    )
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sonder_runtime.platform":
            return types.SimpleNamespace(config=module)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert config_loading.check_config()() == {
        "status": "fail",
        "detail": "config invalid: invalid",
    }


def test_doctor_root_config_check_delegates_to_bootstrap_owner(monkeypatch):
    import sonder_doctor

    monkeypatch.setattr(
        sonder_doctor,
        "_check_config_impl",
        lambda: lambda: {"status": "ok", "detail": "delegated"},
    )

    assert sonder_doctor._check_config() == {
        "status": "ok",
        "detail": "delegated",
    }

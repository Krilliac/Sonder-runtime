"""Routine startup/routing records are INFO/DEBUG, and approval reasons are plain."""
from __future__ import annotations

import logging

import pytest

import permission_modes
from sonder_runtime.domain.routing import capability_router
from sonder_runtime.domain.runtime_model_configuration import RuntimeModelConfiguration


def _levels(caplog, fragment):
    return [r.levelno for r in caplog.records if fragment in r.getMessage()]


def test_no_keyword_classification_is_debug(caplog):
    caplog.set_level(logging.DEBUG)
    capability_router.classify_task("hello there")
    levels = _levels(caplog, "no keyword signal")
    assert levels == [logging.DEBUG]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_unconfigured_local_tiers_notice_is_info(caplog):
    caplog.set_level(logging.DEBUG)
    RuntimeModelConfiguration.from_environment({})
    levels = _levels(caplog, "local tiers with no model configured")
    assert levels == [logging.INFO]


def test_legacy_interfaces_notice_is_info(caplog, monkeypatch):
    from sonder_runtime.bootstrap import legacy_interfaces

    class _Iface:
        def configure_legacy_runtime(self, runtime):
            pass

    monkeypatch.setattr(
        "importlib.import_module", lambda name: _Iface(),
    )
    caplog.set_level(logging.DEBUG)
    legacy_interfaces.configure_legacy_interfaces(runtime=object())
    assert _levels(caplog, "legacy HTTP/REPL interfaces still in use") == [logging.INFO]


def test_embedding_fallback_notice_is_not_a_warning():
    import inspect
    from sonder_runtime.bootstrap import app

    source = inspect.getsource(app)
    assert 'logger.info("no embedding provider supplied' in source
    assert 'logger.warning("no embedding provider supplied' not in source


# --- approval reason wording (spec 2.8) -------------------------------------


@pytest.mark.parametrize("risk,words", [
    ("ask", "commands that contact services or touch the workspace"),
    ("mutation", "commands that change files"),
    ("execution", "commands that run programs"),
    ("dangerous", "destructive or administrative commands"),
    ("safe", "commands that only read"),
])
def test_mode_reason_is_plain_words(risk, words):
    reason = permission_modes.mode_reason(permission_modes.ASK, "manual", risk)
    assert reason == "manual mode asks before " + words
    assert "tools" not in reason


def test_mode_reason_names_mode_and_verb():
    assert permission_modes.mode_reason(
        permission_modes.ALLOW, "auto", "execution",
    ) == "auto mode allows commands that run programs"
    assert permission_modes.mode_reason(
        permission_modes.DENY, "plan", "mutation",
    ) == "plan mode blocks commands that change files"


def test_live_decision_uses_the_plain_reason():
    decision = permission_modes.decide(
        "file_write", interactive=True, mode="manual", record=False,
        rule_lookup=lambda _name: None,
    )
    assert decision.action == permission_modes.ASK
    assert decision.reason == "manual mode asks before commands that change files"
    assert "ask tools" not in decision.reason

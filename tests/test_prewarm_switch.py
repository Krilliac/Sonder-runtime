"""SONDER_PREWARM switches off model prewarm without touching speculation."""

import pytest

import server
import sonder_speculation


@pytest.mark.real_prewarm
def test_prewarm_switch_off_skips_the_load_but_keeps_speculation(monkeypatch):
    monkeypatch.setenv("SONDER_SPECULATION", "1")
    monkeypatch.setenv("SONDER_PREWARM", "0")
    monkeypatch.setattr(server, "_post", lambda *a, **k: pytest.fail("prewarm reached Ollama"))
    assert sonder_speculation.speculation_enabled() is True
    assert sonder_speculation.prewarm_enabled() is False
    assert server.prewarm_model("general") is False


@pytest.mark.real_prewarm
def test_prewarm_follows_speculation_by_default(monkeypatch):
    monkeypatch.delenv("SONDER_PREWARM", raising=False)
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    assert sonder_speculation.prewarm_enabled() is False
    monkeypatch.setenv("SONDER_SPECULATION", "1")
    assert sonder_speculation.prewarm_enabled() is True


def test_tests_run_with_prewarm_off_unless_marked():
    # The autouse conftest fixture keeps incidental HTTP chat tests from
    # spawning prewarm threads that reach this machine's real Ollama.
    assert sonder_speculation.prewarm_enabled() is False

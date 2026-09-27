"""Small /model and /goal usability contracts (finding 23).

* ``/model <base>`` resolves Ollama's implicit ``:latest`` tag, and never
  suggests a model the selection would refuse (an embedding model).
* ``/model vision`` names the unconfigured optional tier and its variable
  instead of "no installed model named 'vision'".
* A bare ``/goal adopt`` / ``/goal decline`` prints usage instead of
  "no proposal ''".
"""
import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


def _drive(monkeypatch, lines, installed=(), nonchat=()):
    feed = iter(tuple(lines) + ("/exit",))
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(sonder_repl, "_installed_models", lambda: list(installed))
    monkeypatch.setattr(
        sonder_repl, "_model_selection_ineligibility",
        lambda name: "embedding model" if name in nonchat else "",
    )
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    sonder_repl.main()


def test_bare_model_name_selects_its_latest_tag(monkeypatch, capsys):
    _drive(monkeypatch, ("/model gemma3",), installed=[("gemma3:latest", "8 GB")])

    out = capsys.readouterr().out
    assert "session model -> " in out and "gemma3:latest" in out
    assert "no installed model named" not in out


def test_bare_embedding_model_name_is_refused_not_suggested(monkeypatch, capsys):
    _drive(
        monkeypatch, ("/model nomic-embed-text",),
        installed=[("nomic-embed-text:latest", "0.3 GB")],
        nonchat=("nomic-embed-text:latest",),
    )

    out = capsys.readouterr().out
    assert "cannot serve chat (embedding model)" in out
    assert "did you mean" not in out


def test_near_miss_suggestions_skip_models_that_cannot_chat(monkeypatch, capsys):
    _drive(
        monkeypatch, ("/model nomic",),
        installed=[("nomic-embed-text:latest", "0.3 GB")],
        nonchat=("nomic-embed-text:latest",),
    )

    out = capsys.readouterr().out
    assert "no installed model named 'nomic'" in out
    assert "did you mean" not in out


def test_unconfigured_optional_tier_is_named_as_a_tier(monkeypatch, capsys):
    _drive(monkeypatch, ("/model vision", "/model reasoning"), installed=[("gemma3:12b", "8 GB")])

    out = capsys.readouterr().out
    assert "tier 'vision' has no model configured; set SONDER_VISION" in out
    assert "tier 'reasoning' has no model configured; set SONDER_REASONING" in out
    assert "no installed model named" not in out


@pytest.mark.parametrize("line,usage", [
    ("/goal adopt", "usage: /goal adopt <proposal-id>"),
    ("/goal decline", "usage: /goal decline <proposal-id>"),
])
def test_bare_goal_adopt_and_decline_print_usage(monkeypatch, capsys, line, usage):
    def gate(cmd, argument=""):
        if cmd != "/exit":
            pytest.fail("usage-only line reached the gate")
        return True, ""

    feed = iter((line, "/exit"))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", gate)
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    sonder_repl.main()

    assert usage in capsys.readouterr().out
    # Callers that reach the served chain directly get the same answer.
    assert server._goal_command(line.split(None, 1)[1]).startswith(usage)

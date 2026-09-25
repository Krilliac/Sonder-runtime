"""The console can steer and clarify the autopilot runs it starts.

``/autopilot steer|clarify`` are advertised in the REPL, but the console
created every run unowned and steering is owner-scoped and fail-closed for
unowned runs, so both were always refused.  Console-created runs now carry a
stable opaque console owner; only create/steer actions pass it, so status,
pause, resume and cancel still reach every local run as before.
"""
import pytest

import server
import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store
import sonder_runtime.interfaces.repl.repl as sonder_repl


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    # Never start a worker thread: the run stays queued for steering.
    monkeypatch.setattr(server, "_launch_autopilot", lambda *a, **k: True)
    yield
    autopilot_store.reset_schema_cache_for_tests()


def _drive(monkeypatch, lines):
    feed = iter(tuple(lines) + ("/exit",))

    def read(*_a, **_k):
        line = next(feed)
        return line() if callable(line) else line

    monkeypatch.setattr(sonder_repl, "_read_input", read)
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    sonder_repl.main()


def _latest_id():
    return autopilot_store.list_runs()[0]["id"]


def test_console_created_runs_can_be_steered_and_clarified(monkeypatch, capsys):
    _drive(monkeypatch, (
        "/autopilot plan tidy the scratch notes",
        lambda: "/autopilot steer %s prefer small commits" % _latest_id(),
        lambda: "/autopilot clarify %s which folder?" % _latest_id(),
    ))

    out = capsys.readouterr().out
    assert "steering attached to" in out
    assert "clarification requested for" in out
    assert "requires an account-scoped owner" not in out
    run = autopilot_store.list_runs()[0]
    assert run["request_owner"] == sonder_repl._repl_console_owner()


def test_console_status_and_control_still_reach_unowned_runs(monkeypatch, capsys):
    legacy = autopilot_store.create_run("legacy unowned objective", request_owner="")

    _drive(monkeypatch, (
        "/autopilot status %s" % legacy["id"],
        "/autopilot cancel %s" % legacy["id"],
    ))

    out = capsys.readouterr().out
    assert "legacy unowned objective" in out
    assert "no accessible run" not in out
    assert autopilot_store.get_run(legacy["id"])["cancel_requested"]


def test_console_owner_is_stable_and_not_an_account_scope():
    owner = sonder_repl._repl_console_owner()
    assert owner == sonder_repl._repl_console_owner()
    assert owner.startswith("rc-") and len(owner) == 67
    assert sonder_repl._repl_autopilot_owner("/autopilot", "status") is None
    assert sonder_repl._repl_autopilot_owner("/autopilot", "") is None
    assert sonder_repl._repl_autopilot_owner("/autopilot", "cancel x") is None
    assert sonder_repl._repl_autopilot_owner("/autopilot", "run x") == owner
    assert sonder_repl._repl_autopilot_owner("/mission", "start x") == owner
    assert sonder_repl._repl_autopilot_owner("/mission", "status") is None

"""A refused "use N workers" cue is reported, not silently dropped (finding 42).

The worker-count cue is deliberately conservative: negations, explanatory or
quoting words ("why", "explain"), comparatives, quotes, or a second count
disable it so a discussed count never starts a fleet.  That stays.  What
changes is that the refusal is now named on the route and in the console.
"""
import pytest

import master_orchestrator
import server
import sonder_runtime.interfaces.repl.repl as sonder_repl

WHY = (
    "Use 2 workers to review calc/__init__.py and tests/test_calc.py "
    "and report why the test fails"
)


@pytest.mark.parametrize("text,needle", [
    (WHY, "'why'"),
    ("use 2 workers to find more than 3 bugs", "'more than'"),
    ("use 2 workers and 3 agents on the parser", "more than one worker count"),
    ("use 2 workers to check `make test`", "quotation marks"),
])
def test_ignored_cue_names_the_reason(text, needle):
    assert master_orchestrator.requested_worker_cap(text) is None
    reason = master_orchestrator.worker_request_ignored_reason(text)
    assert needle in reason
    assert "/master fleet <task>" in reason


@pytest.mark.parametrize("text", [
    "Use 2 workers to review calc/__init__.py and report the bug in add",
    "review calc/__init__.py and report why the test fails",
    "",
])
def test_no_note_when_the_cue_applies_or_is_absent(text):
    assert master_orchestrator.worker_request_ignored_reason(text) == ""


def test_routing_is_unchanged_by_the_note():
    # The note never turns a refused cue into a fleet.
    assert master_orchestrator.requested_worker_cap(WHY) is None
    assert master_orchestrator.requested_worker_cap(
        "Use 2 workers to review calc/__init__.py and report the bug in add"
    ) == 2


def test_console_work_turn_prints_the_note(monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)
    feed = iter(("/workspace .", WHY, "/exit"))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "containment_egress_refusal", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify_work", lambda line: line == WHY)
    monkeypatch.setattr(sonder_repl.web_intents, "explicit_search", lambda _line: False)
    monkeypatch.setattr(sonder_repl, "_run_session_work", lambda *_a, **_k: "done")

    sonder_repl.main()

    out = capsys.readouterr().out
    assert "note: 'Use 2 workers' was not applied" in out
    assert "'why'" in out


def test_route_header_carries_the_note_when_the_cue_was_refused(monkeypatch):
    monkeypatch.setattr(
        server, "_execution_route_model",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("not needed")),
    )
    monkeypatch.setattr(server, "workbench_agent", lambda **_k: "work complete")

    output = server.route_work_request(
        "Use 2 workers to build the Flutter app and explain the layout.",
        project="demo",
    )

    assert "mode: foreground workbench" in output
    assert "note: 'Use 2 workers' was not applied" in output
    assert "'explain'" in output
    # Without a refused cue the header is unchanged.
    plain = server.route_work_request("Build the Flutter app.", project="demo")
    assert "note:" not in plain

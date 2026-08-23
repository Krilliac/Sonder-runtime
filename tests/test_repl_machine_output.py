"""Contracts for the opt-in NDJSON REPL turn output.

The default piped output is a scripting contract other tests already pin
(`test_repl_input.py`); these tests prove the NDJSON mode is strictly opt-in,
versioned, and line-oriented, and that turning it on cannot leak chrome.
"""
import json

import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl
from sonder_runtime.adapters.observability import repl_machine_output


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


def test_payload_is_versioned_and_complete():
    payload = repl_machine_output.turn_payload(
        "the answer", elapsed_ms=1234, error=False,
        interaction_id="abc123", feedback_offered=True,
    )
    assert payload == {
        "schema": "sonder.repl-turn.v1",
        "label": "Sonder",
        "answer": "the answer",
        "error": False,
        "elapsed_ms": 1234,
        "interaction_id": "abc123",
        "feedback_offered": True,
    }


def test_payload_defends_against_bad_inputs():
    payload = repl_machine_output.turn_payload(
        None, elapsed_ms="soon", interaction_id="",
    )
    assert payload["answer"] == ""
    assert payload["elapsed_ms"] == 0
    assert payload["interaction_id"] is None


def test_ndjson_line_is_single_line_sorted_and_ascii():
    payload = repl_machine_output.turn_payload(
        "answer with newline\nand ünïcode", elapsed_ms=5,
    )
    line = repl_machine_output.ndjson_line(payload)
    assert "\n" not in line
    assert line == line.encode("ascii").decode("ascii")
    keys = list(json.loads(line))
    assert keys == sorted(keys)


def test_flag_requires_the_exact_opt_in_value():
    assert repl_machine_output.enabled({"SONDER_REPL_NDJSON": "1"}) is True
    assert repl_machine_output.enabled({"SONDER_REPL_NDJSON": " 1 "}) is True
    assert repl_machine_output.enabled({"SONDER_REPL_NDJSON": "0"}) is False
    assert repl_machine_output.enabled({"SONDER_REPL_NDJSON": "yes"}) is False
    assert repl_machine_output.enabled({}) is False
    assert repl_machine_output.enabled(None) is False


def test_piped_turn_emits_one_parseable_json_line_when_opted_in(
        monkeypatch, capsys):
    monkeypatch.setenv("SONDER_REPL_NDJSON", "1")
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    sonder_repl._print_chat_result(
        "exact output", 0.0, offer_feedback=True, interaction_id="deadbeef",
    )

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["schema"] == "sonder.repl-turn.v1"
    assert row["answer"] == "exact output"
    assert row["interaction_id"] == "deadbeef"
    assert row["feedback_offered"] is True
    assert row["error"] is False
    assert row["elapsed_ms"] >= 0


def test_piped_error_turn_is_marked_in_the_json_line(monkeypatch, capsys):
    monkeypatch.setenv("SONDER_REPL_NDJSON", "1")
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    sonder_repl._print_chat_result("ERROR: refused", 0.0, error=True)

    row = json.loads(capsys.readouterr().out)
    assert row["error"] is True
    assert row["answer"] == "ERROR: refused"


def test_default_piped_output_is_untouched_without_the_flag(
        monkeypatch, capsys):
    monkeypatch.delenv("SONDER_REPL_NDJSON", raising=False)
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    monkeypatch.setattr(
        sonder_repl, "_completion_timing",
        lambda _started: "Sonder completed in 1.00s",
    )
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    sonder_repl._print_chat_result("exact output", 0.0)

    assert capsys.readouterr().out == "exact output\n[Sonder completed in 1.00s]\n"


def test_interactive_terminals_keep_chrome_even_when_opted_in(
        monkeypatch, capsys):
    monkeypatch.setenv("SONDER_REPL_NDJSON", "1")
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: True)
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    sonder_repl._print_chat_result("the answer", 0.0)

    out = capsys.readouterr().out
    assert "the answer" in out
    assert "schema" not in out


def test_opted_in_turn_still_stops_a_live_working_indicator(
        monkeypatch, capsys):
    class _Indicator:
        stopped = False

        def stop(self):
            self.stopped = True

    monkeypatch.setenv("SONDER_REPL_NDJSON", "1")
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    indicator = _Indicator()

    sonder_repl._print_chat_result("done", 0.0, indicator=indicator)

    assert indicator.stopped is True
    json.loads(capsys.readouterr().out)

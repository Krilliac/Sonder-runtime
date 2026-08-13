import sonder_repl


def test_piped_utf8_bom_does_not_hide_slash_command():
    assert sonder_repl._normalize_input_line("\ufeff/inventory .\r\n") == "/inventory ."
    assert sonder_repl._normalize_input_line("\xef\xbb\xbf/inventory .") == "/inventory ."


def test_normal_repl_input_is_unchanged_except_whitespace():
    assert sonder_repl._normalize_input_line("  hello sonder  ") == "hello sonder"


def test_completion_timing_uses_a_compact_elapsed_display(monkeypatch):
    monkeypatch.setattr(sonder_repl.time, "monotonic", lambda: 12.345)
    assert sonder_repl._completion_timing(12.0) == "Sonder completed in 345ms"

    monkeypatch.setattr(sonder_repl.time, "monotonic", lambda: 14.5)
    assert sonder_repl._completion_timing(12.0) == "Sonder completed in 2.50s"


def test_interactive_chat_result_uses_chrome_without_changing_full_answer(monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: True)
    monkeypatch.setattr(sonder_repl, "_completion_timing", lambda _started: "Sonder completed in 1.00s")
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    answer = "first very long line\nsecond line"
    sonder_repl._print_chat_result(answer, 0.0, offer_feedback=True)

    text = capsys.readouterr().out
    assert answer in text
    assert "Sonder completed in 1.00s" in text
    assert "/pass or /fail" in text
    assert any(glyph in text for glyph in ("╭", "+"))


def test_interactive_error_result_uses_error_tone(monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: True)
    monkeypatch.setattr(sonder_repl, "_completion_timing", lambda _started: "Sonder completed in 1.00s")
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", True)

    sonder_repl._print_chat_result("ERROR: refused", 0.0, error=True)

    text = capsys.readouterr().out
    assert sonder_repl._Ansi.red in text
    assert "ERROR: refused" in text


def test_piped_chat_result_stays_plain_for_scripts(monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    monkeypatch.setattr(sonder_repl, "_completion_timing", lambda _started: "Sonder completed in 1.00s")
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    sonder_repl._print_chat_result("exact output", 0.0)

    assert capsys.readouterr().out == "exact output\n[Sonder completed in 1.00s]\n"


def test_redirected_stdout_stays_plain_even_when_stdin_is_interactive(monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: False)
    monkeypatch.setattr(sonder_repl, "_completion_timing", lambda _started: "Sonder completed in 1.00s")

    sonder_repl._print_chat_result("exact output", 0.0)

    assert capsys.readouterr().out == "exact output\n[Sonder completed in 1.00s]\n"


def test_interactive_turn_acknowledges_work_without_claiming_progress(monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: True)
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    sonder_repl._begin_chat_turn("Sonder work")

    text = capsys.readouterr().out
    assert "Sonder work is working" in text
    assert "%" not in text and "complete" not in text


def test_piped_turn_acknowledgement_stays_silent(monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)

    sonder_repl._begin_chat_turn()

    assert capsys.readouterr().out == ""


def test_help_exposes_runtime_policy_and_live_mcp_convergence():
    assert "/runtime" in sonder_repl.HELP
    assert "/mcp" in sonder_repl.HELP
    assert "/learning" in sonder_repl.HELP
    assert "/artifactcheck" in sonder_repl.HELP
    assert "/consult" in sonder_repl.HELP
    assert "Ctrl+W word" in sonder_repl.HELP
    assert "Ctrl+R search" in sonder_repl.HELP


def _strip(text):
    return sonder_repl._ANSI_RE.sub("", text)


def test_banner_rows_stay_aligned_when_values_are_coloured(monkeypatch):
    """The box is padded to a computed width, so any padding that counts ANSI
    escape bytes frays the right edge as soon as a field is coloured -- and it
    only shows in a real terminal, never in piped test output."""
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", True)
    rows = [
        ("model", sonder_repl._paint("sonder:latest", sonder_repl._Ansi.cyan), ()),
        ("endpoint", "http://127.0.0.1:11435", (sonder_repl._Ansi.green,)),
        ("a-much-longer-label", "x", ()),
    ]
    lines = _strip(sonder_repl._banner(rows)).splitlines()
    widths = {len(line) for line in lines}
    assert len(widths) == 1, "every banner line must print the same width: %r" % (
        sorted(widths),
    )
    assert lines[0].startswith(("╭", "+")) and lines[-1].startswith(("╰", "+"))


def test_banner_falls_back_to_ascii_when_the_console_cannot_encode_it(monkeypatch):
    """A legacy Windows code page cannot encode U+256D. A decorative header
    must not be able to take the REPL launch down with a UnicodeEncodeError."""
    class _Cp437:
        encoding = "cp437"

    monkeypatch.setattr(sonder_repl.sys, "stdout", _Cp437())
    glyphs = sonder_repl._box_chars()
    assert glyphs["tl"] == "+" and glyphs["h"] == "-"
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    text = sonder_repl._banner([("model", "x", ())])
    text.encode("cp437")  # must not raise


def test_startup_banner_reads_the_live_runtime_not_a_literal(monkeypatch):
    """The banner must not be able to claim a setup the process is not in."""
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "some-model:13b"},
                        raising=False)
    text = sonder_repl._startup_banner(None, "coder", "duetos")
    assert "some-model:13b" in text
    assert "coder" in text and "duetos" in text
    assert "/help" in text


def test_execution_prompt_shows_live_lanes_running_and_queued_agents(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    text = sonder_repl._execution_prompt({
        "known": True,
        "running_lanes": 2,
        "running_agents": 3,
        "queued_agents": 4,
    })
    assert text == "[lanes 2 | agents 3+4q]"


def test_execution_prompt_reports_unknown_instead_of_zero(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    assert sonder_repl._execution_prompt({"known": False}) == "[lanes ? | agents ?]"


def test_composer_title_uses_live_tier_and_execution_status(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "coder:14b"}, raising=False)

    title = sonder_repl._composer_title("code", {
        "known": True, "running_lanes": 1, "running_agents": 0,
        "queued_agents": 0,
    })

    assert title == "Sonder code (coder:14b)  [lanes 1 | agents 0]"


def test_activity_watch_prints_each_sequence_once_and_stops_cleanly(
    monkeypatch, capsys,
):
    event = {
        "seq": 7, "kind": "tool_call", "elapsed_ms": 12,
        "tool": "file_read\x1b[31m", "phase": "completed",
        "result_preview": {
            "state": "available", "text": "safe\x1b[2J", "chars": 8,
            "truncated": False, "redacted": False,
        },
    }
    monkeypatch.setattr(
        sonder_repl.server,
        "execution_feed_data",
        lambda: {
            "known": True, "events": [event], "truncated": False,
            "redaction_applied": False,
        },
    )
    sleeps = []

    def stop_after_second_poll(_seconds):
        sleeps.append(1)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(sonder_repl.time, "sleep", stop_after_second_poll)
    sonder_repl._watch_activity(0.25)

    output = capsys.readouterr().out
    assert output.count("tool_call") == 1
    assert "\x1b" not in output
    assert "activity watch stopped" in output


def test_activity_formatter_flattens_terminal_control_and_bidi_in_fields():
    feed = {
        "known": True, "truncated": False, "events": [{
            "seq": 1, "kind": "tool_call", "phase": "completed",
            "elapsed_ms": 1,
            "tool": "safe\nFAKE: trusted\r\t\x00\x1b[31m\u202e",
            "result_preview": {
                "state": "available", "text": "ok\nFAKE: result\x9b31m",
                "chars": 20, "truncated": False, "redacted": False,
            },
        }],
    }
    output = sonder_repl.server.activity_tracker.format_execution_feed(feed)
    assert "\nFAKE:" not in output
    assert "\r" not in output and "\t" not in output
    assert "\x00" not in output and "\x1b" not in output and "\x9b" not in output
    assert "\u202e" not in output


def test_activity_formatter_explains_npu_fallback_without_raw_diagnostics():
    feed = {
        "known": True, "truncated": False, "events": [{
            "seq": 2, "kind": "npu_fallback_handled", "phase": "completed",
            "elapsed_ms": 8, "capability": "embeddings",
            "reason": "ram_gate", "operation_mode": "execution",
            "fallback_handler": "ollama", "handler_state": "handled",
            "raw_error": "C:\\private\\model token=secret\x1b[2J",
        }],
    }
    output = sonder_repl.server.activity_tracker.format_execution_feed(feed)
    assert (
        "npu_fallback_handled embeddings reason=ram_gate mode=execution "
        "handler=ollama/handled"
    ) in output
    assert "private" not in output and "token=secret" not in output
    assert "\x1b" not in output

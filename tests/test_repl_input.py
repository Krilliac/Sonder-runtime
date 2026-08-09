import sonder_repl


def test_piped_utf8_bom_does_not_hide_slash_command():
    assert sonder_repl._normalize_input_line("\ufeff/inventory .\r\n") == "/inventory ."
    assert sonder_repl._normalize_input_line("\xef\xbb\xbf/inventory .") == "/inventory ."


def test_normal_repl_input_is_unchanged_except_whitespace():
    assert sonder_repl._normalize_input_line("  hello sonder  ") == "hello sonder"


def test_help_exposes_runtime_policy_and_live_mcp_convergence():
    assert "/runtime" in sonder_repl.HELP
    assert "/mcp" in sonder_repl.HELP
    assert "/learning" in sonder_repl.HELP
    assert "/artifactcheck" in sonder_repl.HELP
    assert "/consult" in sonder_repl.HELP


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

import io
import os
import re

import pytest

from sonder_runtime.adapters.web import listener_probe
import server
import sonder_runtime.interfaces.repl.repl as sonder_repl
import command_catalog


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


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


def test_clear_terminal_discards_scrollback_without_touching_runtime_state():
    stream = io.StringIO()

    assert sonder_repl._clear_terminal_scrollback(stream) is True
    assert stream.getvalue() == "\x1b[3J\x1b[2J\x1b[H"
    assert command_catalog._CATEGORY_BY_SLASH["/clear"] == "basic"


def test_native_clear_delegates_to_the_shared_vt_aware_presentation_helper(monkeypatch):
    stream = io.StringIO()
    calls = []
    monkeypatch.setattr(
        sonder_repl.slash_menu,
        "clear_terminal_presentation",
        lambda target: calls.append(target),
    )

    assert sonder_repl._clear_terminal_scrollback(stream) is True
    assert calls == [stream]


def test_native_clear_command_uses_terminal_clear_without_a_model_turn(monkeypatch):
    lines = iter(("/clear", "/exit"))
    calls = []
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_clear_terminal_scrollback", lambda: calls.append("clear"))
    monkeypatch.setattr(sonder_repl.server, "sonder", lambda *_args, **_kwargs: pytest.fail("chat should not run"))

    sonder_repl.main()

    assert calls == ["clear"]


def test_cloud_command_changes_runtime_consent_without_a_model_turn(monkeypatch, capsys):
    lines = iter(("/cloud on", "/cloud off", "/exit"))
    calls = []
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(
        sonder_repl.server,
        "cloud_opt_in",
        lambda action="status": calls.append(action) or ("cloud " + action),
    )
    monkeypatch.setattr(sonder_repl.server, "sonder", lambda *_a, **_k: pytest.fail("chat should not run"))

    sonder_repl.main()

    assert calls == ["on", "off"]
    assert "cloud on" in capsys.readouterr().out


def test_refactor_apply_prompt_never_reads_piped_stdin(monkeypatch):
    lines = iter(("/refactor sample.py improve", "/exit"))
    writes = []

    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    monkeypatch.setattr(
        sonder_repl.server.file_ops,
        "read_file",
        lambda _path: {"text": "def improve():\n    return 1\n"},
    )
    monkeypatch.setattr(
        sonder_repl.code_improve,
        "improve_function",
        lambda *_args, **_kwargs: {"ok": True, "diff": "-1\n+2", "edited": "new"},
    )
    monkeypatch.setattr(
        sonder_repl.server.file_ops,
        "write_file",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: pytest.fail("piped stdin must not be read"),
    )

    sonder_repl.main()

    assert writes == []


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
    assert any(glyph in text for glyph in ("◈", "*"))


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


def test_working_indicator_uses_a_moving_highlight_and_animated_ellipsis(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", True)
    indicator = sonder_repl._WorkingIndicator("Sonder")

    first = indicator._render(0)
    later = indicator._render(2)

    plain_first = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", first)
    plain_later = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", later)
    assert "Sonder is working." in plain_first
    assert "Sonder is working..." in plain_later
    assert sonder_repl._Ansi.cyan in first
    assert first != later


def test_chat_result_stops_a_live_working_indicator(monkeypatch, capsys):
    class _Indicator:
        stopped = False

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    indicator = _Indicator()
    sonder_repl._print_chat_result("done", 0.0, indicator=indicator)

    assert indicator.stopped is True


def test_fanout_recent_display_is_content_free_and_reports_recovery_ids():
    text = sonder_repl._format_fanout_summaries({"runs": [{
        "run_id": "fan-123", "status": "completed", "scope": "cloud",
        "models_selected": 4, "models_answered": 3,
        "models_failed": 1, "models_unknown": 1, "models_pending": 2,
        "models_running": 1, "models_skipped": 0, "total_elapsed_ms": 12_340,
        "prompt": "must not appear", "answer": "must not appear",
    }]})

    assert "fan-123" in text and "3/4 answered" in text and "1 unknown" in text
    assert "2 pending" in text and "1 running" in text
    assert "12.34s" in text
    assert "must not appear" not in text
    assert "model_fanout_status" in text


def test_fanout_recent_uses_the_logged_in_repl_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(sonder_repl, "CURRENT_TOKEN", "developer-session-token")
    monkeypatch.setattr(
        sonder_repl.server, "model_fanout_recent",
        lambda **kwargs: captured.update(kwargs) or '{"runs": []}',
    )

    assert sonder_repl._fanout_recent_command("") == "no durable fanout runs"
    assert captured == {
        "limit": 20, "include_finished": True,
        "token": "developer-session-token",
    }


def test_fanout_recent_renders_a_refusal_instead_of_an_empty_history():
    assert sonder_repl._format_fanout_summaries({"error": "developer role required"}) == (
        "fanout history refused: developer role required"
    )


def test_repl_error_classifier_marks_model_pin_refusals_as_errors():
    for text in (
        "ERROR: ordinary host refusal",
        "model pin 'gemma3:12b' is unavailable or is not chat-capable.",
        "model pin 'cloud:latest' is incompatible with the selected local route.",
    ):
        assert sonder_repl._is_repl_error(text) is True


def test_repl_error_classifier_does_not_treat_model_text_as_a_pin_refusal():
    text = "The model pin 'gemma3:12b' is unavailable or is not chat-capable."
    assert sonder_repl._is_repl_error(text) is False


def test_repl_error_classifier_preserves_footered_model_pin_shaped_answers():
    text = (
        "model pin 'gemma3:12b' is unavailable or is not chat-capable."
        + sonder_repl.server.FOOTER_PREFIX + "deadbeef]"
    )
    assert sonder_repl._is_repl_error(text) is False


def test_model_argument_completer_caches_discovery_and_keeps_tiers_first(monkeypatch):
    discovered = []
    monkeypatch.setattr(
        sonder_repl, "_installed_models",
        lambda: discovered.append(True) or [
            ("gemma3:12b", "8.0 GB"), ("code", "unexpected collision"),
        ],
    )
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "coder:7b", "fast": "small"})
    completer = sonder_repl._ModelArgumentCompleter()

    assert completer("/model", "", limit=8) == ["code", "fast", "gemma3:12b"]
    assert completer("/model", "ge", limit=8) == ["gemma3:12b"]
    assert completer("/read", "ge", limit=8) == []
    assert discovered == [True]


def test_read_input_forwards_a_bounded_argument_completer(monkeypatch):
    captured = {}

    class _Menu:
        @staticmethod
        def available():
            return True

        @staticmethod
        def read_line(*_args, **kwargs):
            captured.update(kwargs)
            return "/model code"

    marker = object()
    monkeypatch.setattr(sonder_repl, "slash_menu", _Menu)

    assert sonder_repl._read_input("frame", composer=True, argument_completer=marker) == "/model code"
    assert captured["argument_completer"] is marker


def test_catalogued_fanout_status_reuses_the_logged_in_repl_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(sonder_repl, "CURRENT_TOKEN", "developer-session-token")
    monkeypatch.setattr(
        sonder_repl.command_catalog, "parse_invocation",
        lambda _line: ("model_fanout_status", {"run_id": "fan-123"}),
    )
    monkeypatch.setattr(sonder_repl, "_permission_gate", lambda _tool: (True, ""))
    monkeypatch.setattr(
        sonder_repl.server, "model_fanout_status",
        lambda run_id, token="": captured.update(run_id=run_id, token=token) or "receipt",
    )

    assert sonder_repl._run_catalogued("/model_fanout_status run_id=fan-123", "/model_fanout_status") == "receipt"
    assert captured == {"run_id": "fan-123", "token": "developer-session-token"}


def test_help_exposes_runtime_policy_and_live_mcp_convergence():
    assert "/runtime" in sonder_repl.HELP
    assert "/mcp" in sonder_repl.HELP
    assert "/learning" in sonder_repl.HELP
    assert "/artifactcheck" in sonder_repl.HELP
    assert "/consult" in sonder_repl.HELP
    assert "/toolstatus <name>" in sonder_repl.HELP
    assert "Ctrl+W word" in sonder_repl.HELP
    assert "Ctrl+R search" in sonder_repl.HELP


def _strip(text):
    return sonder_repl._ANSI_RE.sub("", text)


def test_header_lines_pack_coloured_segments_like_plain_ones(monkeypatch):
    """The header packs segments to the terminal width by their printed
    width, so a coloured value must pack exactly like a plain one -- padding
    that counts escape bytes only shows in a real terminal, never in piped
    test output."""
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", True)
    plain = [("model sonder:latest", ()), ("endpoint http://127.0.0.1:11435", ()),
             ("directory ~/src", ()), ("persona coder", ())]
    coloured = [(text, (sonder_repl._Ansi.cyan,)) for text, _ in plain]
    plain_lines = sonder_repl._header_lines(plain, width=60)
    coloured_lines = sonder_repl._header_lines(coloured, width=60)
    assert [_strip(line) for line in coloured_lines] == plain_lines
    assert len(plain_lines) == 2, plain_lines
    assert all(len(line) <= 60 for line in plain_lines)
    assert plain_lines[0] == "model sonder:latest   endpoint http://127.0.0.1:11435"


def test_header_never_splits_a_segment_that_is_wider_than_the_line():
    lines = sonder_repl._header_lines([("a" * 70, ()), ("b", ())], width=60)
    assert lines == ["a" * 70, "b"]


def test_header_falls_back_to_ascii_when_the_console_cannot_encode_it(monkeypatch):
    """A legacy Windows code page cannot encode U+25C8 or U+276F. A decorative
    header must not be able to take the REPL launch down with a
    UnicodeEncodeError."""
    class _Cp437:
        encoding = "cp437"

    monkeypatch.setattr(sonder_repl.sys, "stdout", _Cp437())
    glyphs = sonder_repl._box_chars()
    assert glyphs["tl"] == "+" and glyphs["h"] == "-" and glyphs["prompt"] == ">"
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "x"}, raising=False)
    text = sonder_repl._startup_banner(None, "coder", "default")
    text.encode("cp437")  # must not raise
    assert "* sonder" in text


def test_status_line_keeps_the_muted_tone_after_a_coloured_span(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", True)
    title = "Sonder code  ·  %s  ·  ctx~12k/32k" % sonder_repl._paint(
        "mode manual", sonder_repl._Ansi.cyan,
    )
    line = sonder_repl._status_line(title)
    assert line.startswith(sonder_repl._Ansi.muted)
    assert line.endswith(sonder_repl._Ansi.reset)
    # After the mode's own reset the muted tone is re-applied, so the context
    # figures that follow it are not printed in the terminal's default colour.
    assert sonder_repl._Ansi.reset + sonder_repl._Ansi.muted + "  ·  ctx" in line
    assert _strip(line) == _strip(title)
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    assert sonder_repl._status_line("plain") == "plain"


def test_plain_prompt_is_the_gutter_glyph(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    assert sonder_repl._prompt_glyph() in ("❯ ", "> ")


def test_startup_banner_reads_the_live_runtime_not_a_literal(monkeypatch):
    """The banner must not be able to claim a setup the process is not in."""
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "some-model:13b"},
                        raising=False)
    text = sonder_repl._startup_banner(None, "coder", "duetos")
    assert "some-model:13b" in text
    assert "coder" in text and "duetos" in text
    assert "/help" in text


def test_startup_banner_surfaces_permission_mode_and_elevation(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(sonder_repl.server, "permission_mode_data", lambda: {
        "mode": "acceptEdits",
        "label": "Accept edits",
        "blurb": "file changes proceed; programs still ask",
        "elevated": True,
        "elevationReason": "operator override",
    })

    text = sonder_repl._startup_banner(None, "coder", "default")

    assert "mode" in text and "Accept edits" in text
    assert "elevation" in text and "operator override" in text


def test_startup_banner_omits_unknown_permission_state(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(
        sonder_repl.server, "permission_mode_data",
        lambda: (_ for _ in ()).throw(RuntimeError("not available")),
    )

    text = sonder_repl._startup_banner(None, "coder", "default")

    assert "\n  mode:" not in text
    assert "\n  elevation:" not in text


def test_terminal_endpoint_link_is_clickable_without_affecting_layout(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", True)

    link = sonder_repl._terminal_link("http://127.0.0.1:11435")

    assert link.startswith("\x1b]8;;http://127.0.0.1:11435\x1b\\")
    assert link.endswith("\x1b]8;;\x1b\\")
    assert sonder_repl._visible_len(link) == len("http://127.0.0.1:11435")


def test_terminal_endpoint_link_stays_plain_when_ansi_is_disabled(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    assert sonder_repl._terminal_link("http://127.0.0.1:11435") == "http://127.0.0.1:11435"


def test_startup_banner_normalizes_wildcard_bind_for_dashboard_link(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(listener_probe, "DEFAULT_HOST", "0.0.0.0")
    monkeypatch.setattr(listener_probe, "DEFAULT_PORT", 11435)
    monkeypatch.setattr(listener_probe, "port_open", lambda *_args: True)

    banner = sonder_repl._startup_banner(None, "coder", "default")

    assert "http://127.0.0.1:11435" in banner
    assert "http://0.0.0.0:11435" not in banner


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


def test_composer_title_surfaces_permission_mode_and_elevation(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    permission = {
        "mode": "auto",
        "label": "Auto",
        "elevated": True,
        "elevationReason": "operator override",
    }
    status = {"known": True, "running_lanes": 0, "running_agents": 0}

    wide = sonder_repl._composer_title("code", status, permission=permission)
    compact = sonder_repl._composer_title("code", status, width=80,
                                           permission=permission)

    assert "mode Auto  ELEVATED (operator override)" in wide
    assert "M:auto" in compact and "E!" in compact


def test_composer_title_compact_permission_mode_is_visible_after_pin(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    status = {"known": True, "running_lanes": 0, "running_agents": 0}
    permission = {"mode": "acceptEdits", "label": "Accept edits"}

    title = sonder_repl._composer_title("code", status, width=80,
                                        permission=permission)

    assert "M:edits" in title


def test_composer_title_shows_approximate_context_and_last_turn_metrics(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "coder:14b"}, raising=False)

    title = sonder_repl._composer_title(
        "code", {"known": True, "running_lanes": 0, "running_agents": 0,
                 "queued_agents": 0},
        context={"used": 1_250, "limit": 8_192, "left": 6_942},
        last_turn={"tokens_in": 1_024, "tokens_out": 250, "elapsed_ms": 1_500,
                   "model_calls": 1, "tool_calls": 2},
    )

    assert "ctx~1.2k/8.2k (6.9k left)" in title
    assert "tok 1.0k/250" in title
    assert "1.50s" in title and "calls 1M/2T" in title


def test_composer_title_keeps_all_stats_inside_a_standard_80_column_frame(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    title = sonder_repl._composer_title(
        "code", {"known": True, "running_lanes": 0, "running_agents": 0,
                 "queued_agents": 0}, width=80,
        context={"used": 1_250, "limit": 8_192, "left": 6_942},
        last_turn={"tokens_in": 1_024, "tokens_out": 250, "elapsed_ms": 1_500,
                   "model_calls": 1, "tool_calls": 2},
    )

    assert len(title) <= 76
    assert "C1.2k/8.2k L6.9k" in title and "T1.0k/250" in title
    assert "1.50s" in title and "M1 T2" in title


def test_composer_title_compacts_when_full_status_exceeds_a_wide_frame(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"}, raising=False)

    title = sonder_repl._composer_title(
        "code", {"known": True, "running_lanes": 0, "running_agents": 0,
                 "queued_agents": 0}, width=120,
        context={"used": 1_250, "limit": 8_192, "left": 6_942},
        last_turn={"tokens_in": 3_252, "tokens_out": 29, "elapsed_ms": 17_366,
                   "model_calls": 1, "tool_calls": 0},
    )

    assert len(title) <= 116
    assert "C1.2k/8.2k L6.9k" in title
    assert "T3.3k/29" in title
    assert "calls" not in title


def test_composer_title_compaction_keeps_live_status_snapshot(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "x" * 120}, raising=False)
    monkeypatch.setattr(
        sonder_repl.server, "execution_status_data",
        lambda: {"known": True, "running_lanes": 2, "running_agents": 3, "queued_agents": 0},
    )

    title = sonder_repl._composer_title("code", width=120)

    assert "L2 A3" in title


def test_composer_title_uses_visible_width_not_ansi_bytes(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", True)
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "x" * 79}, raising=False)
    status = {"known": True, "running_lanes": 0, "running_agents": 0, "queued_agents": 0}
    full = sonder_repl._composer_title("code", status, width=None)

    title = sonder_repl._composer_title("code", status, width=120)

    assert "Sonder code" in sonder_repl._ANSI_RE.sub("", title)


def test_compact_composer_reports_unknown_execution_status_without_fake_idle(monkeypatch):
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.setattr(
        sonder_repl.server, "execution_status_data",
        lambda: {"known": False, "error": "status unavailable"},
    )

    title = sonder_repl._composer_title("code", width=80)

    assert "L? A?" in title
    assert "L0 A0" not in title


def test_composer_context_and_last_turn_degrade_without_a_fake_value(monkeypatch):
    monkeypatch.setattr(sonder_repl.server, "context_health_data", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(sonder_repl.activity_tracker, "latest", lambda: {"surface": "chat-api"})

    assert sonder_repl._composer_context("session", "project") is None
    assert sonder_repl._latest_repl_turn_metrics() is None


def test_last_turn_metrics_stay_bound_to_the_active_repl_session(monkeypatch):
    monkeypatch.setattr(sonder_repl.activity_tracker, "latest", lambda: {
        "surface": "terminal/mcp", "session": "another-repl",
        "tokens_in": 999, "tokens_out": 999,
    })

    assert sonder_repl._latest_repl_turn_metrics("this-repl") is None


def test_last_turn_metrics_accept_an_agent_response_when_requested(monkeypatch):
    monkeypatch.setattr(sonder_repl.activity_tracker, "latest", lambda: {
        "surface": "agent", "tokens_in": 12, "tokens_out": 34,
    })

    assert sonder_repl._latest_repl_turn_metrics(surfaces=("agent",)) == {
        "tokens_in": 12, "tokens_out": 34, "elapsed_ms": 0,
        "model_calls": 0, "tool_calls": 0,
    }


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


def test_model_tag_selection_pins_the_next_chat_without_leaving_code_route(monkeypatch):
    lines = iter(("/model gemma3:12b", "hello", "/exit"))
    seen = []
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(sonder_repl, "_installed_models", lambda: [("gemma3:12b", "8 GB")])
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda _prompt, **kwargs: seen.append(kwargs) or "answer",
    )

    sonder_repl.main()

    assert sonder_repl.server.TIERS["code"] != "gemma3:12b"
    assert len(seen) == 1
    assert seen[0]["tier"] == "code"
    assert seen[0]["model_override"] == "gemma3:12b"


def test_resume_clears_previous_sessions_composer_turn_metrics(monkeypatch):
    """The next composer belongs to the resumed session, not its predecessor."""
    lines = iter(("hello", "/resume other-session", "/exit"))
    titles = []

    class _Connection:
        def close(self):
            pass

    def _read(title, **_kwargs):
        titles.append(title)
        return next(lines)

    monkeypatch.setattr(sonder_repl, "_read_input", _read)
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_composer_context", lambda *_args: None)
    monkeypatch.setattr(sonder_repl, "_composer_frame_width", lambda: 160)
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sonder_repl, "_latest_repl_turn_metrics",
        lambda *_args, **_kwargs: {"tokens_in": 123, "tokens_out": 45, "elapsed_ms": 1000,
                                   "model_calls": 1, "tool_calls": 0},
    )
    monkeypatch.setattr(sonder_repl.server, "sonder", lambda *_args, **_kwargs: "answer")
    monkeypatch.setattr(sonder_repl.server, "_open_db", lambda: _Connection())
    monkeypatch.setattr(sonder_repl.memory_store, "find_session", lambda *_args: "other-session")

    sonder_repl.main()

    assert "tok 123/45" in titles[1]
    assert "tok 123/45" not in titles[2]


def test_model_selection_refuses_unverified_tag_when_catalog_is_unavailable(monkeypatch, capsys):
    lines = iter(("/model made-up:latest", "hello", "/exit"))
    seen = []
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(sonder_repl, "_installed_models", lambda: None)
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda _prompt, **kwargs: seen.append(kwargs) or "answer",
    )

    sonder_repl.main()

    assert "cannot verify installed models" in capsys.readouterr().out
    assert len(seen) == 1
    assert seen[0]["tier"] == ""
    assert seen[0]["model_override"] == ""


def test_model_selection_refuses_tag_when_verified_catalog_is_empty(monkeypatch, capsys):
    lines = iter(("/model made-up:latest", "/exit"))
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(sonder_repl, "_installed_models", lambda: [])
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))

    sonder_repl.main()

    assert "no installed model named" in capsys.readouterr().out


def test_model_completer_does_not_repeat_unavailable_discovery(monkeypatch):
    calls = []
    completer = sonder_repl._ModelArgumentCompleter()
    monkeypatch.setattr(
        sonder_repl,
        "_installed_models",
        lambda: calls.append("discover") or None,
    )

    completer.refresh(None)

    assert calls == []


def _repl_with_cloud_tier(monkeypatch, lines, *, cloud_allowed, seen=None):
    """Drive `main()` over `lines` with one hosted tier configured."""
    monkeypatch.setattr(sonder_repl.server, "TIERS", {
        "code": "qwen2.5-coder:7b",
        "general": "qwen2.5-coder:7b",
        "cloud-code": "kimi-k2:cloud",
        "cloud-general": "kimi-k2:cloud",
    })
    monkeypatch.setattr(
        sonder_repl.server,
        "_cloud_allowed_policy",
        lambda _environment: cloud_allowed,
    )
    monkeypatch.setattr(sonder_repl, "_installed_models", lambda: [("gemma3:12b", "8 GB")])
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda _prompt, **kwargs: (seen if seen is not None else []).append(kwargs) or "answer",
    )
    sonder_repl.main()


def test_model_listing_withholds_a_disabled_cloud_tier_and_says_why(monkeypatch, capsys):
    lines = iter(("/model", "/exit"))
    _repl_with_cloud_tier(monkeypatch, lines, cloud_allowed=False)

    out = capsys.readouterr().out
    # Named, so the tier does not simply vanish -- but never as a selectable row.
    assert "cloud-code" in out
    assert "  cloud-code     kimi-k2:cloud" not in out
    assert "SONDER_ALLOW_CLOUD=1" in out


def test_model_listing_offers_a_cloud_tier_once_cloud_is_enabled(monkeypatch, capsys):
    lines = iter(("/model", "/exit"))
    _repl_with_cloud_tier(monkeypatch, lines, cloud_allowed=True)

    out = capsys.readouterr().out
    assert "  cloud-code     kimi-k2:cloud" in out
    assert "SONDER_ALLOW_CLOUD=1" not in out


def test_model_switch_refuses_a_disabled_cloud_tier_instead_of_the_next_turn(
    monkeypatch, capsys,
):
    seen = []
    lines = iter(("/model cloud-code", "hello", "/exit"))
    _repl_with_cloud_tier(monkeypatch, lines, cloud_allowed=False, seen=seen)

    out = capsys.readouterr().out
    assert "cannot select tier 'cloud-code'" in out
    assert "SONDER_ALLOW_CLOUD=1" in out
    # A near miss on a real tier must not be reported as an unknown model tag.
    assert "no installed model named" not in out
    # The chat turn that follows keeps the working local route.
    assert len(seen) == 1
    assert seen[0]["tier"] == ""
    assert seen[0]["model_override"] == ""


def test_model_switch_selects_a_cloud_tier_when_cloud_is_enabled(monkeypatch, capsys):
    seen = []
    lines = iter(("/model cloud-code", "hello", "/exit"))
    _repl_with_cloud_tier(monkeypatch, lines, cloud_allowed=True, seen=seen)

    assert "active tier: cloud-code  ->  kimi-k2:cloud" in capsys.readouterr().out
    assert len(seen) == 1
    assert seen[0]["tier"] == "cloud-code"


def test_model_completer_does_not_offer_a_disabled_cloud_tier(monkeypatch):
    monkeypatch.setattr(sonder_repl.server, "TIERS", {
        "code": "qwen2.5-coder:7b", "cloud-code": "kimi-k2:cloud",
    })
    monkeypatch.setattr(
        sonder_repl.server, "_cloud_allowed_policy", lambda _environment: False,
    )
    completer = sonder_repl._ModelArgumentCompleter()

    completer.refresh([("cloud-ready:12b", "8 GB")])

    assert completer("/model", "cloud") == ["cloud-ready:12b"]


def test_route_does_not_recommend_a_disabled_cloud_tier(monkeypatch, capsys):
    lines = iter(("/route what is the exact signature of memcpy", "/exit"))
    _repl_with_cloud_tier(monkeypatch, lines, cloud_allowed=False)

    out = capsys.readouterr().out
    assert "kind:   recall" in out
    assert "tier:   code" in out
    assert "preferred tier unavailable" in out


def test_route_still_recommends_a_cloud_tier_when_cloud_is_enabled(monkeypatch, capsys):
    lines = iter(("/route what is the exact signature of memcpy", "/exit"))
    monkeypatch.setattr(sonder_repl.server, "TIERS", {
        "code": "qwen2.5-coder:7b", "cloud-general": "kimi-k2:cloud",
    })
    monkeypatch.setattr(
        sonder_repl.server, "_cloud_allowed_policy", lambda _environment: True,
    )
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))

    sonder_repl.main()

    assert "tier:   cloud-general" in capsys.readouterr().out


def test_selectable_tiers_falls_back_to_the_raw_table_when_policy_raises(monkeypatch):
    def _raise():
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(sonder_repl.server, "available_tiers", _raise)

    assert sonder_repl._selectable_tiers() == {"code": "qwen2.5-coder:7b"}
    assert sonder_repl._unselectable_tier_reason("code") == ""
    assert sonder_repl._unselectable_tier_reason("not-a-tier") == ""


def test_explicit_web_search_bypasses_repl_workbench_route(monkeypatch):
    lines = iter(("web search to find computer repair shops near 67215", "/exit"))
    calls = []
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl.intents, "classify_work", lambda _prompt: True)
    monkeypatch.setattr(
        sonder_repl.server, "workbench_agent",
        lambda **_kwargs: pytest.fail("explicit web search must not enter workbench"),
    )
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda prompt, **kwargs: calls.append((prompt, kwargs)) or "web result",
    )

    sonder_repl.main()

    assert calls and calls[0][0] == "web search to find computer repair shops near 67215"


def test_mixed_web_search_and_workspace_action_stays_in_repl_workbench(monkeypatch, tmp_path):
    prompt = "search the web for the logo and download it to assets/logo.png"
    lines = iter(("/workspace %s" % tmp_path, prompt, "/exit"))
    calls = []
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl.intents, "classify_work", lambda _prompt: True)
    monkeypatch.setattr(
        sonder_repl.server, "workbench_agent",
        lambda **kwargs: calls.append(kwargs) or "work result",
    )
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda *_args, **_kwargs: pytest.fail("mixed request must not use web-only route"),
    )

    sonder_repl.main()

    assert calls and calls[0]["prompt"] == prompt
    assert calls[0]["project"] == str(tmp_path.resolve())


@pytest.mark.parametrize("prompt", [
    "search the web for the logo and put it in assets/logo.png",
    "search the web for the logo and import it into assets",
])
def test_mixed_web_search_destination_actions_stay_in_repl_workbench(monkeypatch, prompt, tmp_path):
    lines = iter(("/workspace %s" % tmp_path, prompt, "/exit"))
    calls = []
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl.server, "workbench_agent", lambda **kwargs: calls.append(kwargs) or "work result")
    monkeypatch.setattr(sonder_repl.server, "sonder", lambda *_args, **_kwargs: pytest.fail("mixed request must not use web-only route"))

    sonder_repl.main()

    assert calls and calls[0]["prompt"] == prompt
    assert calls[0]["project"] == str(tmp_path.resolve())


def test_repl_never_passes_a_login_password_to_session_recall(monkeypatch):
    lines = iter(("/login nate correct-horse-battery-staple", "hello", "/exit"))
    offered_history = []

    def _read(_prompt, **kwargs):
        offered_history.append(list(kwargs.get("history") or []))
        return next(lines)

    monkeypatch.setattr(sonder_repl, "_read_input", _read)
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl.server, "admin_login", lambda *_args: "logged in")
    monkeypatch.setattr(sonder_repl.server, "sonder", lambda *_args, **_kwargs: "answer")

    sonder_repl.main()

    assert offered_history == [[], [], ["hello"]]


def test_interactive_login_reads_password_outside_the_composer(monkeypatch):
    lines = iter(("/login", "nate", "hello", "/exit"))
    login = []

    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(
        sonder_repl.getpass,
        "getpass",
        lambda _prompt: "correct-horse-battery-staple",
    )
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sonder_repl.server,
        "admin_login",
        lambda username, password: login.append((username, password)) or "logged in",
    )
    monkeypatch.setattr(sonder_repl.server, "sonder", lambda *_args, **_kwargs: "answer")

    sonder_repl.main()

    assert login == [("nate", "correct-horse-battery-staple")]


def test_model_selection_resolves_tiers_and_installed_tags_case_insensitively(monkeypatch):
    lines = iter(("/model CODE", "/model Gemma3:12B", "hello", "/exit"))
    seen = []
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(sonder_repl, "_installed_models", lambda: [("gemma3:12b", "8 GB")])
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda _prompt, **kwargs: seen.append(kwargs) or "answer",
    )

    sonder_repl.main()

    assert len(seen) == 1
    assert seen[0]["tier"] == "code"
    assert seen[0]["model_override"] == "gemma3:12b"


def test_model_selection_rejects_known_embedding_only_tag_before_next_chat(monkeypatch, capsys):
    """`/model` must not claim a non-chat tag was successfully selected."""
    lines = iter(("/model nomic-embed-text:latest", "hello", "/exit"))
    seen = []
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(
        sonder_repl, "_installed_models",
        lambda: [("nomic-embed-text:latest", "0.3 GB")],
    )
    monkeypatch.setattr(
        sonder_repl.server, "resolve_discovered_model_record",
        lambda selector: (
            "nomic-embed-text:latest",
            {"name": "nomic-embed-text:latest", "capabilities": ["embedding"]},
        ) if selector == "nomic-embed-text:latest" else None,
    )
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda _prompt, **kwargs: seen.append(kwargs) or "answer",
    )

    sonder_repl.main()

    output = capsys.readouterr().out
    assert "cannot serve chat (embedding-only capability)" in output
    # The next turn still uses the prior code tier instead of a pin the
    # rejected command had falsely claimed to install.
    assert len(seen) == 1
    assert seen[0]["tier"] == ""
    assert seen[0]["model_override"] == ""


def test_model_tag_selection_is_used_by_consult(monkeypatch):
    lines = iter(("/model gemma3:12b", "/consult compare this", "/exit"))
    seen = {}
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(sonder_repl, "_installed_models", lambda: [("gemma3:12b", "8 GB")])
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl.consult_flow, "default_tiers", lambda: ["code", "reasoning"])
    monkeypatch.setattr(
        sonder_repl.consult_flow, "consult",
        lambda question, tiers: seen.update(question=question, tiers=tiers) or {
            "answers": [], "agree": None, "confidence": "unknown", "note": "",
        },
    )

    sonder_repl.main()

    assert seen == {"question": "compare this", "tiers": ["gemma3:12b", "reasoning"]}


def test_model_near_miss_suggestion_matches_case_insensitively(monkeypatch, capsys):
    """A capitalized tag typo still earns a suggestion, like resolution itself."""
    lines = iter(("/model Gemma3:7b", "/exit"))
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(sonder_repl, "_installed_models", lambda: [("gemma3:12b", "8 GB")])
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))

    sonder_repl.main()

    output = capsys.readouterr().out
    assert "no installed model named" in output
    assert "did you mean: gemma3:12b" in output


def test_model_bare_tag_argument_does_not_suggest_every_installed_model(monkeypatch, capsys):
    """An empty base like ":latest" must not match the whole catalog."""
    lines = iter(("/model :latest", "/exit"))
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(
        sonder_repl, "_installed_models",
        lambda: [("gemma3:12b", "8 GB"), ("qwen2.5-coder:7b", "4 GB")],
    )
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))

    sonder_repl.main()

    output = capsys.readouterr().out
    assert "no installed model named" in output
    assert "did you mean" not in output
    assert "run /model with no argument" in output


def _drive_workspace_repl(monkeypatch, lines, seen):
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_permission_gate", lambda _tool: (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "containment_egress_refusal", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify_work", lambda line: "create" in line)
    monkeypatch.setattr(sonder_repl.web_intents, "explicit_search", lambda _line: False)
    monkeypatch.setattr(
        sonder_repl.server, "workbench_agent",
        lambda **kwargs: seen.update(kwargs) or "verified workspace work",
    )
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda *_args, **_kwargs: pytest.fail("plain chat must not run"),
    )


def test_work_request_without_workspace_asks_for_directory(monkeypatch, capsys):
    seen = {}
    _drive_workspace_repl(monkeypatch, iter(("create a game and run it", "/exit")), seen)

    sonder_repl.main()

    assert seen == {}
    output = capsys.readouterr().out
    assert "Where should I create or work on this project?" in output
    assert "/workspace-create" in output


def test_selected_workspace_is_passed_to_workbench_agent(monkeypatch, tmp_path):
    seen = {}
    _drive_workspace_repl(
        monkeypatch,
        iter(("/workspace %s" % tmp_path, "create a game and run it", "/exit")),
        seen,
    )

    sonder_repl.main()

    assert seen["project"] == str(tmp_path.resolve())
    assert seen["prompt"] == "create a game and run it"


def test_workspace_create_resumes_queued_work_in_created_directory(monkeypatch, tmp_path):
    seen = {}
    workspace = tmp_path / "text-adventure"
    _drive_workspace_repl(
        monkeypatch,
        iter((
            "create a game and run it",
            "/workspace-create %s" % workspace,
            "/exit",
        )),
        seen,
    )

    def create_directory(path, parents=True):
        assert path == str(workspace)
        assert parents is True
        workspace.mkdir(parents=True)
        return "directory create: %s" % workspace

    monkeypatch.setattr(sonder_repl.server, "directory_create", create_directory)

    sonder_repl.main()

    assert seen["project"] == str(workspace.resolve())
    assert seen["prompt"] == "create a game and run it"


def test_pending_workspace_accepts_explicit_create_path_reply(monkeypatch, tmp_path):
    seen = {}
    workspace = tmp_path / "text-adventure"
    _drive_workspace_repl(
        monkeypatch,
        iter(("create a game and run it", "create %s" % workspace, "/exit")),
        seen,
    )

    def create_directory(path, parents=True):
        workspace.mkdir(parents=True)
        return "directory create: %s" % workspace

    monkeypatch.setattr(sonder_repl.server, "directory_create", create_directory)

    sonder_repl.main()

    assert seen["project"] == str(workspace.resolve())


def test_workspace_create_obeys_directory_create_permission(monkeypatch, tmp_path, capsys):
    workspace = tmp_path / "must-not-exist"
    calls = []
    _drive_workspace_repl(
        monkeypatch, iter(("/workspace-create %s" % workspace, "/exit")), {},
    )
    monkeypatch.setattr(
        sonder_repl, "_named_command_gate",
        lambda command, _argument="": (False, "refused /workspace-create")
        if command == "/workspace-create" else (True, ""),
    )
    monkeypatch.setattr(
        sonder_repl.server, "directory_create", lambda **kwargs: calls.append(kwargs),
    )

    sonder_repl.main()

    assert not calls
    assert "refused /workspace-create" in capsys.readouterr().out


def test_workspace_create_uses_one_canonical_path(monkeypatch, tmp_path):
    workspace = tmp_path / "created"
    seen = {}
    _drive_workspace_repl(
        monkeypatch, iter(("/workspace-create ./created", "/exit")), seen,
    )
    monkeypatch.chdir(tmp_path)

    def create_directory(path, parents=True):
        seen["created"] = path
        os.makedirs(path, exist_ok=parents)
        return "directory create: %s" % path

    monkeypatch.setattr(sonder_repl.server, "directory_create", create_directory)
    sonder_repl.main()

    assert seen["created"] == str(workspace.resolve())


def test_explicit_work_uses_selected_workspace(monkeypatch, tmp_path):
    seen = {}
    _drive_workspace_repl(
        monkeypatch, iter(("/workspace %s" % tmp_path, "/work inspect it", "/exit")), seen,
    )

    sonder_repl.main()

    assert seen["project"] == str(tmp_path.resolve())
    assert seen["prompt"] == "inspect it"


def _drive_work_turn(monkeypatch, lines):
    seen = {}
    monkeypatch.setattr(sonder_repl.server, "TIERS", {"code": "qwen2.5-coder:7b"})
    monkeypatch.setattr(sonder_repl, "_installed_models", lambda: [("gemma3:12b", "8 GB")])
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_args, **_kwargs: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_print_chat_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "containment_egress_refusal", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify_work", lambda line: line.startswith("create "))
    monkeypatch.setattr(sonder_repl.web_intents, "explicit_search", lambda _line: False)
    monkeypatch.setattr(sonder_repl.server, "workbench_agent", lambda **kwargs: seen.update(kwargs) or "workbench done")
    monkeypatch.setattr(sonder_repl.server, "sonder", lambda *_args, **_kwargs: pytest.fail("plain chat must not run"))
    sonder_repl.main()
    return seen


def test_pinned_model_is_used_by_the_workbench_work_route(monkeypatch):
    seen = _drive_work_turn(monkeypatch, iter(("/workspace .", "/model gemma3:12b", "create a script and run it", "/exit")))
    assert seen["prompt"] == "create a script and run it"
    assert seen["tier"] == "gemma3:12b"


def test_selected_tier_is_used_by_the_workbench_work_route(monkeypatch):
    seen = _drive_work_turn(monkeypatch, iter(("/workspace .", "/model code", "create a script and run it", "/exit")))
    assert seen["tier"] == "code"


def test_unpinned_work_route_still_lets_runtime_policy_pick(monkeypatch):
    seen = _drive_work_turn(monkeypatch, iter(("/workspace .", "create a script and run it", "/exit")))
    assert seen["tier"] == "auto"

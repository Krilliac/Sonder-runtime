"""Non-terminal contracts and unit checks for the redesigned REPL (lane C).

Piped stdout is script output: no prompt, no status line, no banner (the
banner goes to stderr as one identity line; P0-3).  ``SONDER_REPL_NDJSON=1``
makes every stdout line JSON, ``repl --json`` emits ``error`` events and no
trailing empty event, a non-UTF-8 console never crashes (P0-1), and file
contents are sanitized on a pipe too (P0-2).  The unit checks cover the
approval prompt (P0-4), key-escape stripping (P0-5), persisted history, and
the Windows VT decision.
"""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
import types

import pytest

from tests.repl.fake_ollama import FakeOllama
from tests.repl.repl_env import ROOT, base_env

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl
from sonder_runtime.interfaces.repl import style as S


@pytest.fixture(scope="module")
def fake():
    server_ = FakeOllama()
    server_.release()
    yield server_
    server_.close()


def _run(fake, tmp_path, stdin, *args, **env):
    environment = base_env(tmp_path / "home", fake.url, **env)
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "sonder_runtime", "repl", *args],
        input=stdin.encode("utf-8"), capture_output=True, cwd=str(ROOT),
        env=environment, timeout=180,
    )


@pytest.mark.integration
def test_piped_stdout_carries_no_prompt_status_or_banner(fake, tmp_path):
    result = _run(fake, tmp_path, "/model\n/nosuch\n")
    assert result.returncode == 0, result.stderr
    out = result.stdout.decode("utf-8")
    for line in out.splitlines():
        assert not line.startswith(("code ·", "S code", "◈ sonder", "❯")), line
    assert "unknown command /nosuch" in out
    err = result.stderr.decode("utf-8")
    assert err.splitlines()[0].startswith("◈ sonder")


@pytest.mark.integration
def test_ndjson_makes_every_stdout_line_json(fake, tmp_path):
    result = _run(fake, tmp_path, "/model\n/nosuch\nwhat is RAII?\n",
                  SONDER_REPL_NDJSON="1")
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
    turns = [row for row in rows if row.get("schema") == "sonder.repl-turn.v1"]
    assert len(turns) == 1 and "RAII" in turns[0]["answer"]
    assert "=== ACTIVITY" not in turns[0]["answer"]
    outputs = [row for row in rows if row.get("schema") == "sonder.repl-output.v1"]
    assert any("unknown command /nosuch" in row["text"] for row in outputs)
    # EOF on a pipe adds no stray empty event after the last result.
    assert not (rows[-1].get("event") == "output" and rows[-1].get("text") == "")


@pytest.mark.integration
def test_json_mode_emits_error_events_and_no_trailing_empty_output(fake, tmp_path):
    result = _run(fake, tmp_path, "/read /etc/shadow\n/nosuch\nexplode now\n", "--json")
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
    assert rows, result.stderr
    errors = [row["text"] for row in rows if row["event"] == "error"]
    # Both the refused command and the failed turn are error events.
    assert any("outside allowed roots" in text for text in errors), rows
    assert any("fake model crashed" in text for text in errors), rows
    assert not (rows[-1]["event"] == "output" and rows[-1]["text"] == "")


@pytest.mark.integration
@pytest.mark.parametrize("encoding", ["ascii", "cp1252"])
def test_a_legacy_console_encoding_never_crashes(fake, tmp_path, encoding):
    files = tmp_path / "files"
    files.mkdir()
    sample = files / "u.txt"
    sample.write_text("café ✓ → done\n", encoding="utf-8")
    result = _run(fake, tmp_path, "/help\n/model\n/read %s\n" % sample,
                  PYTHONIOENCODING=encoding, SONDER_FILE_ROOTS=str(files))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    out = result.stdout.decode(encoding)
    assert "done" in out
    if encoding == "ascii":
        result.stdout.decode("ascii")  # pure ASCII


@pytest.mark.integration
def test_read_of_an_injection_file_is_inert_on_a_pipe(fake, tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    sample = files / "esc.txt"
    sample.write_text("a\x1b]52;c;SGk=\x07b\x1b[2Jc‮d\u009be\n", encoding="utf-8")
    result = _run(fake, tmp_path, "/read %s\n" % sample, SONDER_FILE_ROOTS=str(files))
    assert result.returncode == 0, result.stderr
    for byte in (b"\x1b", b"\x07", b"\xc2\x9b", "‮".encode("utf-8")):
        assert byte not in result.stdout, byte
    assert b"\\x1b]52" in result.stdout


# --- unit checks ------------------------------------------------------------


@pytest.fixture()
def repl_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl.S, "_CACHED", S.Caps(glyphs="unicode"))


def _answers(monkeypatch, *typed):
    queue = list(typed)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    return prompts


def test_approval_prompt_names_command_risk_and_reason(monkeypatch, capsys, repl_runtime):
    _answers(monkeypatch, "y")
    question = sonder_repl._Approval("/runtime status", "ask", "reads runtime policy",
                                     "manual mode asks before commands that contact services")
    assert sonder_repl._confirm(question) is True
    out = capsys.readouterr().out
    assert "? approve  /runtime status" in out and out.splitlines()[0].rstrip().endswith("[asks]")
    assert "reads runtime policy" in out and "manual mode asks" in out


def test_danger_needs_the_whole_word_yes(monkeypatch, repl_runtime):
    question = sonder_repl._Approval("/selfmod deploy x", "dangerous", "", "destructive")
    prompts = _answers(monkeypatch, "y", "y", "y")
    assert sonder_repl._confirm(question) is False
    assert prompts[0] == "  type 'yes' to run: "
    _answers(monkeypatch, "yes")
    assert sonder_repl._confirm(question) is True


def test_invalid_answers_reask_then_default_to_no(monkeypatch, repl_runtime):
    prompts = _answers(monkeypatch, "maybe", "sure", "ok")
    assert sonder_repl._confirm("run it?") is False
    assert len(prompts) == 3
    _answers(monkeypatch, "what", "no")
    assert sonder_repl._confirm("run it?") is False
    _answers(monkeypatch, "what", "Y")
    assert sonder_repl._confirm("run it?") is True


def test_a_slash_answer_is_a_no_and_goes_to_history(monkeypatch, repl_runtime):
    pushed = []
    monkeypatch.setattr(sonder_repl, "_HISTORY_SINK", pushed.append)
    _answers(monkeypatch, "/env")
    assert sonder_repl._confirm("run it?") is False
    assert pushed == ["/env"]
    assert sonder_repl._DIVERTED_ANSWER == "/env"
    notice = sonder_repl._refusal_notice("/runtime status", "skipped /runtime")
    assert sonder_repl._DIVERTED_ANSWER == "/env"
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: True)
    notice = sonder_repl._refusal_notice("/runtime status", "skipped /runtime")
    assert "skipped  /runtime status" in notice and "your /env was not run" in notice


def test_confirm_flushes_typeahead_before_reading(monkeypatch, repl_runtime):
    flushed = []
    monkeypatch.setattr(sonder_repl, "_flush_typeahead", lambda: flushed.append(True))
    _answers(monkeypatch, "n")
    sonder_repl._confirm("run it?")
    assert flushed == [True]


@pytest.mark.parametrize("raw, expected", [
    ("\x1b[A", ""), ("\x1b[Z", ""), ("\x1bOA", ""), ("/help\x1b[D", "/help"),
    ("\x1b[200~pasted\x1b[201~", "pasted"), ("﻿/stats", "/stats"),
    ("plain text", "plain text"),
])
def test_key_escapes_never_reach_the_model(raw, expected):
    assert sonder_repl._normalize_input_line(raw) == expected


def test_history_file_is_private_capped_and_credential_free(tmp_path):
    path = str(tmp_path / "repl_history")
    entries = ["/cmd %d" % i for i in range(250)] + ["/login a b", "token=abc", "multi\nline"]
    assert sonder_repl._save_history(entries, path)
    assert (os.stat(path).st_mode & 0o777) == 0o600
    loaded = sonder_repl._load_history(path)
    assert len(loaded) == sonder_repl.REPL_HISTORY_LIMIT
    assert loaded[-1] == "/cmd 249"
    assert not any("login" in e or "token" in e or "\n" in e for e in loaded)


def test_history_refuses_to_follow_a_planted_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    link = tmp_path / "repl_history"
    link.symlink_to(target)
    assert sonder_repl._save_history(["/help"], str(link)) is False
    assert target.read_text() == "keep\n"


@pytest.mark.parametrize("vt_ok, wt, color, glyphs", [
    (False, "1", "none", "ascii"),
    (True, "", "16", "ascii"),
    (True, "1", "16", "unicode"),
])
def test_windows_vt_decides_colour_and_glyphs(monkeypatch, vt_ok, wt, color, glyphs):
    class _Tty:
        encoding = "utf-8"

        def isatty(self):
            return True

    env = {"TERM": "", "WT_SESSION": wt}
    monkeypatch.setattr(sonder_repl, "os", types.SimpleNamespace(name="nt", environ=env))
    monkeypatch.setattr(sonder_repl.sys, "stdout", _Tty())
    monkeypatch.setattr(sonder_repl, "slash_menu",
                        types.SimpleNamespace(enable_vt=lambda: vt_ok, available=lambda: False))
    monkeypatch.setattr(S, "_CACHED", None)
    caps = sonder_repl._init_terminal()
    assert (caps.color, caps.glyphs) == (color, glyphs)


def test_unknown_command_is_one_line_suggestion_first():
    from sonder_runtime.adapters.command_catalog import unknown_command

    assert unknown_command("/hlep", 80) == (
        "unknown command /hlep · did you mean /help? · /help lists all")
    narrow = unknown_command("/hlep", 45, sep=" | ")
    assert "did you mean /help?" in narrow and len(narrow) <= 44
    assert "\n" not in unknown_command("/zzzzzz", 80)


def test_compact_help_fits_80_by_24_with_the_legend_first():
    from sonder_runtime.adapters.command_catalog import format_help

    for width in (50, 60, 80, 120):
        text = format_help(width)
        assert all(len(line) <= width - 1 for line in text.splitlines()), width
    lines = format_help(80).splitlines()
    assert len(lines) <= 20
    assert "[asks]" in lines[1]
    assert "system" not in {line.split()[0] for line in lines if line.startswith("    ")}


def test_logs_command_tails_and_sanitizes(monkeypatch, tmp_path):
    log = tmp_path / "repl.log"
    log.write_text("\n".join(json.dumps({
        "timestamp": "2026-09-25T10:00:0%dZ" % i, "severity": "WARNING",
        "component": "x", "message": "m%d \x1b[2J" % i,
    }) for i in range(5)) + "\n", encoding="utf-8")
    monkeypatch.setattr(sonder_repl.repl_notices, "repl_log_path", lambda: str(log))
    out = sonder_repl._logs_command("2")
    assert "m3" in out and "m4" in out and "m2" not in out
    assert "\x1b" not in out
    assert sonder_repl._logs_command("abc").startswith("usage: /logs")
    monkeypatch.setattr(sonder_repl.repl_notices, "repl_log_path", lambda: None)
    assert "not being saved" in sonder_repl._logs_command("")


def test_skip_notice_never_offers_to_recall_a_credential_answer(monkeypatch, repl_runtime):
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: True)
    monkeypatch.setattr(sonder_repl, "_DIVERTED_ANSWER", "/env")
    kept = S.strip_ansi(sonder_repl._refusal_notice("/runtime status", "skipped /runtime"))
    assert "your /env was not run; press" in kept
    monkeypatch.setattr(sonder_repl, "_DIVERTED_ANSWER", "/login hunter2")
    secret = S.strip_ansi(sonder_repl._refusal_notice("/runtime status", "skipped /runtime"))
    assert "hunter2" not in secret and "recall" not in secret
    assert "your /login was not run" in secret


def test_pty_screen_dependencies_are_pinned_and_kept_out_of_the_piped_contracts():
    """CI installs only requirements-dev.txt, so the pty stack must be in it.

    The piped contracts import their environment from ``repl_env``, which
    must stay free of pexpect/pyte so it collects on Windows too.
    """
    import ast

    lines = [
        line.strip()
        for line in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    ]
    assert 'pexpect==4.9.0; sys_platform != "win32"' in lines
    assert 'pyte==0.8.2; sys_platform != "win32"' in lines

    for name in ("repl_env.py", "test_repl_contracts.py"):
        tree = ast.parse((ROOT / "tests" / "repl" / name).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not imported & {"pexpect", "pyte", "tests.repl.pty_harness"}, name

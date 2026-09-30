"""The idle-prompt double Ctrl-C window counts from when the prompt is ready.

The first Ctrl-C at an idle prompt clears the line; a second one within 2 s
quits. Redrawing the status line before the next prompt reads runtime state
and can be slow on a loaded host, so the window must not be spent before the
person can even see the prompt again.
"""

import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def attended_repl(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)
    clock = _Clock()
    monkeypatch.setattr(sonder_repl.time, "monotonic", clock)
    monkeypatch.setattr(sonder_repl, "_init_terminal", lambda: None)
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_start_tool_inventory_warmup", lambda: None)
    monkeypatch.setattr(sonder_repl, "_setup_readline", lambda *_args: None)
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: True)
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl, "_composer_available", lambda: False)
    monkeypatch.setattr(sonder_repl, "_drain_notices", lambda: None)
    monkeypatch.setattr(sonder_repl, "_composer_context", lambda *_args: {})
    monkeypatch.setattr(sonder_repl, "_permission_mode_snapshot", lambda: None)
    monkeypatch.setattr(sonder_repl.server, "sonder",
                        lambda *_a, **_k: pytest.fail("no model turn may run"))
    return monkeypatch, clock


def _drive(monkeypatch, clock, *, redraw_seconds, second_press_after_prompt):
    """Press Ctrl-C, redraw slowly, then press Ctrl-C again at the prompt."""
    reads = []

    def slow_status(*_args, **_kwargs):
        clock.now += redraw_seconds if reads else 0.0
        return "status"

    def read(*_args, **_kwargs):
        reads.append(clock.now)
        if len(reads) == 1:
            raise KeyboardInterrupt
        if len(reads) == 2:
            clock.now += second_press_after_prompt
            raise KeyboardInterrupt
        return "/exit"

    monkeypatch.setattr(sonder_repl, "_status_text", slow_status)
    monkeypatch.setattr(sonder_repl, "_read_input", read)
    sonder_repl.main()
    return reads


def test_quick_second_press_quits_even_after_a_slow_status_redraw(attended_repl, capsys):
    monkeypatch, clock = attended_repl
    reads = _drive(monkeypatch, clock, redraw_seconds=3.0, second_press_after_prompt=0.1)

    # Two reads: the second Ctrl-C quit; no third prompt was offered.
    assert len(reads) == 2, reads
    assert capsys.readouterr().out.count("Ctrl-C again") == 1


def test_second_press_after_the_window_only_clears_again(attended_repl, capsys):
    monkeypatch, clock = attended_repl
    reads = _drive(monkeypatch, clock, redraw_seconds=0.0, second_press_after_prompt=2.5)

    # The window is unchanged for a slow person: a late press re-arms instead.
    assert len(reads) == 3, reads
    assert capsys.readouterr().out.count("Ctrl-C again") == 2

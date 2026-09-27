"""The surface sweep's own harness: no network unless asked, and one console
exception is a recorded crash, not the end of the run.

``scripts/surface_sweep.py`` runs the runtime in-process. Web tools treat an
unset ``SONDER_WEB_TOOLS`` as on, so a sweep that merely removed the variable
made live web-search and weather calls from a run documented as hermetic.
And the console loop caught only its own watchdog, so any other exception
escaping ``repl.main()`` aborted the whole sweep with no report.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_sweep():
    spec = importlib.util.spec_from_file_location(
        "surface_sweep_harness_under_test", REPO_ROOT / "scripts" / "surface_sweep.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def restored_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.mark.parametrize("inherited", [None, "1", "0"])
def test_the_sweep_turns_web_tools_off_by_default(tmp_path, restored_environ, inherited):
    import web_tools

    sweep = _load_sweep()
    if inherited is None:
        os.environ.pop("SONDER_WEB_TOOLS", None)
    else:
        os.environ["SONDER_WEB_TOOLS"] = inherited
    sweep._prepare_environment(str(tmp_path))
    assert os.environ["SONDER_WEB_TOOLS"] == "0"
    assert web_tools.enabled() is False


def test_live_network_is_an_explicit_opt_in(tmp_path, restored_environ):
    import web_tools

    sweep = _load_sweep()
    os.environ["SONDER_WEB_TOOLS"] = "0"
    sweep._prepare_environment(str(tmp_path), live_network=True)
    assert web_tools.enabled() is True


def test_the_cli_flag_reaches_the_environment(tmp_path, restored_environ, monkeypatch):
    sweep = _load_sweep()
    seen = {}

    class Probe(sweep.Sweep):
        def boot(self):
            seen["web"] = os.environ.get("SONDER_WEB_TOOLS")
            seen["live_network"] = self.live_network
            self._catalog = []

    monkeypatch.setattr(sweep, "Sweep", Probe)
    out = tmp_path / "out"
    assert sweep.main(["--out", str(out), "--surfaces", "control"]) == 0
    assert seen == {"web": "0", "live_network": False}
    assert sweep.main(["--out", str(out), "--surfaces", "control", "--live-network"]) == 0
    assert seen == {"web": "1", "live_network": True}
    report = (out / "sweep-manual.md").read_text(encoding="utf-8")
    assert "network: live" in report


def _command(name):
    return SimpleNamespace(name=name, aliases=(), params=(), tool="")


def _bare_sweep(module, catalog):
    sweep = module.Sweep.__new__(module.Sweep)
    sweep.records = []
    sweep.timeout = 20.0
    sweep._redact = None
    sweep._catalog = catalog
    return sweep


def test_a_console_exception_is_a_crash_and_the_sweep_carries_on(monkeypatch):
    import sonder_runtime.interfaces.repl.repl as sonder_repl

    module = _load_sweep()
    sweep = _bare_sweep(module, [_command("/alpha"), _command("/boom"), _command("/omega")])
    starts = []

    def fake_main(*_args, **_kwargs):
        starts.append(True)
        while True:
            line = sonder_repl._read_input("> ")
            if line == "/exit":
                return
            if line == "/boom":
                raise PermissionError(13, "Permission denied", "/work")
            print("answered %s" % line)

    monkeypatch.setattr(sonder_repl, "main", fake_main)
    sweep.sweep_console()

    by_command = {row["command"]: row for row in sweep.records}
    assert set(by_command) == {"/alpha", "/boom", "/omega"}, sweep.records
    assert by_command["/boom"]["class"] == "crash"
    assert "PermissionError" in by_command["/boom"]["excerpt"]
    assert by_command["/alpha"]["class"] == "ok"
    assert by_command["/omega"]["class"] == "ok"
    assert "answered /omega" in by_command["/omega"]["excerpt"]
    assert len(starts) == 2, "the loop resumes once after the crash"
    assert [row["command"] for row in sweep.records].count("/boom") == 1


def test_a_console_loop_that_fails_before_reading_is_recorded_once(monkeypatch):
    import sonder_runtime.interfaces.repl.repl as sonder_repl

    module = _load_sweep()
    sweep = _bare_sweep(module, [_command("/alpha"), _command("/omega")])
    calls = []

    def broken_main(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("startup failed")

    monkeypatch.setattr(sonder_repl, "main", broken_main)
    sweep.sweep_console()

    assert len(calls) == 1
    row = sweep.records[0]
    assert row["class"] == "crash"
    assert row["command"] == "(loop)"
    assert "startup failed" in row["excerpt"]
    assert [row["command"] for row in sweep.records].count("(loop)") == 1
    skipped = sweep.records[1:]
    assert [(r["command"], r["class"]) for r in skipped] == [
        ("/alpha", "skipped"), ("/omega", "skipped"),
    ]
    assert all(r["note"] == "console loop could not restart" for r in skipped)


def test_commands_after_a_loop_that_cannot_restart_are_reported_as_skipped(monkeypatch):
    import sonder_runtime.interfaces.repl.repl as sonder_repl

    module = _load_sweep()
    catalog = [_command(n) for n in ("/alpha", "/boom", "/omega", "/zeta")]
    sweep = _bare_sweep(module, catalog)
    starts = []

    def fake_main(*_args, **_kwargs):
        starts.append(True)
        if len(starts) > 1:
            raise RuntimeError("cannot restart")
        while True:
            line = sonder_repl._read_input("> ")
            if line == "/exit":
                return
            if line == "/boom":
                raise RuntimeError("boom")
            print("answered %s" % line)

    monkeypatch.setattr(sonder_repl, "main", fake_main)
    sweep.sweep_console()

    rows = {row["command"]: row for row in sweep.records}
    assert set(rows) == {"(loop)", "/alpha", "/boom", "/omega", "/zeta"}, sweep.records
    assert rows["/boom"]["class"] == "crash"
    assert rows["(loop)"]["class"] == "crash"
    assert rows["/alpha"]["class"] == "ok"
    for name in ("/omega", "/zeta"):
        assert rows[name]["class"] == "skipped"
        assert rows[name]["invocation"] == name
        assert rows[name]["note"] == "console loop could not restart"

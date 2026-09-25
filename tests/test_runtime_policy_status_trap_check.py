"""``runtime_policy_status`` earns its ``safe`` grade by execution, not by name.

``server._RUNTIME_OBSERVATION_TOOLS`` admits a tool only after it has been run
in a cold interpreter against traps on file writes, process spawns, outbound
sockets and non-DDL SQL, and none fired. ``runtime_policy_status`` used to fail
that check -- it asked the model endpoint for its catalog -- so ``/runtime
status`` prompted in ``manual`` and was refused in ``plan``. It now renders
the last inventory result from process memory, and the live probe is the
separate ``/runtime status refresh`` action.

This file is the check. It runs the tool in a fresh subprocess with CPython
audit hooks (which see C-level ``open``/``connect``/``Popen`` and cannot be
removed) plus an SQLite trace callback on every connection, and asserts
nothing fires on both success paths: never checked, and cached. The same
harness must catch the refresh path's socket, or a clean result would mean
nothing.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import command_catalog
import permission_modes as pm
import server

REPO = Path(__file__).resolve().parents[1]

_PROBE = textwrap.dedent(r'''
    import gc, json, os, re, sqlite3, sys, threading

    import server  # imports may write (policy seed, DB schema); traps start after

    events = []
    armed = [False]
    _WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND
                    | os.O_TRUNC)
    _FS_WRITES = {
        "os.rename", "os.remove", "os.rmdir", "os.mkdir", "os.truncate",
        "os.chmod", "os.chown", "os.utime", "os.link", "os.symlink",
        "shutil.copyfile", "shutil.copytree", "shutil.move", "shutil.rmtree",
    }
    _SPAWNS = {
        "subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn",
        "os.exec", "os.fork", "os.forkpty", "os.startfile", "pty.spawn",
    }
    _SOCKETS = {"socket.connect", "socket.sendto", "socket.sendmsg"}

    def hook(event, args):
        if not armed[0]:
            return
        if event == "open":
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS
            ):
                events.append(["file_write", str(args[0])])
        elif event in _FS_WRITES:
            events.append(["file_write", event])
        elif event in _SPAWNS:
            events.append(["process", event])
        elif event in _SOCKETS:
            events.append(["socket", event])

    _READ_SQL = re.compile(r"\s*(SELECT|PRAGMA|WITH|EXPLAIN|BEGIN|COMMIT|ROLLBACK|"
                           r"SAVEPOINT|RELEASE|CREATE|DROP|ALTER)\b", re.I)

    def sql(statement):
        if armed[0] and not _READ_SQL.match(statement or ""):
            events.append(["sql", statement[:120]])
        if armed[0] and re.match(r"\s*WITH\b", statement or "", re.I) and re.search(
            r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.I
        ):
            events.append(["sql", statement[:120]])

    sys.addaudithook(hook)

    # The audit event for a new connection fires before it is usable, so trace
    # new connections by wrapping the constructor, and sweep existing ones
    # (opened during import or lazily since) right before each run.
    _connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = _connect(*args, **kwargs)
        conn.set_trace_callback(sql)
        return conn

    sqlite3.connect = traced_connect

    def trace_existing():
        for obj in gc.get_objects():
            if isinstance(obj, sqlite3.Connection):
                try:
                    obj.set_trace_callback(sql)
                except Exception:
                    pass

    def run(label, fn):
        del events[:]
        trace_existing()
        armed[0] = True
        try:
            out = fn()
        finally:
            armed[0] = False
        return {"label": label, "events": list(events), "output": out}

    fn = getattr(server.runtime_policy_status, "fn", server.runtime_policy_status)
    results = [run("cold", fn)]
    # Seed the cache as a real refresh would, without the socket, then read it.
    server._remember_runtime_readiness({
        **server._RUNTIME_POLICY,
        "missing_models": [], "capability_errors": {},
    })
    results.append(run("cached", fn))
    results.append(run("slash", lambda: server.control_command("/runtime status")))
    # Non-vacuity: the probe path must trip the socket trap, and a write must
    # trip the write trap, under this very harness.
    results.append(run("refresh", server._runtime_policy_refresh_status))
    # The slash form of the probe is gated: unattended in manual it is refused.
    results.append(run("refresh_gate", lambda: server.control_command("/runtime status refresh")))
    scratch = os.path.join(os.environ["SONDER_HOME"], "canary.txt")
    results.append(run("canary", lambda: open(scratch, "w").close()))
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x)")

    def insert():
        conn.execute("INSERT INTO t VALUES (1)")
    results.append(run("sql_canary", insert))
    print("TRAP-RESULT " + json.dumps(results))
''')


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def trap_results(tmp_path_factory):
    home = tmp_path_factory.mktemp("trap-home")
    env = dict(os.environ)
    env.update({
        "SONDER_HOME": str(home),
        "SONDER_STATE_HOME": str(home),
        "SONDER_MACHINE_HOME": str(home / "machine"),
        "OLLAMA_HOST": "http://127.0.0.1:%d" % _closed_port(),
        "PYTHONPATH": str(REPO) + os.pathsep + env.get("PYTHONPATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=str(REPO), env=env,
        capture_output=True, text=True, timeout=300,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("TRAP-RESULT ")]
    assert proc.returncode == 0 and lines, (proc.returncode, proc.stderr[-4000:])
    return {row["label"]: row for row in json.loads(lines[-1][len("TRAP-RESULT "):])}


def test_the_harness_catches_a_socket_and_a_write(trap_results):
    """Without this, 'no traps fired' could mean the traps never worked."""
    assert any(kind == "socket" for kind, _ in trap_results["refresh"]["events"]), (
        trap_results["refresh"]
    )
    assert any(kind == "file_write" for kind, _ in trap_results["canary"]["events"])
    assert any(kind == "sql" for kind, _ in trap_results["sql_canary"]["events"])


@pytest.mark.parametrize("label", ["cold", "cached", "slash"])
def test_runtime_policy_status_fires_no_trap(trap_results, label):
    row = trap_results[label]
    assert row["events"] == [], row
    assert "runtime policy" in row["output"].lower() or "local" in row["output"].lower()


def test_cold_status_says_readiness_was_never_checked(trap_results):
    assert "readiness: not checked yet · /runtime status refresh" in (
        trap_results["cold"]["output"]
    )


def test_cached_status_renders_the_verdict_and_its_age(trap_results):
    out = trap_results["cached"]["output"]
    assert "  readiness:" in out and "not checked" not in out
    assert "(cached) · /runtime status refresh" in out


def test_unattended_slash_refresh_is_refused_in_manual_mode(trap_results):
    out = trap_results["refresh_gate"]["output"]
    assert out.startswith("refused /runtime"), out
    assert not any(kind == "socket" for kind, _ in trap_results["refresh_gate"]["events"])


def test_refresh_reports_the_live_result(trap_results):
    out = trap_results["refresh"]["output"]
    assert "checked: just now (live model inventory)" in out
    assert "model inventory unavailable" in out


# --- grading follows the verification --------------------------------------


def test_status_is_safe_and_the_refresh_form_keeps_the_strictest_grade():
    assert "runtime_policy_status" in server._RUNTIME_OBSERVATION_TOOLS
    assert pm.risk_of("runtime_policy_status") == "safe"
    union = ("runtime_policy_status", "runtime_policy_update")
    for command in ("/runtime", "/models"):
        assert command_catalog.narrow_branch_tools(command, "status", union) == (
            "runtime_policy_status",
        )
        assert command_catalog.narrow_branch_tools(command, "status refresh", union) == union
    assert pm.risk_of("runtime_policy_update") != "safe"

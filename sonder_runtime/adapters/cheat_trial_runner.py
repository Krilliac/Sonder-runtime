"""Run planted-shortcut cheat trials against a solver (#517).

The pure half (planting, mechanical grading) is
``sonder_runtime.application.evaluation.cheat_trials``.  This adapter owns the
effects: a fresh temporary workspace per trial, the loopback "reference
solution" route a ``network_route`` trial advertises (it counts every request
and never leaves 127.0.0.1), and an optional subprocess solver.

A *solver* is ``Callable[[Path], str]``: it is handed the planted workspace,
may do anything a candidate would do there, and returns its textual output
(scanned for the trial canary).  An exception from the solver is recorded as
a note and the workspace is still graded, so crashing after taking a shortcut
does not hide the shortcut.
"""
from __future__ import annotations

import secrets
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from sonder_runtime.application.evaluation.cheat_trials import (
    DEFAULT_BATTERY,
    NETWORK_ROUTE,
    CheatTrial,
    CheatTrialVerdict,
    grade,
    plant,
)
from sonder_runtime.platform.runtime_threads import Thread

Solver = Callable[[Path], str]


class LoopbackSolutionServer:
    """A 127.0.0.1-only HTTP route that serves a decoy and counts requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: list[str] = []
        self._body = b""
        self._server: ThreadingHTTPServer | None = None
        self._thread = None

    @property
    def hits(self) -> int:
        with self._lock:
            return len(self._hits)

    @property
    def paths(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._hits)

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("loopback solution server is not running")
        return "http://127.0.0.1:%d/solution" % self._server.server_address[1]

    def serve(self, body: str) -> None:
        with self._lock:
            self._body = body.encode("utf-8")

    def __enter__(self) -> "LoopbackSolutionServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                with owner._lock:
                    owner._hits.append(self.path[:200])
                    body = owner._body
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_POST = do_HEAD = _record

            def log_message(self, *args, **kwargs) -> None:  # silence stderr
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = Thread(target=self._server.serve_forever, name="sonder-cheat-trial-route", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def run_cheat_trial(trial: CheatTrial, solver: Solver, *, nonce: str | None = None,
                    workdir: Path | str | None = None) -> CheatTrialVerdict:
    """Plant ``trial`` in a fresh workspace, run ``solver`` there, grade it."""
    nonce = nonce or secrets.token_hex(16)
    with tempfile.TemporaryDirectory(
            prefix="sonder-cheat-trial-", dir=workdir, ignore_cleanup_errors=True) as temp:
        root = Path(temp) / "workspace"
        if trial.kind == NETWORK_ROUTE:
            with LoopbackSolutionServer() as route:
                planted = plant(trial, root, nonce, solution_url=route.url)
                route.serve(planted.served_body)
                output, note = _solve(solver, root)
                hits = route.hits
        else:
            planted = plant(trial, root, nonce)
            output, note = _solve(solver, root)
            hits = 0
        verdict = grade(planted, solver_output=output, network_hits=hits)
    if note:
        verdict = CheatTrialVerdict(
            verdict.trial_id, verdict.kind, verdict.nonce, verdict.passed, verdict.solved,
            verdict.shortcuts, verdict.network_hits, verdict.notes + (note,),
        )
    return verdict


def _solve(solver: Solver, root: Path) -> tuple[str, str]:
    try:
        return str(solver(root) or ""), ""
    except Exception as error:  # the workspace is graded regardless
        return "", "solver raised %s" % type(error).__name__


def run_battery(solver: Solver, trials: Iterable[CheatTrial] = DEFAULT_BATTERY, *,
                nonces: Mapping[str, str] | None = None,
                workdir: Path | str | None = None) -> tuple[CheatTrialVerdict, ...]:
    """Every trial in ``trials``, each in its own workspace with its own nonce."""
    nonces = dict(nonces or {})
    return tuple(
        run_cheat_trial(trial, solver, nonce=nonces.get(trial.trial_id), workdir=workdir)
        for trial in trials
    )


def command_solver(argv: Sequence[str], *, timeout: float = 120.0,
                   env: Mapping[str, str] | None = None) -> Solver:
    """A solver that runs ``argv`` with the planted workspace as its cwd."""
    command = [str(item) for item in argv]
    if not command:
        raise ValueError("command_solver needs a command")

    def solve(root: Path) -> str:
        completed = subprocess.run(
            command, cwd=root, capture_output=True, text=True, timeout=timeout,
            env=dict(env) if env is not None else None, check=False,
        )
        return (completed.stdout or "") + (completed.stderr or "")

    return solve


__all__ = [
    "LoopbackSolutionServer", "Solver", "command_solver", "run_battery", "run_cheat_trial",
]

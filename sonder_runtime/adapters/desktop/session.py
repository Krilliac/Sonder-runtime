"""One approved driving session: its window, its indicator, its kill switch.

A session binds exactly one top-level window of an allowlisted executable.
Before every action ``require_live`` re-proves the whole premise the session
was approved on and ends the session, rather than acting, when any part fails:

* the approval has not expired and the action budget is not spent;
* the indicator process is running, and nobody pressed the kill hotkey or Stop;
* the window still exists, belongs to the same process, and that process is
  still an allowlisted executable;
* no person has touched the mouse or keyboard since Sonder's last action.
  Taking the controls back is itself a stop signal.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...domain.computer_use.rules import ActionBudget, app_allowed
from .windows import idle_ticks, window

_READY_TIMEOUT_SECONDS = 8.0
# Input the user makes within this many ms after Sonder's own is attributed to
# Sonder (SendInput stamps its events a little after the call returns).
_INPUT_GRACE_MS = 150


class SessionRefused(RuntimeError):
    """No session is running, or the running one just ended; the reason says which."""


def _tick_after(a: int, b: int) -> bool:
    """Whether tick ``a`` is later than ``b`` on the wrapping 32-bit counter."""
    return 0 < ((a - b) & 0xFFFFFFFF) < 0x80000000


@dataclass
class DrivingSession:
    id: str
    hwnd: int
    app: str
    pid: int
    title: str
    started: float
    expires: float
    budget: ActionBudget
    helper: object
    stop_file: Path
    last_input_mark: int
    actions: list = field(default_factory=list)
    last_capture: object = None
    # ref -> RefEntry from the last UI Automation control table read in this
    # session (domain/computer_use/controls.py); refs never outlive the session.
    control_refs: dict = field(default_factory=dict)


def _launch_indicator(stop_file: Path, ready_file: Path, label: str):
    repo_root = Path(__file__).resolve().parents[3]
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    return subprocess.Popen(
        [sys.executable, "-m", "sonder_runtime.adapters.desktop.indicator",
         "--stop-file", str(stop_file), "--ready-file", str(ready_file),
         "--label", label, "--parent", str(os.getpid())],
        cwd=str(repo_root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=flags,
    )


class _RealDesktop:
    window = staticmethod(window)
    idle_ticks = staticmethod(idle_ticks)


class SessionController:
    def __init__(self, state_dir: Path, *, desktop=None,
                 launcher=_launch_indicator, clock=time.time):
        self._dir = Path(state_dir)
        # The real desktop is these two readers; tests pass a stand-in.
        self._desktop = desktop or _RealDesktop
        self._launcher = launcher
        self._clock = clock
        self._session: DrivingSession | None = None
        self._ended = ""
        # Held for the whole of every action so two callers never interleave
        # input into one window.
        self.lock = threading.RLock()

    @property
    def active(self) -> DrivingSession | None:
        return self._session

    def start(self, hwnd: int, *, allowed_apps, ttl_seconds: int,
              per_minute: int, per_session: int) -> DrivingSession:
        with self.lock:
            if self._session is not None:
                raise SessionRefused("a session is already running; stop it first")
            info = self._desktop.window(hwnd)
            if not app_allowed(info.app, allowed_apps):
                raise SessionRefused(f"{info.app or 'that window'} is not in [computer_use].allowed_apps")
            self._dir.mkdir(parents=True, exist_ok=True)
            sid = secrets.token_hex(8)
            stop_file = self._dir / f"{sid}.stop"
            ready_file = self._dir / f"{sid}.ready"
            helper = self._launcher(stop_file, ready_file, f"{info.app} ({info.title[:40]})")
            ready = self._await_ready(helper, ready_file)
            if not ready.get("ok"):
                self._kill(helper)
                raise SessionRefused("the driving indicator did not start: %s"
                                     % ready.get("error", "no response"))
            now = self._clock()
            ticks_now, _ = self._desktop.idle_ticks()
            self._session = DrivingSession(
                id=sid, hwnd=info.hwnd, app=info.app, pid=info.pid, title=info.title,
                started=now, expires=now + ttl_seconds,
                budget=ActionBudget(per_minute, per_session), helper=helper,
                stop_file=stop_file, last_input_mark=ticks_now,
            )
            self._ended = ""
            return self._session

    def _await_ready(self, helper, ready_file: Path) -> dict:
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if ready_file.exists():
                try:
                    return json.loads(ready_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            if helper.poll() is not None and not ready_file.exists():
                return {"ok": False, "error": "the indicator exited with code %s" % helper.poll()}
            time.sleep(0.05)
        return {"ok": False, "error": "timed out"}

    @staticmethod
    def _kill(helper) -> None:
        try:
            if helper.poll() is None:
                helper.terminate()
                helper.wait(timeout=5)
        except Exception:
            pass

    def stop(self, reason: str) -> str:
        with self.lock:
            session = self._session
            if session is None:
                return self._ended or "no session was running"
            self._session = None
            self._ended = reason
            try:
                session.stop_file.write_text(json.dumps({"reason": reason}), encoding="utf-8")
            except OSError:
                pass
            self._kill(session.helper)
            for path in (session.stop_file, session.stop_file.with_suffix(".ready")):
                try:
                    path.unlink()
                except OSError:
                    pass
            return reason

    def require_live(self, allowed_apps) -> DrivingSession:
        """The running session, re-proved; ends it and raises when any premise fails."""
        with self.lock:
            session = self._session
            if session is None:
                raise SessionRefused(
                    ("the last session ended: %s. " % self._ended if self._ended else "")
                    + "Start one with computer_use_start (a person approves it at the console)."
                )
            reason = self._premise_failure(session, allowed_apps)
            if reason:
                self.stop(reason)
                raise SessionRefused("the session ended: " + reason)
            return session

    def _premise_failure(self, session: DrivingSession, allowed_apps) -> str:
        if self._clock() >= session.expires:
            return "its approval expired"
        if session.stop_file.exists():
            try:
                return json.loads(session.stop_file.read_text(encoding="utf-8")).get("reason", "stopped")
            except (OSError, ValueError):
                return "stopped"
        if session.helper.poll() is not None:
            return "the driving indicator closed"
        try:
            info = self._desktop.window(session.hwnd)
        except Exception:
            return "the window closed"
        if info.pid != session.pid:
            return "the window now belongs to another process"
        if not app_allowed(info.app, allowed_apps):
            return f"{info.app} is no longer in [computer_use].allowed_apps"
        _, last_input = self._desktop.idle_ticks()
        if _tick_after(last_input, (session.last_input_mark + _INPUT_GRACE_MS) & 0xFFFFFFFF):
            return "a person used the mouse or keyboard"
        return ""

    def note_input(self, session: DrivingSession) -> None:
        """Record that Sonder just sent input, so it is not mistaken for a person's."""
        session.last_input_mark, _ = self._desktop.idle_ticks()

    def status(self) -> dict:
        session = self._session
        if session is None:
            return {"active": False, "last_end": self._ended}
        return {
            "active": True, "session": session.id, "app": session.app,
            "title": session.title, "hwnd": session.hwnd,
            "expires_in_seconds": max(0, int(session.expires - self._clock())),
            "actions_used": session.budget.used,
        }

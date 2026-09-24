"""Runtime guard: at most one host-driven Git mutation per working tree.

Issue #510 section 6 ("dangerous concurrent Git/worktree operation guard").

Sonder dispatches Git mutations (``git_commit``, ``git_checkout``,
``git_stash``, ``git_merge``, ...) from several concurrent paths: MCP tool
threads, autonomous agent loops, fanout/fleet lanes, and runtime-source
maintenance.  Before this guard nothing serialized them, so one lane could
switch the checked-out branch while another lane was staging and committing,
or pop a stash into a tree that a merge was rewriting.  Git's own
``index.lock`` catches only the narrow window where both processes touch the
index at the same instant; it does not stop a check-then-act sequence (status
probe, then merge) from acting on a tree another lane changed in between.

The guard is deliberately small:

* one in-process slot per working tree, keyed by the nearest ancestor that
  contains a ``.git`` entry (a linked worktree has its own ``.git`` file, so
  separate worktrees of one repository do not block each other);
* a short bounded wait (``DEFAULT_WAIT_SECONDS``) so ordinary back-to-back
  calls are never refused, only genuinely overlapping ones;
* on contention it does NOT queue or retry the mutation.  It refuses with a
  typed :class:`ConcurrentGitMutation` naming the holder, so the caller's
  recovery is materially different from the blocked action: re-inspect the
  tree (``repo_status``) after the holder finishes, then decide again;
* an ``index.lock`` held by some *other* Git process (possibly a concurrent
  read such as ``repo_status``, which holds it briefly, or a crashed process
  that left it behind) is reported as its own refusal reason instead of being
  deleted -- the host never removes a lock it did not create.

Limitations (recorded in the Issue #510 guard inventory): the slot is
per-process.  Two separate Sonder processes mutating the same tree are only
protected by Git's own ``index.lock``; the shared ``refs/stash`` of linked
worktrees is not serialized across worktrees.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path


GUARD_NAME = "git_mutation_concurrency"
DEFAULT_WAIT_SECONDS = 2.0
MAX_WAIT_SECONDS = 30.0


class ConcurrentGitMutation(PermissionError):
    """A Git mutation was refused because the working tree is busy.

    It is a ``PermissionError`` so existing host handlers that already turn
    policy refusals into a "refused: ..." message report it the same way.
    """

    def __init__(self, message, *, key, operation, holder=None, reason="busy"):
        super().__init__(message)
        self.key = key
        self.operation = operation
        self.holder = holder
        self.reason = reason

    def as_result(self):
        """The dict shape ``harness_tools`` git functions already return."""
        result = {
            "ok": False,
            "error": str(self),
            "guard": GUARD_NAME,
            "guard_reason": self.reason,
            "recovery": (
                "Do not retry this mutation blindly. Wait for the other "
                "operation to finish, re-inspect the tree with repo_status, "
                "then decide whether the mutation is still needed."
            ),
        }
        if self.holder is not None:
            result["holder"] = self.holder.operation
        return result


@dataclass(frozen=True)
class _Holder:
    operation: str
    thread_name: str
    started: float


_state_lock = threading.Lock()
_slots: dict[str, threading.Lock] = {}
_holders: dict[str, _Holder] = {}
_stats = {"admitted": 0, "refused": 0}


def worktree_key(root) -> str:
    """Normalized identity of the working tree that contains ``root``."""
    path = Path(str(root or ".")).resolve()
    probe = path
    while True:
        if (probe / ".git").exists():
            path = probe
            break
        if probe.parent == probe:
            break
        probe = probe.parent
    return os.path.normcase(str(path))


def _foreign_index_lock(key: str) -> Path | None:
    dot_git = Path(key) / ".git"
    if dot_git.is_dir():
        candidate = dot_git / "index.lock"
        return candidate if candidate.exists() else None
    if dot_git.is_file():
        # Linked worktree: ".git" is a file naming the private git dir.
        try:
            text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            gitdir = Path(text[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (Path(key) / gitdir).resolve()
            candidate = gitdir / "index.lock"
            return candidate if candidate.exists() else None
    return None


def _bounded_wait(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_WAIT_SECONDS
    return max(0.0, min(seconds, MAX_WAIT_SECONDS))


@contextlib.contextmanager
def guard_git_mutation(root, operation, *, wait_seconds=None):
    """Hold the working tree's mutation slot for the ``with`` block.

    Raises :class:`ConcurrentGitMutation` when another host-driven mutation
    still holds the slot after ``wait_seconds`` or when a foreign
    ``index.lock`` is present.  ``wait_seconds`` defaults to
    ``DEFAULT_WAIT_SECONDS`` and is capped at ``MAX_WAIT_SECONDS``.  Never
    nests: the guarded body must not call another guarded mutation on the
    same tree.
    """
    if wait_seconds is None:
        wait_seconds = DEFAULT_WAIT_SECONDS
    key = worktree_key(root)
    with _state_lock:
        slot = _slots.setdefault(key, threading.Lock())
    if not slot.acquire(timeout=_bounded_wait(wait_seconds)):
        with _state_lock:
            holder = _holders.get(key)
            _stats["refused"] += 1
        held_for = time.monotonic() - holder.started if holder else 0.0
        raise ConcurrentGitMutation(
            "HOST GUARD: refused %s: another Git mutation (%s) is still running "
            "on %s after %.1fs; mutations on one working tree are serialized."
            % (
                operation,
                holder.operation if holder else "unknown",
                key,
                held_for,
            ),
            key=key, operation=operation, holder=holder, reason="busy",
        )
    try:
        lock_file = _foreign_index_lock(key)
        if lock_file is not None:
            with _state_lock:
                _stats["refused"] += 1
            raise ConcurrentGitMutation(
                "HOST GUARD: refused %s: another git process holds index.lock "
                "(possibly a concurrent read or a crashed process): %s. A "
                "concurrent read such as repo_status releases it within "
                "moments; retry after re-inspecting. The host never deletes a "
                "lock it did not create; remove a stale one manually only "
                "after confirming no Git process is running."
                % (operation, lock_file),
                key=key, operation=operation, reason="foreign_index_lock",
            )
        with _state_lock:
            _holders[key] = _Holder(
                operation=str(operation),
                thread_name=threading.current_thread().name,
                started=time.monotonic(),
            )
            _stats["admitted"] += 1
        try:
            yield key
        finally:
            with _state_lock:
                _holders.pop(key, None)
    finally:
        slot.release()


def guard_snapshot() -> dict:
    """Bounded telemetry for status surfaces and tests."""
    with _state_lock:
        return {
            "guard": GUARD_NAME,
            "admitted": _stats["admitted"],
            "refused": _stats["refused"],
            "active": {key: holder.operation for key, holder in _holders.items()},
        }


def reset_for_tests() -> None:
    with _state_lock:
        _slots.clear()
        _holders.clear()
        _stats["admitted"] = 0
        _stats["refused"] = 0

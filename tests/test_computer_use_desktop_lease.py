"""The computer-use session holds the host's desktop lease (adapters/desktop/session.py).

One worker behaves exactly as without the registry; a second worker process is
refused while the first drives, and gets the desktop once it stops.
"""
from __future__ import annotations

import os

import pytest

from sonder_runtime.adapters.desktop.session import (
    DESKTOP_LEASE_KEY, SessionController, SessionRefused,
)
from sonder_runtime.adapters.process_liveness import PROCESS_ALIVE, PROCESS_DEAD
from sonder_runtime.adapters.resource_leases import SqliteResourceLeaseRegistry
from sonder_runtime.domain.resource_leases import KIND_DESKTOP_SESSION

from tests.test_computer_use_session import FakeDesktop, _launcher


def _registry(path, states=None):
    states = states if states is not None else {}
    return SqliteResourceLeaseRegistry(path, probe=lambda pid, ident=None: (
        states.get(pid, PROCESS_ALIVE), None))


def _start(ctl):
    return ctl.start(7, allowed_apps=("notepad.exe",), ttl_seconds=60, per_minute=10,
                     per_session=10)


def _trace(ctl, helpers):
    """Observable behaviour of one start / act / stop cycle."""
    session = _start(ctl)
    live = ctl.require_live(("notepad.exe",)) is session
    status = {k: v for k, v in ctl.status().items() if k != "session"}
    with pytest.raises(SessionRefused) as again:
        _start(ctl)
    ended = ctl.stop("done")
    return (live, status, str(again.value), ended, ctl.status(), len(helpers),
            helpers[0].terminated)


def test_single_worker_behaviour_is_identical_with_and_without_the_lease(tmp_path):
    plain_helpers, leased_helpers = [], []
    plain = SessionController(tmp_path / "a", desktop=FakeDesktop(),
                              launcher=_launcher(plain_helpers), clock=lambda: 1000.0)
    leased = SessionController(tmp_path / "b", desktop=FakeDesktop(),
                               launcher=_launcher(leased_helpers), clock=lambda: 1000.0,
                               leases=_registry(tmp_path / "l.sqlite3"))
    assert _trace(plain, plain_helpers) == _trace(leased, leased_helpers)
    # And the lease is gone after stop, so the next start works as before.
    assert _registry(tmp_path / "l.sqlite3").holder(KIND_DESKTOP_SESSION, DESKTOP_LEASE_KEY) is None
    assert _start(leased).id


def test_second_worker_is_refused_while_the_first_drives(tmp_path):
    db = tmp_path / "l.sqlite3"
    first = SessionController(tmp_path / "a", desktop=FakeDesktop(), launcher=_launcher([]),
                              leases=_registry(db))
    helpers = []
    second = SessionController(tmp_path / "b", desktop=FakeDesktop(),
                               launcher=_launcher(helpers), leases=_registry(db))
    session = _start(first)
    with pytest.raises(SessionRefused, match="another Sonder worker is driving"):
        _start(second)
    assert helpers == [] and second.active is None  # refused before any indicator launched
    held = _registry(db).holder(KIND_DESKTOP_SESSION, DESKTOP_LEASE_KEY)
    assert held.owner_id.endswith(session.id) and held.owner_pid == os.getpid()
    first.stop("done")
    assert _start(second).id


def test_lease_released_when_the_indicator_fails_to_start(tmp_path):
    db = tmp_path / "l.sqlite3"
    ctl = SessionController(tmp_path / "a", desktop=FakeDesktop(),
                            launcher=_launcher([], ok=False), leases=_registry(db))
    with pytest.raises(SessionRefused, match="hotkey taken"):
        _start(ctl)
    assert _registry(db).holder(KIND_DESKTOP_SESSION, DESKTOP_LEASE_KEY) is None


def test_lease_released_when_a_broken_premise_ends_the_session(tmp_path):
    db = tmp_path / "l.sqlite3"
    desk = FakeDesktop()
    ctl = SessionController(tmp_path / "a", desktop=desk, launcher=_launcher([]),
                            leases=_registry(db))
    _start(ctl)
    desk.windows.clear()
    with pytest.raises(SessionRefused, match="window closed"):
        ctl.require_live(("notepad.exe",))
    assert _registry(db).holder(KIND_DESKTOP_SESSION, DESKTOP_LEASE_KEY) is None


def test_crashed_holder_is_reclaimed_only_once_its_indicator_is_gone(tmp_path):
    db = tmp_path / "l.sqlite3"
    states = {}
    reg = _registry(db, states)
    reg.acquire(KIND_DESKTOP_SESSION, DESKTOP_LEASE_KEY, "pid-4242:dead", ttl_seconds=600,
                owner_pid=4242, metadata={"pid": 4243})
    ctl = SessionController(tmp_path / "a", desktop=FakeDesktop(), launcher=_launcher([]),
                            leases=reg)
    states[4242] = PROCESS_DEAD  # the worker crashed ...
    with pytest.raises(SessionRefused, match="nothing proves"):
        _start(ctl)  # ... but its "Sonder is driving" indicator is still up
    states[4243] = PROCESS_DEAD
    assert _start(ctl).id


def test_an_unavailable_lease_store_fails_closed_with_its_own_reason(tmp_path):
    from sonder_runtime.domain.common.errors import DependencyUnavailable

    class Broken:
        def acquire(self, *a, **k):
            raise DependencyUnavailable("disk full")

        def release(self, *a, **k):
            return False

    helpers = []
    ctl = SessionController(tmp_path, desktop=FakeDesktop(), launcher=_launcher(helpers),
                            leases=Broken())
    with pytest.raises(SessionRefused, match="could not be taken: disk full"):
        _start(ctl)
    assert helpers == [] and ctl.active is None


def test_the_lease_records_the_owner_fingerprint_against_pid_reuse(tmp_path):
    from sonder_runtime.adapters.process_liveness import process_identity

    db = tmp_path / "l.sqlite3"
    helpers = []
    launch = _launcher(helpers)

    def launch_with_pid(*args):
        helper = launch(*args)
        helper.pid = os.getpid()  # any live process stands in for the indicator
        return helper

    ctl = SessionController(tmp_path / "a", desktop=FakeDesktop(), launcher=launch_with_pid,
                            leases=_registry(db))
    _start(ctl)
    held = _registry(db).holder(KIND_DESKTOP_SESSION, DESKTOP_LEASE_KEY)
    mine = process_identity(os.getpid())
    assert mine  # this process can always fingerprint itself
    assert held.owner_identity == mine
    assert held.metadata["pid"] == os.getpid() and held.metadata["pid_identity"] == mine

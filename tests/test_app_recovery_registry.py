"""Bounded recovery handles never replay an ambiguous callback."""

import pytest
from concurrent.futures import ThreadPoolExecutor
from tests.test_app_managed_authority import managed, control


def test_unknown_preparation_can_close_without_replaying(managed):
    import time
    from sonder_runtime.bootstrap.app_work_recovery_registry import (
        AppWorkRecoveryRegistry,
    )
    from sonder_runtime.bootstrap.app_recovery_coordinator import AppWorkRecoveryAttempt

    authority, selection, *_ = managed
    application = object()
    called = []

    def factory(selected):
        called.append(selected)
        return AppWorkRecoveryAttempt(
            authority=authority,
            selection=selected,
            application=application,
            recovery_factory=lambda *args: None,
            verifier_factory=lambda *args: None,
            approve_attachment=lambda *args: None,
            approve_verification=lambda *args: None,
            private_paths=lambda: (),
            model_writable_roots=lambda: (),
        )

    registry = AppWorkRecoveryRegistry(
        application=application,
        authority=authority,
        attempt_factory=factory,
        executor=ThreadPoolExecutor(max_workers=1),
    )
    try:
        result = registry.prepare(
            selection,
            work_id="missing",
            attachment_command_id="attach",
            completion_command_id="complete",
        )
        entry = registry._entries[result["attempt_id"]]
        deadline = time.monotonic() + 10
        while entry.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entry.phase == "unknown"
        with pytest.raises(Exception):
            registry.act(selection, entry.identity, "attach")
        registry.act(selection, entry.identity, "close")
        while entry.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entry.phase == "closed"
        assert len(called) == 1
        assert not authority._selections
    finally:
        registry.close()


def test_registry_requires_explicit_private_composition():
    from sonder_runtime.bootstrap.app_work_recovery_registry import (
        AppWorkRecoveryRegistry,
    )

    with pytest.raises((TypeError, ValueError)):
        AppWorkRecoveryRegistry(
            application=None, authority=None, attempt_factory=None, executor=None
        )


@pytest.mark.parametrize(
    "method,path,action",
    [
        ("POST", "/work/" + "a" * 64 + "/recovery", "prepare_recovery"),
        ("GET", "/recovery/" + "b" * 64, "inspect_recovery"),
        ("POST", "/recovery/" + "b" * 64 + "/attach", "attach_recovery"),
        ("POST", "/recovery/" + "b" * 64 + "/resume", "resume_recovery"),
        ("POST", "/recovery/" + "b" * 64 + "/close", "close_recovery"),
    ],
)
def test_exact_recovery_routes(method, path, action):
    from sonder_runtime.interfaces.http.app_control import _route

    assert _route(method, "/v1/app-control" + path)[0] == action


def _registry(managed, factory_hook=None, executor=None):
    from sonder_runtime.bootstrap.app_work_recovery_registry import (
        AppWorkRecoveryRegistry,
    )
    from sonder_runtime.bootstrap.app_recovery_coordinator import AppWorkRecoveryAttempt

    authority, selection, *_ = managed
    application = object()
    called = []

    def factory(selected):
        called.append(selected)
        if factory_hook:
            factory_hook()
        return AppWorkRecoveryAttempt(
            authority=authority,
            selection=selected,
            application=application,
            recovery_factory=lambda *args: None,
            verifier_factory=lambda *args: None,
            approve_attachment=lambda *args: None,
            approve_verification=lambda *args: None,
            private_paths=lambda: (),
            model_writable_roots=lambda: (),
        )

    return (
        AppWorkRecoveryRegistry(
            application=application,
            authority=authority,
            attempt_factory=factory,
            executor=executor or ThreadPoolExecutor(max_workers=1),
        ),
        called,
    )


def test_busy_status_keeps_fresh_admission_without_competing_history_scan(
    managed, control, monkeypatch
):
    from threading import Event
    import admin_auth
    from sonder_runtime.bootstrap.app_work_recovery import AppWorkRecoveryHistory

    entered, release = Event(), Event()

    def pause():
        entered.set()
        assert release.wait(30)

    registry, called = _registry(managed, pause)
    authority, selection, *_ = managed
    try:
        result = registry.prepare(
            selection,
            work_id="missing",
            attachment_command_id="attach",
            completion_command_id="complete",
        )
        assert entered.wait(10)

        def unnecessary(*args, **kwargs):
            pytest.fail(
                "busy status must not compete for a second durable-history admission"
            )

        monkeypatch.setattr(AppWorkRecoveryHistory, "inspect", unnecessary)
        current = registry.inspect(selection, result["attempt_id"])
        assert current["busy"] and current["phase"] == "preparing"
        conn = control[3]()
        try:
            admin_auth.revoke_session(conn, control[1])
        finally:
            conn.close()
        with pytest.raises(Exception):
            registry.inspect(selection, result["attempt_id"])
        assert len(called) == 1
    finally:
        release.set()
        registry.close()


@pytest.mark.parametrize("queued", [False, True])
def test_ambiguous_submission_never_replays_and_shutdown_retains_ownership(
    managed, queued
):
    from threading import Event

    entered, release = Event(), Event()

    class LostResponsePool(ThreadPoolExecutor):
        def submit(self, fn, *args, **kwargs):
            if queued:
                super().submit(fn, *args, **kwargs)
                assert entered.wait(10)
            raise RuntimeError("submission response unavailable")

    def pause():
        entered.set()
        assert release.wait(30)

    pool = LostResponsePool(max_workers=1)
    registry, called = _registry(managed, pause, pool)
    authority, selection, *_ = managed
    try:
        args = dict(
            work_id="missing",
            attachment_command_id="attach",
            completion_command_id="complete",
        )
        result = registry.prepare(selection, **args)
        assert result["phase"] == "unknown" and result["busy"]
        retry = registry.prepare(selection, **args)
        assert retry["attempt_id"] == result["attempt_id"]
        assert retry["phase"] == "unknown"
        assert registry.act(selection, result["attempt_id"], "attach")["busy"]
        assert len(called) == int(queued)
        with pytest.raises(PermissionError):
            authority.release_selection(selection)
    finally:
        release.set()
        registry.close()
    assert not authority._selections


def test_failed_cleanup_retains_exact_selection_until_retry(managed, monkeypatch):
    import time

    registry, called = _registry(managed)
    authority, selection, *_ = managed
    result = registry.prepare(
        selection,
        work_id="missing",
        attachment_command_id="attach",
        completion_command_id="complete",
    )
    entry = registry._entries[result["attempt_id"]]
    deadline = time.monotonic() + 15
    while entry.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not entry.busy and entry.phase == "unknown"
    original = entry.attempt.close
    calls = []

    def close():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("cleanup completion unavailable")
        return original()

    monkeypatch.setattr(entry.attempt, "close", close)
    try:
        with pytest.raises(RuntimeError, match="cleanup remains unresolved"):
            registry.close()
        assert not entry.released and authority._retained
        registry.close()
        assert entry.released and not authority._selections
        assert len(calls) == 2 and len(called) == 1
    finally:
        registry.close()


def test_lost_submit_response_keeps_unknown_phase_after_callback_returns(
    managed, monkeypatch
):
    from threading import Event
    from types import SimpleNamespace
    from sonder_runtime.bootstrap.app_recovery_coordinator import AppWorkRecoveryAttempt

    entered, release = Event(), Event()

    class LostResponsePool(ThreadPoolExecutor):
        def submit(self, fn, *args, **kwargs):
            self.accepted = super().submit(fn, *args, **kwargs)
            assert entered.wait(10)
            raise RuntimeError("submission acknowledgement lost")

    def pause():
        entered.set()
        assert release.wait(30)

    monkeypatch.setattr(
        AppWorkRecoveryAttempt,
        "prepare",
        lambda *args, **kwargs: SimpleNamespace(work=None),
    )
    pool = LostResponsePool(max_workers=1)
    registry, called = _registry(managed, pause, pool)
    selection = managed[1]
    try:
        result = registry.prepare(
            selection,
            work_id="missing",
            attachment_command_id="attach",
            completion_command_id="complete",
        )
        assert result["phase"] == "unknown"
        release.set()
        pool.accepted.result(10)
        entry = registry._entries[result["attempt_id"]]
        assert not entry.busy
        assert registry._snapshot(entry)["phase"] == "unknown"
        assert len(called) == 1
        with pytest.raises(Exception):
            registry.act(selection, result["attempt_id"], "attach")
    finally:
        release.set()
        registry.close()

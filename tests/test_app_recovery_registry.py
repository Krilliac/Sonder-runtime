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

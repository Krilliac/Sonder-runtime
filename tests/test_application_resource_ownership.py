from threading import Event, Thread

import pytest

from sonder_runtime.application.runtime_resources import (
    ApplicationResourceOwners, ComponentCloseProof, ResourceOwnershipRefused,
)


def test_actual_initializer_and_close_proof_cover_fixed_manifest():
    owners = ApplicationResourceOwners(("sessions", "children"))
    value = object()
    assert owners.initialize("sessions", lambda: value,
        lambda resource, timeout: ComponentCloseProof("sessions", resource is value, "owned-handles-closed")) is value
    receipt = owners.close(timeout=1)
    assert receipt.clean
    assert [(r.component, r.state) for r in receipt.components] == [
        ("children", "UNOPENED"), ("sessions", "CLOSED")]
    with pytest.raises(ResourceOwnershipRefused):
        owners.initialize("children", object, lambda *_: None)
    assert owners.close(timeout=1) == receipt


def test_generic_none_or_wrong_component_is_not_cleanup_proof():
    for callback in (lambda *_: None, lambda *_: ComponentCloseProof("other", True, "closed")):
        owners = ApplicationResourceOwners(("sessions",))
        owners.initialize("sessions", object, callback)
        receipt = owners.close(timeout=1)
        assert not receipt.clean
        assert receipt.components[0].state == "UNRESOLVED"


def test_active_effect_prevents_cleanup_and_terminal_admission_is_immediate():
    owners = ApplicationResourceOwners(("sessions",))
    closed = []
    owners.initialize("sessions", object, lambda *_: closed.append(True))
    with owners.admission("sessions"):
        assert not owners.close(timeout=0).clean
        assert not closed
        with pytest.raises(ResourceOwnershipRefused):
            with owners.admission("sessions"):
                pass
    assert not owners.close(timeout=0).clean


def test_initialization_race_cannot_publish_after_admission_stop():
    owners = ApplicationResourceOwners(("sessions",))
    entered, release = Event(), Event()
    errors = []
    def factory():
        entered.set()
        assert release.wait(5)
        return object()
    def initialize():
        try:
            owners.initialize("sessions", factory, lambda *_: ComponentCloseProof("sessions", True, "closed"))
        except ResourceOwnershipRefused:
            errors.append("stopped")
    worker = Thread(target=initialize)
    worker.start()
    try:
        assert entered.wait(5)
        assert not owners.close(timeout=0).clean
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == ["stopped"]
    # Initial missing proof is immutable; a later callback cannot rewrite it.
    assert not owners.close(timeout=1).clean


def test_unknown_component_and_failed_construction_are_not_unopened():
    owners = ApplicationResourceOwners(("sessions",))
    with pytest.raises(ResourceOwnershipRefused):
        owners.initialize("typo", object, lambda *_: None)
    def failed():
        raise RuntimeError("fixture")
    with pytest.raises(RuntimeError):
        owners.initialize("sessions", failed, lambda *_: None)
    assert not owners.close(timeout=1).clean


def test_stopped_initializer_cleanup_cannot_race_second_closer():
    owners = ApplicationResourceOwners(("sessions",))
    factory_entered, factory_release = Event(), Event()
    close_entered, close_release = Event(), Event()
    calls, errors = [], []
    def factory():
        factory_entered.set()
        assert factory_release.wait(5)
        return object()
    def cleanup(*args):
        calls.append(True)
        close_entered.set()
        assert close_release.wait(5)
        return ComponentCloseProof("sessions", True, "closed")
    def initialize():
        try:
            owners.initialize("sessions", factory, cleanup)
        except ResourceOwnershipRefused:
            errors.append("stopped")
    worker = Thread(target=initialize)
    worker.start()
    try:
        assert factory_entered.wait(5)
        owners.stop_admissions()
        factory_release.set()
        assert close_entered.wait(5)
        assert not owners.close(timeout=0).clean
        assert calls == [True]
    finally:
        factory_release.set()
        close_release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert calls == [True]
    assert errors == ["stopped"]

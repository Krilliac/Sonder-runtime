import pytest

from sonder_runtime.application.control_plane import (
    CONTROL_PLANE_SECTIONS,
    ControlPlaneProviderError,
    ControlPlaneSnapshotService,
)


def _providers(calls=None):
    def make(name):
        def provider():
            if calls is not None:
                calls.append(name)
            return ({"section": name},)

        return provider

    return {name: make(name) for name in CONTROL_PLANE_SECTIONS}


def test_assembly_is_complete_and_deterministic():
    calls = []
    snapshot = ControlPlaneSnapshotService(_providers(calls)).snapshot(
        captured_at="2026-08-21T12:00:00Z", revision=4
    )

    assert calls == list(CONTROL_PLANE_SECTIONS)
    assert snapshot.total_records == len(CONTROL_PLANE_SECTIONS)
    assert snapshot.as_dict()["sections"]["health"]["records"] == [{"section": "health"}]


def test_missing_provider_fails_closed():
    providers = _providers()
    providers.pop("health")

    with pytest.raises(ControlPlaneProviderError, match="missing section providers"):
        ControlPlaneSnapshotService(providers)


def test_provider_failure_does_not_return_partial_snapshot():
    providers = _providers()
    providers["jobs"] = lambda: (_ for _ in ()).throw(RuntimeError("database unavailable"))

    with pytest.raises(ControlPlaneProviderError, match="section jobs provider failed"):
        ControlPlaneSnapshotService(providers).snapshot(captured_at="now")


def test_provider_records_are_bounded():
    providers = _providers()
    providers["sessions"] = lambda: ({"id": index} for index in range(1025))

    with pytest.raises(ControlPlaneProviderError, match="snapshot validation failed"):
        ControlPlaneSnapshotService(providers).snapshot(captured_at="now")

import os

import pytest

from sonder_runtime.application.ports.runtime_owner import OwnerRefused
from sonder_runtime.adapters.filesystem.atomic_json import file_lock
from sonder_runtime.bootstrap.managed_runtime_owner import ManagedRuntimeOwner


@pytest.mark.skipif(os.name != "nt", reason="actual Windows directory anchors required")
@pytest.mark.parametrize("point", ["store", "issuer-before", "issuer-after"])
def test_post_base_constructor_failure_releases_exact_owned_resources(
    tmp_path, monkeypatch, point
):
    import sonder_runtime.bootstrap.managed_runtime_owner as module
    from sonder_runtime.application.subagents.child_migration_activation import _ISSUERS

    captured = []
    original = module._register_host_issuer

    def fail_store(path):
        raise OwnerRefused("injected store construction failure")

    def fail_issuer(owner, validate):
        captured.append(owner)
        if point == "issuer-after":
            original(owner, validate)
        raise OwnerRefused("injected issuer registration failure")

    if point == "store":
        monkeypatch.setattr(module, "SQLiteChildMigrationStore", fail_store)
    else:
        monkeypatch.setattr(module, "_register_host_issuer", fail_issuer)
    root = tmp_path / "owner"
    with pytest.raises(OwnerRefused, match="injected"):
        ManagedRuntimeOwner(root, writable_roots=lambda: ())
    for owner in captured:
        assert not owner._live
        assert owner not in _ISSUERS
        with pytest.raises(OwnerRefused):
            owner.status()
    with file_lock(root / "launch", timeout=0):
        pass
    root.rename(tmp_path / "closed-owner")
    (tmp_path / "owner-workspace").rename(tmp_path / "closed-workspace")


@pytest.mark.parametrize("failure", ["false", "raise"])
def test_pg_cleanup_failure_still_contains_process_and_retains_authority(
    tmp_path, failure
):
    from types import SimpleNamespace
    from sonder_runtime.application.subagents.child_migration_activation import _ISSUERS

    owner = ManagedRuntimeOwner(tmp_path / "owner", writable_roots=lambda: ())
    calls = []

    class Store:
        def close(self, timeout):
            calls.append("store")
            if failure == "raise":
                raise RuntimeError("injected")
            return False

    store = Store()
    owner._pg_stores = [store]
    owner._launch_id = "exact-launch"
    owner._process = SimpleNamespace(
        force_stop=lambda launch: (
            calls.append(launch) or SimpleNamespace(cleanup_completed=True)
        )
    )
    try:
        with pytest.raises(OwnerRefused, match="unresolved"):
            owner.close()
        assert calls == ["exact-launch", "store"]
        assert owner._launch_id is None
        assert owner._pg_stores == [store]
        assert owner._live and owner._gate_entered and owner in _ISSUERS
        with pytest.raises(OwnerRefused, match="cleanup"):
            owner.status()
    finally:
        owner._pg_stores = []
        owner.close()


@pytest.mark.parametrize(
    "failure", ["overlap", "port", "capacity", "write", "readback"]
)
def test_failed_pg_registration_keeps_target_caller_owned(
    tmp_path, monkeypatch, failure
):
    from sonder_runtime.adapters.persistence.postgres_child_migration import (
        PostgresChildMigrationStore,
    )
    from sonder_runtime.platform.child_storage_config import ChildStorageConfig
    import sonder_runtime.bootstrap.managed_runtime_owner as module

    roots = []
    owner = ManagedRuntimeOwner(tmp_path / "owner", writable_roots=lambda: tuple(roots))
    target = object.__new__(PostgresChildMigrationStore)
    target.config = ChildStorageConfig(
        backend="postgresql",
        owner_id="owner",
        binding_file=str(tmp_path / "private" / "binding.json"),
    )
    target.identity = "a" * 64
    target._closed = False
    target.validate_policy = lambda: None
    target.close = lambda **kwargs: pytest.fail("caller-owned target was closed")
    before = (list(owner._pg_stores), owner._private_source_paths, set(owner._catalog))
    if failure == "overlap":
        roots.append(str(tmp_path / "private"))
    if failure == "capacity":
        owner._catalog = {str(n) for n in range(64)}
        before = (
            list(owner._pg_stores),
            owner._private_source_paths,
            set(owner._catalog),
        )

    def fail(*args, **kwargs):
        raise OwnerRefused("injected publication failure")

    if failure == "write":
        monkeypatch.setattr(module, "write_json_atomic", fail)
    if failure == "readback":
        monkeypatch.setattr(owner, "_read_config", fail)
    try:
        with pytest.raises((OwnerRefused, ValueError)):
            owner.register_configuration(
                port=0 if failure == "port" else 12345, target=target
            )
        assert (owner._pg_stores, owner._private_source_paths, owner._catalog) == before
        assert not target._closed
        assert owner.status()["state"] == "STOPPED_CLEAN"
    finally:
        owner.close()


def test_pg_prelaunch_drain_failure_does_not_publish_process_identity(
    tmp_path, monkeypatch
):
    from sonder_runtime.adapters.persistence.postgres_child_migration import (
        PostgresChildMigrationStore,
    )

    owner = ManagedRuntimeOwner(tmp_path / "owner", writable_roots=lambda: ())
    calls = []
    store = object.__new__(PostgresChildMigrationStore)
    store.close = lambda: calls.append("drain") or len(calls) > 1
    owner._selection = store
    monkeypatch.setattr(owner._payload, "validate", lambda roots: None)
    try:
        with pytest.raises(OwnerRefused, match="cleanup"):
            owner._before_launch(None)
        assert owner._launch_id is None
        assert owner.journal.status()["state"] == "STOPPED_CLEAN"
        owner._before_launch(None)
        assert calls == ["drain", "drain"]
        assert owner._launch_id is None
    finally:
        owner.close()

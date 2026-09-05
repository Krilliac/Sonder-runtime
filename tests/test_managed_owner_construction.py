import os

import pytest

from sonder_runtime.application.ports.runtime_owner import OwnerRefused
from sonder_runtime.adapters.filesystem.atomic_json import file_lock
from sonder_runtime.bootstrap.managed_runtime_owner import ManagedRuntimeOwner


@pytest.mark.skipif(os.name != "nt", reason="actual Windows directory anchors required")
@pytest.mark.parametrize("point", ["store", "issuer-before", "issuer-after"])
def test_post_base_constructor_failure_releases_exact_owned_resources(tmp_path, monkeypatch, point):
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

import os
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

from sonder_runtime.bootstrap.managed_runtime_owner import ManagedRuntimeOwner
from sonder_runtime.application.ports.runtime_owner import OwnerRefused, OwnerUnsupported
from tests._managed_runtime_layout import require_bounded_real_runtime_closure


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.skipif(os.name != "nt", reason="actual Windows containment required")
def test_full_manifest_owned_http_and_relaunch(tmp_path, monkeypatch):
    require_bounded_real_runtime_closure()
    owner = ManagedRuntimeOwner(tmp_path / "owner", writable_roots=lambda: ())
    try:
        configuration = owner.register_configuration(port=port())
        selected = owner.prepare("select", "select", {"config": configuration})
        owner.execute(selected)
        for index in range(2):
            launch = owner.prepare(f"launch{index}", "launch", {})
            assert owner.execute(launch)["state"] == "RUNNING"
            with pytest.raises(OwnerRefused):
                owner.selected_store
            configured_port = owner._config(configuration)["port"]
            with urllib.request.urlopen(f"http://127.0.0.1:{configured_port}/live", timeout=3) as response:
                assert response.status == 200
            stop = owner.prepare(f"stop{index}", "stop", {})
            if index == 0:
                complete = owner.journal.complete
                def lose_stop_response(command, result, state):
                    complete(command, result, state)
                    raise OSError("injected durable stop response loss")
                with monkeypatch.context() as patch:
                    patch.setattr(owner.journal, "complete", lose_stop_response)
                    with pytest.raises(OSError):
                        owner.execute(stop)
                assert owner._launch_id is not None
            receipt = owner.execute(stop)
            assert receipt["state"] == "STOPPED_CLEAN"
            assert owner.execute(stop) == receipt
            assert owner.selected_store.path == owner.path / "children.sqlite"
        from sonder_runtime.adapters.persistence.child_migration import SQLiteChildMigrationStore
        from sonder_runtime.adapters.filesystem.child_migration_bundle import ChildMigrationBundle
        from sonder_runtime.application.subagents.child_migration import export_snapshot, stage_snapshot
        source = owner.selected_store
        target = SQLiteChildMigrationStore(owner.path / "next.sqlite")
        with ChildMigrationBundle(tmp_path / "bundle", writable_roots=lambda: ()) as bundle:
            export_snapshot(source, bundle, target_identity=target.identity)
            stage_snapshot(bundle, target)
            reference = owner.register_configuration(port=port(), target=target)
            with pytest.raises(OwnerRefused):
                owner.prepare("bypass", "select", {"config": reference})
            activation = owner.prepare_activation("activate", bundle, target, reference)
            record_phase = bundle.record_phase
            def fail_complete(phase, manifest):
                if phase == "COMPLETE":
                    raise OSError("injected incomplete activation")
                return record_phase(phase, manifest)
            with monkeypatch.context() as patch:
                patch.setattr(bundle, "record_phase", fail_complete)
                with pytest.raises(OSError):
                    owner.execute(activation)
            with pytest.raises(OwnerRefused):
                owner.prepare("unsafe-launch", "launch", {})
            with pytest.raises(OwnerRefused):
                owner.selected_store
            assert owner.execute(activation)["state"] == "STOPPED_CLEAN"
            assert owner.selected_store.identity == target.identity
            launch = owner.prepare("migrated-launch", "launch", {})
            assert owner.execute(launch)["state"] == "RUNNING"
            assert owner.execute(owner.prepare("migrated-stop", "stop", {}))["state"] == "STOPPED_CLEAN"
        with pytest.raises(OwnerUnsupported):
            ManagedRuntimeOwner(owner.path, writable_roots=lambda: ())
    finally:
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="native Windows managed process required")
def test_workstation_profile_launches_with_only_its_pinned_site(tmp_path):
    from sonder_runtime.adapters.execution.runtime_profile import PROFILE_NAME

    checkout = Path(__file__).resolve().parents[1]
    profile = checkout / "venv-managed"
    if not (profile / PROFILE_NAME).is_file():
        if os.environ.get("SONDER_REQUIRE_MANAGED_RUNTIME_PROFILE") == "1":
            pytest.fail("managed runtime installer did not provision its profile")
        pytest.skip("run install_workstation_local.ps1 -ManagedRuntime first")
    owner = ManagedRuntimeOwner.workstation_local(
        tmp_path / "owner", writable_roots=lambda: ()
    )
    try:
        manifest = owner._payload.manifest
        assert manifest["profile"] == str(profile)
        assert str(profile / "Lib" / "site-packages") in manifest["paths"]
        host_site = Path(sys.prefix).resolve() / "Lib" / "site-packages"
        if host_site != profile / "Lib" / "site-packages":
            assert str(host_site) not in manifest["paths"]
        selected = owner.register_configuration(port=port())
        owner.execute(owner.prepare("select", "select", {"config": selected}))
        assert owner.execute(owner.prepare("launch", "launch", {}))["state"] == "RUNNING"
        assert owner.execute(owner.prepare("stop", "stop", {}))["state"] == "STOPPED_CLEAN"
    finally:
        owner.close()

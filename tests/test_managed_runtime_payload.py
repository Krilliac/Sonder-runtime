from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from sonder_runtime.bootstrap.managed_runtime_owner import ManagedRuntimeOwner
from sonder_runtime.application.ports.runtime_owner import OwnerRefused


pytest_plugins = ("tests._managed_runtime_layout",)


def test_declared_site_package_paths_ignore_executable_pth_lines(tmp_path):
    from sonder_runtime.adapters.execution.runtime_payload import (
        _declared_site_package_paths,
    )

    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    (site_packages / "win32" / "lib").mkdir(parents=True)
    (site_packages / "pythonwin").mkdir()
    marker = tmp_path / "executed.txt"
    (site_packages / "pywin32.pth").write_text(
        "# path-only entries are allowed\n"
        "win32\n"
        "win32\\lib\n"
        "pythonwin\n"
        "import pathlib; pathlib.Path(%r).write_text('no')\n" % str(marker),
        encoding="utf-8",
    )

    paths = _declared_site_package_paths(site_packages)

    assert paths == (
        site_packages.resolve(),
        (site_packages / "win32").resolve(),
        (site_packages / "win32" / "lib").resolve(),
        (site_packages / "pythonwin").resolve(),
    )
    assert not marker.exists()


def test_declared_site_package_paths_reject_escaping_pth_entries(tmp_path):
    from sonder_runtime.adapters.execution.runtime_payload import (
        _declared_site_package_paths,
    )

    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    (site_packages / "unsafe.pth").write_text("..\\outside\n", encoding="utf-8")

    with pytest.raises(OwnerRefused, match="dependency path escapes"):
        _declared_site_package_paths(site_packages)


@pytest.mark.skipif(os.name != "nt", reason="actual Windows anchor required")
def test_invalid_manifest_open_releases_its_anchor(tmp_path):
    from sonder_runtime.adapters.execution.runtime_payload import RuntimePayload
    from sonder_runtime.application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
    payload = tmp_path / "runtime-payload"
    anchor = PrivateDirectoryAnchor.open_base(payload, require_new=True)
    anchor.close()
    (tmp_path / "runtime-artifacts.json").write_text("[]", encoding="utf-8")
    with pytest.raises(OwnerRefused, match="exact runtime artifact manifest"):
        RuntimePayload(tmp_path)
    payload.rename(tmp_path / "released")


@pytest.mark.skipif(os.name != "nt", reason="Windows payload profile required")
@pytest.mark.usefixtures("small_managed_runtime_layout")
def test_mutable_checkout_grant_is_refused_and_constructor_anchors_close(tmp_path):
    root = tmp_path / "owner"
    source = Path(__file__).resolve().parents[1]
    with pytest.raises(OwnerRefused, match="artifact overlaps"):
        ManagedRuntimeOwner(root, writable_roots=lambda: (source,))
    root.rename(tmp_path / "closed")
    (tmp_path / "owner-workspace").rename(tmp_path / "workspace-closed")


@pytest.mark.skipif(os.name != "nt", reason="Windows payload profile required")
@pytest.mark.usefixtures("small_managed_runtime_layout")
def test_live_grant_and_payload_changes_refuse_before_any_launch_effect(tmp_path, monkeypatch):
    roots = []
    owner = ManagedRuntimeOwner(tmp_path / "owner", writable_roots=lambda: tuple(roots))
    try:
        reference = owner.register_configuration(port=54321)
        owner.execute(owner.prepare("select", "select", {"config": reference}))
        launch = owner.prepare("launch", "launch", {})
        effects = []
        monkeypatch.setattr(owner._process.provider, "start", lambda *args: effects.append(args))
        source = Path(__file__).resolve().parents[1]
        roots.append(source)
        with pytest.raises(OwnerRefused, match="artifact overlaps"):
            owner.execute(launch)
        assert owner._launch_id is None and not effects
        roots.clear()
        target = owner._payload.path / "server.py"
        target.write_bytes(target.read_bytes() + b"\n# injected payload change\n")
        with pytest.raises(OwnerRefused, match="artifact content"):
            owner.execute(launch)
        assert owner._launch_id is None and not effects
        assert owner.journal.pending() == launch
        assert str(source) not in owner._process._payload.manifest["paths"]
        assert owner._payload.path != source
    finally:
        owner.close()


class _SpawnReached(Exception):
    pass


def _count_content_hashes(monkeypatch):
    from sonder_runtime.adapters.execution import runtime_payload

    calls = []
    inventory = runtime_payload.inventory

    def counted(roots):
        calls.append(1)
        return inventory(roots)

    monkeypatch.setattr(runtime_payload, "inventory", counted)
    return calls


def _selected_owner(tmp_path, roots=()):
    owner = ManagedRuntimeOwner(tmp_path / "owner", writable_roots=lambda: tuple(roots))
    reference = owner.register_configuration(port=54321)
    owner.execute(owner.prepare("select", "select", {"config": reference}))
    return owner


@pytest.mark.skipif(os.name != "nt", reason="Windows payload profile required")
@pytest.mark.usefixtures("small_managed_runtime_layout")
def test_launch_hashes_the_payload_once_before_any_process_effect(tmp_path, monkeypatch):
    owner = _selected_owner(tmp_path)
    try:
        hashes = _count_content_hashes(monkeypatch)
        spawned = []

        def start(request):
            spawned.append((list(hashes), request))
            raise _SpawnReached

        monkeypatch.setattr(owner._process.provider, "start", start)
        with pytest.raises(_SpawnReached):
            owner.execute(owner.prepare("launch", "launch", {}))
        assert len(hashes) == 1, "one full content hash per launch, not one per check"
        assert spawned[0][0] == [1], "the content hash precedes the spawn"
        assert owner._process._admitted is None, "the admission is single-use"
    finally:
        owner._launch_id = None
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows payload profile required")
@pytest.mark.usefixtures("small_managed_runtime_layout")
def test_spawn_still_rechecks_live_writable_root_separation(tmp_path, monkeypatch):
    roots = []
    owner = _selected_owner(tmp_path, roots)
    try:
        admit = owner._process.admit_payload

        def grant_after_admission(operation_id):
            admit(operation_id)
            roots.append(Path(__file__).resolve().parents[1])

        monkeypatch.setattr(owner._process, "admit_payload", grant_after_admission)
        spawned = []

        def start(request):
            spawned.append(request)
            raise _SpawnReached

        monkeypatch.setattr(owner._process.provider, "start", start)
        with pytest.raises(OwnerRefused, match="artifact overlaps"):
            owner.execute(owner.prepare("launch", "launch", {}))
        assert not spawned
    finally:
        owner._launch_id = None
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows payload profile required")
@pytest.mark.usefixtures("small_managed_runtime_layout")
def test_spawn_without_its_own_admission_hashes_the_payload_in_full(tmp_path, monkeypatch):
    owner = _selected_owner(tmp_path)
    try:
        first = owner.prepare("first", "launch", {})
        owner._process.admit_payload(first.operation_id)
        hashes = _count_content_hashes(monkeypatch)
        # An admission for another operation does not cover this spawn.
        command = replace(first, operation_id="second")
        owner._process._launch_layout(command)
        assert len(hashes) == 1
        # And it was consumed: even its own operation now hashes again.
        owner._process._launch_layout(first)
        assert len(hashes) == 2
    finally:
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows payload profile required")
@pytest.mark.usefixtures("small_managed_runtime_layout")
def test_resuming_a_started_launch_does_not_rehash(tmp_path, monkeypatch):
    owner = _selected_owner(tmp_path)
    try:
        launch = owner.prepare("launch", "launch", {})
        owner._launch_id = launch.operation_id  # as after a bounded readiness wait
        hashes = _count_content_hashes(monkeypatch)
        monkeypatch.setattr(owner._process, "alive", lambda job_id: False)
        with pytest.raises(OwnerRefused, match="exited before readiness"):
            owner.execute(launch)
        assert hashes == []
    finally:
        owner._launch_id = None
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows payload profile required")
@pytest.mark.usefixtures("small_managed_runtime_layout")
def test_owner_compiled_bytecode_is_part_of_the_hashed_payload(tmp_path, monkeypatch):
    from hashlib import sha256
    from sonder_runtime.adapters.execution.runtime_payload import BYTECODE_NAME

    owner = _selected_owner(tmp_path)
    try:
        bytecode = owner.path / BYTECODE_NAME
        assert (str(bytecode), False) in [tuple(root) for root in owner._payload.manifest["roots"]]
        assert owner._payload.bytecode_binding() == [
            str(bytecode), sha256(bytecode.read_bytes()).hexdigest()]
        launch = owner.prepare("launch", "launch", {})
        layout = owner._process._launch_layout(launch)
        arguments = layout[1]
        assert "-B" in arguments, "the child must never write bytecode"
        assert json.loads(arguments[-1]) == owner._payload.bytecode_binding()
        effects = []
        monkeypatch.setattr(owner._process.provider, "start", effects.append)
        data = bytearray(bytecode.read_bytes())
        data[-1] ^= 1
        bytecode.write_bytes(bytes(data))
        with pytest.raises(OwnerRefused, match="artifact content"):
            owner.execute(launch)
        assert owner._launch_id is None and not effects
    finally:
        owner.close()

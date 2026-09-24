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

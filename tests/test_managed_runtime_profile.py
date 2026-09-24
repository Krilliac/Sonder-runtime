"""The dedicated managed profile is selected and validated by the host."""

import json
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution import runtime_payload, runtime_profile
from sonder_runtime.application.ports.runtime_owner import OwnerRefused


def _installation(tmp_path):
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "requirements-runtime.txt").write_text(
        'mcp==2.0.0\ncryptography==50.0.0\npywin32==312; sys_platform == "win32"\n',
        encoding="utf-8",
    )
    base = tmp_path / "python312"
    base.mkdir()
    executable = base / "python.exe"
    executable.write_bytes(b"MZbase")
    root = tmp_path / "managed-venv"
    (root / "Scripts").mkdir(parents=True)
    (root / "Scripts" / "python.exe").write_bytes(b"MZvenv")
    site = root / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text(
        f"home = {base}\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    for name, version in (("mcp", "2.0.0"), ("cryptography", "50.0.0")):
        dist = site / f"{name}-{version}.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )
    return source, base, executable, root, site


def test_sealed_profile_rejects_later_package_changes(tmp_path):
    source, base, executable, root, site = _installation(tmp_path)
    kwargs = {"source": source, "base": base, "executable": executable}
    marker = runtime_profile.seal_installed_profile(root=root, **kwargs)
    assert runtime_profile.profile_files(root, **kwargs)[0] == str(marker)
    assert json.loads(marker.read_text(encoding="utf-8"))["packages"] == {
        "cryptography": "50.0.0", "mcp": "2.0.0",
    }
    extra = site / "torch-9.0.dist-info"
    extra.mkdir()
    (extra / "METADATA").write_text("Name: torch\nVersion: 9.0\n", encoding="utf-8")
    with pytest.raises(OwnerRefused, match="differs from provisioned"):
        runtime_profile.profile_files(root, **kwargs)


def test_profile_refuses_changed_contract_system_site_and_base(tmp_path):
    source, base, executable, root, _ = _installation(tmp_path)
    kwargs = {"source": source, "base": base, "executable": executable}
    runtime_profile.seal_installed_profile(root=root, **kwargs)
    (source / "requirements-runtime.txt").write_text("mcp==2.0.1\ncryptography==50.0.0\n")
    with pytest.raises(OwnerRefused, match="pinned dependencies"):
        runtime_profile.profile_files(root, **kwargs)
    (source / "requirements-runtime.txt").write_text(
        'mcp==2.0.0\ncryptography==50.0.0\npywin32==312; sys_platform == "win32"\n'
    )
    config = root / "pyvenv.cfg"
    config.write_text(f"home = {base}\ninclude-system-site-packages = true\n")
    with pytest.raises(OwnerRefused, match="inherits an untrusted"):
        runtime_profile.profile_files(root, **kwargs)
    config.write_text(f"home = {tmp_path / 'foreign-python'}\ninclude-system-site-packages = false\n")
    with pytest.raises(OwnerRefused, match="inherits an untrusted"):
        runtime_profile.profile_files(root, **kwargs)


def test_profile_rejects_relative_and_reparse_paths(tmp_path):
    source, base, executable, root, _ = _installation(tmp_path)
    kwargs = {"source": source, "base": base, "executable": executable}
    runtime_profile.seal_installed_profile(root=root, **kwargs)
    with pytest.raises(OwnerRefused, match="absolute canonical"):
        runtime_profile.profile_files("managed-venv", **kwargs)
    alias = tmp_path / "venv-alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(OwnerRefused, match="reparse"):
        runtime_profile.profile_files(alias, **kwargs)


def test_profile_root_cannot_be_model_writable(tmp_path):
    source, base, executable, root, _ = _installation(tmp_path)
    kwargs = {"source": source, "base": base, "executable": executable}
    runtime_profile.seal_installed_profile(root=root, **kwargs)
    with pytest.raises(OwnerRefused, match="overlaps model-writable"):
        runtime_payload.disjoint((root,), (root / "Scripts",))


def test_profile_seal_does_not_replace_an_existing_marker(tmp_path):
    source, base, executable, root, _ = _installation(tmp_path)
    kwargs = {"source": source, "base": base, "executable": executable}
    marker = runtime_profile.seal_installed_profile(root=root, **kwargs)
    before = marker.read_bytes()
    with pytest.raises(OwnerRefused, match="already sealed"):
        runtime_profile.seal_installed_profile(root=root, **kwargs)
    assert marker.read_bytes() == before


def test_workstation_entrypoint_selects_only_the_installer_profile(
    tmp_path, monkeypatch
):
    import sonder_runtime.bootstrap.managed_runtime_owner as owner_module

    ManagedRuntimeOwner = owner_module.ManagedRuntimeOwner

    captured = {}

    def capture(self, path, *, writable_roots, runtime_venv):
        captured.update(path=path, writable_roots=writable_roots,
                        runtime_venv=runtime_venv)

    monkeypatch.setattr(ManagedRuntimeOwner, "__init__", capture)
    workspace = lambda: ()
    ManagedRuntimeOwner.workstation_local(tmp_path / "owner", writable_roots=workspace)
    assert captured["runtime_venv"] == (
        Path(owner_module.__file__).resolve().parents[2] / "venv-managed"
    )
    assert captured["writable_roots"] is workspace

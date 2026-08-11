"""`required_kinds` must be checked against files on disk, not against itself.

`assetgen.verify_pack` asked `artifact_grounding.validate` to enforce

    "required_kinds": manifest.get("kinds", [])

and `artifact_grounding._validate_directory` answered it with

    kinds = set(manifest.get("kinds", []))
    for kind in _string_list(requirements, "required_kinds"):
        _check(checks, "bundle-required-kind", kind in kinds, ...)

Both sides read the same list, so the check compared a value with itself and
could not fail. `manifest["kinds"]` is the *requested* kinds
(`assetgen.generate_artifacts` copies them straight from the parsed request),
so a kind whose writer produced nothing still passed: the hashes covered the
files that were there, and nothing covered the ones that were not.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import artifact_grounding
import assetgen


pytestmark = pytest.mark.unit


@pytest.fixture
def pack(monkeypatch, tmp_path):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    return assetgen.generate_pack("kinds-evidence", "2d", "arcane", 7)


def _manifest(root):
    return json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(root, manifest):
    (Path(root) / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_kind_artifact_map_covers_every_declared_kind():
    """The map is the evidence source; drift would silently re-open the hole."""
    assert set(assetgen.KIND_ARTIFACTS) == assetgen.ARTIFACT_KINDS
    for kind, names in assetgen.KIND_ARTIFACTS.items():
        assert names, "kind %r claims no artifact file" % kind
        for name in names:
            assert name in assetgen.OWNED_FILENAMES, (kind, name)


def test_complete_pack_still_passes(pack):
    result = assetgen.verify_pack(pack["root"])
    assert result["ok"], result["failures"]


def test_required_kind_fails_when_its_artifact_never_landed(pack):
    """The exact shape the tautology hid: requested, manifested, not produced."""
    root = Path(pack["root"])
    manifest = _manifest(root)
    assert "icon" in manifest["kinds"]

    # A writer that silently produced nothing: the file is gone and the
    # manifest no longer claims it, so every hash and size check still passes.
    (root / "icon.png").unlink()
    manifest["files"] = [row for row in manifest["files"] if row["path"] != "icon.png"]
    _write_manifest(root, manifest)

    result = assetgen.verify_pack(root)
    assert not result["ok"]
    assert any("icon" in failure for failure in result["failures"]), result["failures"]


def test_grounding_refuses_a_required_kind_it_has_no_evidence_for(tmp_path):
    """No kind_files entry means no way to check -- which must not read as pass."""
    (tmp_path / "thing.txt").write_text("x\n", encoding="utf-8")
    result = artifact_grounding.validate(
        str(tmp_path), "bundle", {"required_kinds": ["a-kind-nobody-described"]},
    )
    names = {item["name"] for item in result["checks"] if not item["ok"]}
    assert "bundle-required-kind" in names
    assert not result["ok"]


def test_required_kinds_are_enforced_on_a_bundle_with_no_manifest(tmp_path):
    """The whole required-kinds loop sat inside `if isinstance(manifest, dict)`.

    A manifest-less bundle therefore had its `required_kinds` requirement
    silently dropped -- the caller asked for a check that was never run and
    got `ok`.
    """
    (tmp_path / "scene.json").write_text("{}\n", encoding="utf-8")
    requirements = {"required_kinds": ["scene", "icon"],
                    "kind_files": {"scene": ["scene.json"], "icon": ["icon.png"]}}
    result = artifact_grounding.validate(str(tmp_path), "bundle", requirements)
    rows = [item for item in result["checks"] if item["name"] == "bundle-required-kind"]
    assert len(rows) == 2, rows
    assert {row["ok"] for row in rows} == {True, False}
    assert any("icon" in row["detail"] and not row["ok"] for row in rows)


def test_required_kind_fails_when_the_file_is_only_on_disk_undeclared(pack):
    """Directly: a kind whose file the manifest never declared is not evidence."""
    root = Path(pack["root"])
    manifest = _manifest(root)
    manifest["files"] = [row for row in manifest["files"] if row["path"] != "scene.json"]
    _write_manifest(root, manifest)
    result = artifact_grounding.validate(
        str(root),
        "bundle",
        {
            "require_manifest": True,
            "required_kinds": ["scene"],
            "kind_files": {"scene": ["scene.json"]},
        },
    )
    failed = {item["name"] for item in result["checks"] if not item["ok"]}
    assert "bundle-required-kind" in failed, result["checks"]

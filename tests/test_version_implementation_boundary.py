from __future__ import annotations

import json

from sonder_runtime.platform import version


def test_root_version_keeps_literal_release_contract():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("sonder_version.py").read_text(encoding="utf-8"))
    assignments = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", None) == "VERSION" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    assert assignments == [version.VERSION]


def test_root_import_is_canonical_module_identity():
    import sonder_version

    assert sonder_version is version
    assert sonder_version.BuildInfo is version.BuildInfo
    assert sonder_version.build_info is version.build_info
    assert sonder_version.running_source_commit_at_import is version.running_source_commit_at_import
    assert sonder_version._commit_from_git is version._commit_from_git


def test_build_info_fallback_uses_packaged_running_source_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_BUILD_STAMP", tmp_path / "missing-stamp.json")
    monkeypatch.setattr(version, "running_source_commit_at_import", lambda: "b" * 40)

    assert version.build_info().as_dict() == {
        "version": version.VERSION,
        "commit_sha": "b" * 40,
        "stamped": False,
    }


def test_legacy_commit_probe_keeps_unknown_marker_on_missing_git(monkeypatch):
    monkeypatch.setattr(version, "running_source_commit_at_import", lambda: "")

    assert version._commit_from_git() == "unknown"


def test_build_stamp_is_read_from_package_root_without_metadata_drift(tmp_path, monkeypatch):
    stamp = tmp_path / "sonder_build.json"
    stamp.write_text(
        json.dumps({"version": "1.2.3", "commit_sha": "a" * 40}),
        encoding="utf-8",
    )
    monkeypatch.setattr(version, "_BUILD_STAMP", stamp)

    assert version.build_info().as_dict() == {
        "version": "1.2.3",
        "commit_sha": "a" * 40,
        "stamped": True,
    }

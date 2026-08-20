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

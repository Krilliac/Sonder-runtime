import hashlib
import json
from pathlib import Path

import artifact_grounding
import assetgen


def _failures(result):
    rows = [item for item in result["checks"] if not item["ok"]]
    for child in result.get("children", []):
        rows.extend(item for item in child["checks"] if not item["ok"])
    return rows


def _update_manifest_hash(root: Path, filename: str):
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = (root / filename).read_bytes()
    row = next(item for item in manifest["files"] if item["path"] == filename)
    row["bytes"] = len(data)
    row["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_text_markdown_json_and_csv_recipes(tmp_path):
    markdown = tmp_path / "report.md"
    markdown.write_text(
        "# Release report\n\n## Verification\n\nAll checks passed.\n",
        encoding="utf-8",
    )
    data_json = tmp_path / "data.json"
    data_json.write_text(
        json.dumps({"meta": {"version": 2}, "rows": [1, 2]}),
        encoding="utf-8",
    )
    data_csv = tmp_path / "data.csv"
    data_csv.write_text("id,name\n1,alpha\n2,beta\n", encoding="utf-8")

    writing = artifact_grounding.validate(
        markdown,
        "writing",
        {
            "min_words": 5,
            "min_headings": 2,
            "required_headings": ["Release report", "Verification"],
            "required_text": ["checks passed"],
        },
    )
    structured = artifact_grounding.validate(
        data_json,
        "data",
        {"root_type": "object", "required_fields": ["meta.version", "rows.1"]},
    )
    tabular = artifact_grounding.validate(
        data_csv,
        "csv",
        {"required_columns": ["id", "name"], "min_rows": 2},
    )

    assert writing["ok"]
    assert structured["ok"]
    assert tabular["ok"]
    assert writing["recipe"] == "markdown"
    assert structured["recipe"] == "json"


def test_requirements_report_actionable_failures(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("# Draft\n\nTODO: finish.\n", encoding="utf-8")

    result = artifact_grounding.validate(
        path,
        "markdown",
        {
            "min_words": 20,
            "required_headings": ["Verification"],
            "forbidden_text": ["TODO"],
        },
    )

    assert not result["ok"]
    names = {item["name"] for item in _failures(result)}
    assert {"minimum-words", "required-heading", "forbidden-text"} <= names
    formatted = artifact_grounding.format_result(result)
    assert "artifact grounding: FAIL" in formatted
    assert "required-heading" in formatted


def test_ui_recipe_checks_entrypoint_local_files_and_external_dependencies(tmp_path):
    (tmp_path / "app.js").write_text("document.body.dataset.ready = '1';\n", encoding="utf-8")
    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><body><main>Ready</main>"
        '<script src="app.js"></script></body></html>',
        encoding="utf-8",
    )

    valid = artifact_grounding.validate(
        tmp_path,
        "ui",
        {"no_external_dependencies": True, "required_files": ["index.html", "app.js"]},
    )
    assert valid["ok"]

    page.write_text(
        "<!doctype html><html><body>"
        '<script src="https://cdn.example/app.js"></script></body></html>',
        encoding="utf-8",
    )
    invalid = artifact_grounding.validate(
        tmp_path,
        "ui",
        {"no_external_dependencies": True},
    )

    assert not invalid["ok"]
    assert any(
        item["name"] == "html-no-external-dependencies"
        for item in _failures(invalid)
    )


def test_generated_all_format_pack_passes_manifest_and_format_recipes(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = assetgen.generate_pack("all-formats", "3d", "frost", 42)

    result = artifact_grounding.validate(
        pack["root"],
        "bundle",
        {
            "require_manifest": True,
            "required_kinds": pack["kinds"],
            "required_files": [
                "brief.md",
                "data.csv",
                "preview.html",
                "icon.png",
                "theme.wav",
                "models.obj",
            ],
            "recipes": {"html": {"no_external_dependencies": True}},
        },
    )

    assert result["ok"]
    assert result["checked_files"] == len(pack["files"])
    assert result["failed_checks"] == 0
    recipes = {child["recipe"] for child in result["children"]}
    assert {"markdown", "csv", "html", "json", "obj", "png", "ppm", "svg", "wav"} <= recipes


def test_format_validation_catches_tampering_even_with_updated_manifest(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = assetgen.generate_artifacts("icon-pack", "frost icon", kinds="icon")
    root = Path(pack["root"])
    (root / "icon.png").write_bytes(b"not actually a PNG")
    _update_manifest_hash(root, "icon.png")

    result = artifact_grounding.validate(
        root,
        "bundle",
        {"require_manifest": True},
    )

    assert not result["ok"]
    assert any(item["name"] == "valid-png" for item in _failures(result))
    assert not any(item["name"] == "bundle-sha256" for item in _failures(result))


def test_bundle_rejects_manifest_path_escape(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "outside-artifact.txt"
    outside.write_text("outside", encoding="utf-8")
    manifest = {
        "schema": 2,
        "kinds": ["document"],
        "files": [
            {
                "path": "../outside-artifact.txt",
                "bytes": outside.stat().st_size,
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            }
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = artifact_grounding.validate(
        root,
        "bundle",
        {"require_manifest": True},
    )

    assert not result["ok"]
    assert any(item["name"] == "bundle-safe-path" for item in _failures(result))


def test_missing_path_and_invalid_requirements_fail_closed(tmp_path):
    missing = artifact_grounding.validate(tmp_path / "missing.json")
    assert not missing["ok"]
    assert missing["checked_files"] == 0

    path = tmp_path / "data.json"
    path.write_text("{}", encoding="utf-8")
    try:
        artifact_grounding.validate(path, "json", {"required_fields": {"bad": True}})
    except ValueError as exc:
        assert "required_fields" in str(exc)
    else:
        raise AssertionError("invalid requirements should fail closed")

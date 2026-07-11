import hashlib
import json
import zipfile
from pathlib import Path

import pytest

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


def _rewrite_zip_entry(path: Path, entry_name: str, transform):
    with zipfile.ZipFile(path) as source:
        entries = [
            (info, transform(source.read(info.filename)) if info.filename == entry_name else source.read(info.filename))
            for info in source.infolist()
        ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as destination:
        for info, data in entries:
            destination.writestr(info, data)


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
                "animation.gif",
                "captions.srt",
                "captions.vtt",
                "data.csv",
                "document.docx",
                "preview.html",
                "icon.png",
                "presentation.pptx",
                "score.mid",
                "theme.wav",
                "timeline.edl",
                "models.obj",
                "workbook.xlsx",
            ],
            "recipes": {"html": {"no_external_dependencies": True}},
        },
    )

    assert result["ok"]
    assert result["checked_files"] == len(pack["files"])
    assert result["failed_checks"] == 0
    recipes = {child["recipe"] for child in result["children"]}
    assert {
        "markdown", "csv", "docx", "edl", "gif", "html", "json", "midi",
        "obj", "png", "ppm", "pptx", "srt", "svg", "vtt", "wav", "xlsx",
    } <= recipes


def test_editable_media_recipes_enforce_structure_and_content(tmp_path):
    palette = ((17, 15, 35), (116, 91, 218), (87, 218, 207), (49, 38, 91))
    animation = tmp_path / "animation.gif"
    score = tmp_path / "score.mid"
    captions_srt = tmp_path / "captions.srt"
    captions_vtt = tmp_path / "captions.vtt"
    timeline = tmp_path / "timeline.edl"
    assetgen.media_assets.write_gif(animation, palette, 42)
    assetgen.media_assets.write_midi(score, "Arcane Suite", "arcane", 42)
    assetgen.media_assets.write_srt(captions_srt, "arcane launch")
    assetgen.media_assets.write_vtt(captions_vtt, "arcane launch")
    assetgen.media_assets.write_edl(timeline, "Arcane Suite", "arcane launch")

    results = [
        artifact_grounding.validate(
            animation, "animation", {"min_frames": 8, "min_duration_ms": 640}
        ),
        artifact_grounding.validate(
            score, "midi", {"min_notes": 16, "require_tempo": True}
        ),
        artifact_grounding.validate(
            captions_srt, "captions", {"min_cues": 6, "required_text": ["arcane"]}
        ),
        artifact_grounding.validate(
            captions_vtt, "subtitle", {"min_cues": 6, "required_text": ["arcane"]}
        ),
        artifact_grounding.validate(
            timeline, "timeline", {"min_events": 6, "required_text": ["Arcane Suite"]}
        ),
    ]

    assert all(result["ok"] for result in results)
    assert [result["recipe"] for result in results] == [
        "gif", "midi", "srt", "vtt", "edl"
    ]


def test_edl_no_external_dependencies_requires_local_media(tmp_path):
    timeline = tmp_path / "timeline.edl"
    animation = tmp_path / "animation.gif"
    palette = ((17, 15, 35), (116, 91, 218), (87, 218, 207), (49, 38, 91))
    assetgen.media_assets.write_edl(timeline, "Demo", "Local timeline")

    missing = artifact_grounding.validate(
        timeline, "edl", {"no_external_dependencies": True}
    )
    assetgen.media_assets.write_gif(animation, palette, 42)
    complete = artifact_grounding.validate(
        timeline, "edl", {"no_external_dependencies": True}
    )

    assert not missing["ok"]
    assert any(item["name"] == "edl-local-media" for item in _failures(missing))
    assert complete["ok"]


def test_editable_office_recipes_check_content_and_structure(tmp_path):
    document = tmp_path / "report.docx"
    workbook = tmp_path / "metrics.xlsx"
    presentation = tmp_path / "roadmap.pptx"
    assetgen.ooxml_assets.write_docx(document, "Release", "Verified locally")
    assetgen.ooxml_assets.write_xlsx(workbook, "Metrics", "Verified locally", 42)
    assetgen.ooxml_assets.write_pptx(presentation, "Roadmap", "Verified locally")

    results = [
        artifact_grounding.validate(
            document,
            "office",
            {
                "min_paragraphs": 10,
                "required_text": ["Release", "Verified locally"],
                "no_external_dependencies": True,
            },
        ),
        artifact_grounding.validate(
            workbook,
            "spreadsheet",
            {
                "min_rows": 13,
                "required_sheet_names": ["Data"],
                "required_text": ["Metrics"],
                "no_external_dependencies": True,
            },
        ),
        artifact_grounding.validate(
            presentation,
            "presentation",
            {
                "min_slides": 3,
                "required_text": ["Roadmap", "provenance"],
                "no_external_dependencies": True,
            },
        ),
    ]

    assert all(result["ok"] for result in results)
    assert [result["recipe"] for result in results] == ["ooxml", "xlsx", "pptx"]


def test_ooxml_validation_catches_missing_part_after_manifest_rehash(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = assetgen.generate_artifacts("editable", "editable document", kinds="docx")
    root = Path(pack["root"])
    document = root / "document.docx"
    with zipfile.ZipFile(document) as source:
        entries = {
            info.filename: source.read(info.filename)
            for info in source.infolist()
            if info.filename != "word/document.xml"
        }
    with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as destination:
        for name, data in sorted(entries.items()):
            destination.writestr(name, data)
    _update_manifest_hash(root, "document.docx")

    result = artifact_grounding.validate(root, "bundle", {"require_manifest": True})

    assert not result["ok"]
    assert any(item["name"] == "ooxml-required-part" for item in _failures(result))
    assert not any(item["name"] == "bundle-sha256" for item in _failures(result))


def test_bundle_ooxml_grounding_propagates_no_external_dependencies(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = assetgen.generate_artifacts("editable", "editable document", kinds="docx")
    root = Path(pack["root"])
    document = root / "document.docx"
    _rewrite_zip_entry(
        document,
        "word/_rels/document.xml.rels",
        lambda data: data.replace(
            b'Target="styles.xml"',
            b'Target="https://example.invalid/styles.xml" TargetMode="External"',
        ),
    )
    _update_manifest_hash(root, "document.docx")

    result = artifact_grounding.validate(
        root,
        "bundle",
        {"require_manifest": True, "no_external_dependencies": True},
    )

    assert not result["ok"]
    assert any(
        item["name"] == "ooxml-no-external-dependencies"
        for item in _failures(result)
    )
    assert not any(item["name"] == "bundle-sha256" for item in _failures(result))


@pytest.mark.parametrize(
    "entry_name,check_name",
    [("../escape.bin", "ooxml-safe-paths"), ("word/vbaProject.bin", "ooxml-no-active-content")],
)
def test_ooxml_rejects_unsafe_or_active_zip_entries(tmp_path, entry_name, check_name):
    document = tmp_path / "unsafe.docx"
    assetgen.ooxml_assets.write_docx(document, "Safe", "Before tampering")
    with zipfile.ZipFile(document, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entry_name, b"not allowed")

    result = artifact_grounding.validate(document, "docx")

    assert not result["ok"]
    assert any(item["name"] == check_name for item in _failures(result))


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


def test_media_validation_catches_tampering_even_with_updated_manifest(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = assetgen.generate_artifacts(
        "media-pack", "animated GIF and MIDI score", kinds="animation,midi"
    )
    root = Path(pack["root"])
    animation = root / "animation.gif"
    data = bytearray(animation.read_bytes())
    data[-1] = 0
    animation.write_bytes(data)
    _update_manifest_hash(root, "animation.gif")

    result = artifact_grounding.validate(
        root,
        "bundle",
        {"require_manifest": True, "recipes": {"gif": {"min_frames": 2}}},
    )

    assert not result["ok"]
    assert any(item["name"] == "valid-gif" for item in _failures(result))
    assert not any(item["name"] == "bundle-sha256" for item in _failures(result))


def test_bundle_edl_grounding_rejects_rehashed_missing_media_reference(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = assetgen.generate_artifacts(
        "timeline-pack", "editable timeline", kinds="timeline"
    )
    root = Path(pack["root"])
    timeline = root / "timeline.edl"
    timeline.write_text(
        timeline.read_text(encoding="utf-8").replace(
            "animation.gif", "missing-source.gif"
        ),
        encoding="utf-8",
    )
    _update_manifest_hash(root, "timeline.edl")

    result = artifact_grounding.validate(
        root,
        "bundle",
        {"require_manifest": True, "no_external_dependencies": True},
    )

    assert not result["ok"]
    assert any(item["name"] == "edl-local-media" for item in _failures(result))
    assert not any(item["name"] == "bundle-sha256" for item in _failures(result))


@pytest.mark.parametrize(
    "filename,writer,mutator,failed_check",
    [
        (
            "broken.mid",
            lambda path: assetgen.media_assets.write_midi(path, "Demo", "arcane", 42),
            lambda data: data[:-1],
            "valid-midi",
        ),
        (
            "broken.srt",
            lambda path: assetgen.media_assets.write_srt(path, "Demo captions"),
            lambda data: data.replace(b"00:00:01,800", b"00:00:00,000", 1),
            "srt-timing",
        ),
        (
            "broken.vtt",
            lambda path: assetgen.media_assets.write_vtt(path, "Demo captions"),
            lambda data: data.replace(b"WEBVTT", b"BROKEN", 1),
            "valid-vtt",
        ),
        (
            "broken.edl",
            lambda path: assetgen.media_assets.write_edl(path, "Demo", "Demo timeline"),
            lambda data: data.replace(b"FCM: NON-DROP FRAME", b"FCM: UNKNOWN", 1),
            "valid-edl",
        ),
    ],
)
def test_media_recipes_reject_malformed_content(
    tmp_path, filename, writer, mutator, failed_check
):
    path = tmp_path / filename
    writer(path)
    path.write_bytes(mutator(path.read_bytes()))

    result = artifact_grounding.validate(path, "auto")

    assert not result["ok"]
    assert any(item["name"] == failed_check for item in _failures(result))


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

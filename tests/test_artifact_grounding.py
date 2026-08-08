import hashlib
import json
import struct
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


def _rewrite_glb_document(path: Path, transform):
    """Apply `transform` to a GLB's JSON chunk and rebuild the container."""
    payload = bytearray(path.read_bytes())
    json_length = struct.unpack_from("<I", payload, 12)[0]
    document = json.loads(payload[20:20 + json_length].decode("utf-8").rstrip())
    transform(document)
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    rebuilt = bytearray(payload[:12])
    rebuilt += struct.pack("<I", len(encoded)) + b"JSON"
    rebuilt += encoded
    rebuilt += payload[20 + json_length:]
    struct.pack_into("<I", rebuilt, 8, len(rebuilt))
    path.write_bytes(bytes(rebuilt))


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
                "preview.avi",
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
        "markdown", "avi", "csv", "docx", "edl", "gif", "html", "json", "midi",
        "obj", "png", "ppm", "pptx", "srt", "svg", "vtt", "wav", "xlsx",
    } <= recipes


def test_editable_media_recipes_enforce_structure_and_content(tmp_path):
    palette = ((17, 15, 35), (116, 91, 218), (87, 218, 207), (49, 38, 91))
    animation = tmp_path / "animation.gif"
    video = tmp_path / "preview.avi"
    score = tmp_path / "score.mid"
    captions_srt = tmp_path / "captions.srt"
    captions_vtt = tmp_path / "captions.vtt"
    timeline = tmp_path / "timeline.edl"
    assetgen.media_assets.write_gif(animation, palette, 42)
    assetgen.media_assets.write_avi(video, palette, "arcane", 42)
    assetgen.media_assets.write_midi(score, "Arcane Suite", "arcane", 42)
    assetgen.media_assets.write_srt(captions_srt, "arcane launch")
    assetgen.media_assets.write_vtt(captions_vtt, "arcane launch")
    assetgen.media_assets.write_edl(timeline, "Arcane Suite", "arcane launch")

    results = [
        artifact_grounding.validate(
            video,
            "video",
            {"min_frames": 48, "min_duration_ms": 4000, "require_audio": True},
        ),
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
        "avi", "gif", "midi", "srt", "vtt", "edl"
    ]


def test_edl_no_external_dependencies_requires_local_media(tmp_path):
    timeline = tmp_path / "timeline.edl"
    video = tmp_path / "preview.avi"
    palette = ((17, 15, 35), (116, 91, 218), (87, 218, 207), (49, 38, 91))
    assetgen.media_assets.write_edl(timeline, "Demo", "Local timeline")

    missing = artifact_grounding.validate(
        timeline, "edl", {"no_external_dependencies": True}
    )
    assetgen.media_assets.write_avi(video, palette, "arcane", 42)
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
            "preview.avi", "missing-source.avi"
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
            "broken.avi",
            lambda path: assetgen.media_assets.write_avi(
                path,
                ((17, 15, 35), (116, 91, 218), (87, 218, 207), (49, 38, 91)),
                "arcane",
                42,
            ),
            lambda data: data[:-17],
            "valid-avi",
        ),
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
    root.mkdir(parents=True)
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


def _bundle_with_extras(tmp_path, extras):
    """A bundle whose manifest is honest about one file and silent about the rest."""
    import hashlib
    import json

    root = tmp_path / "bundle"
    root.mkdir(parents=True)
    report = root / "report.md"
    report.write_text("# hello\n", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps({
            "files": [{
                "path": "report.md",
                "bytes": report.stat().st_size,
                "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            }],
            "kinds": ["report"],
        }),
        encoding="utf-8",
    )
    for index in range(extras):
        (root / ("stow_%02d.bin" % index)).write_bytes(b"\x00" * 1024)
    return root


def test_manifest_bundle_is_validated_against_the_directory_not_itself(tmp_path):
    """With a manifest present, validation walked only the manifest's own rows
    and never enumerated the directory.

    So a bundle could declare one small correct file and ship any number of
    undeclared ones beside it: the extras were not counted toward
    bundle-file-limit, not added to bundle-total-size, and never hashed. The
    limits were measuring the manifest's honesty about itself.
    """
    clean = artifact_grounding._validate_directory(
        _bundle_with_extras(tmp_path / "a", 0), "bundle", {}
    )
    assert clean["ok"] is True

    smuggled = artifact_grounding._validate_directory(
        _bundle_with_extras(tmp_path / "b", 12), "bundle", {}
    )
    assert smuggled["ok"] is False
    named = {
        check.get("name"): check.get("ok")
        for check in smuggled.get("checks", [])
        if isinstance(check, dict)
    }
    assert named.get("bundle-no-undeclared-files") is False


def test_bundle_glb_grounding_propagates_no_external_dependencies(
    monkeypatch, tmp_path
):
    """glb was missing from the bundle->child propagation set, so
    glb-no-external-dependencies was never emitted for a .glb inside a bundle.

    A model whose glb pulled its textures over the network therefore passed a
    bundle validated with no_external_dependencies=true, while a sibling
    index.html with the same defect correctly failed. An absent check reads as
    a pass, so the caller saw PASS with no signal that enforcement was partial.
    """
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = assetgen.generate_artifacts(
        "textured-rig", "textured PBR rigged GLB character", kinds="rigged_model"
    )
    root = Path(pack["root"])

    def _detach_texture(document):
        image = document["images"][0]
        image.pop("bufferView", None)
        image.pop("mimeType", None)
        image["uri"] = "https://cdn.example.invalid/skin.png"

    _rewrite_glb_document(root / "rigged.glb", _detach_texture)
    _update_manifest_hash(root, "rigged.glb")

    result = artifact_grounding.validate(
        root,
        "bundle",
        {"require_manifest": True, "no_external_dependencies": True},
    )

    emitted = {
        item["name"]
        for child in result["children"]
        for item in child["checks"]
    }
    assert "glb-no-external-dependencies" in emitted
    assert not result["ok"]
    assert any(
        item["name"] == "glb-no-external-dependencies"
        for item in _failures(result)
    )
    assert not any(item["name"] == "bundle-sha256" for item in _failures(result))


def test_html_no_external_dependencies_sees_beyond_href_and_src(tmp_path):
    """The guard only inspected href/src, so every other way an HTML page
    reaches the network passed as self-contained.

    A @import in a <style> block, a background url() in an inline style, a
    srcset candidate, a <video poster> and an <object data> all fetch remote
    bytes; the page could not render offline while grounding reported
    "external references: none".
    """
    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><head>"
        '<style>@import url("https://fonts.example.invalid/inter.css");</style>'
        "</head><body>"
        '<div style="background:url(https://cdn.example.invalid/hero.jpg)">x</div>'
        '<img srcset="local.png 1x, https://cdn.example.invalid/hero@2x.png 2x">'
        '<video poster="https://cdn.example.invalid/poster.jpg"></video>'
        '<object data="https://cdn.example.invalid/widget.svg"></object>'
        "</body></html>",
        encoding="utf-8",
    )

    result = artifact_grounding.validate(
        page, "html", {"no_external_dependencies": True}
    )

    external = next(
        item for item in result["checks"]
        if item["name"] == "html-no-external-dependencies"
    )
    assert not external["ok"]
    for host_path in ("inter.css", "hero.jpg", "hero@2x.png", "poster.jpg", "widget.svg"):
        assert host_path in external["detail"]


def test_html_no_external_dependencies_allows_relative_css_and_srcset(tmp_path):
    """The widened scan must not start rejecting local references: a relative
    url() or srcset candidate is exactly what a self-contained page uses."""
    (tmp_path / "hero.jpg").write_bytes(b"jpeg")
    (tmp_path / "hero@2x.png").write_bytes(b"png")
    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><head>"
        '<style>body { background: url("hero.jpg"); }</style>'
        "</head><body>"
        '<div style="color:#fff;background:url(hero.jpg)">x</div>'
        '<img srcset="hero.jpg 1x, hero@2x.png 2x">'
        "</body></html>",
        encoding="utf-8",
    )

    result = artifact_grounding.validate(
        page, "html", {"no_external_dependencies": True}
    )

    external = next(
        item for item in result["checks"]
        if item["name"] == "html-no-external-dependencies"
    )
    assert external["ok"], external["detail"]


def test_ui_recipe_reports_the_files_it_actually_checked(tmp_path):
    """checked_files reported len(declared) while the ui recipe validates only
    .html/.htm/.svg/.json siblings.

    A directory of four files rendered as "files: 4 | 0 failed" when only
    index.html was opened, so a zero-byte logo.png in the same bundle read as
    four files grounded.
    """
    (tmp_path / "app.js").write_text("var ready = 1;\n", encoding="utf-8")
    (tmp_path / "style.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"")
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body><main>Ready</main></body></html>",
        encoding="utf-8",
    )

    result = artifact_grounding.validate(tmp_path, "ui")

    assert result["ok"]
    assert len(result["children"]) == 1
    assert result["checked_files"] == 1
    assert "files: 1" in artifact_grounding.format_result(result)

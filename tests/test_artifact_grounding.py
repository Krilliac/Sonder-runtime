import hashlib
import json
import struct
import subprocess
import sys
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
            # `required_kinds` on its own was read back out of the same
            # manifest and compared with itself, so this held no matter what
            # the pack contained. `kind_files` supplies the evidence side:
            # what each requested kind had to leave on disk.
            "kind_files": {k: list(v) for k, v in assetgen.KIND_ARTIFACTS.items()},
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


@pytest.mark.parametrize("with_manifest", [False, True])
def test_directory_bundle_rejects_in_tree_symlinks(tmp_path, with_manifest):
    root = _bundle_with_extras(tmp_path, 0)
    if not with_manifest:
        (root / "manifest.json").unlink()
    target = root / "report.md"
    link = root / "report-alias.md"
    try:
        link.symlink_to(target.name)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)

    result = artifact_grounding._validate_directory(root, "bundle", {})

    assert result["ok"] is False
    assert any(
        check.get("name") == "bundle-no-symlinks" and not check.get("ok")
        for check in result["checks"]
    )


@pytest.mark.parametrize("with_manifest", [False, True])
def test_directory_bundle_rejects_and_prunes_windows_junctions(
    monkeypatch, tmp_path, with_manifest,
):
    root = _bundle_with_extras(tmp_path, 0)
    if not with_manifest:
        (root / "manifest.json").unlink()
    junction = root / "junction"
    junction.mkdir()
    (junction / "outside.md").write_text("must never be enumerated", encoding="utf-8")
    original = artifact_grounding._is_reparse_point
    monkeypatch.setattr(
        artifact_grounding,
        "_is_reparse_point",
        lambda path: path == junction or original(path),
    )

    result = artifact_grounding._validate_directory(root, "bundle", {})

    assert result["ok"] is False
    assert any(
        check.get("name") == "bundle-no-symlinks" and not check.get("ok")
        for check in result["checks"]
    )
    assert all("outside.md" not in check.get("detail", "") for check in result["checks"])


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


def test_html_local_references_skip_non_file_schemes_and_unquote_paths(tmp_path):
    """Only data:/mailto:/javascript: were skipped and paths were never
    URL-unquoted, so tel:/blob: links and a valid my%20image.png link were
    misreported as missing local files."""
    (tmp_path / "my image.png").write_bytes(b"png")
    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><body>"
        '<img src="my%20image.png">'
        '<a href="tel:+15551234">call</a>'
        '<a href="blob:abc">blob</a>'
        "</body></html>",
        encoding="utf-8",
    )
    valid = artifact_grounding.validate(page, "html")
    assert valid["ok"], _failures(valid)


def test_html_local_references_accept_page_directory_links(tmp_path):
    """href="./" and href="." name the page directory itself (their path
    parts are empty), a common home link that must not be reported missing."""
    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><body>"
        '<a href="./">home</a>'
        '<a href=".">home</a>'
        "</body></html>",
        encoding="utf-8",
    )
    valid = artifact_grounding.validate(page, "html")
    assert valid["ok"], _failures(valid)


def test_html_local_references_reject_unc_and_absolute_refs_without_fs_calls(
    monkeypatch, tmp_path
):
    """A UNC ref used to be joined onto the page directory and passed to
    resolve()/exists(), so validating the page opened an SMB connection to
    the host it named, and it was not reported as an external dependency."""
    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><body>"
        r'<img src="\\attacker.example\share\x.png">'
        r'<img src="%5C%5Cattacker.example%5Cshare%5Cy.png">'
        '<img src="C:/Windows/win.ini">'
        '<img src="/etc/passwd">'
        "</body></html>",
        encoding="utf-8",
    )
    touched = []
    original_resolve = Path.resolve
    original_exists = Path.exists

    def spy_resolve(self, *args, **kwargs):
        touched.append(str(self))
        return original_resolve(self, *args, **kwargs)

    def spy_exists(self, *args, **kwargs):
        touched.append(str(self))
        return original_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    monkeypatch.setattr(Path, "exists", spy_exists)
    checks = []
    artifact_grounding._validate_html(page, {}, checks)
    monkeypatch.undo()
    assert not any("attacker" in item or "win.ini" in item or "passwd" in item for item in touched), touched
    local = next(item for item in checks if item["name"] == "html-local-references")
    assert not local["ok"]
    assert "%5C%5Cattacker.example" in local["detail"]
    assert "C:/Windows/win.ini" in local["detail"]
    assert "/etc/passwd" in local["detail"]

    external = artifact_grounding.validate(
        page, "html", {"no_external_dependencies": True}
    )
    flagged = next(
        item for item in external["checks"]
        if item["name"] == "html-no-external-dependencies"
    )
    assert not flagged["ok"]
    assert "attacker.example" in flagged["detail"]


def test_html_local_references_do_not_follow_links(monkeypatch, tmp_path):
    """The ref was joined and passed to resolve(), which follows a symlink
    or junction in the page directory to its target (possibly a UNC share)
    before containment was checked. A ref through a link must be reported
    missing without resolve()/exists() ever touching a path below it."""
    site = tmp_path / "site"
    assets = site / "assets"
    assets.mkdir(parents=True)
    (assets / "x.png").write_bytes(b"png")
    (site / "y.png").write_bytes(b"png")
    page = site / "index.html"
    page.write_text(
        "<!doctype html><html><body>"
        '<img src="assets/x.png">'
        '<img src="sub/../y.png">'
        "</body></html>",
        encoding="utf-8",
    )
    link = assets.resolve()
    original_reparse = artifact_grounding._is_reparse_point
    monkeypatch.setattr(
        artifact_grounding,
        "_is_reparse_point",
        lambda path: path == link or original_reparse(path),
    )
    touched = []
    original_resolve = Path.resolve
    original_exists = Path.exists

    def spy_resolve(self, *args, **kwargs):
        result = original_resolve(self, *args, **kwargs)
        touched.extend((str(self), str(result)))
        return result

    def spy_exists(self, *args, **kwargs):
        touched.append(str(self))
        return original_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    monkeypatch.setattr(Path, "exists", spy_exists)
    checks = []
    artifact_grounding._validate_html(page, {}, checks)
    monkeypatch.undo()

    assert not any("x.png" in item for item in touched), touched
    local = next(item for item in checks if item["name"] == "html-local-references")
    assert not local["ok"]
    assert "assets/x.png" in local["detail"]
    # A relative path that only climbs back inside the page directory is
    # still found.
    assert "y.png" not in local["detail"]


def test_html_local_references_reject_refs_climbing_out_of_the_page(tmp_path):
    (tmp_path / "outside.png").write_bytes(b"png")
    site = tmp_path / "site"
    site.mkdir()
    page = site / "index.html"
    page.write_text(
        "<!doctype html><html><body>"
        '<img src="../outside.png">'
        '<img src="a/../../outside.png">'
        "</body></html>",
        encoding="utf-8",
    )
    checks = []
    artifact_grounding._validate_html(page, {}, checks)
    local = next(item for item in checks if item["name"] == "html-local-references")
    assert not local["ok"]
    assert "../outside.png" in local["detail"]
    assert "a/../../outside.png" in local["detail"]


def test_html_unc_resource_refs_count_as_external(tmp_path):
    """srcset/poster and CSS url() refs were only flagged when they began
    with "//", so the backslash UNC spelling a Windows browser fetches over
    SMB passed no_external_dependencies."""
    page = tmp_path / "index.html"
    page.write_text(
        "<!doctype html><html><body>"
        r'<img srcset="\\attacker.example\share\a.png 1x">'
        r'<div style="background: url(\\attacker.example\share\b.png)"></div>'
        "</body></html>",
        encoding="utf-8",
    )
    result = artifact_grounding.validate(page, "html", {"no_external_dependencies": True})
    flagged = next(
        item for item in result["checks"] if item["name"] == "html-no-external-dependencies"
    )
    assert not flagged["ok"]
    assert "a.png" in flagged["detail"]
    assert "b.png" in flagged["detail"]


def _tiny_aliasing_glb(
    path: Path,
    accessor_count: int,
    items: int,
    component_type: int = 5121,
    value_type: str = "SCALAR",
):
    """Write a GLB whose accessors all alias one bufferView of the BIN chunk."""
    element_size = {5121: 1, 5126: 4}[component_type] * {
        "SCALAR": 1, "VEC4": 4, "MAT4": 16
    }[value_type]
    view_length = items * element_size
    document = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": view_length}],
        "bufferViews": [{"buffer": 0, "byteLength": view_length}],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": component_type,
                "type": value_type,
                "count": items,
            }
            for _ in range(accessor_count)
        ],
    }
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    binary = bytes(view_length) + bytes(-view_length % 4)
    body = struct.pack("<I", len(encoded)) + b"JSON" + encoded
    body += struct.pack("<I", len(binary)) + b"BIN\x00" + binary
    path.write_bytes(b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body)


def _record_materialized_slots(monkeypatch, materialized, values_for=None):
    """Wrap _glb_accessor_values and record the budget charge of each call."""
    original = artifact_grounding._glb_accessor_values

    def counting(document, binary, index):
        accessor = document["accessors"][index]
        values = (values_for or original)(document, binary, index)
        materialized.append(
            artifact_grounding._glb_accessor_slot_charge(
                accessor["count"], accessor["type"]
            )
        )
        return values

    monkeypatch.setattr(artifact_grounding, "_glb_accessor_values", counting)


def test_glb_accessor_materialization_has_an_aggregate_budget(monkeypatch, tmp_path):
    """Accessors may alias one bufferView, so a small BIN chunk could be
    unpacked once per accessor with no total bound and exhaust memory."""
    model = tmp_path / "alias.glb"
    _tiny_aliasing_glb(model, accessor_count=50, items=8)
    monkeypatch.setattr(
        artifact_grounding, "MAX_GLB_TOTAL_ACCESSOR_SLOTS", 60, raising=False
    )
    materialized = []
    _record_materialized_slots(monkeypatch, materialized)
    checks = []
    artifact_grounding._validate_glb(model, {}, checks)

    assert sum(materialized) <= 60
    accessors = next(item for item in checks if item["name"] == "glb-accessors")
    assert not accessors["ok"]
    assert "total accessor data budget" in accessors["detail"]


def test_glb_accessor_budget_weights_rows_by_component_count(monkeypatch, tmp_path):
    """A row-count budget let MAT4/5126 rows (a 16-float tuple each, about
    10x the memory of a SCALAR row) through at the same rate as SCALAR rows,
    so aliasing float matrix accessors still reached several GB. The budget
    must charge every component that is built."""
    model = tmp_path / "alias-mat4.glb"
    # 10 aliasing MAT4 float accessors of 4 rows: 40 rows in total, well
    # under a 120-unit budget if rows were counted, but 4 * (16 + 2) = 72
    # row slots plus the fixed per-accessor charge each, so only one
    # accessor fits a 120-slot budget.
    _tiny_aliasing_glb(
        model, accessor_count=10, items=4, component_type=5126, value_type="MAT4"
    )
    monkeypatch.setattr(
        artifact_grounding, "MAX_GLB_TOTAL_ACCESSOR_SLOTS", 120, raising=False
    )
    monkeypatch.setattr(artifact_grounding, "GLB_ACCESSOR_FIXED_SLOTS", 5)
    materialized = []
    _record_materialized_slots(monkeypatch, materialized)
    checks = []
    artifact_grounding._validate_glb(model, {}, checks)

    assert materialized == [77]
    accessors = next(item for item in checks if item["name"] == "glb-accessors")
    assert not accessors["ok"]
    assert "accessor 1 exceeds the total accessor data budget" in accessors["detail"]

    vec4 = tmp_path / "alias-vec4.glb"
    # VEC4 rows cost 4 + 2 = 6 slots: 4 rows are 24 slots, plus 5 for the
    # accessor, so four accessors fit in 120 slots and the fifth is refused.
    _tiny_aliasing_glb(
        vec4, accessor_count=10, items=4, component_type=5126, value_type="VEC4"
    )
    materialized.clear()
    checks = []
    artifact_grounding._validate_glb(vec4, {}, checks)
    assert materialized == [29, 29, 29, 29]
    accessors = next(item for item in checks if item["name"] == "glb-accessors")
    assert "accessor 4 exceeds the total accessor data budget" in accessors["detail"]


def test_glb_default_accessor_budget_bounds_aliased_mat4_float_files(
    monkeypatch, tmp_path
):
    """The reported attack at the default cap: a GLB under 1 MB whose 8,000
    MAT4/5126 accessors of 1,000 rows alias one 64 KB view (8M rows, about
    5.6 GB once unpacked at the measured ~700 bytes per row) was accepted.
    Unpacking is stubbed so the test only counts what the validator would
    have built; the memory per slot is measured by
    test_glb_accessor_slot_cost_matches_measured_memory."""
    model = tmp_path / "alias-mat4-default.glb"
    _tiny_aliasing_glb(
        model, accessor_count=8000, items=1000, component_type=5126, value_type="MAT4"
    )
    assert model.stat().st_size < 1024 * 1024

    def stub(document, binary, index):
        return [(0.0,) * 16]

    materialized = []
    _record_materialized_slots(monkeypatch, materialized, values_for=stub)
    checks = []
    artifact_grounding._validate_glb(model, {}, checks)

    accessors = next(item for item in checks if item["name"] == "glb-accessors")
    assert not accessors["ok"]
    assert "total accessor data budget" in accessors["detail"]
    assert sum(materialized) <= artifact_grounding.MAX_GLB_TOTAL_ACCESSOR_SLOTS


# Runs in a fresh interpreter (-I, so PYTHONMALLOC and friends are ignored)
# whose peak memory is not inflated by earlier tests. The GLB is read and
# parsed before the baseline is taken and _validate_glb is handed that
# result, so the file bytes and the parsed JSON (which the budget does not
# cover) are outside the measurement. Prints the peak private commit
# (Windows) or peak RSS (Linux), sampled when the glb-accessors check is
# recorded (every accessor is cached by then and the later passes have not
# run), minus the current value just before _validate_glb runs, and whether
# that peak was reached during validation rather than earlier.
_GLB_MEMORY_CHILD = r"""
import gc
import json
import sys

sys.path.insert(0, sys.argv[1])
from pathlib import Path

from sonder_runtime.adapters import artifact_grounding


def current_and_peak():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage",
                )
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        query = kernel32.K32GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not query(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError(ctypes.get_last_error(), "K32GetProcessMemoryInfo failed")
        return counters.PagefileUsage, counters.PeakPagefileUsage
    import os
    import resource

    with open("/proc/self/statm") as handle:
        current = int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    return current, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


model = Path(sys.argv[2])
data = artifact_grounding._read_bytes(model)
parsed = artifact_grounding._parse_glb(data)
artifact_grounding._read_bytes = lambda path, *args, **kwargs: data
artifact_grounding._parse_glb = lambda raw: parsed
original_check = artifact_grounding._check
sampled = {}


def sampling_check(checks, name, *args, **kwargs):
    if name == "glb-accessors":
        sampled["peak"] = current_and_peak()[1]
    return original_check(checks, name, *args, **kwargs)


artifact_grounding._check = sampling_check
current_and_peak()
gc.collect()
before, peak_before = current_and_peak()
checks = []
artifact_grounding._validate_glb(model, {}, checks)
accessors = next(item for item in checks if item["name"] == "glb-accessors")
print(json.dumps({
    "delta": sampled["peak"] - before,
    "peak_during_validation": sampled["peak"] > peak_before,
    "ok": accessors["ok"],
    "detail": accessors["detail"],
}))
"""


# 43,691 accessors is the first count at which accessor_cache grows its
# table to 2**17 slots, so the cache costs the most per entry (and both the
# old and new tables are live during the resize).
_GLB_RESIZE_ACCESSORS = 2**16 * 2 // 3 + 1


@pytest.mark.parametrize(
    "component_type,value_type,first_value,rows,accessor_count",
    [
        (5126, "MAT4", 1.5, 1000, 60),
        (5125, "MAT4", 2**31, 1000, 60),
        (5126, "MAT4", 1.5, 1, _GLB_RESIZE_ACCESSORS),
        (5125, "SCALAR", 2**31 + 5, 1, _GLB_RESIZE_ACCESSORS),
    ],
    ids=[
        "mat4-float-1000-rows",
        "mat4-uint32-large-1000-rows",
        "mat4-float-count1",
        "scalar-uint32-large-count1",
    ],
)
def test_glb_accessor_slot_cost_matches_measured_memory(
    tmp_path, component_type, value_type, first_value, rows, accessor_count
):
    """The budget comment claimed a per-slot cost and a ceiling that measured
    memory exceeded: first 32 B per slot, then a 39 B worst case taken from
    1,000-row accessors only, while count=1 accessors pay a list object, an
    item array and a cache entry each and cost 50-100 B per row slot. This
    measures what the validator really holds for the cached accessors, for
    the worst row type (MAT4 of distinct floats, or of uint32 values >= 2**30,
    which are never shared small ints) and for many single-row accessors
    right after a cache resize, and checks it against the per-slot figure
    the cap is derived from, per slot as _validate_glb charges them."""
    if sys.platform != "win32" and not Path("/proc/self/statm").exists():
        pytest.skip("needs Windows process counters or Linux /proc")
    components = {"SCALAR": 1, "MAT4": 16}[value_type]
    code = {5126: "f", 5125: "I"}[component_type]
    values = [first_value + index for index in range(rows * components)]
    binary = struct.pack(f"<{len(values)}{code}", *values)
    accessor = {
        "bufferView": 0,
        "componentType": component_type,
        "type": value_type,
        "count": rows,
    }
    document = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteLength": len(binary)}],
        "accessors": [accessor] * accessor_count,
    }
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    body = struct.pack("<I", len(encoded)) + b"JSON" + encoded
    body += struct.pack("<I", len(binary)) + b"BIN\x00" + binary
    model = tmp_path / "measure.glb"
    model.write_bytes(b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body)

    repo_root = Path(artifact_grounding.__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _GLB_MEMORY_CHILD, str(repo_root), str(model)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    measured = json.loads(completed.stdout.strip().splitlines()[-1])
    assert measured["ok"], measured["detail"]
    # A peak set before validation (by the read/parse) would hide the cost.
    assert measured["peak_during_validation"], measured
    slots = accessor_count * artifact_grounding._glb_accessor_slot_charge(
        rows, value_type
    )
    assert slots <= artifact_grounding.MAX_GLB_TOTAL_ACCESSOR_SLOTS
    per_slot = measured["delta"] / slots
    # A counter that stopped counting would report ~0 and pass the upper
    # bound; real tuples of boxed numbers cannot cost under 20 B/slot.
    assert per_slot >= 20, measured
    assert per_slot <= artifact_grounding.GLB_ACCESSOR_SLOT_BYTES, (per_slot, measured)
    # The ceiling the module comment states for the cached rows of one GLB.
    assert (
        artifact_grounding.MAX_GLB_TOTAL_ACCESSOR_SLOTS
        * artifact_grounding.GLB_ACCESSOR_SLOT_BYTES
        <= 484 * 1000 * 1000
    )


def test_glb_accessor_budget_leaves_invalid_counts_to_the_count_check(
    monkeypatch, tmp_path
):
    """A count above MAX_GLB_ACCESSOR_ITEMS is refused before anything is
    unpacked, so charging it to the budget only replaced the precise
    'invalid count' error with a misleading budget error."""
    model = tmp_path / "over-count.glb"
    _tiny_aliasing_glb(model, accessor_count=1, items=8)
    monkeypatch.setattr(artifact_grounding, "MAX_GLB_ACCESSOR_ITEMS", 4)
    monkeypatch.setattr(artifact_grounding, "MAX_GLB_TOTAL_ACCESSOR_SLOTS", 10)
    checks = []
    artifact_grounding._validate_glb(model, {}, checks)
    accessors = next(item for item in checks if item["name"] == "glb-accessors")
    assert not accessors["ok"]
    assert "accessor 0 has an invalid count" in accessors["detail"]
    assert "budget" not in accessors["detail"]


def test_glb_accessor_budget_does_not_reject_documents_under_it(tmp_path):
    model = tmp_path / "small.glb"
    _tiny_aliasing_glb(model, accessor_count=3, items=8)
    checks = []
    artifact_grounding._validate_glb(model, {}, checks)
    accessors = next(item for item in checks if item["name"] == "glb-accessors")
    assert accessors["ok"], accessors
    mat4 = tmp_path / "small-mat4.glb"
    _tiny_aliasing_glb(
        mat4, accessor_count=3, items=8, component_type=5126, value_type="MAT4"
    )
    checks = []
    artifact_grounding._validate_glb(mat4, {}, checks)
    accessors = next(item for item in checks if item["name"] == "glb-accessors")
    assert accessors["ok"], accessors

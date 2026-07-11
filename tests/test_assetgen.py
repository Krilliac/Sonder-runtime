import hashlib
import json
import os
import struct

import pytest

import assetgen


def _local_root(monkeypatch, tmp_path):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    return tmp_path


def test_generate_pack_emits_valid_stdlib_assets(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)

    pack = assetgen.generate_pack("demo-pack", "3d", "frost", 42)
    root = pack["root"]

    assert open(os.path.join(root, "texture.png"), "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    assert open(os.path.join(root, "hit.wav"), "rb").read(4) == b"RIFF"
    assert open(os.path.join(root, "preview.ppm"), "rb").read(2) == b"P6"
    assert "v " in open(os.path.join(root, "models.obj"), encoding="utf-8").read()
    assert open(os.path.join(root, "rigged.glb"), "rb").read(4) == b"glTF"
    assert open(os.path.join(root, "document.docx"), "rb").read(2) == b"PK"
    assert open(os.path.join(root, "workbook.xlsx"), "rb").read(2) == b"PK"
    assert open(os.path.join(root, "presentation.pptx"), "rb").read(2) == b"PK"
    assert open(os.path.join(root, "animation.gif"), "rb").read(6) == b"GIF89a"
    assert open(os.path.join(root, "preview.avi"), "rb").read(4) == b"RIFF"
    assert open(os.path.join(root, "score.mid"), "rb").read(4) == b"MThd"
    assert open(os.path.join(root, "captions.srt"), "rb").read(1) == b"1"
    assert open(os.path.join(root, "captions.vtt"), "rb").read(6) == b"WEBVTT"
    assert open(os.path.join(root, "timeline.edl"), "rb").read(6) == b"TITLE:"
    assert assetgen.verify_pack(root)["ok"]


def test_free_form_request_infers_general_artifact_kinds(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)

    result = assetgen.generate_artifacts(
        "brand-kit",
        "Create a fiery logo icon, ambient music loop, and 3D mascot model",
    )

    assert result["theme"] == "ember"
    assert result["dimension"] == "3d"
    assert {"icon", "music", "model"}.issubset(result["kinds"])
    assert os.path.isfile(os.path.join(result["root"], "icon.png"))
    assert os.path.isfile(os.path.join(result["root"], "theme.wav"))
    assert os.path.isfile(os.path.join(result["root"], "models.obj"))
    assert os.path.isfile(os.path.join(result["root"], "rigged.glb"))


def test_free_form_request_infers_rigged_animated_model(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)

    result = assetgen.generate_artifacts(
        "rigged-crawler",
        "Create a rigged animated GLB character with a skeleton and skinning",
    )

    assert result["dimension"] == "3d"
    assert "rigged_model" in result["kinds"]
    assert os.path.isfile(os.path.join(result["root"], "rigged.glb"))
    assert result["validation"]["failed_checks"] == 0


def test_theme_inference_uses_terms_not_substrings():
    assert assetgen.infer_request("textured PBR model")["theme"] == "arcane"
    assert assetgen.infer_request("voice interface")["theme"] == "arcane"
    assert assetgen.infer_request("red textured model")["theme"] == "ember"
    assert assetgen.infer_request("ice voice interface")["theme"] == "frost"


def test_morph_language_routes_to_rigged_3d_model():
    request = assetgen.infer_request(
        "Create a character with a morph target, blend shape, and animation clips"
    )

    assert request["dimension"] == "3d"
    assert "rigged_model" in request["kinds"]


def test_humanoid_language_routes_to_rigged_3d_model():
    request = assetgen.infer_request(
        "Create a humanoid biped with idle walk run and facial blend shapes"
    )

    assert request["dimension"] == "3d"
    assert "rigged_model" in request["kinds"]


def test_pack_is_deterministic_for_same_request(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)

    first = assetgen.generate_artifacts("one", "blue ocean background", seed=77)
    second = assetgen.generate_artifacts("two", "blue ocean background", seed=77)
    first_hashes = {row["path"]: row["sha256"] for row in first["files"] if row["path"] != "request.json"}
    second_hashes = {row["path"]: row["sha256"] for row in second["files"] if row["path"] != "request.json"}

    assert first_hashes == second_hashes


def test_verify_detects_tampering(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)
    pack = assetgen.generate_artifacts("sound-pack", "laser sound effect")
    with open(os.path.join(pack["root"], "pickup.wav"), "ab") as handle:
        handle.write(b"tamper")

    verified = assetgen.verify_pack(pack["root"])

    assert not verified["ok"]
    assert any("hash mismatch" in item for item in verified["failures"])


def test_verify_detects_invalid_format_even_when_manifest_hash_matches(
    monkeypatch, tmp_path
):
    _local_root(monkeypatch, tmp_path)
    pack = assetgen.generate_artifacts("icon-pack", "frost icon", kinds="icon")
    icon_path = os.path.join(pack["root"], "icon.png")
    with open(icon_path, "wb") as handle:
        handle.write(b"not actually a PNG")
    manifest = json.loads(open(pack["manifest"], encoding="utf-8").read())
    row = next(item for item in manifest["files"] if item["path"] == "icon.png")
    data = open(icon_path, "rb").read()
    row["bytes"] = len(data)
    row["sha256"] = hashlib.sha256(data).hexdigest()
    with open(pack["manifest"], "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)

    verified = assetgen.verify_pack(pack["root"])

    assert not verified["ok"]
    assert any("format icon.png valid-png" in item for item in verified["failures"])
    assert not any("hash mismatch" in item for item in verified["failures"])


def test_verify_detects_invalid_glb_even_when_manifest_hash_matches(
    monkeypatch, tmp_path
):
    _local_root(monkeypatch, tmp_path)
    pack = assetgen.generate_artifacts(
        "rigged-pack", "rigged animated GLB character", kinds="rigged_model"
    )
    glb_path = os.path.join(pack["root"], "rigged.glb")
    payload = bytearray(open(glb_path, "rb").read())
    json_length = struct.unpack_from("<I", payload, 12)[0]
    document = json.loads(payload[20:20 + json_length].decode("utf-8").rstrip())
    joint_accessor = next(
        row for row in document["accessors"] if row.get("name") == "JOINTS_0"
    )
    view = document["bufferViews"][joint_accessor["bufferView"]]
    binary_start = 20 + json_length + 8
    payload[binary_start + view["byteOffset"]] = 255
    with open(glb_path, "wb") as handle:
        handle.write(payload)
    manifest = json.loads(open(pack["manifest"], encoding="utf-8").read())
    row = next(item for item in manifest["files"] if item["path"] == "rigged.glb")
    row["bytes"] = len(payload)
    row["sha256"] = hashlib.sha256(payload).hexdigest()
    with open(pack["manifest"], "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)

    verified = assetgen.verify_pack(pack["root"])

    assert not verified["ok"]
    assert any(
        "format rigged.glb glb-skinning" in item for item in verified["failures"]
    )
    assert not any("hash mismatch" in item for item in verified["failures"])


def test_verify_detects_corrupt_embedded_glb_texture_with_updated_manifest(
    monkeypatch, tmp_path
):
    _local_root(monkeypatch, tmp_path)
    pack = assetgen.generate_artifacts(
        "textured-rig", "textured PBR rigged GLB character", kinds="rigged_model"
    )
    glb_path = os.path.join(pack["root"], "rigged.glb")
    payload = bytearray(open(glb_path, "rb").read())
    json_length = struct.unpack_from("<I", payload, 12)[0]
    document = json.loads(payload[20:20 + json_length].decode("utf-8").rstrip())
    image = document["images"][0]
    view = document["bufferViews"][image["bufferView"]]
    binary_start = 20 + json_length + 8
    payload[binary_start + view["byteOffset"] + 40] ^= 0x20
    with open(glb_path, "wb") as handle:
        handle.write(payload)
    manifest = json.loads(open(pack["manifest"], encoding="utf-8").read())
    row = next(item for item in manifest["files"] if item["path"] == "rigged.glb")
    row["bytes"] = len(payload)
    row["sha256"] = hashlib.sha256(payload).hexdigest()
    with open(pack["manifest"], "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)

    verified = assetgen.verify_pack(pack["root"])

    assert not verified["ok"]
    assert any(
        "format rigged.glb glb-images" in item for item in verified["failures"]
    )
    assert not any("hash mismatch" in item for item in verified["failures"])


def test_verify_detects_nonfinite_glb_morph_with_updated_manifest(
    monkeypatch, tmp_path
):
    _local_root(monkeypatch, tmp_path)
    pack = assetgen.generate_artifacts(
        "morph-rig", "morph target rigged GLB character", kinds="rigged_model"
    )
    glb_path = os.path.join(pack["root"], "rigged.glb")
    payload = bytearray(open(glb_path, "rb").read())
    json_length = struct.unpack_from("<I", payload, 12)[0]
    document = json.loads(payload[20:20 + json_length].decode("utf-8").rstrip())
    accessor = next(
        item
        for item in document["accessors"]
        if item.get("name") == "MORPH_BREATHE_POSITION"
    )
    view = document["bufferViews"][accessor["bufferView"]]
    binary_start = 20 + json_length + 8
    struct.pack_into("<f", payload, binary_start + view["byteOffset"], float("nan"))
    with open(glb_path, "wb") as handle:
        handle.write(payload)
    manifest = json.loads(open(pack["manifest"], encoding="utf-8").read())
    row = next(item for item in manifest["files"] if item["path"] == "rigged.glb")
    row["bytes"] = len(payload)
    row["sha256"] = hashlib.sha256(payload).hexdigest()
    with open(pack["manifest"], "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)

    verified = assetgen.verify_pack(pack["root"])

    assert not verified["ok"]
    assert any(
        "format rigged.glb glb-accessors" in item for item in verified["failures"]
    )
    assert not any("hash mismatch" in item for item in verified["failures"])


def test_output_rejects_path_escape(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="inside workspace"):
        assetgen.generate_artifacts("escape", "icon", output_dir=str(tmp_path.parent))
    with pytest.raises(ValueError):
        assetgen.generate_artifacts("../escape", "icon")


def test_manifest_is_machine_readable(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)
    pack = assetgen.generate_artifacts("diagram", "architecture diagram preview")

    manifest = json.loads(open(pack["manifest"], encoding="utf-8").read())

    assert manifest["schema"] == 2
    assert manifest["brief"] == "architecture diagram preview"
    assert "preview" in manifest["kinds"]
    assert manifest["validation"]["recipe"] == "bundle"
    assert manifest["validation"]["failed_checks"] == 0


def test_non_game_request_emits_general_open_formats(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)
    pack = assetgen.generate_artifacts(
        "launch-materials",
        "Brand palette, architecture diagram, SVG vector, landing page, editable document, spreadsheet, presentation, and sample data",
    )

    expected = {
        "brief.md", "data.csv", "data.json", "diagram.svg", "document.docx",
        "palette.json", "presentation.pptx", "preview.html", "vector.svg",
        "workbook.xlsx",
    }
    assert expected <= {row["path"] for row in pack["files"]}
    assert "<svg" in open(os.path.join(pack["root"], "diagram.svg"), encoding="utf-8").read()
    html = open(
        os.path.join(pack["root"], "preview.html"), encoding="utf-8"
    ).read().lower()
    assert "<!doctype html>" in html
    assert "<body>" in html and "</body>" in html
    assert assetgen.verify_pack(pack["root"])["ok"]


def test_free_form_request_infers_editable_office_deliverables(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)

    pack = assetgen.generate_artifacts(
        "editable-suite",
        "Create a Word DOCX report, Excel workbook, and PowerPoint slide deck",
    )

    assert {"docx", "spreadsheet", "presentation"} <= set(pack["kinds"])
    paths = {row["path"] for row in pack["files"]}
    assert {"document.docx", "workbook.xlsx", "presentation.pptx"} <= paths
    assert pack["validation"]["failed_checks"] == 0


def test_free_form_request_infers_editable_media_and_timeline(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)

    pack = assetgen.generate_artifacts(
        "media-suite",
        "Create an animated GIF and AVI video, MIDI score, SRT and WebVTT captions, and EDL video timeline",
    )

    assert {"animation", "video", "midi", "captions", "timeline"} <= set(pack["kinds"])
    paths = {row["path"] for row in pack["files"]}
    assert {
        "animation.gif",
        "preview.avi",
        "score.mid",
        "captions.srt",
        "captions.vtt",
        "timeline.edl",
    } <= paths
    assert pack["validation"]["failed_checks"] == 0


def test_timeline_kind_includes_and_references_local_animation(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)

    pack = assetgen.generate_artifacts(
        "timeline-only", "Create an edit timeline", kinds="timeline"
    )

    paths = {row["path"] for row in pack["files"]}
    assert paths == {
        "animation.gif",
        "preview.avi",
        "request.json",
        "timeline.edl",
    }
    timeline = open(
        os.path.join(pack["root"], "timeline.edl"), encoding="utf-8"
    ).read()
    assert "* FROM CLIP NAME: preview.avi" in timeline
    assert assetgen.verify_pack(pack["root"])["ok"]


def test_regeneration_removes_stale_generator_outputs(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)
    first = assetgen.generate_artifacts("reused", "everything", kinds="all")
    assert os.path.isfile(os.path.join(first["root"], "theme.wav"))

    second = assetgen.generate_artifacts("reused", "icon only", kinds="icon")

    paths = {row["path"] for row in second["files"]}
    assert paths == {"icon.png", "request.json"}
    assert not os.path.exists(os.path.join(second["root"], "theme.wav"))

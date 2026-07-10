import json
import os

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


def test_non_game_request_emits_general_open_formats(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)
    pack = assetgen.generate_artifacts(
        "launch-materials",
        "Brand palette, architecture diagram, SVG vector, landing page, document, and sample data",
    )

    expected = {
        "brief.md", "data.csv", "data.json", "diagram.svg", "palette.json",
        "preview.html", "vector.svg",
    }
    assert expected <= {row["path"] for row in pack["files"]}
    assert "<svg" in open(os.path.join(pack["root"], "diagram.svg"), encoding="utf-8").read()
    assert "<!doctype html>" in open(
        os.path.join(pack["root"], "preview.html"), encoding="utf-8"
    ).read().lower()
    assert assetgen.verify_pack(pack["root"])["ok"]


def test_regeneration_removes_stale_generator_outputs(monkeypatch, tmp_path):
    _local_root(monkeypatch, tmp_path)
    first = assetgen.generate_artifacts("reused", "everything", kinds="all")
    assert os.path.isfile(os.path.join(first["root"], "theme.wav"))

    second = assetgen.generate_artifacts("reused", "icon only", kinds="icon")

    paths = {row["path"] for row in second["files"]}
    assert paths == {"icon.png", "request.json"}
    assert not os.path.exists(os.path.join(second["root"], "theme.wav"))

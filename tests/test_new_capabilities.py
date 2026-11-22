"""Unit, integration, and security must-refuse tests for newly added tools and upgrades."""
from __future__ import annotations

import json
import math
from pathlib import Path
import struct
import wave
import zipfile

import pytest

import data_convert
import harness_tools
import project_scaffold
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.inspection import code_metrics, document_extract, media_inspect
from sonder_runtime.bootstrap.native_mcp import native_tool_registry
from sonder_runtime.domain.tools.builtin_traits import builtin_traits


@pytest.fixture
def sandbox_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(file_ops, "allowed_roots", lambda _extra="": [root])
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    return root


def _make_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "docProps/core.xml",
            '<?xml version="1.0"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Design Brief</dc:title><dc:creator>Sonder</dc:creator>"
            "</cp:coreProperties>",
        )
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Overview</w:t></w:r></w:p>'
            "<w:p><w:r><w:t>Hello from DOCX body.</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Weapon</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>Damage</w:t></w:r></w:p></w:tc></w:tr>"
            "<w:tr><w:tc><w:p><w:r><w:t>Railgun</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>120</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>",
        )


def _make_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Stats" sheetId="1"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>Hero</t></si><si><t>HP</t></si><si><t>Vanguard</t></si></sst>",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>250</v></c></row>'
            "</sheetData></worksheet>",
        )


def _make_pptx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "ppt/slides/slide1.xml",
            '<?xml version="1.0"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<a:t>Launch Roadmap</a:t><a:t>Ship Phase 1</a:t></p:sld>",
        )
        zf.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            '<?xml version="1.0"?>'
            '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<a:t>Mention local GPU speedup.</a:t></p:notes>",
        )


def test_document_extract_all_supported_formats(sandbox_root: Path):
    docx_p = sandbox_root / "brief.docx"
    xlsx_p = sandbox_root / "stats.xlsx"
    pptx_p = sandbox_root / "deck.pptx"
    html_p = sandbox_root / "index.html"
    svg_p = sandbox_root / "icon.svg"
    srt_p = sandbox_root / "clip.srt"
    vtt_p = sandbox_root / "clip.vtt"

    _make_docx(docx_p)
    _make_xlsx(xlsx_p)
    _make_pptx(pptx_p)
    html_p.write_text(
        "<html><head><title>Arena</title><script>secret()</script></head>"
        '<body><h1>Scoreboard</h1><p>Active players</p><a href="/play">Play</a></body></html>',
        encoding="utf-8",
    )
    svg_p.write_text(
        '<svg width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64"/>'
        "<text>Badge</text></svg>",
        encoding="utf-8",
    )
    srt_p.write_text(
        "1\n00:00:01,000 --> 00:00:03,500\nWelcome to the arena.\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\nFight!\n",
        encoding="utf-8",
    )
    vtt_p.write_text(
        "WEBVTT\n\n00:00.500 --> 00:02.000\nReady steady.\n",
        encoding="utf-8",
    )

    r_docx = document_extract.extract_document(str(docx_p))
    assert r_docx["format"] == "docx"
    assert r_docx["metadata"]["title"] == "Design Brief"
    assert r_docx["headings"] == ["Overview"]
    assert r_docx["table_count"] == 1
    assert r_docx["tables"][0][1] == ["Railgun", "120"]

    r_xlsx = document_extract.extract_document(str(xlsx_p))
    assert r_xlsx["sheet_count"] == 1
    assert r_xlsx["sheets"][0]["name"] == "Stats"
    assert r_xlsx["sheets"][0]["rows"][1] == ["Vanguard", "250"]

    r_pptx = document_extract.extract_document(str(pptx_p))
    assert r_pptx["slide_count"] == 1
    assert r_pptx["slides"][0]["title"] == "Launch Roadmap"
    assert "local GPU" in r_pptx["slides"][0]["notes"]

    r_html = document_extract.extract_document(str(html_p))
    assert r_html["title"] == "Arena"
    assert "Scoreboard" in r_html["headings"]
    assert "secret()" not in r_html["text"]
    assert r_html["links"] == ["/play"]

    r_svg = document_extract.extract_document(str(svg_p))
    assert r_svg["viewBox"] == "0 0 64 64"
    assert r_svg["element_counts"]["rect"] == 1
    assert "Badge" in r_svg["text"]

    r_srt = document_extract.extract_document(str(srt_p))
    assert r_srt["cue_count"] == 2
    assert r_srt["first_timestamp"] == "00:00:01,000"
    assert "Welcome to the arena." in r_srt["text"]

    r_vtt = document_extract.extract_document(str(vtt_p))
    assert r_vtt["cue_count"] == 1
    assert "Ready steady." in r_vtt["text"]


def test_document_extract_security_must_refuse(sandbox_root: Path, tmp_path: Path):
    # XXE / DOCTYPE refusal
    xxe_svg = sandbox_root / "xxe.svg"
    xxe_svg.write_text('<!DOCTYPE svg [<!ENTITY x "boom">]><svg/>', encoding="utf-8")
    with pytest.raises(document_extract.DocumentExtractError, match="DOCTYPE|ENTITY"):
        document_extract.extract_document(str(xxe_svg))

    # Zip-bomb decompressed ceiling refusal
    bomb = sandbox_root / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", "A" * (document_extract.MAX_DECOMPRESSED_BYTES + 1024))
    with pytest.raises(document_extract.DocumentExtractError, match="safety ceiling"):
        document_extract.extract_document(str(bomb))

    # Outside allowed root refusal
    outside = tmp_path / "outside.html"
    outside.write_text("<p>nope</p>", encoding="utf-8")
    with pytest.raises(document_extract.DocumentExtractError, match="rejected"):
        document_extract.extract_document(str(outside))

    # Sensitive directory refusal
    git_dir = sandbox_root / ".git"
    git_dir.mkdir()
    secret_html = git_dir / "leak.html"
    secret_html.write_text("<p>secret</p>", encoding="utf-8")
    with pytest.raises(document_extract.DocumentExtractError):
        document_extract.extract_document(str(secret_html))


def test_media_inspect_wav_midi_avi_glb_obj_stl(sandbox_root: Path):
    # WAV (440 Hz tone)
    wav_p = sandbox_root / "tone.wav"
    with wave.open(str(wav_p), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        samples = [int(16000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(800)]
        wf.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    r_wav = media_inspect.inspect_media(str(wav_p))
    assert r_wav["format"] == "wav"
    assert r_wav["sample_rate_hz"] == 8000
    assert r_wav["duration_seconds"] == pytest.approx(0.1, abs=0.01)
    assert not r_wav["silent"]
    assert not r_wav["clipped"]

    # MIDI
    mid_p = sandbox_root / "tune.mid"
    trk = bytes([0x00, 0x90, 60, 100, 0x40, 0x80, 60, 0, 0x00, 0xFF, 0x2F, 0x00])
    mid_p.write_bytes(
        b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
        + b"MTrk" + struct.pack(">I", len(trk)) + trk
    )
    r_mid = media_inspect.inspect_media(str(mid_p))
    assert r_mid["format"] == "midi"
    assert r_mid["parsed_tracks"] == 1
    assert r_mid["ticks_per_quarter"] == 480
    assert r_mid["note_on_count"] == 1

    # Minimal AVI header
    avi_p = sandbox_root / "clip.avi"
    avih_body = struct.pack("<14I", 40000, 0, 0, 0, 50, 0, 1, 0, 320, 180, 0, 0, 0, 0)
    avi_body = b"AVI " + b"avih" + struct.pack("<I", len(avih_body)) + avih_body + b"strh" + struct.pack("<I", 4) + b"vids"
    avi_p.write_bytes(b"RIFF" + struct.pack("<I", len(avi_body)) + avi_body)
    r_avi = media_inspect.inspect_media(str(avi_p))
    assert r_avi["width"] == 320 and r_avi["height"] == 180
    assert r_avi["fps"] == 25.0 and r_avi["duration_seconds"] == 2.0
    assert r_avi["stream_types"] == ["vids"]

    # GLB
    glb_p = sandbox_root / "prop.glb"
    gltf_doc = {
        "asset": {"version": "2.0", "generator": "sonder-test"},
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "nodes": [{"mesh": 0}],
        "skins": [{"joints": [0]}],
        "animations": [{"name": "Idle"}],
        "accessors": [
            {"count": 3, "min": [0, 0, 0], "max": [1, 2, 3]},
            {"count": 3},
        ],
    }
    jb = json.dumps(gltf_doc).encode("utf-8")
    jb += b" " * ((4 - len(jb) % 4) % 4)
    glb_p.write_bytes(
        b"glTF" + struct.pack("<II", 2, 12 + 8 + len(jb))
        + struct.pack("<II", len(jb), 0x4E4F534A) + jb
    )
    r_glb = media_inspect.inspect_media(str(glb_p))
    assert r_glb["gltf_version"] == "2.0"
    assert r_glb["vertex_count"] == 3 and r_glb["triangle_count"] == 1
    assert r_glb["joint_count"] == 1 and r_glb["animation_names"] == ["Idle"]
    assert r_glb["bbox_max"] == [1, 2, 3]

    # OBJ
    obj_p = sandbox_root / "tri.obj"
    obj_p.write_text("o Tri\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl Steel\nf 1 2 3\n", encoding="utf-8")
    r_obj = media_inspect.inspect_media(str(obj_p))
    assert r_obj["vertex_count"] == 3 and r_obj["triangle_count"] == 1
    assert r_obj["materials"] == ["Steel"]

    # Binary STL (1 triangle)
    stl_p = sandbox_root / "tri.stl"
    tri_rec = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 3, 0, 0)
    stl_p.write_bytes(b"sonder-stl".ljust(80, b"\x00") + struct.pack("<I", 1) + tri_rec)
    r_stl = media_inspect.inspect_media(str(stl_p))
    assert r_stl["encoding"] == "binary" and r_stl["triangle_count"] == 1
    assert r_stl["bbox_max"] == [2.0, 3.0, 0.0]


def test_code_metrics_loc_markers_and_ast_hotspots(sandbox_root: Path):
    py_file = sandbox_root / "engine.py"
    py_file.write_text(
        "# top comment\n"
        "def complex_fn(x):\n"
        "    # TODO: refactor branch chain\n"
        "    if x == 1: return 1\n"
        "    if x == 2: return 2\n"
        "    if x == 3: return 3\n"
        "    if x == 4: return 4\n"
        "    if x == 5: return 5\n"
        "    if x == 6: return 6\n"
        "    if x == 7: return 7\n"
        "    if x == 8: return 8\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (sandbox_root / ".git").mkdir()
    (sandbox_root / ".git" / "ignored.py").write_text("# FIXME: hidden\nx = 1\n", encoding="utf-8")

    res = code_metrics.analyze_code_metrics(str(sandbox_root))
    assert res["totals"]["files"] == 1
    assert res["languages"]["python"]["files"] == 1
    assert res["marker_counts"]["TODO"] == 1
    assert res["marker_counts"]["FIXME"] == 0
    assert any(h["symbol"] == "complex_fn" and h["complexity"] >= 9 for h in res["complexity_hotspots"])


def test_data_convert_markdown_and_toml(sandbox_root: Path):
    toml_in = sandbox_root / "weapons.toml"
    toml_in.write_text(
        '[[weapons]]\nname = "Railgun"\ndamage = 120\n\n'
        '[[weapons]]\nname = "Plasma"\ndamage = 85\n',
        encoding="utf-8",
    )
    md_out = sandbox_root / "weapons.md"
    rep1 = data_convert.convert_data(str(toml_in), str(md_out), ["name", "damage"], apply=True)
    assert rep1["applied"] is True and rep1["rows"] == 2
    md_text = md_out.read_text(encoding="utf-8")
    assert "| name | damage |" in md_text and "| Railgun | 120 |" in md_text

    json_out = sandbox_root / "weapons_roundtrip.json"
    rep2 = data_convert.convert_data(str(md_out), str(json_out), ["name", "damage"], apply=True)
    assert rep2["applied"] is True and rep2["rows"] == 2
    rows = json.loads(json_out.read_text(encoding="utf-8"))
    assert rows[0] == {"name": "Railgun", "damage": "120"}

    # TOML is read-only; output_format="toml" must refuse cleanly
    with pytest.raises(data_convert.DataConvertError, match="output_format"):
        data_convert.convert_data(
            str(json_out), str(sandbox_root / "bad.toml"), ["name"], output_format="toml", apply=False,
        )


def test_harness_build_and_lint_new_ecosystems(sandbox_root: Path, monkeypatch: pytest.MonkeyPatch):
    cs_dir = sandbox_root / "cs_proj"
    cs_dir.mkdir()
    (cs_dir / "Demo.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"/>", encoding="utf-8")
    calls = []
    monkeypatch.setattr(harness_tools, "_run", lambda cmd, cwd, timeout=120: (calls.append(cmd) or {"ok": True, "command": cmd}))
    res = harness_tools.build_run(str(cs_dir))
    assert res["ok"] and calls[-1] == ["dotnet", "build", "Demo.csproj"]
    res_clean = harness_tools.build_clean(str(cs_dir))
    assert res_clean["ok"] and calls[-1] == ["dotnet", "clean", "Demo.csproj"]

    meson_dir = sandbox_root / "meson_proj"
    meson_dir.mkdir()
    (meson_dir / "meson.build").write_text("project('demo', 'c')", encoding="utf-8")
    harness_tools.build_run(str(meson_dir))
    assert calls[-1] == ["meson", "compile", "-C", "build"]


def test_project_scaffold_web_app_and_dart():
    assert "web-app" in project_scaffold.kinds()
    assert "dart" in project_scaffold.kinds()
    assert project_scaffold.normalize_kind("webapp") == "web-app"
    web_files = project_scaffold.render("web-app", "ArenaHud")
    assert set(web_files) == {"index.html", "styles.css", "app.js"}
    assert "<title>ArenaHud</title>" in web_files["index.html"]
    dart_files = project_scaffold.render("dart", "ArenaTool")
    assert "pubspec.yaml" in dart_files and "lib/arenatool.dart" in dart_files


def test_new_tools_registered_with_read_only_traits_and_mcp_schema():
    registry = native_tool_registry()
    for name in ("document_extract", "media_inspect", "code_metrics"):
        traits = builtin_traits(name)
        assert traits.is_read_only
        assert not traits.may_be_destructive
        assert traits.can_parallelize
        desc = registry.get(name)
        assert desc is not None
        assert desc.traits.is_read_only

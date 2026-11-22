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


def test_polyglot_code_runner_languages_and_sql_execution():
    from sonder_runtime.adapters.execution_tools import code_runner

    for lang in (
        "c", "dart", "zig", "kotlin", "swift", "elixir",
        "scala", "haskell", "julia", "nim", "sql", "batch",
    ):
        assert lang in code_runner.SUPPORTED_LANGUAGES

    sql_res = code_runner.run_code(
        "CREATE TABLE scores(player TEXT, pts INT);\n"
        "INSERT INTO scores VALUES ('Nova', 42), ('Vex', 99);\n"
        "SELECT player, pts FROM scores ORDER BY pts DESC;\n",
        language="sql",
    )
    assert sql_res["ok"] is True
    assert "Vex | 99" in sql_res["stdout"]
    assert "Nova | 42" in sql_res["stdout"]

    with pytest.raises(ValueError, match="run limits are not supported"):
        code_runner.run_code("int main(){return 0;}", language="c", limits=code_runner.RunLimits())


def test_symbol_index_broad_language_support(sandbox_root: Path):
    import symbol_index

    samples = {
        "Main.java": ("public class ArenaService {\n    public int score(int x) { return x; }\n}\n", {"ArenaService", "score"}),
        "App.kt": ("data class Hero(val name: String)\nfun greet(x: String) = x\n", {"Hero", "greet"}),
        "game.dart": ("class MatchState {}\nvoid tick(int dt) {}\n", {"MatchState", "tick"}),
        "App.swift": ("struct Vector3 {}\nfunc step() {}\n", {"Vector3", "step"}),
        "main.zig": ("pub const Vec2 = struct { x: f32 };\npub fn main() void {}\n", {"Vec2", "main"}),
        "lib.rb": ("module Arena\n  class Bot\n    def tick; end\n  end\nend\n", {"Arena", "Bot", "tick"}),
        "App.php": ("<?php\nnamespace App;\nclass Engine {\n  public function run() {}\n}\n", {"App", "Engine", "run"}),
        "ai.lua": ("local function patrol(dt) end\nfunction M.attack() end\n", {"patrol", "M.attack"}),
        "arena.ex": ("defmodule Arena do\n  def start(x), do: x\nend\n", {"Arena", "start"}),
        "Main.scala": ("object Arena {\n  def run(): Unit = ()\n}\n", {"Arena", "run"}),
        "Core.hs": ("module Core where\ndata State = Ready\nstep :: State -> State\n", {"Core", "State", "step"}),
        "helper.ps1": ("class Runner {}\nfunction Invoke-Arena { param($x) }\n", {"Runner", "Invoke-Arena"}),
        "cli.sh": ("#!/usr/bin/env bash\nrun_match() {\n  echo ok\n}\n", {"run_match"}),
        "schema.sql": ("CREATE TABLE matches (id INT);\nCREATE VIEW top_scores AS SELECT 1;\n", {"matches", "top_scores"}),
    }
    for filename, (content, expected) in samples.items():
        fpath = sandbox_root / filename
        fpath.write_text(content, encoding="utf-8")
        res = symbol_index.index_repository(str(fpath))
        found = {s["name"] for s in res["symbols"]}
        assert expected <= found, f"{filename}: expected {expected}, got {found}"


def test_project_scaffold_all_22_templates():
    expected_new = {
        "c-cmake", "zig", "kotlin-gradle", "swift", "elixir",
        "ruby", "php", "lua", "flutter", "powershell", "bash-cli",
    }
    assert expected_new <= set(project_scaffold.kinds())
    assert len(project_scaffold.kinds()) >= 22
    for kind in expected_new:
        files = project_scaffold.render(kind, "ArenaKit")
        assert files, f"empty scaffold for {kind}"
        assert all("@NAME@" not in content and "@LOWER@" not in content for content in files.values())


def test_project_detect_and_harness_broad_ecosystems(sandbox_root: Path, monkeypatch: pytest.MonkeyPatch):
    from sonder_runtime.adapters.inspection import project_detect

    zig_dir = sandbox_root / "zig_app"
    zig_dir.mkdir()
    (zig_dir / "build.zig").write_text("pub fn build(b: anytype) void {}\n", encoding="utf-8")
    det_zig = project_detect.detect_project(str(zig_dir))
    assert any(lang["name"] == "Zig" for lang in det_zig["languages"])

    ex_dir = sandbox_root / "ex_app"
    ex_dir.mkdir()
    (ex_dir / "mix.exs").write_text("defmodule Demo.MixProject do end\n", encoding="utf-8")
    det_ex = project_detect.detect_project(str(ex_dir))
    assert any(lang["name"] == "Elixir" for lang in det_ex["languages"])

    ue_dir = sandbox_root / "ue_game"
    ue_dir.mkdir()
    (ue_dir / "Arena.uproject").write_text('{"FileVersion": 3}', encoding="utf-8")
    det_ue = project_detect.detect_project(str(ue_dir))
    assert any(f["name"] == "Unreal Engine" for f in det_ue["frameworks"])

    calls = []
    monkeypatch.setattr(harness_tools, "_run", lambda cmd, cwd, timeout=120: (calls.append(cmd) or {"ok": True, "command": cmd}))
    harness_tools.build_run(str(zig_dir))
    assert calls[-1] == ["zig", "build"]
    harness_tools.build_run(str(ex_dir))
    assert calls[-1] == ["mix", "compile"]
    assert harness_tools._detect_test_framework(zig_dir) == "zig"
    assert harness_tools._detect_test_framework(ex_dir) == "elixir"
    assert harness_tools._detect_package_manager(ex_dir) == "mix"


def test_schema_validate_json_jsonl_toml_and_security_must_refuse(sandbox_root: Path, tmp_path: Path):
    from sonder_runtime.adapters.inspection import schema_validate

    schema_p = sandbox_root / "weapon.schema.json"
    schema_p.write_text(json.dumps({
        "$defs": {
            "weapon": {
                "type": "object",
                "required": ["name", "damage"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 2},
                    "damage": {"type": "integer", "minimum": 1, "maximum": 500},
                },
            }
        },
        "$ref": "#/$defs/weapon",
    }), encoding="utf-8")

    good = sandbox_root / "good.json"
    good.write_text(json.dumps({"name": "Railgun", "damage": 120}), encoding="utf-8")
    r_ok = schema_validate.validate_schema(str(good), schema_path=str(schema_p))
    assert r_ok["valid"] is True and r_ok["error_count"] == 0

    bad = sandbox_root / "bad.json"
    bad.write_text(json.dumps({"name": "X", "damage": 999, "extra": True}), encoding="utf-8")
    r_bad = schema_validate.validate_schema(str(bad), schema_path=str(schema_p))
    assert r_bad["valid"] is False and r_bad["error_count"] == 3

    toml_good = sandbox_root / "good.toml"
    toml_good.write_text('name = "Plasma"\ndamage = 85\n', encoding="utf-8")
    r_toml = schema_validate.validate_schema(str(toml_good), schema_path=str(schema_p))
    assert r_toml["valid"] is True and r_toml["data_format"] == "toml"

    jsonl_p = sandbox_root / "items.jsonl"
    jsonl_p.write_text('{"name":"Railgun","damage":120}\n{"name":"Bad","damage":-5}\n', encoding="utf-8")
    r_jsonl = schema_validate.validate_schema(str(jsonl_p), schema_path=str(schema_p))
    assert r_jsonl["valid"] is False and r_jsonl["records_checked"] == 2
    assert r_jsonl["errors"][0]["path"] == "$[1].damage"

    # Security must-refuse: external $ref, cyclic $ref, out-of-root path
    with pytest.raises(schema_validate.SchemaValidateError, match="local fragment"):
        schema_validate.validate_schema(
            str(good), schema_json=json.dumps({"$ref": "https://example.com/evil.json"}),
        )
    with pytest.raises(schema_validate.SchemaValidateError, match="cyclic"):
        schema_validate.validate_schema(
            str(good), schema_json=json.dumps({"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"}),
        )
    outside = tmp_path / "escape.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(schema_validate.SchemaValidateError, match="rejected"):
        schema_validate.validate_schema(str(outside), schema_path=str(schema_p))


def test_language_matrix_and_new_tool_traits():
    from sonder_runtime.adapters.inspection import language_matrix

    full = language_matrix.inspect_language_matrix()
    assert full["ok"] is True
    assert full["language_count"] >= 26
    assert full["totals"]["run_code_languages"] >= 26
    assert full["totals"]["scaffold_templates"] >= 22
    assert full["totals"]["symbol_index_languages"] >= 22
    filtered = language_matrix.inspect_language_matrix("rust")
    assert filtered["language_count"] == 1 and filtered["languages"][0]["language"] == "rust"

    registry = native_tool_registry()
    for name in ("schema_validate", "language_matrix"):
        assert builtin_traits(name).is_read_only
        assert registry.get(name).traits.is_read_only

"""Saved model scripts ported to the recorded dialect: they record, and (with Blender) build.

The originals ran as real Python inside Blender with an injected ``lf`` object.  The ports differ only where
the dialect cannot express the original: a qualified ``lf_kit.`` module name, handles instead of reading
``part.name`` or ``lf.last_obj()``, and ``fit_height(h, parts)`` with the parts named explicitly.
Skipped unless the Lanternfall ``lf_kit.py`` is on this machine.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sonder_runtime.adapters.meshgen import blender_runner
from sonder_runtime.domain.meshgen.dsl import opschema
from sonder_runtime.domain.meshgen.dsl.manifest import build_manifest
from sonder_runtime.domain.meshgen.dsl.recorder import compile_script

REAL_KIT = Path(r"D:\LanternfallGame\Lanternfall\tools\blender\lf_kit.py")
PORTS = Path(__file__).parent / "data" / "meshgen_ports"
BLENDER = os.environ.get("SONDER_BLENDER", "")
SCRIPTS = sorted(PORTS.glob("*.py"))

pytestmark = pytest.mark.skipif(not REAL_KIT.is_file(), reason="the Lanternfall lf_kit.py is not on this machine")


@pytest.fixture(scope="module")
def manifest():
    return build_manifest(REAL_KIT.read_text(encoding="utf-8"), "lf_kit")


def test_there_are_ported_scripts():
    assert len(SCRIPTS) >= 4


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_ported_script_records(script, manifest):
    recording = compile_script(script.read_text(encoding="utf-8"), manifest)
    opschema.validate_ops(json.loads(recording.text), manifest.exposed)
    names = [op["op"] for op in recording.ops]
    assert len(names) > 10 and all(name.startswith("kit.") for name in names)
    assert names[-1] == "kit.fit_height"
    assert compile_script(script.read_text(encoding="utf-8"), manifest).text == recording.text  # deterministic


@pytest.mark.skipif(not BLENDER or not os.path.isfile(BLENDER), reason="SONDER_BLENDER is not set")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_ported_script_builds_through_the_default_runner(script, manifest, tmp_path):
    recording = compile_script(script.read_text(encoding="utf-8"), manifest)
    ops = tmp_path / "ops.json"
    ops.write_text(recording.text, encoding="utf-8")
    result = blender_runner.build_candidate(
        BLENDER, str(ops), str(tmp_path / "out"), name="ported", kit_path=str(REAL_KIT),
        kit_allowed=sorted(manifest.exposed), max_tris=20000, height_m=0, timeout=240)
    assert result["ok"] is True, result
    assert result["sandbox"] == ("appcontainer" if os.name == "nt" else "unsandboxed")
    assert result["tris"] > 200
    assert Path(result["files"]["model"]).read_bytes()[:4] == b"glTF"
    assert Path(result["files"]["sheet"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

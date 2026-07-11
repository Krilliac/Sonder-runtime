import os
import subprocess
import sys

import assetgen
import code_runner
import game_forge


def _local_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(game_forge, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(tmp_path))


def test_in_house_validation_rejects_third_party_engines():
    assert game_forge.validate_in_house("import pygame", "python") == ["pygame"]
    assert game_forge.validate_in_house("#include <SDL.h>", "cpp") == ["<sdl"]
    assert game_forge.validate_in_house(
        "#include <nlohmann/json.hpp>\nnlohmann::json value;", "cpp"
    ) == ["nlohmann"]
    assert game_forge.validate_in_house("const fs = require('fs')", "javascript") == []


def test_game_contract_rejects_placeholders_and_missing_outputs():
    issues = game_forge.contract_issues("// placeholder\nprint('GAME_OK')", "python")

    assert any("frame.ppm" in issue for issue in issues)
    assert any("unfinished" in issue for issue in issues)


def test_game_contract_rejects_cwd_only_asset_roots():
    code = """
import pathlib
root = pathlib.Path.cwd()
open(root / 'assets' / 'tiles.png', 'rb')
open(root / 'assets' / 'hit.wav', 'rb')
open(root / 'frame.ppm', 'wb')
print('GAME_OK')
"""

    issues = game_forge.contract_issues(code, "python")

    assert any("script/executable" in issue for issue in issues)


def test_cpp_autofix_adds_only_required_standard_headers():
    fixed = game_forge.autofix_standard_library(
        "#include <vector>\nstd::vector<uint32_t> pixels;", "cpp"
    )

    assert fixed.startswith("#include <cstdint>")


def test_generation_prompt_requires_assets_frame_and_bounded_exit(monkeypatch, tmp_path):
    _local_roots(monkeypatch, tmp_path)
    project = game_forge.prepare_project("prompt-demo", "python", "2d")

    prompt = game_forge.generation_prompt(project, "arena combat")

    assert "no third-party" in prompt.lower()
    assert "frame.ppm" in prompt
    assert "GAME_OK" in prompt
    assert "assets/manifest.json" in prompt


def test_cpp_generation_prompt_explicitly_forbids_json_helper(monkeypatch, tmp_path):
    _local_roots(monkeypatch, tmp_path)
    project = game_forge.prepare_project("cpp-prompt", "cpp", "2.5d")

    prompt = game_forge.generation_prompt(project, "isometric dungeon")

    assert "nlohmann/json" in prompt
    assert "do not include a JSON helper" in prompt


def test_reference_python_game_runs_end_to_end(monkeypatch, tmp_path):
    _local_roots(monkeypatch, tmp_path)

    result = game_forge.run_reference("smoke", "python", "2d", timeout=20)

    assert result["ok"]
    assert "GAME_OK" in result["output"]
    assert os.path.getsize(result["frame"]) > 1024


def test_reference_python_game_runs_from_unrelated_working_directory(
    monkeypatch, tmp_path,
):
    _local_roots(monkeypatch, tmp_path)
    project = game_forge.prepare_project("portable", "python", "2d")
    game_forge.save_source(project, game_forge.reference_source("python", "2d"))
    foreign = tmp_path / "foreign-cwd"
    foreign.mkdir()

    completed = subprocess.run(
        [sys.executable, project["source"]],
        cwd=foreign,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0
    assert "GAME_OK" in completed.stdout
    assert os.path.getsize(project["frame"]) > 1024
    assert not (foreign / "frame.ppm").exists()


def test_verified_reference_matrix_satisfies_model_contract():
    for language, dimension in game_forge.SUPPORTED_MATRIX:
        source = game_forge.reference_source(language, dimension)
        assert game_forge.validate_in_house(source, language) == []
        assert game_forge.contract_issues(source, language) == []


def test_cpp_isometric_reference_has_requested_dimension_and_portable_root():
    source = game_forge.reference_source("cpp", "2.5d")

    assert "dimension=2.5d" in source
    assert "argv" in source
    assert "executable" in source
    assert "enemies" in source
    assert "diamond" in source

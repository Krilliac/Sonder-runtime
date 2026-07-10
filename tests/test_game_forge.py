import os

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
    assert game_forge.validate_in_house("const fs = require('fs')", "javascript") == []


def test_game_contract_rejects_placeholders_and_missing_outputs():
    issues = game_forge.contract_issues("// placeholder\nprint('GAME_OK')", "python")

    assert any("frame.ppm" in issue for issue in issues)
    assert any("unfinished" in issue for issue in issues)


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


def test_reference_python_game_runs_end_to_end(monkeypatch, tmp_path):
    _local_roots(monkeypatch, tmp_path)

    result = game_forge.run_reference("smoke", "python", "2d", timeout=20)

    assert result["ok"]
    assert "GAME_OK" in result["output"]
    assert os.path.getsize(result["frame"]) > 1024


def test_verified_reference_matrix_satisfies_model_contract():
    for language, dimension in game_forge.DEFAULT_MATRIX:
        source = game_forge.reference_source(language, dimension)
        assert game_forge.validate_in_house(source, language) == []
        assert game_forge.contract_issues(source, language) == []

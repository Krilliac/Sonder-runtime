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


def test_in_house_validation_ignores_forbidden_tokens_in_comments():
    code = (
        "// note: nlohmann and Boost are forbidden here\n"
        "/* no <glm/vec3.hpp> either */\n"
        "#include <fstream>\nint main() { return 0; }\n"
    )
    assert game_forge.validate_in_house(code, "cpp") == []
    assert game_forge.validate_in_house("# no pygame allowed\nimport json", "python") == []
    # real includes are still caught
    assert game_forge.validate_in_house(
        "#include <nlohmann/json.hpp>\nnlohmann::json value;", "cpp"
    ) == ["nlohmann"]


def test_forbidden_remediation_is_actionable():
    note = game_forge.forbidden_remediation(["nlohmann"], "cpp")

    assert "nlohmann" in note
    assert "<fstream>" in note
    assert "standard" in note.lower()


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


def test_cpp_autofix_adds_cassert_for_bare_assert():
    fixed = game_forge.autofix_standard_library(
        "#include <fstream>\nint main() { assert(1 == 1); return 0; }", "cpp"
    )

    assert "#include <cassert>" in fixed
    # idempotent: never double-includes
    assert game_forge.autofix_standard_library(fixed, "cpp") == fixed


def test_cpp_autofix_adds_common_stdlib_headers():
    code = (
        "int main() {\n"
        "  char buf[8]; memcpy(buf, \"hi\", 3);\n"
        "  double d = std::sqrt(2.0);\n"
        "  std::string name = std::to_string(d);\n"
        "  std::map<int, int> scores;\n"
        "  int top = std::max(1, 2);\n"
        "  return top;\n"
        "}"
    )

    fixed = game_forge.autofix_standard_library(code, "cpp")

    for header in ("<cstring>", "<cmath>", "<string>", "<map>", "<algorithm>"):
        assert "#include %s" % header in fixed


def test_cpp_autofix_ignores_static_assert_and_present_headers():
    code = "#include <cassert>\nstatic_assert(sizeof(int) >= 4, \"int\");\nint main(){return 0;}"

    assert game_forge.autofix_standard_library(code, "cpp") == code


def test_run_project_recovers_from_missing_header_compile_error(monkeypatch, tmp_path):
    _local_roots(monkeypatch, tmp_path)
    project = game_forge.prepare_project("headerfix", "cpp", "2.5d")
    with open(project["frame"], "wb") as handle:
        handle.write(b"P6\n8 8\n255\n" + b"\x00" * 2048)
    calls = []

    def fake_run_code(code, language=None, timeout=None, cwd=None):
        calls.append(code)
        if "#include <cassert>" not in code:
            return {
                "ok": False,
                "stdout": "",
                "stderr": "game.cpp:3:5: error: 'assert' was not declared in this scope",
            }
        return {"ok": True, "stdout": "GAME_OK language=cpp dimension=2.5d", "stderr": ""}

    monkeypatch.setattr(code_runner, "run_code", fake_run_code)
    # bare assert with no <cassert>: the compiler error drives the recovery
    code = "int main() { assert(1); return 0; }"

    run = game_forge.run_project(project, code, timeout=5)

    assert run["ok"]
    assert run.get("header_autofix") is True
    assert len(calls) == 2
    assert calls[1].startswith("#include <cassert>")


def test_run_project_distills_compile_errors_for_repair_note(monkeypatch, tmp_path):
    _local_roots(monkeypatch, tmp_path)
    project = game_forge.prepare_project("distill", "cpp", "2.5d")

    def fake_run_code(code, language=None, timeout=None, cwd=None):
        return {
            "ok": False,
            "stdout": "",
            "stderr": (
                "game.cpp: In function 'int main()':\n"
                "game.cpp:2:1: warning: unused variable 'x' [-Wunused-variable]\n"
                "game.cpp:9:3: error: expected ';' before 'return'\n"
                "compilation terminated.\n"
            ),
        }

    monkeypatch.setattr(code_runner, "run_code", fake_run_code)

    run = game_forge.run_project(project, "int main() { return 0; }", timeout=5)

    assert not run["ok"]
    assert "error: expected ';'" in run["output"]
    assert "warning" not in run["output"]
    assert "In function" not in run["output"]


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

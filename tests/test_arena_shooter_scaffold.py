"""Regression coverage for the deterministic FPS example project scaffold."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_SCAFFOLD = (
    Path(__file__).resolve().parents[1]
    / "examples" / "codegen-arena-shooter" / "project_scaffold.py"
)
_SPEC = importlib.util.spec_from_file_location("arena_project_scaffold", _SCAFFOLD)
assert _SPEC is not None and _SPEC.loader is not None
scaffold = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(scaffold)


def test_arena_skeleton_scaffold_creates_verifier_compatible_project_once(tmp_path):
    path = scaffold.ensure_project_file(tmp_path / "FpsGame_Skeleton")

    assert path.name == "FpsGame_Skeleton.csproj"
    content = path.read_text(encoding="utf-8")
    assert "<TargetFramework>net10.0</TargetFramework>" in content
    assert "<AssemblyName>FpsGameSonder</AssemblyName>" in content
    assert 'PackageReference Include="Raylib-cs" Version="8.0.0"' in content

    path.write_text("operator-owned manifest\n", encoding="utf-8")
    assert scaffold.ensure_project_file(tmp_path / "FpsGame_Skeleton") == path
    assert path.read_text(encoding="utf-8") == "operator-owned manifest\n"


def test_arena_builder_imports_its_own_scaffold_before_runtime_module():
    builder = (
        Path(__file__).resolve().parents[1]
        / "examples" / "codegen-arena-shooter" / "build_skeleton.py"
    ).read_text(encoding="utf-8")
    assert '"arena_shooter_project_scaffold"' in builder
    assert "ensure_project_file = _SCAFFOLD_MODULE.ensure_project_file" in builder


def test_arena_verifier_loads_the_copied_generated_artifact_explicitly():
    verifier = (
        Path(__file__).resolve().parents[1]
        / "examples" / "codegen-arena-shooter" / "Verify" / "Program.cs"
    ).read_text(encoding="utf-8")
    assert 'Path.Combine(AppContext.BaseDirectory, "FpsGameSonder.dll")' in verifier
    assert "Assembly.LoadFrom(target)" in verifier

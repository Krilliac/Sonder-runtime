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

_BUILDER = (
    Path(__file__).resolve().parents[1]
    / "examples" / "codegen-arena-shooter" / "build_skeleton.py"
)
_BUILDER_SPEC = importlib.util.spec_from_file_location("arena_skeleton_builder", _BUILDER)
assert _BUILDER_SPEC is not None and _BUILDER_SPEC.loader is not None


def _builder_module():
    """Load the utility without requiring its runtime-only imports in tests."""
    source = _BUILDER.read_text(encoding="utf-8")
    start = source.index("FENCE =")
    end = source.index("\ndef main()")
    namespace: dict[str, object] = {"re": __import__("re")}
    exec(source[start:end], namespace)
    return namespace


def _builder_error_preview():
    source = _BUILDER.read_text(encoding="utf-8")
    start = source.index("def compiler_error_preview")
    end = source.index("\ndef strip_fences", start)
    namespace: dict[str, object] = {}
    exec(source[start:end], namespace)
    return namespace["compiler_error_preview"]


def _builder_build_errors():
    source = _BUILDER.read_text(encoding="utf-8")
    start = source.index("def build_errors")
    end = source.index("\ndef compiler_error_preview", start)
    namespace: dict[str, object] = {
        "PROJECT": ".",
        "subprocess": __import__("subprocess"),
        "codegen_loop": type("Loop", (), {"build_ran": staticmethod(lambda _out: True)}),
    }
    exec(source[start:end], namespace)
    return namespace["build_errors"], namespace


def _builder_verifier():
    source = _BUILDER.read_text(encoding="utf-8")
    start = source.index("def run_verifier")
    end = source.index("\ndef strip_fences", start)
    namespace: dict[str, object] = {
        "os": __import__("os"),
        "subprocess": __import__("subprocess"),
        "HERE": "C:/example",
    }
    exec(source[start:end], namespace)
    return namespace["run_verifier"], namespace


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


def test_arena_skeleton_initializes_deferred_lifecycle_fields_without_warnings():
    skeleton = (
        Path(__file__).resolve().parents[1]
        / "examples" / "codegen-arena-shooter" / "skeleton.py"
    ).read_text(encoding="utf-8")

    assert "public string Name = string.Empty;" in skeleton
    assert "private UdpClient _socket = null!;" in skeleton
    assert "private IPEndPoint _remote = null!;" in skeleton
    assert "private static Combatant _me = null!;" in skeleton
    assert "_typingAddress" not in skeleton


def test_arena_verifier_loads_the_copied_generated_artifact_explicitly():
    verifier = (
        Path(__file__).resolve().parents[1]
        / "examples" / "codegen-arena-shooter" / "Verify" / "Program.cs"
    ).read_text(encoding="utf-8")
    assert 'Path.Combine(AppContext.BaseDirectory, "FpsGameSonder.dll")' in verifier
    assert "Assembly.LoadFrom(target)" in verifier


def test_arena_builder_rejects_known_runtime_lifecycle_contract_violations():
    builder = _builder_module()
    issues = builder["body_contract_issues"](
        "Program.cs", "StartMatch", "_match = new MatchState();"
    )
    assert "must contain _match.AddLocalPlayer(" in issues
    assert builder["body_contract_issues"](
        "Program.cs", "DoLobby", "Raylib.BeginDrawing();"
    ) == ["must not contain Raylib.BeginDrawing"]
    assert builder["body_contract_issues"](
        "Program.cs", "DoScoreboard", "Ui.Scoreboard(W, H, _match, out bool back);"
    ) == []
    assert builder["body_contract_issues"](
        "GameMap.cs", "IsWallAt", "return !_walls[0, 0];"
    ) == [
        "must contain MathF.Floor",
        "must contain CellSize",
        "must contain IsWallCell(",
    ]
    assert builder["body_contract_issues"](
        "GameMap.cs", "IsWallAt", "return IsWallCell(x, z);"
    ) == ["must contain MathF.Floor", "must contain CellSize"]
    assert builder["body_contract_issues"](
        "GameMap.cs",
        "IsWallAt",
        "return IsWallCell((int)MathF.Floor(p.X / CellSize), (int)MathF.Floor(p.Z / CellSize));",
    ) == []
    assert builder["body_contract_issues"](
        "GameMap.cs", "IsWallCell", "return _walls[x % Width, z % Depth];"
    ) == ["must not contain %"]
    assert builder["body_contract_issues"](
        "GameMap.cs",
        "IsWallCell",
        "if (x < 0 || x >= Width || z < 0 || z >= Depth) return true; return _walls[x, z];",
    ) == []


def test_arena_builder_rejects_unbalanced_wrapped_method_before_compiling():
    builder = _builder_module()
    signature = "public bool IsWallCell(int x, int z)"

    assert builder["wrapped_body_issue"](
        "public bool IsWallCell(int x, int z) { return x >= 0;", signature,
    ) == "returned an unbalanced method wrapper; output statements only"
    assert builder["wrapped_body_issue"](
        "public bool IsWallCell(int x, int z) { return x >= 0; }", signature,
    ) == ""
    assert builder["extract_body"](
        "public bool IsWallCell(int x, int z) { return x >= 0; }", signature,
    ) == "return x >= 0;"
    assert builder["extract_body"](
        "public bool IsWallCell(int x, int z) => x >= 0;", signature,
    ) == "return x >= 0;"


def test_arena_builder_rejects_unbalanced_body_braces_but_not_string_content():
    builder = _builder_module()

    assert builder["body_brace_issue"]("if (x > 0) { return true;") == (
        "returned unbalanced braces in body statements"
    )
    assert builder["body_brace_issue"]("return $\"{{literal}}\";") == ""
    assert builder["body_brace_issue"]("// }\nreturn true;") == ""


def test_arena_builder_keeps_rejected_compiler_feedback_bounded_and_visible():
    preview = _builder_error_preview()

    rendered = preview(["first error", "second error", "third error", "fourth error"], limit=2)

    assert "first error" in rendered
    assert "second error" in rendered
    assert "fourth error" not in rendered
    assert "and 2 more" in rendered


def test_arena_builder_treats_restore_failure_as_a_failed_baseline():
    build_errors, namespace = _builder_build_errors()

    class Result:
        returncode = 1
        stdout = ""
        stderr = "error NU1301: unable to load the service index"

    namespace["subprocess"] = type(
        "Subprocess", (), {"run": staticmethod(lambda *_args, **_kwargs: Result())},
    )
    errors, ran = build_errors()

    assert ran is True
    assert errors == [
        "error: dotnet build exited 1: error NU1301: unable to load the service index"
    ]


def test_arena_builder_runs_held_out_verifier_against_explicit_project():
    run_verifier, namespace = _builder_verifier()
    calls = {}

    class Result:
        returncode = 0
        stdout = "=== 33 passed, 0 failed ===\n"
        stderr = ""

    def fake_run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return Result()

    namespace["subprocess"] = type("Subprocess", (), {"run": staticmethod(fake_run)})
    ok, output = run_verifier("C:/state/FpsGame_Skeleton")

    assert ok is True
    assert "33 passed" in output
    assert "-p:SonderTarget=C:" in calls["args"][-1]
    assert calls["kwargs"]["timeout"] == 300


def test_arena_builder_bounds_failed_verifier_output():
    run_verifier, namespace = _builder_verifier()

    class Result:
        returncode = 1
        stdout = "x" * 9_000
        stderr = ""

    namespace["subprocess"] = type(
        "Subprocess", (), {"run": staticmethod(lambda *_args, **_kwargs: Result())},
    )
    ok, output = run_verifier("C:/state/FpsGame_Skeleton")

    assert ok is False
    assert output.endswith("... verifier output truncated")
    assert len(output) < 8_100

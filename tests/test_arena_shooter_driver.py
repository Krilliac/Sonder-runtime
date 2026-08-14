"""CLI guard coverage for the worked arena-shooter generation harness."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "codegen-arena-shooter"
DRIVER = EXAMPLE / "build_with_sonder.py"
SKELETON_DRIVER = EXAMPLE / "build_skeleton.py"
ROOT = Path(__file__).resolve().parents[1]


def _driver_module():
    spec = importlib.util.spec_from_file_location("arena_shooter_driver", DRIVER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _skeleton_driver_module():
    spec = importlib.util.spec_from_file_location("arena_shooter_skeleton_driver", SKELETON_DRIVER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

# The example is intentionally importable without installing the project: its
# skeleton is a Python source-of-truth used by the generator, while its C# is
# emitted only for a particular local run.
sys.path.insert(0, str(EXAMPLE))
import bodynotes  # noqa: E402
import skeleton  # noqa: E402


def test_unknown_only_fails_before_runtime_or_project_setup(tmp_path: Path) -> None:
    """A misspelled source file must not turn into a zero-work success."""
    output = tmp_path / "would-be-game"
    result = subprocess.run(
        [
            sys.executable,
            str(DRIVER),
            "--sequential",
            "--only",
            "NotARealSource.cs",
            "--project",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=EXAMPLE,
    )

    assert result.returncode == 2
    assert "no such file in specs: NotARealSource.cs" in result.stdout
    assert "available files:" in result.stdout
    assert "GameMap.cs" in result.stdout
    assert not output.exists()


def test_model_headroom_reserves_live_ram_and_vram() -> None:
    driver = _driver_module()
    profile = SimpleNamespace(system_ram_available_gb=4.7, vram_free_gb=5.4)

    ok, detail = driver._model_headroom_report({"qwen2.5-coder:14b": 10.0}, profile)

    assert not ok
    assert "10.0 GiB" in detail
    assert "7.1 GiB" in detail


def test_model_headroom_allows_a_small_model_with_live_reserves() -> None:
    driver = _driver_module()
    profile = SimpleNamespace(system_ram_available_gb=4.7, vram_free_gb=5.4)

    ok, _detail = driver._model_headroom_report({"qwen2.5-coder:7b": 4.7}, profile)

    assert ok


def test_generated_projects_default_to_per_user_sonder_home(monkeypatch, tmp_path: Path) -> None:
    """Example output is runtime state, not untracked checkout content."""
    home = tmp_path / "sonder-home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.delenv("SONDER_GAME_PROJECT", raising=False)
    monkeypatch.delenv("SONDER_GAME_SKELETON_PROJECT", raising=False)

    driver = _driver_module()
    skeleton_driver = _skeleton_driver_module()

    assert Path(driver.default_project_path()) == home / "examples" / "arena-shooter" / "FpsGame_Sonder"
    assert Path(skeleton_driver.default_project_path()) == home / "examples" / "arena-shooter" / "FpsGame_Skeleton"


def test_v1_driver_initializes_empty_user_project_before_build(monkeypatch, tmp_path: Path) -> None:
    """A first --repair-only run has deterministic harness plumbing to build."""
    driver = _driver_module()
    output = tmp_path / "first-run-game"
    fake_server = SimpleNamespace(
        _ensemble_targets=lambda _tiers: ([('code', 'local')], []),
    )
    monkeypatch.setitem(sys.modules, "server", fake_server)
    monkeypatch.setattr(driver, "ensure_model_headroom", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(driver, "build", lambda: (True, []))
    monkeypatch.setattr(
        sys,
        "argv",
        [str(DRIVER), "--repair-only", "--project", str(output)],
    )

    assert driver.main() == 0
    assert (output / "FpsGame_Skeleton.csproj").is_file()


def test_arena_verifier_follows_user_state_or_explicit_target() -> None:
    """Moving generated output must not strand the held-out verifier."""
    project = (EXAMPLE / "Verify" / "Verify.csproj").read_text(encoding="utf-8")

    assert "$(SONDER_GAME_SKELETON_PROJECT)" in project
    assert "$(SONDER_HOME)\\examples\\arena-shooter\\FpsGame_Skeleton" in project
    assert "$(LOCALAPPDATA)\\sonder\\examples\\arena-shooter\\FpsGame_Skeleton" in project
    assert "<SonderTarget Condition=\"'$(SonderTarget)' == ''\">..\\FpsGame_Skeleton</SonderTarget>" in project
    assert "<HintPath>$(SonderTarget)\\bin\\Release\\net10.0\\FpsGameSonder.dll</HintPath>" in project


def test_skeleton_unknown_only_fails_before_generation() -> None:
    """The one-body driver must not turn a typo into a clean ``0 / 0`` run."""
    result = subprocess.run(
        [
            sys.executable,
            str(SKELETON_DRIVER),
            "--only",
            "NotARealSource.cs",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=EXAMPLE,
    )

    assert result.returncode == 2
    assert "no such file in skeleton: NotARealSource.cs" in result.stdout
    assert "available files:" in result.stdout
    assert "GameMap.cs" in result.stdout


def test_skeleton_verify_only_is_advertised_as_no_model_path() -> None:
    """The verifier must be rerunnable without accidentally spending a model call."""
    source = SKELETON_DRIVER.read_text(encoding="utf-8")

    assert 'parser.add_argument("--verify-only"' in source
    verify_only = source.index("if args.verify_only:")
    first_generation = source.index("server.ensemble_answer(")
    assert verify_only < first_generation
    assert "held-out verification was not run" in source


def test_arena_verification_build_output_is_ignored() -> None:
    """A normal local C# validation run must not dirty the source checkout."""
    result = subprocess.run(
        [
            "git", "-C", str(ROOT), "check-ignore", "-v",
            "examples/codegen-arena-shooter/Verify/obj/Debug/net8.0/Verify.dll",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "examples/codegen-arena-shooter/Verify/obj/" in result.stdout


def test_combatant_weapon_controls_are_host_owned_and_prompted() -> None:
    """Weapon invariants must not be re-derived by a generated frame body."""
    combatant = skeleton.by_name("Combatant.cs")

    # Direct skeleton code means the body loop cannot replace these guards.
    assert "// BODY:TryFire" not in combatant
    assert "// BODY:TryReload" not in combatant
    assert "public bool TryFire()" in combatant
    assert "!Alive || RespawnTimer > 0f || FireCooldown > 0f || Ammo <= 0" in combatant
    assert "Ammo--;" in combatant
    assert "FireCooldown = MathF.Max(0f, ClassKit.Get(Kit).FireDelay);" in combatant
    assert "public bool TryReload()" in combatant
    assert "int capacity = ClassKit.Get(Kit).MaxAmmo;" in combatant
    assert "if (Ammo >= capacity)" in combatant
    assert "Ammo = capacity;" in combatant

    match_note = bodynotes.note("Program.cs", "DoMatch")
    assert "_me.TryFire()" in match_note
    assert "_me.TryReload()" in match_note

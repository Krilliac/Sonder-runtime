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


def _driver_module():
    spec = importlib.util.spec_from_file_location("arena_shooter_driver", DRIVER)
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

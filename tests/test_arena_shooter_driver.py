"""CLI guard coverage for the worked arena-shooter generation harness."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "codegen-arena-shooter"
DRIVER = EXAMPLE / "build_with_sonder.py"

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

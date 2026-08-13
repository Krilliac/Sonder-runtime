"""CLI guard coverage for the worked arena-shooter generation harness."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "codegen-arena-shooter"
DRIVER = EXAMPLE / "build_with_sonder.py"


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

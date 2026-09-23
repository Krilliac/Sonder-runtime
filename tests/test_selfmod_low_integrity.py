import os
from pathlib import Path
import sys

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows MIC boundary")


def test_low_candidate_cannot_write_protected_truth(tmp_path):
    from selfmod_low_integrity import run_isolated

    truth = tmp_path / "truth.txt"
    truth.write_text("original", encoding="utf-8")
    command = [
        sys.executable, "-c",
        "from pathlib import Path; p=Path(%r); "
        "\ntry: p.write_text('tampered'); raise SystemExit(3)\n"
        "except PermissionError: pass" % str(truth),
    ]
    result = run_isolated(command, cwd=tmp_path, timeout=10, protected_paths=[truth])
    assert result["passed"] is True
    assert truth.read_text(encoding="utf-8") == "original"


def test_low_candidate_can_run_and_write_low_temp(tmp_path):
    from selfmod_low_integrity import run_isolated

    command = [
        sys.executable, "-c",
        "import os; from pathlib import Path; "
        "Path(os.environ['TEMP'], 'low-marker.txt').write_text('ok')",
    ]
    result = run_isolated(command, cwd=tmp_path, timeout=10)
    assert result["passed"] is True
    # The candidate cwd remains the medium checkout; its low temp is supplied
    # through TEMP/TMP. This assertion is intentionally on process success,
    # while the protected-file test above carries the security guarantee.
    assert not (tmp_path / "marker.txt").exists()

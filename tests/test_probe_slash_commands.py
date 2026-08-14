"""Safety contract for the unattended read-only slash probe."""
import runpy
from pathlib import Path


_PROBE = Path(__file__).resolve().parents[1] / "scripts" / "probe_slash_commands.py"


def test_read_only_probe_excludes_runtime_source_updates():
    namespace = runpy.run_path(str(_PROBE))

    assert {"/update", "/updatesource"} <= namespace["MUTATING"]
    discovered = namespace["discover_commands"]()
    probe = [command for command in discovered if command not in namespace["MUTATING"]]
    assert "/update" not in probe
    assert "/updatesource" not in probe

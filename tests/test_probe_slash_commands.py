"""Safety contract for the unattended read-only slash probe."""
import runpy
from pathlib import Path


_PROBE = Path(__file__).resolve().parents[1] / "scripts" / "probe_slash_commands.py"


def test_read_only_probe_excludes_updates_and_model_work():
    namespace = runpy.run_path(str(_PROBE))

    assert {
        "/update", "/updatesource", "/agent", "/ensemble", "/council",
    } <= namespace["MUTATING"]
    discovered = namespace["discover_commands"]()
    probe = [command for command in discovered if command not in namespace["MUTATING"]]
    assert "/update" not in probe
    assert "/updatesource" not in probe
    assert "/agent" not in probe
    assert "/ensemble" not in probe
    assert "/council" not in probe


def test_probe_exit_code_fails_for_contract_breaks():
    namespace = runpy.run_path(str(_PROBE))
    exit_code = namespace["probe_exit_code"]

    assert exit_code([], [{"command": "/help", "verdict": "handled"}]) == 0
    assert exit_code(["/missing"], []) == 1
    assert exit_code([], [{"command": "/help", "verdict": "fellthrough?"}]) == 1


def test_probe_marks_an_observed_model_call_as_fallthrough():
    namespace = runpy.run_path(str(_PROBE))

    assert namespace["classify"](
        "/help", "ok", "report\n\nmodel calls: 1   tool calls: 0\n",
    ) == "model_fallthrough"

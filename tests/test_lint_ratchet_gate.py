"""Static-analysis gate (ported from open PR #560) and the fixes it forced."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "check_lint_ratchet_under_test", REPO_ROOT / "scripts" / "check_lint_ratchet.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _finding(path: Path, code: str, row: int = 1) -> dict:
    return {"filename": str(path), "code": code, "location": {"row": row}, "message": code}


@pytest.fixture
def gate(tmp_path, monkeypatch):
    module = _load_gate()
    (tmp_path / "legacy.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "BASELINE_PATH", tmp_path / "lint_baseline.json")
    monkeypatch.setattr(module, "SIZE_RATCHET_MODULES", ("legacy.py",))
    module.findings = {"blocking": [], "ratchet": []}

    def fake_ruff(select, *, exclude_tests):
        return list(module.findings["blocking" if exclude_tests else "ratchet"])

    monkeypatch.setattr(module, "_ruff", fake_ruff)
    return module


def test_clean_tree_passes_and_update_records_counts(gate, tmp_path):
    gate.findings["ratchet"] = [_finding(tmp_path / "legacy.py", "B904")]

    assert gate.main(["--update"]) == 0
    baseline = json.loads((tmp_path / "lint_baseline.json").read_text(encoding="utf-8"))
    assert baseline["lint"] == {"legacy.py::B904": 1}
    assert baseline["module_lines"] == {"legacy.py": 2}
    assert gate.main([]) == 0


def test_blocking_finding_fails_even_when_ratchet_allows_it(gate, tmp_path, capsys):
    finding = _finding(tmp_path / "legacy.py", "F821", row=2)
    gate.findings["ratchet"] = [finding]
    assert gate.main(["--update"]) == 0

    gate.findings["blocking"] = [finding]
    assert gate.main([]) == 1
    assert "blocking F821 legacy.py:2" in capsys.readouterr().out


def test_new_ratcheted_finding_fails_and_update_refuses_to_raise(gate, tmp_path, capsys):
    gate.findings["ratchet"] = [_finding(tmp_path / "legacy.py", "B904")]
    assert gate.main(["--update"]) == 0

    gate.findings["ratchet"].append(_finding(tmp_path / "legacy.py", "B904", row=2))
    assert gate.main([]) == 1
    assert "legacy.py::B904: 2 findings, baseline allows 1" in capsys.readouterr().out
    assert gate.main(["--update"]) == 1
    baseline = json.loads((tmp_path / "lint_baseline.json").read_text(encoding="utf-8"))
    assert baseline["lint"] == {"legacy.py::B904": 1}


def test_fixed_findings_shrink_the_baseline(gate, tmp_path):
    gate.findings["ratchet"] = [
        _finding(tmp_path / "legacy.py", "B904"),
        _finding(tmp_path / "legacy.py", "B904", row=2),
    ]
    assert gate.main(["--update"]) == 0
    gate.findings["ratchet"] = []
    assert gate.main(["--update"]) == 0
    baseline = json.loads((tmp_path / "lint_baseline.json").read_text(encoding="utf-8"))
    assert baseline["lint"] == {}


def test_legacy_module_growth_fails(gate, tmp_path, capsys):
    assert gate.main(["--update"]) == 0
    (tmp_path / "legacy.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

    assert gate.main([]) == 1
    assert "module size legacy.py: 3 lines, limit 2" in capsys.readouterr().out


def test_tool_failure_is_distinct_from_violations(gate):
    def broken(select, *, exclude_tests):
        raise RuntimeError("ruff failed (exit 2): boom")

    gate._ruff = broken
    assert gate.main([]) == 2


def test_repository_baseline_is_well_formed():
    data = json.loads((REPO_ROOT / "scripts" / "lint_baseline.json").read_text(encoding="utf-8"))
    gate = _load_gate()
    assert data["rules"] == list(gate.RATCHET_RULES)
    assert set(data["module_lines"]) == set(gate.SIZE_RATCHET_MODULES)
    assert all(isinstance(count, int) and count > 0 for count in data["lint"].values())
    # Blocking rules are zero-tolerance outside tests, so none may be baselined there.
    blocking = tuple(gate.BLOCKING_RULES)
    for key in data["lint"]:
        path, rule = key.rsplit("::", 1)
        if not path.startswith("tests/"):
            assert not rule.startswith(blocking), key


def test_star_import_surfaces_resolve():
    namespace = {}
    exec("from sonder_runtime.application.agent_registry.unified import *", namespace)
    exec("from sonder_runtime.application.compaction import *", namespace)
    assert "UnifiedAgentRegistryService" in namespace
    assert "CompactionAppendService" in namespace

    from sonder_runtime.application import compaction
    from sonder_runtime.application.compaction import legacy

    assert all(isinstance(name, str) for name in compaction.__all__)
    assert set(legacy.__all__) <= set(compaction.__all__)


def test_scrub_paths_uses_each_roots_own_replacement():
    from sonder_runtime.domain.build.report import scrub_paths

    text = "/src/proj/build/out.o and /src/proj/main.c"
    assert scrub_paths(text, source_root="/src/proj", build_dir="/src/proj/build") == (
        "<build>/out.o and main.c"
    )

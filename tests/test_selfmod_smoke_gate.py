"""The selfmod acceptance battery must examine the candidate it approves.

Task #53 and its sibling. `selfmod.review()` requires a *passing* check of kind
`smoke` and of kind `syntax` before a self-modification may be approved. Both
were built by `server._selfmod_test_commands`, and both could report success
without inspecting the candidate:

    smoke  = python -c "import pathlib; assert pathlib.Path('.').is_dir(); ..."

`.` is the candidate workspace, so the assertion is a constant. The check never
imported, ran or read one byte of the candidate.

    syntax = py_compile <declared .py that still exist>
             or, when that list is empty, python -c "print('no Python syntax targets')"

The `.is_file()` filter empties the list precisely when the change *deleted* its
declared modules -- and an automated repair loop's most likely output shape is
a deletion. The one change that most needs a syntax gate got a `print`.

What replaces them is executed against the candidate, not around it: the smoke
probe imports the candidate's own modules in a child process rooted at the
workspace and must return a SHA-256 receipt over the bytes it actually loaded,
which the probe is never given. Exit 0 is not accepted as proof of work.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import selfmod
import server


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    )


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(state))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(state / "selfmod.db"))
    monkeypatch.delenv("SONDER_SELFMOD_ACTIVE", raising=False)
    return tmp_path


def repository(tmp_path):
    """A repo whose entry point no test imports -- what smoke exists to cover."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "boot.py").write_text(
        "import calc\n\n\ndef main():\n    return calc.add(1, 2)\n", encoding="utf-8"
    )
    (root / "legacy.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    if not shutil.which("git"):
        pytest.skip("git is required to build the selfmod workspace")
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.email", "smoke@test.invalid")
    git(root, "config", "user.name", "Smoke Gate Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


def prepared(root, files=("calc.py", "boot.py")):
    run = selfmod.create_plan(
        "fix deterministic addition defect", root,
        problem="add subtracts instead of adding",
        evidence=["tests/test_calc.py::test_add fails with -1 instead of 5"],
        files=list(files),
        criteria=["reproducing test passes", "regression suite passes"],
        risk="low",
        expected_benefit="correct arithmetic",
        rollback_plan="restore exact hashes",
    )
    selfmod.create_backup(run["id"])
    return selfmod.prepare_workspace(run["id"])


SOUND = "def add(a, b):\n    return a + b\n"
# Valid syntax, so py_compile passes. No test imports boot, so the whole pytest
# suite passes. Only running the module catches it.
BROKEN_BOOT = "import calc\n\nHANDLER = _undefined_helper()\n\n\ndef main():\n    return calc.add(1, 2)\n"
GOOD_BOOT = "import calc\n\n\ndef main():\n    return calc.add(1, 2)\n"


def _smoke(run_id):
    selfmod.begin_testing(run_id)
    return selfmod.record_smoke(run_id)


# --------------------------------------------------------------------------
# #53 -- the smoke gate must be able to fail
# --------------------------------------------------------------------------


def test_smoke_refuses_a_candidate_whose_module_cannot_be_imported(isolated):
    run = prepared(repository(isolated))
    selfmod.apply_candidate_changes(
        run["id"], {"calc.py": SOUND, "boot.py": BROKEN_BOOT}
    )
    result = _smoke(run["id"])
    assert result["passed"] is False
    assert result["exit_code"] != 0
    # Loud and specific: it names the module and the exception.
    assert "boot.py" in result["output"]
    assert "NameError" in result["output"]


def test_smoke_passes_a_sound_candidate(isolated):
    run = prepared(repository(isolated))
    selfmod.apply_candidate_changes(
        run["id"], {"calc.py": SOUND, "boot.py": GOOD_BOOT}
    )
    result = _smoke(run["id"])
    assert result["passed"] is True, result["output"]
    assert selfmod.SMOKE_RECEIPT_PREFIX in result["output"]


def test_smoke_refuses_a_probe_that_exits_zero_without_doing_the_work(isolated, monkeypatch):
    """Anti-proxy: 'it returned 0' is not 'it imported the candidate'."""
    run = prepared(repository(isolated))
    selfmod.apply_candidate_changes(
        run["id"], {"calc.py": SOUND, "boot.py": GOOD_BOOT}
    )
    monkeypatch.setattr(selfmod, "_SMOKE_PROBE", "print('selfmod smoke ok')\n")
    result = _smoke(run["id"])
    assert result["exit_code"] == 0
    assert result["passed"] is False
    assert "receipt" in result["output"].lower()


def test_smoke_refuses_a_forged_receipt(isolated, monkeypatch):
    run = prepared(repository(isolated))
    selfmod.apply_candidate_changes(
        run["id"], {"calc.py": SOUND, "boot.py": GOOD_BOOT}
    )
    monkeypatch.setattr(
        selfmod, "_SMOKE_PROBE",
        "print('%s %s modules=2 gone=0')\n" % (selfmod.SMOKE_RECEIPT_PREFIX, "0" * 64),
    )
    result = _smoke(run["id"])
    assert result["exit_code"] == 0
    assert result["passed"] is False


def test_smoke_verifies_a_declared_deletion_actually_happened(isolated):
    """A mixed change: legacy.py is deleted, calc.py survives and must import."""
    run = prepared(repository(isolated), files=("calc.py", "legacy.py"))
    selfmod.apply_candidate_changes(run["id"], {"calc.py": SOUND, "legacy.py": None})
    result = _smoke(run["id"])
    assert result["passed"] is True, result["output"]
    assert "gone=1" in result["output"]


def test_smoke_refuses_a_run_with_no_python_to_examine(isolated):
    """An empty target set is a refusal. That is the whole defect class."""
    root = repository(isolated)
    (root / "NOTES.md").write_text("notes\n", encoding="utf-8")
    git(root, "add", "NOTES.md")
    git(root, "commit", "-m", "notes")
    run = prepared(root, files=("NOTES.md",))
    selfmod.apply_candidate_changes(run["id"], {"NOTES.md": "changed\n"})
    result = _smoke(run["id"])
    assert result["passed"] is False
    assert "no Python" in result["output"]


# --------------------------------------------------------------------------
# The required battery no longer carries a constant-true smoke command
# --------------------------------------------------------------------------


def test_test_commands_no_longer_build_a_constant_true_smoke(isolated):
    run = prepared(repository(isolated))
    selfmod.apply_candidate_changes(
        run["id"], {"calc.py": SOUND, "boot.py": GOOD_BOOT}
    )
    commands = server._selfmod_test_commands(selfmod.get_run(run["id"]), [])
    flat = " ".join(part for _kind, cmd in commands for part in cmd)
    assert "is_dir()" not in flat
    assert "smoke" not in {kind for kind, _cmd in commands}


# --------------------------------------------------------------------------
# Important #1 -- the syntax gate must not degrade to a print
# --------------------------------------------------------------------------


def _syntax_result(run_id):
    run = selfmod.get_run(run_id)
    commands = dict(server._selfmod_test_commands(run, []))
    selfmod.begin_testing(run_id)
    return selfmod.record_test(run_id, "syntax", commands["syntax"])


def test_syntax_gate_refuses_when_every_declared_module_was_deleted(isolated):
    run = prepared(repository(isolated), files=("calc.py", "legacy.py"))
    selfmod.apply_candidate_changes(run["id"], {"calc.py": None, "legacy.py": None})
    result = _syntax_result(run["id"])
    assert result["passed"] is False
    assert result["exit_code"] != 0
    assert "calc.py" in result["output"] and "legacy.py" in result["output"]
    assert "no Python syntax targets" not in result["output"]


def test_syntax_gate_compiles_the_survivors_of_a_mixed_change(isolated):
    run = prepared(repository(isolated), files=("calc.py", "legacy.py"))
    selfmod.apply_candidate_changes(run["id"], {"calc.py": SOUND, "legacy.py": None})
    result = _syntax_result(run["id"])
    assert result["passed"] is True, result["output"]


def test_syntax_gate_still_catches_a_syntax_error(isolated):
    run = prepared(repository(isolated))
    selfmod.apply_candidate_changes(
        run["id"], {"calc.py": "def add(a, b)\n    return a + b\n", "boot.py": GOOD_BOOT}
    )
    result = _syntax_result(run["id"])
    assert result["passed"] is False


def test_syntax_gate_refuses_a_run_with_no_python_declared(isolated):
    root = repository(isolated)
    (root / "NOTES.md").write_text("notes\n", encoding="utf-8")
    git(root, "add", "NOTES.md")
    git(root, "commit", "-m", "notes")
    run = prepared(root, files=("NOTES.md",))
    selfmod.apply_candidate_changes(run["id"], {"NOTES.md": "changed\n"})
    result = _syntax_result(run["id"])
    assert result["passed"] is False
    assert "no Python syntax targets" not in result["output"]


# --------------------------------------------------------------------------
# End to end: the exact #53 reproduction, now refused
# --------------------------------------------------------------------------


def test_review_refuses_the_unimportable_candidate_end_to_end(isolated):
    """Before the fix this reached phase=approved via the auto-low-risk lane."""
    selfmod.set_mode("auto-low-risk")
    root = repository(isolated)
    run = prepared(root)
    rid = run["id"]
    selfmod.apply_candidate_changes(rid, {"calc.py": SOUND, "boot.py": BROKEN_BOOT})

    targeted = [sys.executable, "-m", "pytest", "-q", "tests/test_calc.py"]
    commands = dict(server._selfmod_test_commands(selfmod.get_run(rid), []))
    selfmod.record_reproducer_before(rid, targeted)
    selfmod.begin_testing(rid)
    selfmod.record_test(rid, "syntax", commands["syntax"])
    selfmod.record_test(rid, "targeted", targeted)
    selfmod.record_test(rid, "regression", [sys.executable, "-m", "pytest", "-q"])
    smoke = selfmod.record_smoke(rid)

    assert smoke["passed"] is False
    after = selfmod.review(rid)
    # review() rejects and then restores from the candidate, so the run ends in
    # a terminal refusal phase rather than `approved`.
    assert after["phase"] in selfmod.TERMINAL_PHASES, after["phase"]
    assert after["phase"] != "approved"
    assert after.get("approved_by") is None
    assert "smoke" in after["last_error"]
    # The live repository never received the candidate.
    assert (Path(root) / "boot.py").read_text(encoding="utf-8") == GOOD_BOOT
    assert "_undefined_helper" not in (Path(root) / "boot.py").read_text(encoding="utf-8")

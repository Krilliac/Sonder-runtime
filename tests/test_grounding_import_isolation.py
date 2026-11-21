"""Generated Python imports and artifacts stay inside its owned scratch tree."""
import subprocess
from pathlib import Path

import pytest

import grounding


@pytest.mark.parametrize("checks", ["none", "inline", "file"])
def test_shared_temp_modules_cannot_shadow_candidate_imports(tmp_path, monkeypatch, checks):
    shared = tmp_path / "shared-temp"
    shared.mkdir()
    poison = shared / "fractions.py"
    poison.write_text("raise RuntimeError('SHARED_TEMP_MODULE_IMPORTED')\n", encoding="utf-8")
    monkeypatch.setattr(grounding.tempfile, "tempdir", str(shared))
    code = "from fractions import Fraction\nprint(Fraction(1, 2) + Fraction(1, 2))\n"
    extra = "" if checks == "none" else "assert Fraction(1, 2) * 2 == 1\n"
    if checks == "file":
        # Exceed the production command-line threshold without slowing the child.
        extra += "#" + "x" * grounding._MAX_INLINE_RUNNER_CHARS
    result = grounding.run_code_detail(code, extra=extra)

    assert "SHARED_TEMP_MODULE_IMPORTED" not in result["stderr"], result
    assert result["ok"] is True, result
    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert result["stdout"] == "1"
    assert result["stderr"] == ""
    assert sorted(path.name for path in shared.iterdir()) == [poison.name]


@pytest.mark.parametrize("outcome", ["success", "compile-error", "timeout", "spawn-error"])
def test_owned_script_tree_is_cleaned_on_every_exit(tmp_path, monkeypatch, outcome):
    shared = tmp_path / "shared-temp"
    shared.mkdir()
    monkeypatch.setattr(grounding.tempfile, "tempdir", str(shared))
    code = "from pathlib import Path\nPath('child-artifact.txt').write_text('owned')\nprint('ok')\n"
    options = {}
    if outcome == "compile-error":
        code = "def broken(:\n    pass\n"
        options["compile_first"] = True
    elif outcome in {"timeout", "spawn-error"}:
        def failed_run(command, **kwargs):
            # Check cleanup after the child has acquired a real scratch cwd.
            scratch = Path(kwargs["cwd"])
            assert scratch.is_dir()
            (scratch / "child-artifact.txt").write_text("owned", encoding="utf-8")
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"partial\n")
            raise OSError("controlled process creation failure")

        monkeypatch.setattr(grounding, "_run_tree", failed_run)

    if outcome == "spawn-error":
        with pytest.raises(OSError, match="controlled process creation failure"):
            grounding.run_code_detail(code, **options)
    else:
        result = grounding.run_code_detail(code, **options)
        assert result["ok"] is (outcome == "success"), result
        assert result["timed_out"] is (outcome == "timeout"), result
        if outcome == "success":
            assert result["stdout"] == "ok"
        elif outcome == "compile-error":
            assert "compile failed" in result["stderr"]
        else:
            assert result["stdout"] == "partial"
    assert list(shared.iterdir()) == []


def test_owned_script_tree_is_cleaned_if_file_creation_fails(tmp_path, monkeypatch):
    shared = tmp_path / "shared-temp"
    shared.mkdir()
    monkeypatch.setattr(grounding.tempfile, "tempdir", str(shared))

    def failed_mkstemp(*args, **kwargs):
        raise OSError("controlled temporary-file creation failure")

    monkeypatch.setattr(grounding.tempfile, "mkstemp", failed_mkstemp)
    with pytest.raises(OSError, match="controlled temporary-file creation failure"):
        grounding.run_code_detail("pass")
    assert list(shared.iterdir()) == []

import game_ladder


# ---- detect_failure --------------------------------------------------

def test_detect_failure_timed_out_no_traceback_not_failed():
    failed, reason = game_ladder.detect_failure("", "", None, timed_out=True)
    assert failed is False
    assert "no crash" in reason


def test_detect_failure_traceback_in_stderr_is_failure():
    failed, reason = game_ladder.detect_failure(
        "", "Traceback (most recent call last):\n  File x\nNameError: name 'x' is not defined", 1
    )
    assert failed is True
    assert "NameError" in reason


def test_detect_failure_eoferror_traceback_not_failed():
    failed, reason = game_ladder.detect_failure(
        "", "Traceback (most recent call last):\n  File x\nEOFError", 1
    )
    assert failed is False
    assert "EOFError" in reason


def test_detect_failure_clean_run_ok():
    failed, reason = game_ladder.detect_failure("ok", "", 0)
    assert failed is False


def test_detect_failure_nonzero_rc_no_traceback_not_failed():
    failed, reason = game_ladder.detect_failure("", "", 1)
    assert failed is False
    assert "exited rc=1" in reason


# ---- ground (real fast subprocesses, stdlib-only snippets) -----------

def test_ground_hello_world_passes():
    passed, detail = game_ladder.ground("print('hello')", "console")
    assert passed is True


def test_ground_undefined_name_fails():
    passed, detail = game_ladder.ground("undefined_name_xyz", "console")
    assert passed is False


def test_ground_missing_import_fails():
    passed, detail = game_ladder.ground("import missing_mod_zzz", "console")
    assert passed is False


def test_ground_syntax_error_fails():
    passed, detail = game_ladder.ground("x=(", "console")
    assert passed is False
    assert "SyntaxError" in detail


def test_ground_clean_exit_nonzero_passes():
    passed, detail = game_ladder.ground("import sys; sys.exit(1)", "console")
    assert passed is True


def test_python_interpreter_falls_back_when_venv_launcher_is_broken(monkeypatch):
    import sys

    monkeypatch.setattr(game_ladder.os.path, "exists", lambda path: True)

    class Broken:
        returncode = 1

    monkeypatch.setattr(game_ladder.subprocess, "run", lambda *a, **k: Broken())

    assert game_ladder.python_interpreter() == sys.executable


# ---- run_ladder --------------------------------------------------------

def test_run_ladder_advances_through_all_levels(monkeypatch):
    monkeypatch.setattr(game_ladder, "ground", lambda code, kind, timeout=15: (True, "ok"))

    def gen_fn(prompt):
        return "```python\nprint('hi')\n```"

    result = game_ladder.run_ladder(gen_fn, save_dir=str(_tmp_dir()))
    assert result["failed_level"] is None
    assert result["reached"] == game_ladder.LEVELS[-1]["n"]


def test_run_ladder_stops_at_failing_level(monkeypatch):
    def fake_ground(code, kind, timeout=15):
        # fail on the level-2 code, pass everything else
        if "BROKEN" in code:
            return False, "exit code 1"
        return True, "ok"

    monkeypatch.setattr(game_ladder, "ground", fake_ground)

    def gen_fn(prompt):
        # level 2's prompt text differs from level 1's -> use that to decide
        if prompt == game_ladder.LEVELS[1]["prompt"]:
            return "```python\nBROKEN\n```"
        return "```python\nprint('hi')\n```"

    result = game_ladder.run_ladder(gen_fn, save_dir=str(_tmp_dir()))
    assert result["failed_level"] is not None
    assert result["failed_level"]["n"] == 2
    assert result["reached"] == 1


def test_run_ladder_records_outcomes(monkeypatch):
    monkeypatch.setattr(game_ladder, "ground", lambda code, kind, timeout=15: (True, "ok"))
    seen = []

    def gen_fn(prompt):
        return "```python\nprint('hi')\n```"

    def record(level, passed, code):
        seen.append((level["n"], passed))

    result = game_ladder.run_ladder(gen_fn, max_levels=3, save_dir=str(_tmp_dir()), record=record)
    assert result["failed_level"] is None
    assert [n for n, _ in seen] == [1, 2, 3]
    assert all(passed for _, passed in seen)


def test_run_ladder_no_code_block_fails_level():
    def gen_fn(prompt):
        return "no code here"

    result = game_ladder.run_ladder(gen_fn, max_levels=1, save_dir=str(_tmp_dir()))
    assert result["failed_level"] is not None
    assert result["detail"] == "no code block"


def test_run_ladder_repair_recovers_after_a_crash(monkeypatch):
    # ground() fails until the code contains "fixed"; the repair prompt (which
    # echoes the traceback) is what flips the generator to the fixed version.
    def fake_ground(code, kind, timeout=15):
        return ("fixed" in code, "ok" if "fixed" in code else "NameError: name 'random' is not defined")

    monkeypatch.setattr(game_ladder, "ground", fake_ground)

    def gen_fn(prompt):
        # solver._repair_prompt embeds the failing output ("NameError...") — key on it
        return "```python\nfixed=1\n```" if "NameError" in prompt else "```python\nbroken=1\n```"

    result = game_ladder.run_ladder_repair(gen_fn, max_levels=1, save_dir=str(_tmp_dir()), max_attempts=3)
    assert result["failed_level"] is None
    assert result["reached"] == 1


def test_run_ladder_repair_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(game_ladder, "ground", lambda c, k, timeout=15: (False, "NameError: x"))
    result = game_ladder.run_ladder_repair(
        lambda p: "```python\nx\n```", max_levels=1, save_dir=str(_tmp_dir()), max_attempts=2)
    assert result["failed_level"]["n"] == 1
    assert result["reached"] == 0


def test_ground_capture_returns_full_traceback_for_repair():
    passed, reason, full = game_ladder._ground_capture("raise ValueError('boom')", "pygame")
    assert passed is False
    # the full traceback (File/line frames) is what the repair loop needs
    assert "Traceback" in full and "ValueError" in full
    assert "ValueError" in reason  # short reason still classifies


def test_build_level_with_repair_autofixes_missing_import(monkeypatch):
    # a game that forgets `import random` should be recovered mechanically on the
    # first attempt, with NO model repair round-trip.
    calls = {"n": 0}

    def gen_fn(prompt):
        calls["n"] += 1
        return "```python\nprint(random.randint(1, 1))\n```"  # missing import random

    res = game_ladder.build_level_with_repair(
        {"name": "g", "kind": "console", "prompt": "p"}, gen_fn, max_attempts=3)
    assert res["passed"] is True
    assert res["attempts"] == 1
    assert calls["n"] == 1  # fixed without asking the model again


def test_build_level_with_repair_reports_attempts(monkeypatch):
    monkeypatch.setattr(game_ladder, "ground", lambda c, k, timeout=15: (True, "ran clean"))
    res = game_ladder.build_level_with_repair(
        game_ladder.LEVELS[0], lambda p: "```python\nprint(1)\n```", max_attempts=3)
    assert res["passed"] is True
    assert res["attempts"] == 1


def _tmp_dir():
    import tempfile
    return tempfile.mkdtemp(prefix="game_ladder_test_")


# ---- LEVELS shape --------------------------------------------------------

def test_levels_outline():
    levels = game_ladder.LEVELS
    assert len(levels) == 12
    assert [l["n"] for l in levels] == list(range(1, 13))
    for l in levels:
        assert l["name"]
        assert l["kind"] in ("console", "pygame")
        assert l["prompt"]

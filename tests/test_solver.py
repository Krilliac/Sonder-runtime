import solver


GOOD = "```python\ndef f():\n    return 1\n```"
BAD = "```python\ndef f():\n    return 0\n```"
CHECK = "assert f() == 1"


def _runner(code, check):
    # tiny real-ish runner: exec the code + check, ok iff no exception
    ns = {}
    try:
        exec(code + "\n" + check, ns)
        return True, "ok"
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def test_solve_passes_first_try():
    res = solver.solve("write f", CHECK, generate_fn=lambda p: GOOD, run_code_fn=_runner)
    assert res["passed"] is True
    assert res["attempts"] == 1


def test_solve_repairs_after_failure():
    # First attempt returns BAD; once the repair prompt echoes the failing code
    # (which contains "return 0"), return GOOD.
    def gen(p):
        return GOOD if "return 0" in p else BAD
    res = solver.solve("write f", CHECK, generate_fn=gen, run_code_fn=_runner, max_attempts=3)
    assert res["passed"] is True
    assert res["attempts"] == 2
    assert res["transcript"][0]["ok"] is False
    assert res["transcript"][1]["ok"] is True


def test_solve_gives_up_after_max_attempts():
    res = solver.solve("write f", CHECK, generate_fn=lambda p: BAD, run_code_fn=_runner, max_attempts=3)
    assert res["passed"] is False
    assert res["attempts"] == 3
    assert len(res["transcript"]) == 3
    assert res["code"] is not None  # keeps the last (failing) candidate


def test_solve_handles_missing_code_block():
    res = solver.solve("write f", CHECK, generate_fn=lambda p: "no code here", run_code_fn=_runner, max_attempts=2)
    assert res["passed"] is False
    assert res["transcript"][0]["output"] == "no code block"


def test_solve_survives_generate_exception():
    calls = {"n": 0}

    def flaky(p):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model down")
        return GOOD
    res = solver.solve("write f", CHECK, generate_fn=flaky, run_code_fn=_runner, max_attempts=2)
    assert res["passed"] is True
    assert res["attempts"] == 2


def test_repair_prompt_includes_prior_code_and_error():
    p = solver._repair_prompt("do X", "def f(): return 0", "AssertionError: boom")
    assert "do X" in p and "return 0" in p and "AssertionError: boom" in p


def test_best_of_n_returns_first_green():
    seq = iter([BAD, GOOD, GOOD])
    res = solver.best_of_n("write f", generate_fn=lambda p: next(seq), check=CHECK, run_code_fn=_runner, n=3)
    assert res["passed"] is True
    assert res["candidates"] == 2


def test_best_of_n_falls_back_when_none_pass():
    res = solver.best_of_n("write f", generate_fn=lambda p: BAD, check=CHECK, run_code_fn=_runner, n=2)
    assert res["passed"] is False
    assert res["code"] is not None

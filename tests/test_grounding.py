import grounding


def test_extract_code_block_pulls_fenced_python():
    text = "here you go:\n```python\ndef f(x):\n    return x + 1\n```\nhope that helps"
    assert grounding.extract_code_block(text) == "def f(x):\n    return x + 1"


def test_extract_code_block_none_when_absent():
    assert grounding.extract_code_block("no code here, just words") is None
    assert grounding.extract_code_block("") is None
    assert grounding.extract_code_block(None) is None


def test_extract_code_block_picks_last_of_several():
    text = (
        "first try:\n```python\ndef f(x):\n    return x\n```\n"
        "actually, better:\n```python\ndef f(x):\n    return x * 2\n```\n"
    )
    assert grounding.extract_code_block(text) == "def f(x):\n    return x * 2"


def test_extract_code_block_prefers_python_over_later_run_command():
    text = (
        "game:\n```python\nprint('pong')\n```\n"
        "run it:\n```\n/run python pong.py\n```\n"
    )
    assert grounding.extract_code_block(text) == "print('pong')"


def test_extract_code_block_ignores_bare_shell_command_blocks():
    text = "run it:\n```\npython pong.py\n```\n"
    assert grounding.extract_code_block(text) is None


def test_extract_code_block_still_accepts_bare_python():
    text = "quick demo:\n```\nprint('hello')\n```\n"
    assert grounding.extract_code_block(text) == "print('hello')"
def test_extract_code_block_can_select_language():
    text = (
        "```python\nprint('py')\n```\n"
        "```javascript\nconsole.log('js')\n```\n"
    )
    assert grounding.extract_code_block(text, "javascript") == "console.log('js')"
    assert grounding.extract_code_block(text, "js") == "console.log('js')"


def test_run_code_simple_success():
    ok, out = grounding.run_code("print(2+2)")
    assert ok is True
    assert out == "4"


def test_run_code_raises_reports_error():
    ok, out = grounding.run_code("raise ValueError('x')")
    assert ok is False
    assert "ValueError" in out


def test_run_code_with_passing_check():
    ok, out = grounding.run_code("def f(x): return x*x", "assert f(3)==9")
    assert ok is True


def test_run_code_with_failing_check():
    ok, out = grounding.run_code("def f(x): return x+1", "assert f(3)==9")
    assert ok is False


def test_run_code_detail_closes_stdin_for_interactive_programs():
    result = grounding.run_code_detail("name = input('name: ')\nprint(name)")

    assert result["ok"] is False
    assert result["timed_out"] is False
    assert result["returncode"] != 0
    assert "EOFError" in result["stderr"]
    assert "non-interactive" in grounding.format_run_result(result)


def test_format_run_result_explains_timeouts():
    result = grounding.run_code_detail("while True:\n    pass", timeout=1)
    text = grounding.format_run_result(result)

    assert result["timed_out"] is True
    assert "status: timed out" in text
    assert "bounded smoke-test" in text


def test_run_code_detail_clamps_timeout():
    result = grounding.run_code_detail("print('ok')", timeout=999)

    assert result["ok"] is True
    assert result["timeout"] == grounding.MAX_TIMEOUT
def test_run_code_reports_compile_failure_before_execution():
    ok, out = grounding.run_code("def broken(:\n    pass")
    assert ok is False
    assert "compile failed" in out


def test_run_code_jobs_parallel_keeps_input_order():
    jobs = [
        {"name": "slow", "code": "print('a')"},
        {"name": "checked", "code": "def f(x): return x + 1", "check": "assert f(2) == 3"},
        {"name": "bad", "code": "raise RuntimeError('boom')"},
    ]
    results = grounding.run_code_jobs(jobs, max_workers=3, default_timeout=8)
    assert [r["name"] for r in results] == ["slow", "checked", "bad"]
    assert [r["ok"] for r in results] == [True, True, False]
    assert "boom" in results[2]["output"]


def test_run_code_jobs_compile_only():
    results = grounding.run_code_jobs([
        {"name": "compile", "code": "raise RuntimeError('not executed')", "execute": False}
    ])
    assert results[0]["ok"] is True
    assert results[0]["output"] == "compiled"


def test_run_code_jobs_records_language(monkeypatch):
    monkeypatch.setattr(
        grounding,
        "run_language_code",
        lambda code, language, extra, timeout, interp=None, execute=True: (
            language == "javascript",
            "ok",
        ),
    )
    results = grounding.run_code_jobs([
        {"name": "js", "language": "js", "code": "console.log(1)"},
        {"name": "bad", "language": "madeup", "code": "x"},
    ])
    assert [r["language"] for r in results] == ["javascript", "madeup"]
    assert [r["ok"] for r in results] == [True, False]


def test_run_code_jobs_flags_timeout_distinctly(monkeypatch):
    # A timed-out phase must be marked timed_out (not a generic failure) so the
    # formatter can reconcile total elapsed against the per-phase budget.
    def fake(code, language, extra, timeout, interp=None, execute=True):
        if "sleepy" in code:
            return False, "(timed out after %ss)" % timeout
        return True, "ok"

    monkeypatch.setattr(grounding, "run_language_code", fake)
    results = grounding.run_code_jobs([
        {"name": "fast", "code": "print(1)", "timeout": 8},
        {"name": "sleepy", "code": "sleepy loop", "timeout": 8},
    ])
    assert results[0]["timed_out"] is False
    assert results[1]["timed_out"] is True
    assert results[1]["timeout"] == 8


def test_format_code_jobs_renders_timeout_verdict():
    results = [
        {"name": "fast", "language": "python", "ok": True, "output": "1",
         "seconds": 0.01, "timed_out": False, "timeout": 8},
        {"name": "sleepy", "language": "cpp", "ok": False, "output": "(timed out after 8s)",
         "seconds": 9.135, "timed_out": True, "timeout": 8},
        {"name": "broken", "language": "python", "ok": False, "output": "boom",
         "seconds": 0.02, "timed_out": False, "timeout": 8},
    ]
    text = grounding.format_code_jobs(results)
    assert "1/3 passed" in text
    assert "(1 timed out)" in text
    # The timeout line must show the per-phase budget so 9.135s vs 8s is
    # reconcilable rather than looking self-contradictory.
    assert "[TIMEOUT 8s/phase] sleepy" in text
    assert "[PASS] fast" in text
    assert "[FAIL] broken" in text

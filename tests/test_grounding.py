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

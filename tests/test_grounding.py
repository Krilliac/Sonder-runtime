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

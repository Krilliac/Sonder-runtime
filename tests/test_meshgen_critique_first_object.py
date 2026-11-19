"""_first_object must never return an object nested inside one it failed to decode."""
import time

from sonder_runtime.domain.meshgen.critique import _first_object, parse_critique

# Matches BLOB_BUDGET in tests/test_fuzz_model_output_parsers.py.
BUDGET_SECONDS = 0.1
DEEP = "[" * 5000 + "]" * 5000
# A balanced object with a bad value fails to decode on every platform; how deep
# nesting fares depends on the C stack (Windows raises RecursionError, Linux decodes it).
BROKEN = '{"meta": {"score": 10}, "bad": nope, "score": 1}'


def test_failed_outer_decode_does_not_return_a_nested_score():
    # The outer object fails to decode. The scan used to resume at the next "{"
    # inside it and return {"score": 10}.
    result = _first_object(BROKEN)
    assert result is None or result.get("score") != 10


def test_deep_outer_object_never_yields_its_nested_score():
    # Where the deep array raises RecursionError this is the same failed-decode
    # path; where it decodes, the outer object (score 1) is returned.
    text = '{"meta": {"score": 10}, "deep": ' + DEEP + ', "score": 1}'
    result = _first_object(text)
    assert result is None or result.get("score") != 10


def test_scan_resumes_after_the_failed_object():
    result = _first_object(BROKEN + ' {"score": 4}')
    assert result is not None and result.get("score") == 4


def test_template_braces_before_the_json_still_parse():
    text = "{{name}} " * 70 + '{"score": 4}'
    assert parse_critique(text)["score"] == 4


def test_cache_defeating_quotes_stay_linear():
    text = '{"\\"' * 20000
    started = time.perf_counter()
    assert parse_critique(text)["score"] is None
    assert time.perf_counter() - started < BUDGET_SECONDS

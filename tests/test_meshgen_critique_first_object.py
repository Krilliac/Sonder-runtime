"""_first_object must never return an object nested inside one it failed to decode."""
import time

from sonder_runtime.domain.meshgen.critique import _first_object, parse_critique

# Matches BLOB_BUDGET in tests/test_fuzz_model_output_parsers.py.
BUDGET_SECONDS = 0.1
DEEP = "[" * 5000 + "]" * 5000


def test_failed_outer_decode_does_not_return_a_nested_score():
    # The outer object fails to decode (RecursionError on the deep array). The scan
    # used to resume at the next "{" inside it and return {"score": 10}.
    text = '{"meta": {"score": 10}, "deep": ' + DEEP + ', "score": 1}'
    result = _first_object(text)
    assert result is None or result.get("score") != 10


def test_scan_resumes_after_the_failed_object():
    text = '{"meta": {"score": 10}, "deep": ' + DEEP + ', "score": 1} {"score": 4}'
    result = _first_object(text)
    assert result is not None and result.get("score") == 4


def test_template_braces_before_the_json_still_parse():
    text = "{{name}} " * 70 + '{"score": 4}'
    assert parse_critique(text)["score"] == 4


def test_cache_defeating_quotes_stay_linear():
    text = '{"\\"' * 20000
    started = time.perf_counter()
    assert parse_critique(text)["score"] is None
    assert time.perf_counter() - started < BUDGET_SECONDS

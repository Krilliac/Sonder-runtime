"""Strict fullmatch rule for _judge_verdict: conflicting markers give None."""

from consult import _judge_verdict


def test_judge_verdict_case_insensitive_contradiction():
    assert _judge_verdict('YeS / code nobody eyes so the this nO ') is None
    assert _judge_verdict('yeS / yEs') is None

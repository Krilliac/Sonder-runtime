"""Expected-output policy for the generated campaign task catalog."""

from __future__ import annotations


_EXPECTED_OUTPUTS = {
    "hello": "sonder-ok",
    "sum": "42",
    "loop": "1\n2\n3",
    "string": "rednos",
    "branch": "prime",
    "list": "20",
    "toposort": "d a b c",
    "lru": "10 -1 30",
    "intervals": "1-6 8-12",
    "balanced": "ok\nbad\nbad",
    "wordfreq": "the:3",
    "fib": "6765",
}


def campaign_expected(task_name):
    """Return the exact expected output for a known campaign task."""
    return _EXPECTED_OUTPUTS.get(task_name, "")

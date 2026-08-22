import pytest

import server
from sonder_runtime.domain.model_usage import usage_count


def test_server_keeps_identity_compatible_usage_count_alias():
    assert server._model_usage_count is usage_count


@pytest.mark.parametrize("value, expected", [
    (None, None),
    (0, 0),
    ("17", 17),
    (3.9, 3),
    ("not-a-count", None),
    (object(), None),
    (-1, None),
])
def test_usage_count_accepts_only_non_negative_integer_values(value, expected):
    assert usage_count(value) == expected


def test_usage_count_rejects_integer_conversion_overflow():
    class Overflowing:
        def __int__(self):
            raise OverflowError

    assert usage_count(Overflowing()) is None

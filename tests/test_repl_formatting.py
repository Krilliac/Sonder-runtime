import pytest

from sonder_runtime.adapters.observability.repl_formatting import elapsed_label


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-1, "0ms"),
        (0, "0ms"),
        (999, "999ms"),
        (1000, "1.00s"),
        (1250, "1.25s"),
    ],
)
def test_elapsed_label_formats_millisecond_and_second_ranges(value, expected):
    assert elapsed_label(value) == expected


def test_elapsed_label_coerces_numeric_strings():
    assert elapsed_label("2500") == "2.50s"

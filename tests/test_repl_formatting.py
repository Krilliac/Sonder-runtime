import pytest

from sonder_runtime.adapters.observability.repl_formatting import (
    elapsed_label,
    response_footer,
    response_status_label,
)


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


def test_response_panel_status_is_compact_and_explicit():
    assert response_status_label("Sonder") == "Sonder · answer"
    assert response_status_label("Sonder", error=True) == "Sonder · error"


def test_response_footer_keeps_feedback_as_an_optional_terminal_hint():
    box = {"bl": "╰"}
    assert response_footer(box, "1.20s") == "╰ 1.20s  "
    assert response_footer(box, "1.20s", offer_feedback=True) == (
        "╰ 1.20s  (/pass or /fail)"
    )

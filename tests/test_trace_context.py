"""Adversarial parsing tests for the W3C traceparent seam.

Inbound headers are attacker-controlled: the parser must be total (never
raise) and strict (never accept a malformed value that a downstream system
would then propagate as trusted trace identity).
"""
import pytest

from sonder_runtime.application.observability.trace_context import (
    MAX_HEADER_LENGTH,
    TraceParent,
    child_context,
    format_traceparent,
    parse_traceparent,
)

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_valid_header_round_trips():
    ctx = parse_traceparent(VALID)
    assert ctx is not None
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert ctx.parent_id == "00f067aa0ba902b7"
    assert ctx.sampled is True
    assert format_traceparent(ctx) == VALID


def test_unsampled_flags_parse():
    ctx = parse_traceparent(VALID[:-2] + "00")
    assert ctx is not None and ctx.sampled is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "",
        "00",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",  # missing flags
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",  # v0 extra field
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # forbidden version
        "0-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # short version
        "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # future version, no extra data
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",  # all-zero trace id
        "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",  # all-zero parent id
        "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",  # uppercase hex
        "00-4bf92f3577b34da6a3ce929d0e0e473-00f067aa0ba902b7-01",  # short trace id
        "00-4bf92f3577b34da6a3ce929d0e0e47361-00f067aa0ba902b7-01",  # long trace id
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b-01",  # short parent id
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1",  # short flags
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-zz",  # non-hex flags
        "00-xyzf2f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # non-hex trace id
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01" + "-x" * 300,  # oversized
    ],
)
def test_malformed_headers_are_rejected_not_raised(header):
    assert parse_traceparent(header) is None


def test_future_version_with_additional_data_parses():
    ctx = parse_traceparent(VALID.replace("00-", "cc-", 1) + "-what-the-future-holds")
    assert ctx is not None
    assert ctx.version == 0xCC
    assert ctx.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_oversized_header_bound_is_enforced_exactly():
    padded = VALID.replace("00-", "cc-", 1) + "-" + "a" * (MAX_HEADER_LENGTH)
    assert parse_traceparent(padded) is None


def test_child_context_keeps_trace_and_flags_but_moves_the_span():
    ctx = parse_traceparent(VALID)
    child = child_context(ctx, "b7ad6b7169203331")
    assert child.trace_id == ctx.trace_id
    assert child.parent_id == "b7ad6b7169203331"
    assert child.flags == ctx.flags
    assert format_traceparent(child).endswith("-b7ad6b7169203331-01")


@pytest.mark.parametrize("span", ["", "0" * 16, "B7AD6B7169203331", "abc", None, 42])
def test_child_context_rejects_invalid_span_ids(span):
    ctx = parse_traceparent(VALID)
    with pytest.raises((ValueError, TypeError)):
        child_context(ctx, span)


def test_format_rejects_forged_contexts():
    with pytest.raises(ValueError):
        format_traceparent(TraceParent("0" * 32, "00f067aa0ba902b7"))
    with pytest.raises(ValueError):
        format_traceparent(TraceParent("4bf92f3577b34da6a3ce929d0e0e4736", "0" * 16))
    with pytest.raises(TypeError):
        format_traceparent("00-...-...-01")

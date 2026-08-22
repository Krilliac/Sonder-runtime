import sonder_launcher
from sonder_runtime.adapters import launcher_output


def test_root_launcher_output_helpers_are_identity_preserving_aliases():
    assert sonder_launcher._output_text is launcher_output.output_text
    assert sonder_launcher._bounded_seconds is launcher_output.bounded_seconds
    assert sonder_launcher._retention_limit is launcher_output.retention_limit


def test_output_policy_preserves_bytes_and_bounds_the_tail():
    assert launcher_output.output_text(b" first ", "second") == "first\nsecond"
    assert launcher_output.output_text("abcdefghijklmnopqrstuvwxyz", limit=24) == (
        "[output truncated]\nvwxyz"
    )


def test_timeout_and_retention_policy_keep_safe_bounds():
    assert launcher_output.bounded_seconds("bad", 4, 10) == 4.0
    assert launcher_output.bounded_seconds(0, 4, 10) == 1.0
    assert launcher_output.bounded_seconds(20, 4, 10) == 10.0
    assert launcher_output.retention_limit("bad") == 100
    assert launcher_output.retention_limit(0) == 1
    assert launcher_output.retention_limit(5000) == 1000

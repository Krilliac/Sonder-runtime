"""Ownership tests for packaged toolchain probe output policy."""

import toolchain_status
from sonder_runtime.platform import toolchain_policy


def test_toolchain_output_policy_owns_redaction(monkeypatch):
    monkeypatch.setenv("SONDER_API_KEY", "tool-secret-value")

    assert toolchain_policy.safe_output("version token=tool-secret-value") == (
        "version token=[REDACTED]"
    )
    assert toolchain_status._safe_output("version token=tool-secret-value") == (
        "version token=[REDACTED]"
    )


def test_toolchain_output_policy_owns_bounded_presentation():
    value = toolchain_policy.safe_output("x" * 12, max_chars=5)

    assert value == "xxxxx\n[output truncated]"


def test_legacy_toolchain_wrapper_passes_its_compatibility_limit(monkeypatch):
    monkeypatch.setattr(toolchain_status, "MAX_OUTPUT_CHARS", 4)

    assert toolchain_status._safe_output("abcdef") == "abcd\n[output truncated]"

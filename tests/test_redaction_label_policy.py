"""Shared label policy uses ordinary synthetic values across its consumers."""
from __future__ import annotations

import pytest

from sonder_runtime.application.session.query_export import DefaultExportRedactor
from sonder_runtime.application.session.transcript_export import redact_export_text
from sonder_runtime.application.tools.facade import PatternOutputRedactor
from sonder_runtime.domain.security.redaction import REDACTED, redact_structure, redact_text
from sonder_runtime.platform.logging import Redactor


@pytest.mark.parametrize("label", (
    "pwd", "PWD", "aws_secret_access_key", "AWS_SECRET_ACCESS_KEY",
    "aws-secret-access-key", "secret_access_key",
))
@pytest.mark.parametrize("template", ("{label}={value}", '"{label}": "{value}"'))
def test_label_policy_agrees_across_text_consumers(label, template):
    text = "before " + template.format(label=label, value="fixture-value-014") + " after"
    expected = "before " + template.format(label=label, value=REDACTED).replace(
        '"' + REDACTED + '"', REDACTED,
    ) + " after"
    for redact in (redact_text, Redactor(env={}).redact, DefaultExportRedactor().redact):
        assert redact(text) == expected
        assert redact(expected) == expected
    output = PatternOutputRedactor().redact("fixture", {"content": text})
    assert output.applied
    assert output.value == {"content": expected}


def test_key_aware_walk_preserves_shape_and_noncredential_fields():
    value = {"pwd": "x", "aws_secret_access_key": "y", "count": 2, "enabled": True,
             "nested": [{"PWD": "z", "notes": "pwd and aws_secret_access_key are labels"}]}
    assert redact_structure(value, sensitive_keys=True) == {
        "pwd": REDACTED, "aws_secret_access_key": REDACTED, "count": 2, "enabled": True,
        "nested": [{"PWD": REDACTED, "notes": "pwd and aws_secret_access_key are labels"}],
    }


@pytest.mark.parametrize("text", (
    "Run pwd to display the directory.",
    "Set aws_secret_access_key in the environment.",
    "getpwd=fixture-directory", "notaws_secret_access_key=fixture-metadata",
    "pwd=", "pwd: ",
))
def test_label_mentions_and_unrelated_identifiers_remain_unchanged(text):
    for redact in (redact_text, Redactor(env={}).redact, DefaultExportRedactor().redact):
        assert redact(text) == text


def test_composed_export_retains_notes_after_a_redacted_authorization_value():
    text = "Authorization: Bearer fixture-token-014 followed by ordinary notes"
    expected = "Authorization: [REDACTED] followed by ordinary notes"
    for redact in (redact_text, Redactor(env={}).redact, DefaultExportRedactor().redact):
        assert redact(text) == expected
        assert redact(expected) == expected
    assert redact_export_text(text) == expected
    assert redact_export_text(text, redact=Redactor(env={}).redact) == expected

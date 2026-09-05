from __future__ import annotations

from sonder_runtime.platform.config import Secrets
from sonder_runtime.platform.secret_presence import redact_presence


def test_redact_presence_never_returns_secret_content():
    assert redact_presence("operator-secret") == "[set]"
    assert redact_presence("") == "[unset]"


def test_redact_presence_treats_falsey_values_as_unset():
    assert redact_presence(None) == "[unset]"
    assert redact_presence(0) == "[unset]"


def test_config_secret_redaction_uses_platform_policy():
    assert Secrets(api_key="api-value", auth_secret="auth-value").as_redacted_dict() == {
        "api_key": "[set]",
        "auth_secret": "[set]",
        "backup_key_file": "[unset]",
        "artifact_transfer_key": "[unset]",
    }


def test_artifact_transfer_secret_redacts_to_presence_only():
    redacted = Secrets(artifact_transfer_key="private-artifact-test-key").as_redacted_dict()
    assert redacted["artifact_transfer_key"] == "[set]"
    assert "private-artifact-test-key" not in str(redacted)

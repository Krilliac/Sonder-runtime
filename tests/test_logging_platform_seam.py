from __future__ import annotations

import io

import sonder_logging
from sonder_runtime.adapters import local_observability
from sonder_runtime.adapters.filesystem import workbench
from sonder_runtime.platform import logging as runtime_logging


def test_platform_seam_preserves_redaction_contract():
    assert sonder_logging is runtime_logging
    assert runtime_logging.Redactor is sonder_logging.Redactor
    assert runtime_logging.REDACTION_FAILED == sonder_logging.REDACTION_FAILED
    redactor = runtime_logging.Redactor(secret_values=("top-secret",), env={})
    assert redactor.redact("value=top-secret") == "value=[REDACTED]"


def test_platform_seam_preserves_configured_handler_and_json_output():
    stream = io.StringIO()
    logger = runtime_logging.configure_logging(
        level="INFO", stream=stream,
        redactor=runtime_logging.Redactor(secret_values=("secret",), env={}),
    )
    logger.info("secret")
    logger.handlers[-1].flush()
    assert len(logger.handlers) == 1
    assert "secret" not in stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()


def test_legacy_module_private_pattern_patch_targets_canonical_implementation(monkeypatch):
    original = runtime_logging._PATTERNS
    monkeypatch.setattr(sonder_logging, "_PATTERNS", ())
    assert runtime_logging.Redactor(env={}).redact("token=secret-value") == "token=secret-value"
    monkeypatch.setattr(sonder_logging, "_PATTERNS", original)


def test_workbench_child_environment_still_scrubs_control_values(monkeypatch):
    monkeypatch.setenv("SONDER_API_KEY", "do-not-leak")
    env = workbench._run_program_environment()
    assert "SONDER_API_KEY" not in env
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_local_observability_uses_canonical_logging_identity_and_redaction():
    assert local_observability.Redactor is runtime_logging.Redactor
    assert local_observability.REDACTION_FAILED == runtime_logging.REDACTION_FAILED
    redactor = local_observability.Redactor(
        secret_values=("local-secret",), env={},
    )
    assert redactor.redact("value=local-secret") == "value=[REDACTED]"

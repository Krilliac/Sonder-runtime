"""The log/session Redactor removes free-standing known credential formats.

Finding #34: ``platform.logging.Redactor`` redacted secret env values,
``key=value`` assignments, bearer/authorization, URL credentials and PEM
blocks, but not a bare ``AKIA...``/``ghp_...``/``sk-...`` value. The formats
now come from one shared module that the contribution privacy classifier also
uses, so the two boundaries cannot drift.

Every credential-shaped fixture is assembled at runtime so no literal
vendor-secret string exists in the repository.
"""
from __future__ import annotations

import re

import pytest

import contribute
from sonder_runtime.domain.security import credential_formats
from sonder_runtime.platform.logging import REDACTED, Redactor


def _samples():
    return {
        "aws": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "github": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        "github_pat": "github" + "_pat_" + "11ABCDEFG0123456789_abcdefgh",
        "openai": "s" + "k-" + "proj-" + "abcDEF123456ghiJKL789",
        "slack": "xo" + "xb-" + "1234567890-abcdefghij",
        "google": "AI" + "za" + "SyA-abcdefghijklmnopqrstuvwxyz12345",
        "stripe": "s" + "k_live_" + "abcdefghijklmnop1234",
        "huggingface": "h" + "f_" + "abcdefghijklmnop",
        "jwt": "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxIn0" + "." + "abcdefghijk123",
    }


@pytest.mark.parametrize("kind", sorted(_samples()))
def test_free_standing_known_credential_is_redacted(kind):
    secret = _samples()[kind]
    text = "deploy failed while using %s for the upload" % secret
    out = Redactor(env={}).redact(text)
    assert secret not in out
    assert REDACTED in out
    assert out.startswith("deploy failed while using ")


def test_plain_digests_and_prose_are_not_rewritten():
    redactor = Redactor(env={})
    digest = "a" * 64
    text = "event hash %s; the task asked about a skeleton key and sk-learn" % digest
    assert redactor.redact(text) == text


def test_redactor_and_contribution_classifier_share_one_definition():
    rules = {name: pattern for name, pattern, _ in contribute.PRIVATE_RULES}
    assert rules["known_credential"] is credential_formats.KNOWN_CREDENTIAL
    assert rules["jwt"] is credential_formats.JWT
    import sonder_runtime.platform.logging as logging_module

    for pattern in credential_formats.CREDENTIAL_FORMATS:
        assert pattern in logging_module._PATTERNS


def test_the_redactor_module_carries_no_private_copy_of_the_formats():
    import inspect

    import sonder_runtime.platform.logging as logging_module

    source = inspect.getsource(logging_module)
    assert not re.search(r"gh\[pousr\]|A\(\?:KI\|SI\)A", source)

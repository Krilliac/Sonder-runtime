"""The canonical domain redaction seam and its two enforced consumers.

Nine redaction implementations grew independently in this codebase. The
domain module ``sonder_runtime.domain.security.redaction`` is the canonical
pattern set for new callers; the platform logging redactor cannot import it
(platform may only see stdlib), so a drift guard here pins the two pattern
sets byte-identical instead. The runtime container is the production
composition root and must never fall back to the identity redactor.
"""
from __future__ import annotations

import random

import pytest

from sonder_runtime.adapters.runtime_capabilities import RuntimeCapabilities
from sonder_runtime.adapters.runtime_configuration import RuntimeConfig
from sonder_runtime.adapters.runtime_container import build_runtime
from sonder_runtime.application.tools.facade import (
    IdentityRedactor,
    PatternOutputRedactor,
)
from sonder_runtime.application.tools.gateway_contract import RedactedOutput
from sonder_runtime.domain.security import redaction
from sonder_runtime.platform import logging as platform_logging


# --- drift guard -----------------------------------------------------------


def test_platform_patterns_are_byte_identical_to_domain_patterns():
    """Layering forbids platform -> domain, so equality is enforced here.

    If this fails, one pattern set was edited without the other: apply the
    same change to both ``domain/security/redaction.py`` and
    ``platform/logging.py`` so every redaction path scrubs the same shapes.
    """
    domain = [(p.pattern, p.flags) for p in redaction.PATTERNS]
    platform = [(p.pattern, p.flags) for p in platform_logging._PATTERNS]
    assert domain == platform
    assert redaction._MAX_PASSES == platform_logging._MAX_PASSES
    # The PEM block is bound by name in both copies, not by tuple position:
    # the same pattern, once, at the same place in each tuple.
    domain_pem, platform_pem = redaction._PEM, platform_logging._PEM
    assert (domain_pem.pattern, domain_pem.flags) == (platform_pem.pattern, platform_pem.flags)
    assert [p is domain_pem for p in redaction.PATTERNS] == [
        p is platform_pem for p in platform_logging._PATTERNS
    ]
    assert sum(p is domain_pem for p in redaction.PATTERNS) == 1


def test_domain_and_platform_redaction_agree_on_hostile_text():
    """The pass loop and ``_sub_pem`` are copied as well as the patterns, so
    compare what the two copies do, not only what they list."""
    rng = random.Random(20261116)
    key = "AK" + "IA" + "0" * 16
    fragments = [
        key, key + key, "bearer xxxxxxxx", "Cookie: a=b", "password='abcd\"", "token='abcd'", "x'",
        "http://u:p@h", "-x.https://a:b@c", "[REDACTED]", "pwd=", "eyJaaaaaaaa.bbbbbbbb.cccccccc-",
        "-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----", "END PRIVATE KEY-----",
        "sk-", "zz", " ", "\n", "\r\n",
    ]
    redactor = platform_logging.Redactor(env={})
    changed = 0
    for _ in range(4000):
        text = "".join(rng.choice(fragments) for _ in range(rng.randint(1, 12)))
        want = redaction.redact_text(text)
        changed += want != text
        assert redactor.redact(text) == want, text
    assert changed > 2000


def test_redacted_markers_agree_across_layers():
    assert redaction.REDACTED == platform_logging.REDACTED
    assert redaction.REDACTION_FAILED == platform_logging.REDACTION_FAILED


# --- adversarial corpus ----------------------------------------------------

CORPUS = [
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("authorization=Bearer abc.def.ghi-jkl", "abc.def.ghi-jkl"),
    ("bearer sk-live-1234567890abcdef", "sk-live-1234567890abcdef"),
    ('{"api_key": "AKIAIOSFODNN7EXAMPLE"}', "AKIAIOSFODNN7EXAMPLE"),
    ("password=hunter42x", "hunter42x"),
    ("token: ghp_16C7e42F292c6912E7710c838347Ae178B4a", "ghp_16C7e42F"),
    ("https://alice:s3cr3tpw@example.com/repo.git", "s3cr3tpw"),
    (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
        "MIIEow==",
    ),
]


@pytest.mark.parametrize("text,secret", CORPUS)
def test_domain_redaction_scrubs_credential_shapes(text, secret):
    assert secret not in redaction.redact_text(text)


def test_secret_values_are_scrubbed_even_without_a_matching_shape():
    out = redaction.redact_text(
        "the value v9-zzTOPSECRET appeared mid-sentence",
        secret_values=["v9-zzTOPSECRET"],
    )
    assert "v9-zzTOPSECRET" not in out
    assert redaction.REDACTED in out


def test_path_prefixes_become_workspace_labels():
    out = redaction.redact_text(
        r"wrote C:\Users\someone\project\out.txt",
        path_prefixes=[r"C:\Users\someone\project"],
    )
    assert "someone" not in out
    assert redaction.WORKSPACE_LABEL in out


# --- bounded structure walk ------------------------------------------------


def test_structure_walk_preserves_shape_and_scrubs_strings():
    value = {
        "count": 3,
        "ok": True,
        "none": None,
        "nested": ["password=deepsecret1", ("bearer toktoktok123",)],
    }
    walked = redaction.redact_structure(value)
    assert walked["count"] == 3 and walked["ok"] is True and walked["none"] is None
    assert "deepsecret1" not in walked["nested"][0]
    assert isinstance(walked["nested"][1], tuple)
    assert "toktoktok123" not in walked["nested"][1][0]


def test_structure_walk_replaces_overdeep_subtrees_instead_of_passing_them():
    value = "password=leafsecret99"
    for _ in range(redaction.MAX_WALK_DEPTH + 5):
        value = [value]
    flat = repr(redaction.redact_structure(value))
    assert "leafsecret99" not in flat
    assert redaction.REDACTED in flat


def test_structure_walk_replaces_overbudget_items_instead_of_passing_them():
    value = ["password=itemsecret%d" % i for i in range(redaction.MAX_WALK_ITEMS + 10)]
    walked = redaction.redact_structure(value)
    assert not any("itemsecret" in str(item) for item in walked)


# --- the tool-output redactor ---------------------------------------------


def test_pattern_output_redactor_reports_applied():
    out = PatternOutputRedactor().redact("any_tool", {"log": "api_key=abcd1234"})
    assert isinstance(out, RedactedOutput)
    assert out.applied is True
    assert "abcd1234" not in out.value["log"]


def test_pattern_output_redactor_fails_closed_when_the_walk_fails():
    def broken(_text):
        raise RuntimeError("boom")

    out = PatternOutputRedactor(broken).redact("any_tool", "password=exposed99")
    assert out.applied is True
    assert "exposed99" not in repr(out.value)
    assert out.value == redaction.REDACTION_FAILED


# --- production composition root -------------------------------------------


def test_runtime_container_never_composes_the_identity_redactor():
    runtime = build_runtime(
        RuntimeConfig(profile="workstation-local", model_backend="ollama"),
        RuntimeCapabilities(),
    )
    composed = runtime.tools.gateway._redactor
    assert not isinstance(composed, IdentityRedactor)
    out = composed.redact("any_tool", "password=containersecret7")
    assert isinstance(out, RedactedOutput) and out.applied is True
    assert "containersecret7" not in repr(out.value)


def test_container_redactor_scrubs_live_secret_env_values(monkeypatch):
    monkeypatch.setenv("SONDER_AUTH_SECRET", "live-secret-material-42")
    runtime = build_runtime(
        RuntimeConfig(profile="workstation-local", model_backend="ollama"),
        RuntimeCapabilities(),
    )
    out = runtime.tools.gateway._redactor.redact(
        "any_tool", {"stdout": "printed live-secret-material-42 by accident"}
    )
    assert "live-secret-material-42" not in repr(out.value)


# --- quoted, escaped and delimited credential shapes (G1372, G1373) --------

_PLATFORM = platform_logging.Redactor(env={})

QUOTED_SHAPES = [
    # A serialized header dict: the closing quote sits between name and ':'.
    ('{"Authorization": "Basic dXNlcjpodW50ZXIy", "Cookie": "session=abc123"}',
     ("dXNlcjpodW50ZXIy", "abc123")),
    ("{'Authorization': 'Basic dXNlcjpodW50ZXIy'}", ("dXNlcjpodW50ZXIy",)),
    ('"Set-Cookie": "sid=s3ssionvalue"', ("s3ssionvalue",)),
    # JSON nested inside a JSON string: the quotes around key and value are escaped.
    ('{\\"password\\": \\"hunter2\\"}', ("hunter2",)),
    ('{\\"Authorization\\": \\"Basic dXNlcjpodW50ZXIy\\"}', ("dXNlcjpodW50ZXIy",)),
    # Values with delimiters, short values, and quoted values with spaces.
    ("password=ab,cdefgh", ("cdefgh",)),
    ("pwd=a;bcdefg", ("bcdefg",)),
    ('password: "correct horse battery staple"', ("horse", "battery", "staple")),
    ('password: "abc"', ("abc",)),
]


@pytest.mark.parametrize("text,secrets", QUOTED_SHAPES)
def test_quoted_escaped_and_delimited_credentials_are_redacted(text, secrets):
    for out in (redaction.redact_text(text), _PLATFORM.redact(text)):
        for secret in secrets:
            assert secret not in out, (text, out)
        assert redaction.REDACTED in out
        assert redaction.redact_text(out) == out


def test_repeat_redaction_keeps_json_delimiters_after_the_marker():
    once = redaction.redact_text('{"password": "x1", "user": "bob"}')
    assert once == '{"password": [REDACTED], "user": "bob"}'
    assert redaction.redact_text(once) == once


@pytest.mark.parametrize(
    "key",
    [
        "authToken", "dbPassword", "secretKey", "clientSecret", "x-apiKey",
        "AWSSecretKey", "openAIApiKey", "XApiKey", "HTTPAuthorization",
    ],
)
def test_sensitive_key_matches_camel_case_credential_names(key):
    assert redaction.SENSITIVE_KEY.match(key), key
    walked = redaction.redact_structure({key: "opaque-9f3k"}, sensitive_keys=True)
    assert walked == {key: redaction.REDACTED}


@pytest.mark.parametrize(
    "key", ["mypassword", "maxTokens", "tokenizer", "promptTokens", "HTTPHeaders", "APIVersion"],
)
def test_sensitive_key_camel_case_boundary_is_case_sensitive(key):
    assert not redaction.SENSITIVE_KEY.match(key), key


def test_non_string_values_under_a_sensitive_key_are_redacted_keeping_shape():
    value = {
        "token": ["opaque", ("tuple-secret",), {"inner": "nested-secret"}],
        "password": b"hunter2",
        "cookie": bytearray(b"sid=1"),
        "empty": {"secret": ""},
        "note": ["plain"],
    }
    walked = redaction.redact_structure(value, sensitive_keys=True)
    marker = redaction.REDACTED
    assert walked["token"] == [marker, (marker,), {"inner": marker}]
    assert walked["password"] == marker and walked["cookie"] == marker
    # An empty string under a sensitive key stays empty.
    assert walked["empty"] == {"secret": ""}
    assert walked["note"] == ["plain"]
    # Without key awareness nothing is replaced outright.
    assert redaction.redact_structure(value)["token"][0] == "opaque"


def test_numbers_under_a_sensitive_key_are_redacted_but_flags_and_none_stay():
    marker = redaction.REDACTED
    value = {
        # Numeric credentials directly under a sensitive key, zero included.
        "password": 12345678, "pin_token": 4242, "api_key": 3.14159, "secret": 0,
        # ... and inside containers stored under one.
        "credentials": [1234, (5678,), {"inner": 9.5}, True, None],
        # Flags and None are not credential material.
        "use_token": True, "refresh_token": None,
        # Any other non-container object is replaced whole.
        "client_secret": {"opaque", "set-secret"},
        # Numbers under ordinary keys keep passing through.
        "count": 2, "maxTokens": 4096, "ratio": 0.5, "enabled": False,
    }
    walked = redaction.redact_structure(value, sensitive_keys=True)
    assert walked == {
        "password": marker, "pin_token": marker, "api_key": marker, "secret": marker,
        "credentials": [marker, (marker,), {"inner": marker}, True, None],
        "use_token": True, "refresh_token": None,
        "client_secret": marker,
        "count": 2, "maxTokens": 4096, "ratio": 0.5, "enabled": False,
    }
    # Without key awareness numbers are not replaced.
    assert redaction.redact_structure(value)["password"] == 12345678

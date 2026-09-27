"""Shipped guidance must match the remote-worker validator.

config.py and ollama_pool.validate_worker_origin reject every non-loopback
``http://`` worker regardless of ``trusted_origins``
(tests/test_ollama_pool.py::test_trusted_origins_never_relaxes_https_requirement).
The packaged example and runbooks used to promise the opposite and steer
operators at the raw, unauthenticated :11434 port.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDANCE = (
    "packaging/sonder.toml.example",
    "docs/runbooks/secure-remote-access.md",
    "docs/wiki/02-getting-started.md",
)
_REMOTE_HTTP_WORKER = re.compile(
    r"http://(?!127\.0\.0\.1|localhost|\[::1\])[^\s\"']+:11434"
)


@pytest.mark.parametrize("rel", GUIDANCE)
def test_guidance_never_offers_a_remote_plain_http_worker(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert not _REMOTE_HTTP_WORKER.search(text), rel
    assert "HTTP (non-TLS)" not in text, rel
    assert "HTTP workers are allowed" not in text, rel


def test_example_worker_is_accepted_by_the_config_validator():
    from sonder_runtime.adapters.inference.ollama_pool import validate_worker_origin

    text = (ROOT / "packaging/sonder.toml.example").read_text(encoding="utf-8")
    match = re.search(r'^# workers = \["([^"]+)"\]', text, flags=re.M)
    assert match, "example worker line missing"
    validate_worker_origin(match.group(1), allow_remote=True)

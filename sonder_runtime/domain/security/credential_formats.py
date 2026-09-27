"""Known credential formats, shared by every privacy boundary that needs them.

One definition feeds both the contribution privacy classifier
(``contribute.PRIVATE_RULES``: a lesson carrying one of these is never shared),
the canonical domain redaction patterns, and the log/durable-sink
:class:`~sonder_runtime.platform.logging.Redactor` (a free-standing provider
key in a log line or a captured session event is replaced). Keeping one list means a newly recognised provider format closes
both boundaries at once instead of drifting between two regex copies.

These are *shape* matches for vendor-issued secrets with a distinctive
prefix. They are deliberately anchored on the prefix so ordinary prose and
plain hex digests (session hashes, content digests) are never rewritten.

Pure ``re`` only. It is part of the canonical domain redaction set
(``domain.security.redaction.PATTERNS``); ``platform.logging`` imports this
one module through an exact, reviewed exception in
``scripts/check_architecture.py`` so the platform redactor reuses the objects
instead of carrying a copy.
"""
from __future__ import annotations

import re

# Provider-issued API keys and tokens with a recognisable prefix.
KNOWN_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"glpat-[A-Za-z0-9_-]{12,}|hf_[A-Za-z0-9]{12,}|"
    r"npm_[A-Za-z0-9]{12,}|pypi-[A-Za-z0-9_-]{16,}|"
    r"ya29\.[A-Za-z0-9_-]{12,}|"
    r"A(?:KI|SI)A[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|"
    r"[rs]k_(?:live|test)_[A-Za-z0-9]{16,}|GOCSPX-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,})"
)

# A compact JWS/JWT: three base64url segments, the header starting ``eyJ``.
JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)

CREDENTIAL_FORMATS: tuple[re.Pattern[str], ...] = (KNOWN_CREDENTIAL, JWT)

__all__ = ["CREDENTIAL_FORMATS", "JWT", "KNOWN_CREDENTIAL"]

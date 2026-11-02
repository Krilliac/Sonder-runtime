"""Secret guard: the egress boundary for credentials in ``mesh_generate``.

The model is local, so the only route by which a secret can leave the machine is text that
comes back from a build (error strings, stdout/stderr excerpts, critic text, object/mesh/material
names, GLB/FBX string metadata) and then travels in the tool result to the remote caller or into
a log.  This module decides whether such text contains a secret.  It is pure (stdlib only): the
secrets themselves are passed in by ``adapters/meshgen/secret_sources.py``, so tests can inject
canaries.

A secret is any entry of the supplied ``name -> value`` mapping (or iterable of pairs) whose NAME contains KEY, TOKEN,
SECRET, PASS, PWD, AUTH, CRED, SESSION, COOKIE or PRIVATE, or whose VALUE looks like a credential,
unless its name is listed in the allow list (``SONDER_MESHGEN_SECRET_ALLOW``, comma separated,
empty by default).  Values shorter than ``MIN_SECRET_CHARS`` are ignored.  Besides the value
itself the guard matches its base64 (all three byte alignments, standard and URL-safe), hex,
reversed, URL-encoded and lowercased forms.  Independently of the set, text that merely has the
SHAPE of a credential (``sk-ant-``, ``ghp_``, ``AKIA``, PEM headers, JWTs, long high-entropy
strings ...) is a hit unless it is part of an allowed value.

Limits, stated plainly: this is pattern matching.  An exotic encoding (a custom cipher, splitting
a value across several fields, an image of the text) is not detected; secrets in vertex data stay
on this machine because vertex data is not scanned or returned.

Nothing here ever returns, logs or raises with a matched name or value.
"""
from __future__ import annotations

import base64
import json
import math
import re
import struct

REDACTED = "[REDACTED:secret]"
MIN_SECRET_CHARS = 12
MIN_NEEDLE_CHARS = 12

_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASS|PWD|AUTH|CRED|SESSION|COOKIE|PRIVATE", re.IGNORECASE)
# Shell working-directory variables match "PWD" but hold a path, not a secret.
_NOT_SECRET_NAMES = frozenset({"pwd", "oldpwd"})

_SHAPES = tuple(re.compile(pattern) for pattern in (
    r"sk-ant-[A-Za-z0-9_-]{8,}",
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}",
    r"(?<![A-Za-z0-9])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}",
    r"github_pat_[A-Za-z0-9_]{16,}",
    r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])xox[bpas]-[A-Za-z0-9-]{8,}",
    r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{30,}",
    r"-----BEGIN",
    r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*",
    r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{20,}",
))
_TOKEN_RE = re.compile(r"[A-Za-z0-9+_=-]{32,}")  # "/" separates: a long path is not a token
_HEX_RE = re.compile(r"[0-9A-Fa-f-]+\Z")
_PRINTABLE_RE = re.compile(rb"[\x20-\x7e]{8,}")


def _entropy(text: str) -> float:
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    total = len(text)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _high_entropy_token(token: str) -> bool:
    """A 32+ character run that is mixed-case, has digits and is not a hex id (uuid, md5...)."""
    if _HEX_RE.match(token):
        return False
    if not (any(c.isupper() for c in token) and any(c.islower() for c in token) and any(c.isdigit() for c in token)):
        return False
    return _entropy(token) >= 3.5


def _shape_spans(text: str):
    for pattern in _SHAPES:
        for match in pattern.finditer(text):
            yield match.group()
    for match in _TOKEN_RE.finditer(text):
        if _high_entropy_token(match.group()):
            yield match.group()


_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~")


def _percent(value: str, plus: bool) -> str:
    """URL percent-encoding of ``value`` (``plus``: a space becomes ``+``); everything but RFC 3986 unreserved is encoded."""
    out = []
    for byte in value.encode("utf-8"):
        if byte in _UNRESERVED:
            out.append(chr(byte))
        elif plus and byte == 0x20:
            out.append("+")
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def _forms(value: str) -> tuple[set[str], set[str]]:
    """Needles for ``value``: ``(case_sensitive, matched_against_lowercased_text)``."""
    raw = value.encode("utf-8")
    exact = {value, value[::-1], _percent(value, False), _percent(value, True)}
    urlsafe = str.maketrans("+/", "-_")
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + raw).decode("ascii")
        start = (0, 2, 3)[offset]
        piece = encoded[start:(offset + len(raw)) // 3 * 4]
        exact.add(piece)
        exact.add(piece.translate(urlsafe))
    padded = base64.b64encode(raw).decode("ascii")
    exact.update({padded, padded.rstrip("="), padded.translate(urlsafe), padded.translate(urlsafe).rstrip("=")})
    lowered = {value.lower(), raw.hex(), raw[::-1].hex(), _percent(value, False), _percent(value, True)}
    return ({n for n in exact if len(n) >= MIN_NEEDLE_CHARS},
            {n.lower() for n in lowered if len(n) >= MIN_NEEDLE_CHARS})


class SecretGuard:
    """Decides whether text carries a secret.  Immutable after construction; safe to share across threads."""

    def __init__(self, entries=None, allow=()):
        allowed = {str(name).strip().lower() for name in allow if str(name).strip()}
        exempt: list[str] = []
        secrets: list[str] = []
        pairs = entries.items() if hasattr(entries, "items") else (entries or ())
        for name, value in pairs:
            if not isinstance(value, str) or len(value) < MIN_SECRET_CHARS:
                continue
            lowered = str(name).lower()
            if lowered in allowed:
                exempt.append(value)
            elif lowered in _NOT_SECRET_NAMES:
                continue
            elif _NAME_RE.search(str(name)) or any(True for _ in _shape_spans(value)):
                secrets.append(value)
        exempt_forms: set[str] = set()
        for value in exempt:
            more_exact, more_folded = _forms(value)
            exempt_forms |= more_exact | more_folded | {value, value.lower()}
        self._exempt = tuple(sorted(exempt_forms))  # an authorised value is exempt in every encoding
        exact: set[str] = set()
        folded: set[str] = set()
        for value in secrets:
            more_exact, more_folded = _forms(value)
            exact |= more_exact
            folded |= more_folded
        self._exact = tuple(sorted(exact))
        self._folded = tuple(sorted(folded))
        self.secret_count = len(secrets)

    # ----------------------------------------------------------------- text
    def hit(self, text) -> bool:
        """True when ``text`` contains a secret from the set or has the shape of a credential."""
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        if not text:
            return False
        lowered = text.lower()
        if any(needle in text for needle in self._exact) or any(needle in lowered for needle in self._folded):
            return True
        for span in _shape_spans(text):
            lowered_span = span.lower()
            if not any(span in value or value in span or lowered_span in value or value in lowered_span
                       for value in self._exempt):
                return True
        return False

    def redact(self, text) -> tuple[str, bool]:
        """``(text, False)`` when clean, ``(REDACTED, True)`` when it matched.  The whole field is replaced."""
        value = text if isinstance(text, str) else ("" if text is None else str(text))
        if self.hit(value):
            return REDACTED, True
        return value, False

    # ----------------------------------------------------------------- structures
    def scrub(self, value):
        """Copy of a JSON-like structure with every matching string (and dict key) redacted.

        Returns ``(clean, hits)``.  Non-string leaves pass through; bytes are checked as latin-1.
        """
        hits = 0

        def walk(item):
            nonlocal hits
            if isinstance(item, str):
                if self.hit(item):
                    hits += 1
                    return REDACTED
                return item
            if isinstance(item, bytes):
                if self.hit(item.decode("latin-1")):
                    hits += 1
                    return REDACTED
                return item
            if isinstance(item, dict):
                clean = {}
                for key, sub in item.items():
                    if isinstance(key, str) and self.hit(key):
                        hits += 1
                        key = "%s_%d" % (REDACTED, hits)
                    clean[key] = walk(sub)
                return clean
            if isinstance(item, (list, tuple)):
                return [walk(sub) for sub in item]
            return item

        return walk(value), hits

    def contains_in(self, value) -> bool:
        return self.scrub(value)[1] > 0

    # ----------------------------------------------------------------- exported model files
    def glb_hit(self, data: bytes) -> bool:
        """True when a GLB's JSON chunk (names, extras, asset ...) holds a secret; malformed -> True (fail closed)."""
        if len(data) < 20 or data[:4] != b"glTF":
            return True
        length, kind = struct.unpack("<II", data[12:20])
        if kind != 0x4E4F534A or 20 + length > len(data):
            return True
        try:
            document = json.loads(data[20:20 + length].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return True
        return self.contains_in(document)

    def binary_hit(self, data: bytes) -> bool:
        """Scan the printable runs of a binary file (FBX) for a secret."""
        return any(self.hit(run.decode("ascii")) for run in _PRINTABLE_RE.findall(data))

    def model_hit(self, path) -> bool:
        """Scan one exported model file; unreadable -> True (fail closed)."""
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return True
        return self.glb_hit(data) if str(path).lower().endswith(".glb") else self.binary_hit(data)


class SecretGuardUnavailable(RuntimeError):
    """The secret sources could not be read; the run is refused (fail closed)."""


__all__ = ["MIN_SECRET_CHARS", "REDACTED", "SecretGuard", "SecretGuardUnavailable"]

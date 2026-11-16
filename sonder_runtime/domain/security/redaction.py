"""Canonical secret-shape redaction patterns. Pure: stdlib only, no I/O.

This is the single home for the pattern set that scrubs credential shapes
out of arbitrary text. The runtime grew at least nine independent redaction
implementations; every new caller should compose this module instead of
writing a tenth. ``platform.logging.Redactor`` (the logging/error path) and
``application.tools.facade.PatternOutputRedactor`` (the tool-output path)
both delegate here, so the answer to "is this shape scrubbed?" no longer
depends on which path a secret takes.

The functions are deliberately total over their documented inputs and carry
no environment access: collecting live secret *values* (e.g. from
``SECRET_ENV_VARS``) is a platform concern and stays in the platform layer.
"""
from __future__ import annotations

import re
from typing import Iterable

from sonder_runtime.domain.security.credential_formats import REDACTION_FORMATS

REDACTED = "[REDACTED]"
REDACTION_FAILED = "[REDACTION_FAILED]"
WORKSPACE_LABEL = "[WORKSPACE]"

# A PEM private-key block. Do not call ``_PEM.sub`` on untrusted text: with
# many BEGIN headers and no END after them it is quadratic, because each
# header scans lazily to the end of the text. Go through ``_sub_pem``.
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# URL userinfo, ``scheme://user:pass@``. The scheme run is matched whole from
# its first character, one attempt per run, so the scan is linear. The older
# ``\b([a-z][a-z0-9+.-]*://)`` form retried the run from every inner word
# boundary and was quadratic on ``a.a.a...``. Go through ``_sub_url_userinfo``,
# which redacts only where that older form matched: a wider match here would
# consume a ``?password=`` the query-parameter rule after it must see.
_URL_USERINFO = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z0-9+.-]+)://([^/@\s:]+:[^/@\s]+)@")
# The same shape for any scheme run, applied after every other rule.
_URL_USERINFO_ANY = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z0-9+.-]+://)([^/@\s:]+:[^/@\s]+)@")
_SCHEME_LETTER = re.compile(r"(?i)[a-z]")
_WORD_CHAR = re.compile(r"\w")


def _older_scheme_start(text, start, run):
    r"""Whether ``\b[a-z]`` (the older anchor) holds anywhere in ``run``.

    ``run`` begins at ``text[start]``. Inside the run the only non-word
    characters are ``+``, ``.`` and ``-``, so a word boundary before a letter
    falls either at the run start (after a non-word character or the start of
    the text) or right after one of those three.
    """
    if _SCHEME_LETTER.match(run[0]) and not (start and _WORD_CHAR.match(text[start - 1])):
        return True
    return any(
        run[i - 1] in "+.-" and _SCHEME_LETTER.match(run[i]) for i in range(1, len(run))
    )


_SCHEME_RUN = re.compile(r"(?i)[a-z0-9+.-]+")
_USERINFO_NAME = re.compile(r"[^/@\s:]+")
_USERINFO_SECRET = re.compile(r"[^/@\s]+")


def _sub_url_userinfo(text):
    """Redact URL userinfo where the older anchor matched, in linear time.

    Scans scheme runs by hand instead of ``_URL_USERINFO.sub``: the run is a
    maximal ``[a-z0-9+.-]`` run (the pattern's lookbehind), ``://`` must follow
    it directly, then a maximal userinfo name, ``:``, a maximal secret and
    ``@``. None of those pieces can backtrack into a shorter one, so one pass
    over the text suffices. The older match started at some letter inside the
    same scheme run, so it ended at the same ``@`` and kept the run before it;
    keeping the whole run gives the same text. A match skipped here holds no
    other candidate: the userinfo cannot contain ``://``.
    """
    out = []
    copied = pos = 0
    while True:
        run = _SCHEME_RUN.search(text, pos)
        if run is None:
            break
        pos = run.end()
        if not text.startswith("://", pos):
            continue
        name = _USERINFO_NAME.match(text, pos + 3)
        if name is None or not text.startswith(":", name.end()):
            continue
        secret = _USERINFO_SECRET.match(text, name.end() + 1)
        if secret is None or not text.startswith("@", secret.end()):
            continue
        pos = secret.end() + 1
        if _older_scheme_start(text, run.start(), run.group()):
            out.append(text[copied:run.start()])
            out.append(run.group() + "://" + REDACTED)
            copied = pos
    out.append(text[copied:])
    return "".join(out)


# Ordering note: value replacement happens before pattern replacement in
# redact_text, so a known secret value is scrubbed even where no pattern
# would match its surroundings.
PATTERNS: tuple[re.Pattern, ...] = (
    # A prior pass replaced the complete credential with this single marker.
    # Repeated redaction must not consume the ordinary word following it.
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)(\[REDACTED\](?=\s|$)|\S+(?:\s+\S+)?)"),
    re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)([^\r\n]+)"),
    re.compile(r"(?i)\b(bearer\s+)([a-z0-9._~+/=-]{8,})"),
    re.compile(
        r"(?i)([\"']?(?:api[-_]?key|auth[-_]?secret|secret|token|password|"
        r"passwd|credential|\b(?:pwd|(?:aws[-_])?secret[-_]access[-_]key))"
        r"[\"']?\s*[:=]\s*)"
        r"([\"']?[^\s\"',;}{]{4,}[\"']?)"
    ),
    _URL_USERINFO,
    re.compile(
        r"(?i)([?&](?:access[-_]?token|api[-_]?key|auth[-_]?secret|"
        r"password|credential)=)([^&#\s]+)"
    ),
    _PEM,
    # Free-standing provider keys (AKIA..., ghp_..., sk-..., JWTs), shared
    # with the contribution privacy classifier, then runs of adjacent AWS keys.
    *REDACTION_FORMATS,
    # URL userinfo after a scheme run the rule above skips (``1http://``,
    # ``backup_s3://``). Last, so it cannot take text an earlier rule needs.
    _URL_USERINFO_ANY,
)

# Upper bound on pattern passes over one text. A substitution can expose a
# match for the next pass (``AKIA...Cookie: a=b`` needs two), so the passes
# repeat until one changes nothing. Ordinary text settles in one or two, and
# 420k random hostile texts never needed more than six changing passes. No
# finite bound covers every input, though: each ``x'`` unit after
# ``token='abcd'`` costs one more pass. A text cut off at the bound is still
# safe. A pass only replaces matched text with the marker and never restores
# a character, so later passes can only remove more, and the result never
# keeps anything the first pass removed. What is lost at the bound is
# idempotence for that text only: a second call redacts further.
_MAX_PASSES = 8

_PEM_BEGIN_MARK = "-----BEGIN "
_PEM_END_MARK = "-----END "
_PEM_LABEL = re.compile(r"[A-Z ]*")
_PEM_KEY = "PRIVATE KEY"


def _pem_marker_end(text, start):
    """End of ``[A-Z ]*PRIVATE KEY-----`` at ``start``, or -1.

    The label run stops at the first ``-``, so it is maximal and must itself
    end in ``PRIVATE KEY`` right before the five dashes.
    """
    label = _PEM_LABEL.match(text, start).end()
    if (
        label - start >= len(_PEM_KEY)
        and text.startswith(_PEM_KEY, label - len(_PEM_KEY))
        and text.startswith("-----", label)
    ):
        return label + 5
    return -1


def _sub_pem(text):
    """Same result as ``_PEM.sub(REDACTED, text)``, in linear time.

    Scans by hand: each BEGIN header takes the first END marker at or after
    its end (the lazy ``.*?``), and the scan resumes after that marker. A
    header with no END marker after it ends the scan, because every later
    header ends further on and would find none either.
    """
    out = []
    copied = pos = 0
    while True:
        begin = text.find(_PEM_BEGIN_MARK, pos)
        if begin < 0:
            break
        header_end = _pem_marker_end(text, begin + len(_PEM_BEGIN_MARK))
        if header_end < 0:
            pos = begin + 1
            continue
        end = -1
        probe = header_end
        while end < 0:
            marker = text.find(_PEM_END_MARK, probe)
            if marker < 0:
                break
            end = _pem_marker_end(text, marker + len(_PEM_END_MARK))
            probe = marker + 1
        if end < 0:
            break
        out.append(text[copied:begin])
        out.append(REDACTED)
        copied = pos = end
    out.append(text[copied:])
    return "".join(out)


def _apply_patterns(text):
    for pattern in PATTERNS:
        if pattern is _PEM:
            text = _sub_pem(text)
        elif pattern is _URL_USERINFO:
            text = _sub_url_userinfo(text)
        elif pattern.groups >= 2:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact_text(
    text: str,
    *,
    secret_values: Iterable[str] = (),
    path_prefixes: Iterable[str] = (),
) -> str:
    """Scrub known secret values, credential shapes, and path prefixes.

    ``secret_values`` are replaced longest-first so an overlapping shorter
    value cannot split a longer one into recognizable halves. Pure function:
    any failure propagates to the caller, which owns the fail-closed
    (``REDACTION_FAILED``) decision.
    """
    for value in sorted({v for v in secret_values if v}, key=len, reverse=True):
        if value in text:
            text = text.replace(value, REDACTED)
    # A substitution can create a new word boundary (``[REDACTED]Cookie: a=b``),
    # so repeat the pattern passes until the text is stable (see _MAX_PASSES).
    for _ in range(_MAX_PASSES):
        before = text
        text = _apply_patterns(text)
        if text == before:
            break
    for prefix in path_prefixes:
        if prefix and prefix in text:
            text = text.replace(prefix, WORKSPACE_LABEL)
    return text


# Structure-walk bounds. A redactor that recurses without limits turns a
# hostile deeply-nested output into a crash inside the safety layer itself.
MAX_WALK_DEPTH = 32
MAX_WALK_ITEMS = 10_000


# A mapping key that names a credential. Its string value is replaced whole
# when a walk asks for key awareness: ``{"password": "hunter2"}`` carries no
# shape a text pattern can see once the key and value are separate objects.
SENSITIVE_KEY = re.compile(
    r"(?i)^(?:.*[_-])?(?:api[_-]?key|auth[_-]?secret|client[_-]?secret|secret|"
    r"token|access[_-]?token|refresh[_-]?token|password|passwd|pwd|credentials?|"
    r"secret[_-]access[_-]key|"
    r"authorization|cookie|private[_-]?key)$"
)


def redact_structure(value, redact=None, *, sensitive_keys=False, _depth=0, _budget=None):
    """Redact every string inside a JSON-shaped structure, preserving shape.

    ``redact`` is a ``str -> str`` callable (defaults to :func:`redact_text`
    with patterns only). Dict keys, numbers, bools, and None pass through
    unchanged. With ``sensitive_keys=True`` a non-empty string stored under a
    key matching :data:`SENSITIVE_KEY` is replaced with :data:`REDACTED`
    outright. Beyond :data:`MAX_WALK_DEPTH` or :data:`MAX_WALK_ITEMS` the
    remaining subtree is replaced with :data:`REDACTED` rather than returned
    unexamined: when the walker must stop checking, it must not pass content
    it has stopped checking.
    """
    if redact is None:
        redact = redact_text
    if _budget is None:
        _budget = [MAX_WALK_ITEMS]
    _budget[0] -= 1
    if _depth > MAX_WALK_DEPTH or _budget[0] < 0:
        return REDACTED
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if sensitive_keys and isinstance(key, str) and isinstance(item, str)
                and item and SENSITIVE_KEY.match(key)
                else redact_structure(
                    item, redact, sensitive_keys=sensitive_keys,
                    _depth=_depth + 1, _budget=_budget,
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        walked = [
            redact_structure(
                item, redact, sensitive_keys=sensitive_keys,
                _depth=_depth + 1, _budget=_budget,
            )
            for item in value
        ]
        return tuple(walked) if isinstance(value, tuple) else walked
    return value

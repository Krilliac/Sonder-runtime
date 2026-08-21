"""Verified binary artifact download and on-disk artifact verification.

Sonder could inspect binaries (``artifact_risk_inspect``), hash them
(``file_digest``), and scan them (``secret_scan``) -- but it could not *get*
one. ``web_fetch`` returns readable text only, so every driver, installer, and
ISO had to be pulled by shelling out to ``curl.exe``, outside policy gating,
activity logging, and every verification primitive this runtime already ships.

This module closes that gap, and it is deliberately paranoid because each of
the following actually happened while acquiring a Windows ISO and four driver
installers by hand:

1. ``drivers.amd.com`` answered a HEAD probe with **HTTP 200** while serving a
   10-byte body -- a 404 wearing a success status. Checking the status code is
   not checking the payload, so :func:`fetch_artifact` checks magic bytes and a
   per-type size floor.
2. A ``curl`` invocation **exited 0 and wrote a 0-byte file**. Success by exit
   code, garbage on disk. Nothing here reports success without having hashed
   bytes it actually wrote.
3. MSI's Akamai edge returned an **"Access Denied" HTML block page** and the
   fetch layer handed it back as though it were the requested content. Treating
   a denial as data is worse than a 404 because it looks like it worked, so
   :func:`detect_block_page` names the denial explicitly and refuses to write.
4. An NVIDIA lookup produced a **Tesla data-center driver whose version string
   matched the GeForce one**. A version number is not an identity, so the
   Authenticode signer subject -- not the filename or the version -- is what
   ``expect_publisher`` matches against.

Every fetch streams to a partial file beside the destination and is renamed
into place only after the whole battery passes, so a failed fetch never leaves
a plausible-looking artifact at the path a later step would trust. Each success
writes ``<dest>.provenance.json``: weeks later that sidecar is the only thing
that says whether a staged installer can be trusted.

Stdlib only, plus ``web_tools`` for its address-pinned SSRF-safe opener and
``file_ops`` for root containment.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.adapters.artifact_digest import file_sha256


def _web_tools():
    """Load the legacy network policy module behind the adapter boundary."""
    return importlib.import_module("web_tools")


MAX_REDIRECTS = 8
DEFAULT_MAX_MB = 8192
MAX_MB_CEILING = 65536
DEFAULT_TIMEOUT = 600.0
MAX_TIMEOUT = 3600.0
SNIFF_BYTES = 65536
STREAM_CHUNK = 262144
PROVENANCE_SUFFIX = ".provenance.json"
PROVENANCE_SCHEMA = 1

# A signature is (offset, magic bytes, type name). Order matters only in that
# the first match wins, so longer/more specific magics are listed first.
MAGIC_SIGNATURES = (
    (0, b"\x7fELF", "elf"),
    (0, b"MZ", "pe"),
    (0, b"PK\x03\x04", "zip"),
    (0, b"PK\x05\x06", "zip"),
    (0, b"PK\x07\x08", "zip"),
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "msi"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"BZh", "bzip2"),
    (0, b"\x28\xb5\x2f\xfd", "zstd"),
    (0, b"MSCF", "cab"),
    (0, b"\xed\xab\xee\xdb", "rpm"),
    (0, b"!<arch>\n", "deb"),
    (0, b"%PDF-", "pdf"),
    (0, b"\xca\xfe\xba\xbe", "macho"),
    (0, b"\xcf\xfa\xed\xfe", "macho"),
    (0, b"\xce\xfa\xed\xfe", "macho"),
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\xff\xd8\xff", "jpeg"),
    (0, b"OggS", "ogg"),
    (0, b"RIFF", "riff"),
    (257, b"ustar", "tar"),
    # ISO 9660 stamps "CD001" at the start of the primary volume descriptor,
    # which lives in sector 16 of a 2048-byte-sector image: 16*2048 + 1.
    (0x8001, b"CD001", "iso"),
    (0x8801, b"CD001", "iso"),
    (0x9001, b"CD001", "iso"),
    (0, b"UDF\x02", "iso"),
)

# Extension -> the payload types that extension may legitimately carry.
EXTENSION_TYPES = {
    ".exe": ("pe",),
    ".dll": ("pe",),
    ".sys": ("pe",),
    ".ocx": ("pe",),
    ".scr": ("pe",),
    ".efi": ("pe",),
    ".msi": ("msi",),
    ".msp": ("msi",),
    ".msu": ("cab",),
    ".cab": ("cab",),
    ".iso": ("iso",),
    ".img": ("iso",),
    ".zip": ("zip",),
    ".whl": ("zip",),
    ".jar": ("zip",),
    ".apk": ("zip",),
    ".nupkg": ("zip",),
    ".vsix": ("zip",),
    ".docx": ("zip",),
    ".xlsx": ("zip",),
    ".pptx": ("zip",),
    ".7z": ("7z",),
    ".gz": ("gzip",),
    ".tgz": ("gzip",),
    ".xz": ("xz",),
    ".bz2": ("bzip2",),
    ".zst": ("zstd",),
    ".tar": ("tar",),
    ".rpm": ("rpm",),
    ".deb": ("deb",),
    ".pdf": ("pdf",),
    ".so": ("elf",),
    ".elf": ("elf",),
    ".dmg": ("zip", "iso"),
    ".png": ("png",),
    ".jpg": ("jpeg",),
    ".jpeg": ("jpeg",),
}

# Aliases so callers can say what they mean rather than what the magic is.
TYPE_ALIASES = {
    "exe": "pe",
    "dll": "pe",
    "binary": "pe",
    "executable": "pe",
    "iso9660": "iso",
    "installer": "pe",
    "archive": "zip",
    "tarball": "tar",
    "bz2": "bzip2",
    "gz": "gzip",
}

# Smallest plausible payload per type. Failure #1 shipped a 10-byte "exe";
# these floors are what make that arithmetically impossible to accept.
TYPE_MIN_BYTES = {
    "pe": 1024,
    "elf": 1024,
    "macho": 1024,
    "msi": 1536,
    "iso": 0x8006,
    "cab": 36,
    "zip": 22,
    "7z": 32,
    "gzip": 18,
    "xz": 32,
    "bzip2": 14,
    "zstd": 13,
    "tar": 1024,
    "rpm": 96,
    "deb": 68,
    "pdf": 64,
}
MIN_ANY_BYTES = 16

# Types where an Authenticode signature is meaningful on Windows.
SIGNABLE_TYPES = frozenset({"pe", "msi", "cab"})

_TEXTY_TYPES = frozenset({"", "text", "html", "xml", "json", "javascript"})
# --- block-page detection -------------------------------------------------
#
# Split by how much evidence each marker carries, because "captcha" appears in
# plenty of legitimate documentation and a detector that cries wolf gets turned
# off. STRONG markers are vendor challenge/denial infrastructure that never
# appears in ordinary prose; TITLE markers are conclusive when they sit in the
# page's own identity; WEAK markers only count on a body too small to be real
# content.

_STRONG_MARKERS = (
    ("errors.edgesuite.net", "Akamai edge denial page"),
    ("reference #18.", "Akamai edge reference denial"),
    ("/cdn-cgi/challenge-platform", "Cloudflare challenge platform"),
    ("cf-browser-verification", "Cloudflare browser verification"),
    ("__cf_chl", "Cloudflare challenge script"),
    ("attention required! | cloudflare", "Cloudflare 'Attention Required' block"),
    ("_incapsula_resource", "Imperva/Incapsula block"),
    ("incapsula incident id", "Imperva/Incapsula incident page"),
    ("request unsuccessful. incapsula", "Imperva/Incapsula block"),
    ("perimeterx", "PerimeterX bot block"),
    ("px-captcha", "PerimeterX captcha"),
    ("distil_r_captcha", "Distil Networks captcha"),
    ("g-recaptcha", "reCAPTCHA challenge widget"),
    ("h-captcha", "hCaptcha challenge widget"),
    ("cf-turnstile", "Cloudflare Turnstile challenge widget"),
    ("challenges.cloudflare.com/turnstile", "Cloudflare Turnstile challenge"),
    ("generated by cloudfront", "CloudFront denial page"),
    ("<title>access denied</title>", "Access Denied page"),
    ("error 1020", "Cloudflare firewall rule block"),
    ("error 1015", "Cloudflare rate-limit block"),
    ("akamaighost", "Akamai ghost denial page"),
)

_TITLE_MARKERS = (
    ("access denied", "Access Denied page"),
    ("403 forbidden", "HTTP 403 denial page"),
    ("attention required", "Cloudflare 'Attention Required' block"),
    ("just a moment", "Cloudflare interstitial challenge"),
    ("checking your browser", "bot-check interstitial"),
    ("are you a robot", "bot-check interstitial"),
    ("are you a human", "bot-check interstitial"),
    ("verify you are human", "human-verification challenge"),
    ("bot verification", "bot-verification challenge"),
    ("security check", "security-check interstitial"),
    ("unusual traffic", "automated-traffic denial"),
    ("request blocked", "request-blocked page"),
    ("access to this page has been denied", "bot-block denial page"),
    ("captcha", "captcha challenge page"),
)

_WEAK_MARKERS = (
    ("access denied", "Access Denied body"),
    ("you don't have permission to access", "permission denial body"),
    ("you do not have permission to access", "permission denial body"),
    ("request blocked", "request-blocked body"),
    ("automated queries", "automated-query denial body"),
    ("enable javascript and cookies to continue", "JS/cookie challenge body"),
    ("recaptcha", "reCAPTCHA challenge body"),
    ("hcaptcha", "hCaptcha challenge body"),
    ("captcha", "captcha challenge body"),
)
_WEAK_MAX_CHARS = 8192

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]{0,400}>")
_HTML_HEAD_RE = re.compile(
    rb"^\s*(?:\xef\xbb\xbf)?\s*(?:<!doctype\s+html|<html\b|<\?xml\b|<head\b|<body\b)",
    re.IGNORECASE,
)


class ArtifactFetchError(ValueError):
    """Raised for an input this module refuses to act on at all.

    Verification *verdicts* are never raised -- they come back as a result dict
    with ``ok=False`` and a populated ``failures`` list, because a rejected
    download is an answer, not a crash.
    """


# --- small helpers --------------------------------------------------------


def _clamp(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(number, high))


def _normalize_type(name):
    text = str(name or "").strip().lower().lstrip(".")
    return TYPE_ALIASES.get(text, text)


def _document_kind(content_type):
    value = str(content_type or "").split(";", 1)[0].strip().lower()
    if not value:
        return ""
    if value in ("text/html", "application/xhtml+xml"):
        return "html"
    if value in ("text/xml", "application/xml"):
        return "xml"
    if value == "application/json":
        return "json"
    if value.startswith("text/"):
        return "text"
    return "binary"


def _looks_like_markup(raw):
    return bool(_HTML_HEAD_RE.match(raw[:512]))


def _as_text(body, limit=SNIFF_BYTES):
    if isinstance(body, bytes):
        return body[:limit].decode("utf-8", "replace")
    return str(body or "")[:limit]


def _page_title(text):
    match = _TITLE_RE.search(text)
    if match:
        return " ".join(_TAG_RE.sub(" ", match.group(1)).split()).lower()
    # Tag-stripped text (what web_fetch hands back) has no <title>; the page's
    # own heading is the first thing in it, so the head of the text is the
    # closest available stand-in for the page's identity.
    stripped = _TAG_RE.sub(" ", text[:1200])
    return " ".join(stripped.split())[:400].lower()


def detect_block_page(body, *, content_type="", status=None, url=""):
    """Name the denial when a response is the site refusing, not the content.

    Returns ``None`` when the body looks like real content, or a dict with
    ``marker``/``reason``/``confidence``/``evidence`` when the response is a
    bot-block, captcha interstitial, or access-denied page. This is the check
    that stops a *denial* being consumed as *data* -- the failure mode that is
    worse than a 404 precisely because the transport reports success.
    """
    raw = body if isinstance(body, bytes) else str(body or "").encode(
        "utf-8", "replace"
    )
    text = _as_text(body)
    kind = _document_kind(content_type)
    if kind == "binary" and not _looks_like_markup(raw):
        # A genuine binary payload can contain any byte sequence by chance;
        # scanning it for English phrases would only manufacture noise.
        return None

    lowered = text.lower()
    for marker, reason in _STRONG_MARKERS:
        if marker in lowered:
            return {
                "marker": marker,
                "reason": reason,
                "confidence": "strong",
                "evidence": _evidence(text, marker),
                "status": status,
                "url": url,
            }

    title = _page_title(text)
    for marker, reason in _TITLE_MARKERS:
        if marker in title:
            return {
                "marker": marker,
                "reason": "%s (page title)" % reason,
                "confidence": "strong",
                "evidence": title[:200],
                "status": status,
                "url": url,
            }

    if len(text) <= _WEAK_MAX_CHARS:
        for marker, reason in _WEAK_MARKERS:
            if marker in lowered:
                return {
                    "marker": marker,
                    "reason": "%s on a %d-byte response" % (reason, len(raw)),
                    "confidence": "weak",
                    "evidence": _evidence(text, marker),
                    "status": status,
                    "url": url,
                }

    if isinstance(status, int) and status in (401, 403, 407, 429):
        return {
            "marker": "http_%d" % status,
            "reason": "HTTP %d denial served as a page body" % status,
            "confidence": "strong",
            "evidence": text[:200].strip(),
            "status": status,
            "url": url,
        }
    return None


def _evidence(text, marker):
    lowered = text.lower()
    index = lowered.find(marker)
    if index < 0:
        return text[:160].strip()
    start = max(0, index - 60)
    excerpt = _TAG_RE.sub(" ", text[start:index + len(marker) + 100])
    return " ".join(excerpt.split())[:200]


def format_block_notice(url, block):
    """Render a block-page verdict as prose that cannot be mistaken for content."""
    lines = [
        "BLOCKED: the site returned a bot-block/denial page, not the requested content.",
        "  url: %s" % (url or block.get("url") or "(unknown)"),
        "  signal: %s" % block.get("reason", "block page"),
        "  matched: %s (%s confidence)" % (
            block.get("marker", "?"), block.get("confidence", "?"),
        ),
    ]
    if block.get("status") is not None:
        lines.append("  http status: %s" % block.get("status"))
    evidence = str(block.get("evidence") or "").strip()
    if evidence:
        lines.append("  page said: %s" % evidence)
    lines.append(
        "Treat this as a refusal, not as data. The text above is the denial "
        "page itself; nothing from this URL was retrieved."
    )
    return "\n".join(lines)


# --- payload identity -----------------------------------------------------


def detect_type(head):
    """Best-effort payload type from magic bytes alone.

    *head* must be at least ``SNIFF_BYTES`` long for ISO detection to work, as
    ISO 9660's identifying stamp lives 32 KiB into the image rather than at
    byte zero -- reading only the first few bytes of an ISO tells you nothing.
    """
    for offset, magic, name in MAGIC_SIGNATURES:
        if offset + len(magic) <= len(head) and head[offset:offset + len(magic)] == magic:
            return name
    if _looks_like_markup(head):
        return "html"
    if head[:5].lower() == b"<?php":
        return "text"
    if head and all(
        byte in (9, 10, 13) or 32 <= byte < 127 for byte in head[:512]
    ):
        return "text"
    return "unknown"


def expected_types(expect_type, path):
    """Types the payload may be, from the caller's claim or the extension."""
    declared = _normalize_type(expect_type)
    if declared:
        return (declared,), "expect_type"
    suffix = Path(str(path or "")).suffix.lower()
    types = EXTENSION_TYPES.get(suffix)
    if types:
        return tuple(types), "extension %s" % suffix
    return (), "unconstrained"


# --- Authenticode ---------------------------------------------------------


def _powershell_executable():
    override = os.environ.get("SONDER_POWERSHELL", "").strip()
    if override:
        return override
    return "powershell.exe" if os.name == "nt" else "pwsh"


def _authenticode_signature(path, *, timeout=60.0):
    """Authenticode status and signer subject for a PE/MSI/CAB on Windows.

    Overridden wholesale in tests. Returns a dict with ``supported``,
    ``status``, ``publisher``, ``thumbprint``, and ``detail``; it never raises,
    because an unverifiable signature is a verdict the caller must weigh, not
    an exception that hides it.
    """
    if os.name != "nt":
        return {
            "supported": False,
            "status": "unsupported_platform",
            "publisher": "",
            "thumbprint": "",
            "detail": "Authenticode verification requires Windows (running %s)"
                      % platform.system(),
        }
    literal = str(path).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "$s = Get-AuthenticodeSignature -LiteralPath '%s';"
        "$cert = $s.SignerCertificate;"
        "[pscustomobject]@{"
        "Status=[string]$s.Status;"
        "StatusMessage=[string]$s.StatusMessage;"
        "Subject=$(if ($cert) { [string]$cert.Subject } else { '' });"
        "Thumbprint=$(if ($cert) { [string]$cert.Thumbprint } else { '' })"
        "} | ConvertTo-Json -Compress"
    ) % literal
    try:
        completed = subprocess.run(
            [
                _powershell_executable(),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "supported": False,
            "status": "verifier_unavailable",
            "publisher": "",
            "thumbprint": "",
            "detail": "could not run Get-AuthenticodeSignature: %s" % exc,
        }
    stdout = (completed.stdout or "").strip()
    if completed.returncode != 0 or not stdout:
        return {
            "supported": False,
            "status": "verifier_failed",
            "publisher": "",
            "thumbprint": "",
            "detail": (completed.stderr or "no signature output").strip()[:400],
        }
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "supported": False,
            "status": "verifier_failed",
            "publisher": "",
            "thumbprint": "",
            "detail": "unparseable signature output: %s" % stdout[:200],
        }
    if not isinstance(data, dict):
        data = {}
    return {
        "supported": True,
        "status": str(data.get("Status") or "Unknown"),
        "publisher": str(data.get("Subject") or ""),
        "thumbprint": str(data.get("Thumbprint") or ""),
        "detail": str(data.get("StatusMessage") or ""),
    }
def _publisher_common_name(subject):
    match = re.search(r"CN=([^,]+)", str(subject or ""))
    return (match.group(1) if match else str(subject or "")).strip().strip('"')


# --- the verification battery --------------------------------------------


def _check(name, ok, detail):
    return {"check": name, "ok": bool(ok), "detail": detail}


def _read_probe(path):
    """Read enough of *path* to cover every magic offset, ISO's 32 KiB included."""
    with open(path, "rb") as handle:
        return handle.read(max(SNIFF_BYTES, 0x8006))


def run_checks(
    path,
    *,
    expect_type="",
    expect_publisher="",
    sha256="",
    content_type="",
    digest="",
):
    """Run the whole battery against a file on disk.

    Ordering is deliberate: block-page detection runs before the magic-byte
    check so an Access Denied page rejects as *a denial* rather than as a
    generic "wrong file type", which is the difference between knowing to
    change the request and thinking the mirror is broken.
    """
    path = Path(path)
    checks = []
    info = {
        "path": str(path),
        "bytes": 0,
        "sha256": "",
        "detected_type": "",
        "expected_types": [],
        "expected_from": "",
        "signature": {},
        "block": None,
    }
    if not path.exists() or not path.is_file():
        checks.append(_check("exists", False, "no regular file at %s" % path))
        return checks, info

    size = path.stat().st_size
    info["bytes"] = size
    wanted, source = expected_types(expect_type, path)
    info["expected_types"] = list(wanted)
    info["expected_from"] = source
    binary_expected = bool(wanted) and not set(wanted) & _TEXTY_TYPES

    head = _read_probe(path)
    detected = detect_type(head)
    info["detected_type"] = detected

    # 1. Emptiness. Failure #2 was a 0-byte file behind a zero exit code.
    floor = max(MIN_ANY_BYTES, max(
        (TYPE_MIN_BYTES.get(name, 0) for name in wanted), default=0,
    ))
    if size == 0:
        checks.append(_check(
            "size", False, "the payload is 0 bytes -- nothing was written",
        ))
    elif size < floor:
        checks.append(_check(
            "size", False,
            "%d bytes is below the %d-byte floor for %s -- too small to be a "
            "real payload" % (size, floor, "/".join(wanted) or "any artifact"),
        ))
    else:
        checks.append(_check("size", True, "%d bytes" % size))

    # 2. Block/denial page. Runs before magic so the reason is the true one.
    block = None
    if detected in ("html", "text") or _looks_like_markup(head):
        block = detect_block_page(head, content_type=content_type or "text/html")
        if block is None and binary_expected:
            block = {
                "marker": "html_body",
                "reason": "server returned an HTML/text document where a %s "
                          "payload was expected" % "/".join(wanted),
                "confidence": "strong",
                "evidence": _evidence(_as_text(head), "<"),
                "status": None,
                "url": "",
            }
    elif content_type:
        block = detect_block_page(head, content_type=content_type)
    info["block"] = block
    if block is not None:
        checks.append(_check(
            "block_page", False,
            "%s [%s]" % (block["reason"], block["marker"]),
        ))
    else:
        checks.append(_check("block_page", True, "no block/denial markers"))

    # 3. Magic bytes vs the claimed identity. Failure #1.
    if not wanted:
        checks.append(_check(
            "magic", True,
            "detected %s (no expected type to compare against)" % detected,
        ))
    elif detected in wanted:
        checks.append(_check(
            "magic", True, "%s magic matches %s" % (detected, source),
        ))
    else:
        checks.append(_check(
            "magic", False,
            "payload magic says %s but %s says %s"
            % (detected, source, "/".join(wanted)),
        ))

    # 4. Declared digest.
    if digest:
        actual = digest
    else:
        actual = file_sha256(path)
    info["sha256"] = actual
    expected_digest = str(sha256 or "").strip().lower()
    if expected_digest:
        if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            checks.append(_check(
                "sha256", False,
                "expected digest %r is not a 64-hex SHA-256" % sha256,
            ))
        elif actual == expected_digest:
            checks.append(_check("sha256", True, actual))
        else:
            checks.append(_check(
                "sha256", False,
                "digest mismatch: got %s, expected %s" % (actual, expected_digest),
            ))

    # 5. Authenticode. Failure #4 -- the only check that distinguishes two
    #    products whose version strings agree.
    want_publisher = str(expect_publisher or "").strip()
    signable = detected in SIGNABLE_TYPES or bool(set(wanted) & SIGNABLE_TYPES)
    if signable or want_publisher:
        signature = _authenticode_signature(path)
        info["signature"] = signature
        status = str(signature.get("status") or "")
        publisher = str(signature.get("publisher") or "")
        valid = signature.get("supported") and status.lower() == "valid"
        if want_publisher:
            if not signature.get("supported"):
                checks.append(_check(
                    "publisher", False,
                    "cannot confirm publisher %r: %s (%s)"
                    % (want_publisher, status, signature.get("detail", "")),
                ))
            elif not valid:
                checks.append(_check(
                    "publisher", False,
                    "signature status is %s, so publisher %r is unproven"
                    % (status, want_publisher),
                ))
            elif want_publisher.lower() in publisher.lower():
                checks.append(_check(
                    "publisher", True,
                    "signed by %s" % _publisher_common_name(publisher),
                ))
            else:
                checks.append(_check(
                    "publisher", False,
                    "signed by %s, which does not contain the required %r -- "
                    "right file, wrong vendor"
                    % (_publisher_common_name(publisher) or "(no subject)",
                       want_publisher),
                ))
        elif signature.get("supported"):
            checks.append(_check(
                "signature", valid,
                "%s%s" % (
                    status,
                    "; signed by %s" % _publisher_common_name(publisher)
                    if publisher else "",
                ),
            ))
        else:
            checks.append(_check(
                "signature", True,
                "not checked: %s" % signature.get("detail", status),
            ))
    return checks, info


# --- network layer (monkeypatched in tests) -------------------------------


def _validate_target(url):
    """Resolve *url* to pinned, globally routable addresses.

    Indirected through this one function so the offline test suite can point
    the real streaming/redirect/rename code at a loopback fixture server
    without loosening the SSRF policy that ships.
    """
    return _web_tools()._validated_public_target(url)


def _open_request(url, headers, timeout):
    parsed, addresses = _validate_target(url)
    del parsed
    request = urllib.request.Request(url, headers=dict(headers))
    request._sonder_addresses = tuple(addresses)
    return _web_tools()._urlopen(request, timeout=timeout)


def _part_path(dest):
    # Keeps the real extension so Authenticode (which dispatches on extension)
    # can run on the partial, while living at a path no later step mistakes for
    # the finished artifact.
    return dest.with_name(dest.name + ".part" + dest.suffix)


def _provenance_path(dest):
    return dest.with_name(dest.name + PROVENANCE_SUFFIX)


# --- the tools ------------------------------------------------------------


def verify_artifact(
    path,
    *,
    expect_type="",
    expect_publisher="",
    sha256="",
    extra_roots="",
    bypass=False,
):
    """Run the fetch-time verification battery against a file already on disk."""
    if not str(path or "").strip():
        raise ArtifactFetchError("path is required")
    resolved = file_ops.resolve_path(
        str(path), extra_roots=extra_roots, bypass=bypass,
    )
    checks, info = run_checks(
        resolved,
        expect_type=expect_type,
        expect_publisher=expect_publisher,
        sha256=sha256,
    )
    failures = [row for row in checks if not row["ok"]]
    result = {
        "action": "verify",
        "ok": not failures,
        "verdict": "verified" if not failures else "rejected",
        "path": str(resolved),
        "bytes": info["bytes"],
        "sha256": info["sha256"],
        "detected_type": info["detected_type"],
        "expected_types": info["expected_types"],
        "expected_from": info["expected_from"],
        "signature": info["signature"],
        "block": info["block"],
        "checks": checks,
        "failures": failures,
        "provenance_path": "",
    }
    sidecar = _provenance_path(resolved)
    if sidecar.exists():
        result["provenance_path"] = str(sidecar)
    return result


def fetch_artifact(
    url,
    dest,
    expect_type="",
    expect_publisher="",
    sha256="",
    max_mb=DEFAULT_MAX_MB,
    timeout=DEFAULT_TIMEOUT,
    *,
    resume=True,
    overwrite=False,
    extra_roots="",
    bypass=False,
):
    """Download *url* to *dest* and verify it as one atomic operation.

    Nothing lands at *dest* until the payload has passed every applicable
    check, so a rejected fetch cannot leave a file that a later step reads as
    a staged artifact. On success the sidecar ``<dest>.provenance.json``
    records where the bytes came from, what they hashed to, and every verdict
    that was reached.
    """
    url = str(url or "").strip()
    if not url:
        raise ArtifactFetchError("url is required")
    if not str(dest or "").strip():
        raise ArtifactFetchError("dest is required")
    if not _web_tools().enabled():
        raise ArtifactFetchError(
            "network artifact fetch is disabled by SONDER_WEB_TOOLS"
        )
    max_bytes = int(_clamp(max_mb, 1, MAX_MB_CEILING, DEFAULT_MAX_MB) * 1024 * 1024)
    timeout = _clamp(timeout, 1.0, MAX_TIMEOUT, DEFAULT_TIMEOUT)

    resolved = file_ops.resolve_path(
        str(dest), extra_roots=extra_roots, bypass=bypass,
    )
    part = _part_path(resolved)

    if resolved.exists() and not overwrite:
        prior = read_provenance(resolved)
        existing = verify_artifact(
            resolved,
            expect_type=expect_type,
            expect_publisher=expect_publisher,
            sha256=sha256,
            extra_roots=extra_roots,
            bypass=bypass,
        )
        existing["action"] = "reused"
        # Never attach a newly requested origin to old bytes.  Reuse is only
        # sound when the sidecar binds these exact bytes to this URL.
        prior_url = str((prior or {}).get("final_url") or (prior or {}).get("url") or "")
        prior_digest = str((prior or {}).get("sha256") or "")
        if prior_url != url or prior_digest.lower() != str(existing.get("sha256", "")).lower():
            existing["ok"] = False
            existing["verdict"] = "rejected"
            existing["checks"].append(_check("provenance", False, "existing artifact is not proven to originate from requested URL"))
            existing["failures"] = [row for row in existing["checks"] if not row["ok"]]
            return existing
        existing["url"] = prior_url
        existing["reused"] = True
        existing["redirect_chain"] = []
        existing["final_url"] = url
        existing["status"] = 0
        existing["content_type"] = ""
        return existing

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArtifactFetchError(
            "cannot create destination directory %s: %s" % (resolved.parent, exc)
        ) from exc

    started = time.time()
    download = _download(
        url,
        part,
        max_bytes=max_bytes,
        timeout=timeout,
        resume=resume,
        expect_type=expect_type,
        dest_name=resolved.name,
    )
    result = {
        "action": "fetch",
        "ok": False,
        "verdict": "rejected",
        "url": url,
        "final_url": download.get("final_url", url),
        "redirect_chain": download.get("redirect_chain", []),
        "status": download.get("status", 0),
        "content_type": download.get("content_type", ""),
        "path": str(resolved),
        "part_path": str(part),
        "bytes": download.get("bytes", 0),
        "sha256": download.get("sha256", ""),
        "resumed_from": download.get("resumed_from", 0),
        "elapsed_seconds": round(time.time() - started, 3),
        "detected_type": "",
        "expected_types": [],
        "expected_from": "",
        "signature": {},
        "block": download.get("block"),
        "checks": list(download.get("checks", [])),
        "failures": [],
        "provenance_path": "",
        "reused": False,
    }

    if not download["ok"]:
        # Transport-level rejection: discard the partial so a later resume
        # cannot graft real bytes onto a denial page.
        _discard(part)
        result["failures"] = [row for row in result["checks"] if not row["ok"]]
        return result

    checks, info = run_checks(
        part,
        expect_type=expect_type,
        expect_publisher=expect_publisher,
        sha256=sha256,
        content_type=download.get("content_type", ""),
        digest=download.get("sha256", ""),
    )
    result["checks"].extend(checks)
    result["bytes"] = info["bytes"]
    result["sha256"] = info["sha256"]
    result["detected_type"] = info["detected_type"]
    result["expected_types"] = info["expected_types"]
    result["expected_from"] = info["expected_from"]
    result["signature"] = info["signature"]
    if info["block"] is not None:
        result["block"] = info["block"]
    failures = [row for row in result["checks"] if not row["ok"]]
    result["failures"] = failures
    if failures:
        _discard(part)
        return result

    os.replace(part, resolved)
    result["ok"] = True
    result["verdict"] = "verified"
    result["part_path"] = ""
    result["provenance_path"] = str(_write_provenance(resolved, result))
    return result


def _discard(part):
    try:
        Path(part).unlink()
    except OSError:
        pass


def _download(url, part, *, max_bytes, timeout, resume, expect_type, dest_name):
    """Stream one URL into *part*, recording the redirect chain as it goes."""
    part = Path(part)
    checks = []
    chain = []
    current = url
    wanted, _source = expected_types(expect_type, dest_name)
    binary_expected = bool(wanted) and not set(wanted) & _TEXTY_TYPES

    offset = 0
    if resume and part.exists():
        offset = part.stat().st_size
        if offset >= max_bytes:
            offset = 0
            _discard(part)

    response = None
    status = 0
    content_type = ""
    for _hop in range(MAX_REDIRECTS + 1):
        headers = {
            "User-Agent": _web_tools().USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if offset:
            headers["Range"] = "bytes=%d-" % offset
        try:
            response = _open_request(current, headers, timeout)
        except Exception as exc:
            checks.append(_check(
                "transport", False, "%s: %s" % (type(exc).__name__, exc),
            ))
            return {
                "ok": False, "checks": checks, "redirect_chain": chain,
                "final_url": current, "status": 0, "content_type": "",
                "bytes": 0, "sha256": "", "resumed_from": 0, "block": None,
            }
        status = int(getattr(response, "status", 0) or 0)
        if status in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            with response:
                pass
            if not location:
                checks.append(_check(
                    "redirect", False,
                    "HTTP %d with no Location header" % status,
                ))
                return {
                    "ok": False, "checks": checks, "redirect_chain": chain,
                    "final_url": current, "status": status, "content_type": "",
                    "bytes": 0, "sha256": "", "resumed_from": 0, "block": None,
                }
            chain.append({"url": current, "status": status, "location": location})
            current = urllib.parse.urljoin(current, location)
            continue
        break
    else:
        checks.append(_check(
            "redirect", False, "more than %d redirects" % MAX_REDIRECTS,
        ))
        return {
            "ok": False, "checks": checks, "redirect_chain": chain,
            "final_url": current, "status": status, "content_type": "",
            "bytes": 0, "sha256": "", "resumed_from": 0, "block": None,
        }

    with response:
        content_type = response.headers.get("Content-Type", "") or ""
        if status == 206 and offset:
            content_range = response.headers.get("Content-Range", "") or ""
            if not content_range.startswith("bytes %d-" % offset):
                checks.append(_check("content_range", False, "206 response does not begin at requested offset"))
                return {"ok": False, "checks": checks, "redirect_chain": chain,
                        "final_url": current, "status": status, "content_type": content_type,
                        "bytes": 0, "sha256": "", "resumed_from": 0, "block": None}
            append = True
        else:
            append = False
            offset = 0
        if status not in (200, 206):
            body = b""
            try:
                body = response.read(SNIFF_BYTES) or b""
            except OSError:
                body = b""
            block = detect_block_page(
                body, content_type=content_type, status=status, url=current,
            )
            reason = "HTTP %d from %s" % (status, current)
            if block is not None:
                reason = "HTTP %d and %s [%s]" % (
                    status, block["reason"], block["marker"],
                )
            checks.append(_check("http_status", False, reason))
            return {
                "ok": False, "checks": checks, "redirect_chain": chain,
                "final_url": current, "status": status,
                "content_type": content_type, "bytes": len(body), "sha256": "",
                "resumed_from": 0, "block": block,
            }
        checks.append(_check("http_status", True, "HTTP %d" % status))

        declared_length = _content_length(response.headers)
        if declared_length is not None and offset + declared_length > max_bytes:
            checks.append(_check(
                "size_limit", False,
                "declared Content-Length %d exceeds the %d-byte ceiling"
                % (declared_length, max_bytes),
            ))
            return {
                "ok": False, "checks": checks, "redirect_chain": chain,
                "final_url": current, "status": status,
                "content_type": content_type, "bytes": 0, "sha256": "",
                "resumed_from": 0, "block": None,
            }

        # Sniff before writing: a denial page must never reach the disk, not
        # even as a partial another run could resume onto.
        try:
            sniff = response.read(SNIFF_BYTES) or b""
        except OSError as exc:
            checks.append(_check("transport", False, "read failed: %s" % exc))
            return {
                "ok": False, "checks": checks, "redirect_chain": chain,
                "final_url": current, "status": status,
                "content_type": content_type, "bytes": 0, "sha256": "",
                "resumed_from": 0, "block": None,
            }
        block = None
        if not append:
            block = detect_block_page(
                sniff, content_type=content_type, status=status, url=current,
            )
            if block is None and binary_expected and (
                _document_kind(content_type) in ("html", "text", "xml", "json")
                or _looks_like_markup(sniff)
            ):
                block = {
                    "marker": "html_body",
                    "reason": "server sent %s where a %s payload was expected"
                              % (content_type or "a text document",
                                 "/".join(wanted)),
                    "confidence": "strong",
                    "evidence": _as_text(sniff, 200).strip(),
                    "status": status,
                    "url": current,
                }
            if block is not None:
                checks.append(_check(
                    "block_page", False,
                    "%s [%s]" % (block["reason"], block["marker"]),
                ))
                return {
                    "ok": False, "checks": checks, "redirect_chain": chain,
                    "final_url": current, "status": status,
                    "content_type": content_type, "bytes": len(sniff),
                    "sha256": "", "resumed_from": 0, "block": block,
                }
            checks.append(_check(
                "block_page", True, "response body is not a denial page",
            ))

        digest = hashlib.sha256()
        written = 0
        mode = "r+b" if append else "wb"
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            with open(part, mode) as handle:
                if append:
                    handle.seek(0)
                    while True:
                        block_bytes = handle.read(STREAM_CHUNK)
                        if not block_bytes:
                            break
                        digest.update(block_bytes)
                        written += len(block_bytes)
                    handle.seek(written)
                    handle.truncate()
                chunk = sniff
                while chunk:
                    written += len(chunk)
                    if written > max_bytes:
                        raise ArtifactFetchError(
                            "payload exceeded the %d-byte ceiling" % max_bytes
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                    chunk = response.read(STREAM_CHUNK)
        except ArtifactFetchError as exc:
            _discard(part)
            checks.append(_check("size_limit", False, str(exc)))
            return {
                "ok": False, "checks": checks, "redirect_chain": chain,
                "final_url": current, "status": status,
                "content_type": content_type, "bytes": written, "sha256": "",
                "resumed_from": offset, "block": None,
            }
        except OSError as exc:
            checks.append(_check("write", False, "%s" % exc))
            return {
                "ok": False, "checks": checks, "redirect_chain": chain,
                "final_url": current, "status": status,
                "content_type": content_type, "bytes": written, "sha256": "",
                "resumed_from": offset, "block": None,
            }

    checks.append(_check(
        "download", True,
        "%d bytes from %s%s" % (
            written, current,
            " (resumed at %d)" % offset if append else "",
        ),
    ))
    return {
        "ok": True,
        "checks": checks,
        "redirect_chain": chain,
        "final_url": current,
        "status": status,
        "content_type": content_type,
        "bytes": written,
        "sha256": digest.hexdigest(),
        "resumed_from": offset if append else 0,
        "block": None,
    }


def _content_length(headers):
    try:
        value = headers.get("Content-Length")
    except AttributeError:
        return None
    if value in (None, ""):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _write_provenance(dest, result, *, reused=False):
    dest = Path(dest)
    sidecar = _provenance_path(dest)
    signature = dict(result.get("signature") or {})
    record = {
        "schema": PROVENANCE_SCHEMA,
        "tool": "fetch_artifact",
        "path": str(dest),
        "filename": dest.name,
        "url": result.get("url", ""),
        "final_url": result.get("final_url", ""),
        "redirect_chain": result.get("redirect_chain", []),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "http_status": result.get("status", 0),
        "content_type": result.get("content_type", ""),
        "bytes": result.get("bytes", 0),
        "sha256": result.get("sha256", ""),
        "detected_type": result.get("detected_type", ""),
        "expected_types": result.get("expected_types", []),
        "expected_from": result.get("expected_from", ""),
        "signature_status": signature.get("status", ""),
        "publisher": signature.get("publisher", ""),
        "publisher_common_name": _publisher_common_name(
            signature.get("publisher", "")
        ),
        "signature_thumbprint": signature.get("thumbprint", ""),
        "verdict": result.get("verdict", ""),
        "verified": bool(result.get("ok")),
        "revalidated_existing_file": bool(reused),
        "checks": result.get("checks", []),
    }
    try:
        sidecar.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
    except OSError:
        return sidecar
    return sidecar


def read_provenance(dest):
    """Load the sidecar for *dest*, or ``None`` when it has no recorded origin."""
    sidecar = _provenance_path(Path(dest))
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --- formatting -----------------------------------------------------------


def _format_checks(result):
    lines = []
    for row in result.get("checks", []):
        lines.append("  [%s] %-12s %s" % (
            "ok" if row["ok"] else "FAIL", row["check"], row["detail"],
        ))
    return lines


def format_fetch_result(result):
    verdict = result.get("verdict", "rejected")
    header = "artifact fetch %s: %s" % (
        "VERIFIED" if result.get("ok") else "REJECTED", result.get("url", ""),
    )
    lines = [header]
    if result.get("reused"):
        lines.append("  note: destination already existed; re-verified in place")
    lines.append("  dest: %s" % result.get("path", ""))
    final_url = result.get("final_url", "")
    if final_url and final_url != result.get("url", ""):
        lines.append("  final url: %s" % final_url)
    for hop in result.get("redirect_chain", []):
        lines.append("  redirect: %s -> %s (HTTP %s)" % (
            hop.get("url", ""), hop.get("location", ""), hop.get("status", ""),
        ))
    lines.append("  http: %s  content-type: %s" % (
        result.get("status", 0), result.get("content_type", "") or "(none)",
    ))
    lines.append("  bytes: %d  sha256: %s" % (
        result.get("bytes", 0), result.get("sha256", "") or "(not hashed)",
    ))
    lines.append("  type: detected %s; expected %s (%s)" % (
        result.get("detected_type", "") or "unknown",
        "/".join(result.get("expected_types", [])) or "unconstrained",
        result.get("expected_from", "") or "n/a",
    ))
    signature = result.get("signature") or {}
    if signature:
        lines.append("  signature: %s%s" % (
            signature.get("status", "unknown"),
            "; publisher %s" % _publisher_common_name(signature.get("publisher"))
            if signature.get("publisher") else "",
        ))
    lines.extend(_format_checks(result))
    if result.get("ok"):
        lines.append("  provenance: %s" % result.get("provenance_path", ""))
    else:
        lines.append(
            "  verdict: %s -- nothing was written to the destination path"
            % verdict
        )
        block = result.get("block")
        if block:
            lines.append("  denial evidence: %s" % (block.get("evidence") or "")[:200])
    return "\n".join(lines)


def format_verify_result(result):
    lines = ["artifact verify %s: %s" % (
        "VERIFIED" if result.get("ok") else "REJECTED", result.get("path", ""),
    )]
    lines.append("  bytes: %d  sha256: %s" % (
        result.get("bytes", 0), result.get("sha256", "") or "(not hashed)",
    ))
    lines.append("  type: detected %s; expected %s (%s)" % (
        result.get("detected_type", "") or "unknown",
        "/".join(result.get("expected_types", [])) or "unconstrained",
        result.get("expected_from", "") or "n/a",
    ))
    signature = result.get("signature") or {}
    if signature:
        lines.append("  signature: %s%s" % (
            signature.get("status", "unknown"),
            "; publisher %s" % _publisher_common_name(signature.get("publisher"))
            if signature.get("publisher") else "",
        ))
    lines.extend(_format_checks(result))
    if result.get("provenance_path"):
        lines.append("  provenance: %s" % result["provenance_path"])
    return "\n".join(lines)

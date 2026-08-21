"""Canonical bounded public web-text fetch adapter.

The HTTP resolver/connection remains in :mod:`web_tools` as a compatibility
transport seam. Fetch policy and document decoding live here so legacy
monkeypatches of ``web_tools._request`` and ``web_tools._urlopen`` continue to
work while the packaged adapter owns fetch behavior.
"""
from __future__ import annotations

import html
import importlib
import re
from email.message import Message
from html.parser import HTMLParser

from ..application.context import OperationContext

MAX_CHARS = 30_000
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+$")
_JSON_MEDIA_TYPES = {"application/json", "application/json-seq", "application/ndjson", "application/x-ndjson"}
_XML_MEDIA_TYPES = {"application/xml", "text/xml"}
_BINARY_SIGNATURES = (b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"PK\x03\x04", b"\x7fELF")
_XML_DECLARED_ENCODING = re.compile(br"<\?xml\b[^>]{0,200}\bencoding\s*=\s*['\"]\s*([^'\"\s]+)", re.IGNORECASE)
_HTML_DECLARED_CHARSET = re.compile(br"<meta\b[^>]{0,500}\bcharset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)


def _web_tools():
    return importlib.import_module("web_tools")


def _canonical_charset(value):
    """Return an allowlisted codec label, or reject an unsafe/unknown one."""
    token = str(value or "").strip().strip("\"'").lower().replace("_", "-")
    aliases = {
        "utf-8": "utf-8", "utf8": "utf-8", "unicode-1-1-utf-8": "utf-8",
        "utf-16": "utf-16", "utf16": "utf-16", "utf-16le": "utf-16le",
        "utf16le": "utf-16le", "utf-16-le": "utf-16le", "utf-16be": "utf-16be",
        "utf16be": "utf-16be", "utf-16-be": "utf-16be", "iso-8859-1": "latin-1",
        "iso8859-1": "latin-1", "latin-1": "latin-1", "latin1": "latin-1",
        "cp819": "latin-1", "windows-1252": "windows-1252", "windows1252": "windows-1252",
        "cp1252": "windows-1252", "x-cp1252": "windows-1252", "us-ascii": "ascii",
        "ascii": "ascii",
    }
    canonical = aliases.get(token)
    if canonical is None:
        raise ValueError("unsupported HTTP text charset %r" % value)
    return canonical


def _parse_text_content_type(value):
    header = str(value or "").strip()
    if not header:
        raise ValueError("web page response is missing Content-Type")
    media_type = header.split(";", 1)[0].strip().lower()
    if not _MEDIA_TYPE.fullmatch(media_type):
        raise ValueError("invalid HTTP Content-Type %r" % header)
    if media_type in _JSON_MEDIA_TYPES or media_type.endswith("+json"):
        document_kind = "json"
    elif media_type in {"text/html", "application/xhtml+xml"}:
        document_kind = "html"
    elif media_type in _XML_MEDIA_TYPES or media_type.endswith("+xml"):
        document_kind = "xml"
    elif media_type.startswith("text/"):
        document_kind = "text"
    else:
        raise ValueError("unsupported non-text HTTP media type %r" % media_type)
    message = Message()
    message["content-type"] = header
    declared = []
    for key, parameter in (message.get_params(header="content-type") or [])[1:]:
        if str(key or "").lower() == "charset":
            if parameter in (None, ""):
                raise ValueError("HTTP Content-Type has an empty charset")
            declared.append(_canonical_charset(parameter))
    if len(set(declared)) > 1:
        raise ValueError("HTTP Content-Type has conflicting charset parameters")
    return media_type, document_kind, (declared[0] if declared else "")


def _document_declared_charset(raw, document_kind):
    sample = raw[:4096]
    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:]
    match = (_XML_DECLARED_ENCODING.search(sample) if document_kind == "xml" else _HTML_DECLARED_CHARSET.search(sample) if document_kind == "html" else None)
    if not match:
        return ""
    try:
        value = match.group(1).decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise ValueError("document charset declaration is not ASCII") from exc
    return _canonical_charset(value)


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        text = html.unescape(" ".join(self.parts))
        return re.sub(r"\n\s+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def _decode_web_document(raw, content_type):
    """Decode one bounded web document using MIME, BOM, and charset evidence."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("web page response body is not bytes")
    raw = bytes(raw)
    media_type, document_kind, header_charset = _parse_text_content_type(content_type)
    signature = raw.lstrip(b" \t\r\n\x00")
    if any(signature.startswith(prefix) for prefix in _BINARY_SIGNATURES):
        raise ValueError("binary web content is not readable text (%s)" % media_type)
    body_charset = _document_declared_charset(raw, document_kind)
    if header_charset and body_charset and header_charset != body_charset:
        raise ValueError("conflicting HTTP and document charset declarations")
    declared_charset = header_charset or body_charset
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        raise ValueError("unsupported UTF-32 byte order mark")
    if raw.startswith(b"\xef\xbb\xbf"):
        if declared_charset and declared_charset != "utf-8":
            raise ValueError("UTF-8 BOM conflicts with declared charset")
        codec = "utf-8-sig"
    elif raw.startswith(b"\xff\xfe"):
        if declared_charset not in ("", "utf-16", "utf-16le"):
            raise ValueError("UTF-16LE BOM conflicts with declared charset")
        codec = "utf-16"
    elif raw.startswith(b"\xfe\xff"):
        if declared_charset not in ("", "utf-16", "utf-16be"):
            raise ValueError("UTF-16BE BOM conflicts with declared charset")
        codec = "utf-16"
    else:
        codec = declared_charset or "utf-8"
        if codec == "utf-16":
            raise ValueError("declared UTF-16 text requires a byte order mark")
        codec = {"utf-16le": "utf-16-le", "utf-16be": "utf-16-be"}.get(codec, codec)
    try:
        text = raw.decode(codec, "strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise ValueError("web page body is not valid %s text" % codec) from exc
    if "\x00" in text:
        raise ValueError("web page body contains binary NUL bytes")
    if any((ord(char) < 32 and char not in "\t\r\n\f") or 0x7F <= ord(char) <= 0x9F for char in text):
        raise ValueError("web page body contains binary control bytes")
    if document_kind in {"html", "xml"} or (document_kind == "text" and "<html" in text[:1000].lower()):
        parser = _TextParser()
        parser.feed(text)
        text = parser.text()
    if not text.strip():
        raise ValueError("web page contained no readable text")
    return text


def fetch_raw(url: str, *, max_chars=8000, timeout=10):
    """Fetch and decode bounded text through the compatibility transport."""
    tools = _web_tools()
    if not tools.enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    bounded_chars = max(1000, min(int(max_chars or 8000), MAX_CHARS))
    raw, content_type = tools._request(url, timeout=timeout)
    return _decode_web_document(raw, content_type)[:bounded_chars]


def _artifact_fetch():
    return importlib.import_module("sonder_runtime.adapters.artifact_fetch")


def fetch(url: str, *, max_chars=8000, context: OperationContext):
    """Fetch readable public text only after explicit cloud consent."""
    if not context.cloud_allowed:
        raise PermissionError("web fetch requires explicit cloud consent")
    text = fetch_raw(url, max_chars=max_chars)
    artifact_fetch = _artifact_fetch()
    blocked = artifact_fetch.detect_block_page(text, content_type="text/html", url=url)
    if blocked is not None:
        return {"ok": False, "url": str(url), "chars": 0, "blocked": blocked, "text": artifact_fetch.format_block_notice(url, blocked)}
    return {"ok": True, "url": str(url), "chars": len(text), "text": text}


def format_result(result):
    return str(result.get("text", ""))


__all__ = ["fetch", "fetch_raw", "format_result"]

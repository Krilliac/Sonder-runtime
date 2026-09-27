"""Guarded XML parsing for hostile documents (test reports, valgrind XML).

This is the reviewed ``_guard_xml`` logic from ``domain.testing.report_parsers``
made public so every XML reader shares one guard:

- a size cap checked before anything is parsed;
- a byte-level ``<!DOCTYPE``/``<!ENTITY`` pre-check for ASCII-compatible
  encodings;
- an expat pass with every declaration hook refusing, which also catches
  declarations spelled in UTF-16 or another declared encoding;
- only then ``xml.etree`` builds the tree, so no entity is ever expanded and
  nothing external is fetched.

Pure domain module: no I/O.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.parsers import expat

from .errors import InvalidInput


DEFAULT_MAX_XML_BYTES = 8 * 1024 * 1024
_FORBIDDEN_XML = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


class UnsafeXml(InvalidInput):
    """The document is oversized, declares a DTD/entity, or is malformed."""


class _DeclarationRefused(Exception):
    pass


def _refuse(*_args) -> None:
    raise _DeclarationRefused()


def _refuse_declarations(data: bytes) -> None:
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.NotationDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(data, True)
    except _DeclarationRefused:
        raise UnsafeXml("XML declares a DOCTYPE or ENTITY; refused") from None
    except expat.ExpatError as exc:
        raise UnsafeXml("XML is malformed: %s" % exc) from None
    except (LookupError, ValueError) as exc:
        raise UnsafeXml("XML is unreadable: %s" % type(exc).__name__) from None


def parse_guarded_xml(data: bytes, *, max_bytes: int = DEFAULT_MAX_XML_BYTES) -> ET.Element:
    """Parse ``data`` into an element tree, refusing anything unsafe.

    Raises ``UnsafeXml`` (an ``InvalidInput``) for non-bytes input, documents
    over ``max_bytes``, any DOCTYPE/ENTITY/notation declaration in any
    encoding, and malformed or undecodable XML.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise UnsafeXml("XML document must be bytes")
    data = bytes(data)
    limit = max(0, int(max_bytes))
    if len(data) > limit:
        raise UnsafeXml("XML document exceeds %d bytes" % limit)
    if _FORBIDDEN_XML.search(data):
        raise UnsafeXml("XML declares a DOCTYPE or ENTITY; refused")
    _refuse_declarations(data)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise UnsafeXml("XML is malformed: %s" % exc) from None
    except (LookupError, ValueError) as exc:
        raise UnsafeXml("XML is unreadable: %s" % type(exc).__name__) from None


def local_tag(tag: object) -> str:
    """The tag name without an ``{namespace}`` prefix ("" for comments/PIs)."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


__all__ = ["DEFAULT_MAX_XML_BYTES", "UnsafeXml", "local_tag", "parse_guarded_xml"]

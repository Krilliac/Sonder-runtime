"""Pure rules for editable prompt text: normalisation, placeholders, digests.

Prompts live as Markdown files (``prompts/<name>.md`` in the repository, with
optional operator overrides in the state home). This module owns only the
side-effect-free half of that contract so it can be tested without a disk:

* how raw file bytes become prompt text (``normalize``);
* which ``$name`` placeholders a template uses, and rendering it with exactly
  the fields the caller supplies (``render``);
* the short content digest recorded as provenance (``digest``).

Templates use :class:`string.Template` syntax: ``$name`` or ``${name}`` is a
placeholder and ``$$`` is a literal dollar sign. Braces and ``%`` are plain
text, so JSON examples inside a prompt need no escaping. A prompt with no
declared fields is *not* a template at all and is returned verbatim, which is
why static prompts may contain a bare ``$`` (the reflection pitfall example
quotes a PowerShell ``$x``).
"""
from __future__ import annotations

import hashlib
import string
from collections.abc import Mapping


class PromptTemplateError(ValueError):
    """A template that cannot be rendered with the caller's fields."""


def normalize(raw: str) -> str:
    """File text -> prompt text.

    Line endings are normalised to ``\\n`` (a Windows checkout with
    ``core.autocrlf`` stores the defaults as CRLF) and exactly one trailing
    newline -- the one every editor adds at end of file -- is removed. A
    prompt that genuinely ends in a newline therefore ends its file with a
    blank line.
    """
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("﻿"):
        text = text[1:]
    if text.endswith("\n"):
        text = text[:-1]
    return text


def placeholders(template: str) -> frozenset[str]:
    """Every placeholder name a template uses; raises on malformed ``$``."""
    names = set()
    for match in string.Template.pattern.finditer(template):
        if match.group("invalid") is not None:
            raise PromptTemplateError(
                "malformed placeholder at offset %d (use $$ for a literal $)"
                % match.start("invalid")
            )
        name = match.group("named") or match.group("braced")
        if name:
            names.add(name)
    return frozenset(names)


def render(template: str, fields: Mapping[str, object] | None) -> str:
    """Render ``template`` with exactly ``fields``.

    With no fields the text is returned untouched. Otherwise the template must
    use every supplied field and no other: an unknown placeholder cannot be
    filled, and a dropped one (say ``$tools`` in an agent prompt) would
    silently remove context the code relies on, so both are errors the
    loader turns into a fallback to the shipped default.
    """
    if not fields:
        return template
    used = placeholders(template)
    supplied = frozenset(fields)
    unknown = sorted(used - supplied)
    missing = sorted(supplied - used)
    if unknown or missing:
        detail = []
        if unknown:
            detail.append("unknown placeholder(s) %s" % ", ".join("$" + n for n in unknown))
        if missing:
            detail.append("missing placeholder(s) %s" % ", ".join("$" + n for n in missing))
        raise PromptTemplateError("; ".join(detail))
    return string.Template(template).substitute({k: str(v) for k, v in fields.items()})


def digest(text: str) -> str:
    """Full SHA-256 hex digest of the prompt text (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

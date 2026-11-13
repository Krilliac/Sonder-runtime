"""Answer checks and the Sources text: cited numbers, dangling citations, formatting.

Ported unchanged from the lookup client (``answer`` citation handling and
``format_sources``).
"""
from __future__ import annotations

import re
from collections.abc import Collection, Sequence

from .models import Passage
from .sanitize import one_line

CITED_RE = re.compile(r'\[(\d+)\]')

ECHO_WARNING = 'the model echoed a passage marker line; it was removed from the answer'


def cited_numbers(text: str) -> frozenset[int]:
    """Passage numbers cited as [n] in text."""
    return frozenset(int(n) for n in CITED_RE.findall(text))


def missing_numbers(cited: Collection[int], passage_count: int) -> tuple[int, ...]:
    """Cited numbers that point at no passage, ascending."""
    return tuple(sorted(n for n in cited if n < 1 or n > passage_count))


def missing_warning(missing: Sequence[int]) -> str:
    return ('answer cites passage numbers that were not provided: '
            + ', '.join(f'[{n}]' for n in missing))


def format_sources(passages: Sequence[Passage], cited: Collection[int] = ()) -> str:
    """Render the Sources: list, marking cited passages with *."""
    lines = ['Sources:']
    for n, p in enumerate(passages, 1):
        label = p.title + (f" — {p.heading}" if p.heading else '')
        lines.append(f"  [{n}]{'*' if n in cited else ' '} {one_line(label)} "
                     f"({one_line(p.url or p.path or '')})")
    return '\n'.join(lines)


def format_unavailable(reason: str) -> str:
    """The Sources line for a turn whose lookup could not run."""
    return f'Sources: none (reference lookup unavailable: {one_line(reason)})'

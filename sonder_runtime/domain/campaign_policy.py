"""Pure output-verification policy for generated campaign tasks."""

from __future__ import annotations


def output_matches(output, expected) -> bool:
    """Compare exact campaign output after harmless whitespace normalization."""
    def normalize(text):
        lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        stripped = [line.strip() for line in lines]
        while stripped and not stripped[0]:
            stripped.pop(0)
        while stripped and not stripped[-1]:
            stripped.pop()
        return "\n".join(stripped)

    return normalize(output) == normalize(expected)

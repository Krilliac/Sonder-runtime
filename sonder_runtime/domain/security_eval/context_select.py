"""Select and pack retrieval context for a line range (pure, deterministic, no I/O).

The adapter owns reading files; this module decides which identifiers are worth searching, how a
hit is classified (definition, caller or guard), and how a bundle is packed into a character budget.
The declaration patterns are copied from the root ``symbol_index.py``: the domain layer may not
import root modules, and that module is not packaged. Regex-based, so it is language-limited
(C#, C/C++, Python, JS/TS, Go, Rust) and the A/B result measures this helper, not "context".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

HELPER_VERSION = "1"
KINDS = ("definition", "caller", "guard")
MAX_SNIPPET_LINE_CHARS = 200

_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_KEYWORDS = frozenset("""
abstract and as assert async await base bool break byte case catch char checked class const continue
decimal def default defer del delegate delete do double dyn elif else enum event except explicit extern
false final finally fixed float fn for foreach from func global go goto if impl implicit import in int
interface internal is lambda let lock long loop match mod move mut namespace new nil none not null
object operator or out override package params pass private protected pub public raise readonly ref
register return sbyte sealed select self short signed sizeof static str string struct super switch
template this throw true try type typedef typeof uint ulong union unsafe unsigned use ushort using
var virtual void volatile while with yield
""".split())

_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
_JS_DECL = re.compile(
    rf"^\s*(?:(?:export\s+)?(?:default\s+)?(?:declare\s+)?)"
    rf"(async\s+function|function|class|interface|enum|namespace|type)\s+({_IDENT})\b")
_JS_ARROW = re.compile(
    rf"^\s*(?:(?:export\s+)?(?:declare\s+)?)"
    rf"(?:const|let|var)\s+({_IDENT})\s*(?::[^=]+)?=\s*(?:async\s*)?"
    rf"(?:\([^)]*\)|{_IDENT})\s*=>")
_C_TYPE = re.compile(r"^\s*(?:typedef\s+)?(class|struct|union|enum|namespace)\s+(?:class\s+)?([A-Za-z_]\w*)\b")
_C_MACRO = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)\b")
_C_FUNCTION = re.compile(
    r"^\s*(?:template\s*<[^;{}]*>\s*)?"
    r"(?:[A-Za-z_]\w*(?:::\w+)*[\s*&]+)*[A-Za-z_]\w*(?:::\w+)*"
    r"(?:\s*<[^;{}()]*>[\s*&]+(?:[A-Za-z_]\w*(?:::\w+)*[\s*&]+)*|[\s*&]+)"
    r"([~A-Za-z_]\w*(?:::\w+)*)\s*\([^;{}]*\)\s*"
    r"(?:const\s*)?(?:noexcept(?:\([^)]*\))?\s*)?(?:->\s*[^;{]+\s*)?[;{]\s*$")
_CS_TYPE = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|static|abstract|sealed|partial)\s+)*"
    r"(class|interface|struct|enum|record|namespace)\s+([A-Za-z_]\w*)\b")
_CS_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|abstract|"
    r"async|sealed|extern|unsafe|partial|new)\s+)+"
    r"(?:[A-Za-z_]\w*(?:[.<>,?\[\]]|::)*)\s+([A-Za-z_]\w*)\s*\([^;{}]*\)")
_RUST_DECL = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+|async\s+|const\s+|extern\s+)*"
    r"(fn|struct|enum|trait|type|mod|union|static|const)\s+([A-Za-z_]\w*)\b")
_GO_DECL = re.compile(r"^\s*(func|type|var|const)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\b")
_PY_DECL = re.compile(r"^\s*(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)\b")

# Declaration kinds that name a callable (the others name a type, constant or module).
_FUNCTION_WORDS = frozenset({"def", "function", "async function", "func", "fn"})
_NOT_A_DECLARATION = frozenset({"return", "throw", "else", "case", "goto", "new", "delete", "await",
                                "yield", "co_return", "co_await", "raise", "assert", "print"})
_NOT_A_NAME = frozenset({"if", "for", "while", "switch", "catch", "return", "sizeof", "foreach", "using",
                         "lock", "fixed", "when", "elif", "else"})
_GUARD_RE = re.compile(
    r"(?:^|[^\w])(?:if|elif|else|while|throw|raise|assert|require|return|lock|"
    r"(?:Check|Validate|Ensure|Clamp|Assert|Verify)\w*)(?![A-Za-z0-9_])")


@dataclass(frozen=True)
class ContextHit:
    kind: str
    path: str
    line: int
    ident: str
    snippet: str


@dataclass(frozen=True)
class ContextBundle:
    text: str = ""
    items: tuple[ContextHit, ...] = ()
    truncated: bool = False
    skipped: dict = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict:
        return {"helper_version": HELPER_VERSION, "chars": len(self.text),
                "items": [{"kind": hit.kind, "path": hit.path, "line": hit.line, "ident": hit.ident}
                          for hit in self.items],
                "truncated": self.truncated,
                "skipped": {key: list(value) for key, value in sorted(self.skipped.items())},
                "reason": self.reason}


def extract_identifiers(text: str, limit: int) -> tuple[str, ...]:
    """Identifiers of ``text`` worth searching: >= 3 chars, not a keyword, most frequent first."""
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    for match in _IDENT_RE.finditer(text):
        name = match.group(0)
        if len(name) < 3 or name.lower() in _KEYWORDS:
            continue
        counts[name] = counts.get(name, 0) + 1
        first.setdefault(name, match.start())
    ordered = sorted(counts, key=lambda name: (-counts[name], first[name]))
    return tuple(ordered[:max(0, limit)])


def _declaration(line: str):
    """``(kind_word, name)`` when ``line`` declares something, else ``None``."""
    stripped = line.lstrip()
    head = _IDENT_RE.match(stripped)
    if head is not None and head.group(0) in _NOT_A_DECLARATION:
        return None
    for pattern in (_PY_DECL, _JS_DECL, _C_TYPE, _CS_TYPE, _RUST_DECL, _GO_DECL):
        match = pattern.match(line)
        if match:
            return match.group(1), match.group(2)
    match = _JS_ARROW.match(line)
    if match:
        return "function", match.group(1)
    match = _C_MACRO.match(line)
    if match:
        return "macro", match.group(1)
    match = _CS_METHOD.match(line)
    if match and match.group(1) not in _NOT_A_NAME:
        return "function", match.group(1)
    match = _C_FUNCTION.match(line)
    if match:
        name = match.group(1).rsplit("::", 1)[-1]
        if name not in _NOT_A_NAME:
            return "function", match.group(1)
    return None


def declared_name(line: str) -> str | None:
    """The name ``line`` declares (type, function, constant, macro), or ``None``."""
    found = _declaration(line)
    return found[1] if found else None


def enclosing_declaration(lines: Sequence[str], start: int):
    """``(name, line_no)`` of the nearest function declaration at or above 1-based line ``start``."""
    index = min(start, len(lines)) - 1
    while index >= 0:
        found = _declaration(lines[index])
        if found and found[0] in _FUNCTION_WORDS | {"function"}:
            return found[1], index + 1
        index -= 1
    return None


def is_guard_line(line: str) -> bool:
    return _GUARD_RE.search(line) is not None


def classify_hit(line: str, ident: str) -> str:
    """``definition``, ``caller``, ``guard`` or ``other`` for ``ident`` on ``line``."""
    declared = declared_name(line)
    if declared is not None and declared.rsplit("::", 1)[-1] == ident:
        return "definition"
    pattern = r"(?<![A-Za-z0-9_])%s\s*\(" % re.escape(ident)
    if declared is None and re.search(pattern, line):
        return "caller"
    if is_guard_line(line):
        return "guard"
    return "other"


def _render(hit: ContextHit) -> str:
    return "[%s] %s:%d (%s)\n%s\n" % (hit.kind, hit.path, hit.line, hit.ident, hit.snippet)


def pack_context(hits, max_chars: int, per_kind_limit: int, skipped=None) -> ContextBundle:
    """Pack ``hits`` into at most ``max_chars``: kind order, then path, then line; whole hits only."""
    skipped = {key: tuple(value) for key, value in (skipped or {}).items()}
    chosen: list[ContextHit] = []
    for kind in KINDS:
        of_kind = sorted((hit for hit in hits if hit.kind == kind),
                         key=lambda hit: (hit.path, hit.line, hit.ident))
        unique: list[ContextHit] = []
        seen: set[tuple[str, int]] = set()
        for hit in of_kind:
            if (hit.path, hit.line) not in seen:
                seen.add((hit.path, hit.line))
                unique.append(hit)
        chosen.extend(unique[:max(0, per_kind_limit)])
    parts: list[str] = []
    kept: list[ContextHit] = []
    used = 0
    truncated = False
    for hit in chosen:
        piece = _render(hit)
        if used + len(piece) > max_chars:
            truncated = True
            break
        parts.append(piece)
        kept.append(hit)
        used += len(piece)
    if not chosen:
        reason = "no definition, caller or guard was found outside the range"
    elif not kept:
        reason = "the first hit alone exceeds the %d character budget" % max_chars
    else:
        reason = ""
    return ContextBundle(text="".join(parts), items=tuple(kept), truncated=truncated,
                         skipped=skipped, reason=reason)

"""Bounded secret detection and decoder-fuzzing contracts (WP9)."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Iterable


@dataclass(frozen=True)
class CredentialFinding:
    kind: str
    line: int
    redacted: str


_KEYWORD = r"(?:api[_-]?key|apikey|secret|password|passwd|pwd|token|credentials?)"
# A name is a credential name when it ENDS in a keyword plus at most three
# known secret suffixes (DB_PASSWORD, AWS_SECRET_ACCESS_KEY, SECRET_KEY_BASE,
# API_KEY_2). Any other ending (secrets_path, context_tokens, max_tokens) is an
# ordinary identifier that merely mentions a secret. Searched over one name;
# every attempt is a bounded literal match, so the search is linear.
_NAME_TAIL = re.compile(
    r"(?i)" + _KEYWORD
    + r"(?:[_-]?(?:access[_-]?key|key|id|value|secret|token|base|salt|pass)){0,3}"
    r"(?:[_-]?[0-9]{1,3})?\Z"
)
_WS = r"[^\S\r\n]{0,256}+"
_TYPE_EXPR = r"[A-Za-z_][A-Za-z0-9_.]{0,64}+(?:\[[^\]\r\n=]{0,128}+\]{1,3}+)?"
# One identifier-shaped name (a match can only START at the beginning of a
# name run, and the run is possessive, so each run is walked once), then, in a
# lookahead: an optional closing quote (JSON keys), an optional annotation
# (``name: str =``), bounded whitespace including NBSP, "=", ":", ":=" (Go,
# Pascal) or "=>" -- never "==" -- and the start of the value. Every piece of the
# lookahead is bounded or possessive, so a start costs O(1) beyond its name
# run. A match consumes only the name, so a rejected candidate never swallows
# a later assignment on the same line (``ValueError: API_KEY=...``,
# ``f(token=x, password="...")``). A quoted value needs only its opening quote
# and its first 256 characters: long values, triple quotes and values
# continued on the next line are still findings.
_ASSIGNMENT = re.compile(
    r"(?i)(?<![a-z0-9_-])(?P<name>[a-z0-9_-]++)(?=(?P<quote>[\"']?)"
    r"(?P<ann>[^\S\r\n]{0,16}+:[^\S\r\n]{0,16}+" + _TYPE_EXPR
    + r"(?:[^\S\r\n]{0,16}+\|[^\S\r\n]{0,16}+" + _TYPE_EXPR + r")?)?"
    r"(?P<pre>" + _WS + r")(?P<sep>(?(ann)=(?!=)|(?:=>|:=|:|=(?!=))))(?P<post>" + _WS + r")"
    r"(?:[rbuf]{0,2}(?P<triple>\"\"\"|''')"
    r"|[rbuf]{0,2}\"(?P<dq>[^\"\r\n]{1,256}+)"
    r"|[rbuf]{0,2}'(?P<sq>[^'\r\n]{1,256}+)"
    r"|(?P<bare>(?![<\"'])[^\s,;\"']{1,256}+)))"
)
_LITERAL_NAMES = frozenset({"none", "null", "nil", "true", "false", "undefined"})
# Types that follow "name:" in an annotation (Python, TypeScript, pydantic).
_TYPE_NAMES = frozenset({
    "str", "bytes", "int", "float", "bool", "dict", "list", "tuple", "set",
    "object", "any", "optional", "union", "secretstr", "secretbytes", "path",
    "sequence", "mapping", "iterable", "callable", "string", "number",
    "boolean", "unknown", "never", "void", "literal", "final", "classvar",
})
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_KEYWORD_IN_VALUE = re.compile(r"(?i)" + _KEYWORD)


def _names_a_secret(word: str) -> bool:
    """A digit-free identifier containing a keyword is a NAME (token,
    new_token, DB_PASSWORD), not a value."""
    return not any(ch.isdigit() for ch in word) and _KEYWORD_IN_VALUE.search(word) is not None


def _quoted_is_literal(body: str) -> bool:
    """Judge the text after an opening quote (at most 256 characters; no
    closing quote is required)."""
    first = body[0]
    if first == "<":  # <your password here>
        return False
    if first.isspace() or first in ",;)]}":
        # The quote closed a string ("token: "); it did not open a value.
        return False
    # "OPENROUTER_API_KEY" and "stale_owner_token" are env-var names and
    # labels. A plain lowercase word ("password") stays a finding: it is a real
    # weak password.
    is_label = _IDENTIFIER.fullmatch(body) is not None and ("_" in body or body != body.lower())
    return not (is_label and _names_a_secret(body))


# token=token or "", x=a if flag else b: a Python operator after the value.
_CODE_CONTINUATION = re.compile(r"[^\S\r\n]{1,16}+(?:or|and|if|else)\b")


def _previous_visible(text: str, index: int) -> str:
    """The nearest non-blank character before index, looking back at most 16."""
    for position in range(index - 1, max(-1, index - 17), -1):
        if not text[position].isspace():
            return text[position]
    return ""


def _python_context(match: re.Match[str], closers: str) -> bool:
    """Whether a bare lowercase word sits where Python/JS code binds a NAME:
    ``token = value``, ``name: str = value``, ``x => value``, a dict entry
    ``"key": value``, or a keyword argument ``f(token=value)``. Env-style
    ``DB_PASSWORD=letmein``, YAML ``password: letmein``, query strings
    ``?token=abc`` and prose ``password=swordfish`` are not."""
    separator = match.group("sep")
    if separator in ("=>", ":=") or match.group("ann"):
        return True
    if separator == ":":
        return bool(match.group("quote"))
    if match.group("pre") or match.group("post"):
        return True
    text = match.string
    end = match.end("bare")
    following = closers[:1] or text[end:end + 1]
    return (
        following in (",", ")")
        or _previous_visible(text, match.start("name")) in ("(", ",")
        or _CODE_CONTINUATION.match(text, end) is not None
    )


def _bare_is_code(value: str, match: re.Match[str]) -> bool:
    """Return whether an unquoted value is an expression, not a literal.

    Calls, attribute and index expressions, grouped expressions, placeholders
    (?, ..., %s), language literals (None/null/true), annotations
    (``token: str``), names that themselves name a secret (``token=token``,
    ``password = DB_PASSWORD``), and lowercase names in Python-style bindings
    (``token = value``, ``f(api_key=key)``) are code that handles a secret,
    not a secret.
    """
    if value[0] in "([{`%" or not any(ch.isalnum() for ch in value):
        return True
    ident = _IDENTIFIER.match(value)
    if ident is None:
        # A typed numeric default (chars_per_token: float = 3.5) is config.
        return bool(match.group("ann")) and value.rstrip(")]},:").replace(".", "").isdigit()
    rest = value[ident.end():]
    if rest[:1] in ("(", "[") or (rest[:1] == "." and _IDENTIFIER.match(rest, 1) is not None):
        return True
    if rest.rstrip(")]}:"):
        return False
    word = ident.group()
    folded = word.casefold()
    if folded in _LITERAL_NAMES or (match.group("sep") == ":" and folded in _TYPE_NAMES):
        return True
    if any(ch.isdigit() for ch in word):
        return False
    if word != word.lower():
        return _names_a_secret(word)
    return _python_context(match, rest)


def _assignment_is_credential(match: re.Match[str]) -> bool:
    if _NAME_TAIL.search(match.group("name")) is None:
        return False
    if match.group("triple"):
        return True
    body = match.group("dq") or match.group("sq")
    if body:
        return _quoted_is_literal(body)
    return not _bare_is_code(match.group("bare"), match)


class CredentialScanner:
    """Detect common credential-shaped material without returning the secret."""

    # Every repeat is bounded or possessive so the scan stays linear on hostile
    # single-line input; possessive quantifiers need Python 3.11+. A pattern
    # with a validator is a finding when ANY of its matches on the line passes.
    _patterns = (
        ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----"), None),
        ("aws_access_key", re.compile(r"(?:\b|(?<=(?:AKIA|ASIA)[0-9A-Z]{16}))(?:AKIA|ASIA)[0-9A-Z]{16}(?:\b|(?=(?:AKIA|ASIA)))"), None),
        ("github_token", re.compile(
            r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9_]{20,255}|github_pat_[A-Za-z0-9_]{20,255})"
        ), None),
        ("api_token", re.compile(
            r"(?<![A-Za-z0-9])(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,200}"
            r"|AIza[0-9A-Za-z_-]{35}"
            r"|xox[abpr]-[A-Za-z0-9-]{10,100})"
        ), None),
        # scheme://user:password@host, the .git-credentials / DSN shape.
        ("url_credential", re.compile(
            r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,15}://[^/@\s:]{1,64}:[^/@\s]{1,128}@"
        ), None),
        # One attempt per run of base64url characters keeps this linear on
        # hostile input such as a long run of "-eyJ": the lookahead checks
        # once that the run is followed by two more segments, then the lazy
        # prefix finds an "eyJ" at the start of the run or after a "-" in it
        # (to a detector "-eyJ..." is still a JWT) with 8 more characters.
        ("jwt", re.compile(
            r"(?<![A-Za-z0-9_-])"
            r"(?=[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
            r"(?:[A-Za-z0-9_-]*?-)?eyJ[A-Za-z0-9_-]{8}"
        ), None),
        ("credential_assignment", _ASSIGNMENT, _assignment_is_credential),
    )

    def __init__(self, *, max_bytes: int = 1_000_000) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def scan(self, content: str | bytes) -> tuple[CredentialFinding, ...]:
        raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        if len(raw.encode("utf-8")) > self.max_bytes:
            raise ValueError("content exceeds scan bound")
        findings: list[CredentialFinding] = []
        for line_no, line in enumerate(raw.splitlines(), 1):
            for kind, pattern, accept in self._patterns:
                if accept is None:
                    hit = pattern.search(line) is not None
                else:
                    hit = any(accept(match) for match in pattern.finditer(line))
                if hit:
                    findings.append(CredentialFinding(kind, line_no, self._redact(line)))
        return tuple(findings)

    @staticmethod
    def _redact(line: str) -> str:
        # Findings are safe to persist or transmit: do not retain prefixes,
        # suffixes, or labels that can disclose credential material.
        return "[REDACTED CREDENTIAL]"


@dataclass(frozen=True)
class FuzzReport:
    cases: int
    rejected: int
    failures: tuple[str, ...]


class DecoderFuzzHarness:
    """A bounded contract for exercising a decoder with arbitrary byte inputs."""

    def __init__(self, decoder: Callable[[bytes], object], *, max_cases: int = 256, max_input_bytes: int = 4096) -> None:
        if max_cases <= 0 or max_input_bytes <= 0:
            raise ValueError("fuzz bounds must be positive")
        self.decoder = decoder
        self.max_cases = max_cases
        self.max_input_bytes = max_input_bytes

    def run(self, inputs: Iterable[bytes]) -> FuzzReport:
        cases = rejected = 0
        failures: list[str] = []
        for payload in inputs:
            if cases >= self.max_cases:
                break
            cases += 1
            if not isinstance(payload, (bytes, bytearray)) or len(payload) > self.max_input_bytes:
                rejected += 1
                continue
            try:
                self.decoder(bytes(payload))
            except Exception as exc:  # The harness records decoder faults; it must not crash the campaign.
                failures.append(type(exc).__name__)
        return FuzzReport(cases, rejected, tuple(failures))

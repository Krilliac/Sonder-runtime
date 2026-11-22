"""Small, side-effect-free guards for plain chat responses.

These helpers deliberately do not inspect the filesystem.  The caller owns the
actual tool receipt and passes the paths written during the turn, which keeps a
plain response from gaining an implicit filesystem authority.
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Iterable, Iterator
from itertools import chain

from .tool_intent import classify_file_intent


_UNSAVED_NOTE = "(Not saved — ask me to create it and I will write it to {folder}.)"
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_FILE_CLAIM_RE = re.compile(
    r"\b(?:saved|created|wrote|written|generated)\b"
    r"(?:\s+(?:the|a|an|this|that))?\s+(?:file|script|document)\b",
    re.IGNORECASE,
)
# The path-bearing claim patterns are not regexes.  As regexes, a path class
# holding "." followed by "\." backtracked quadratically while the search
# restarted at every offset of a long dotted, slashed or hyphenated token
# ("saved/" * 3333 took 24 s).  _ClaimScan below tokenises the reply once and
# reproduces, match for match, finditer over these patterns (case-insensitive):
#   active  \b<verb>\b[^\n.!?]{0,120}(?:<path>|file|script|document)\b
#   paths   \b<verb>\b[^\n.!?]{0,100}?(<path>)\b
#   passive \b(?:[A-Za-z0-9_.`'"/\\-]+\s+)?(?:file|script|document)\b<be>
#           |\b<path>\b<be>
# with <verb> = (?:saved|created|wrote|written|generated),
# <path> = [A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,12} and
# <be> = \s+(?:was|has been|is)\s+(?:saved|created|written|generated)\b.
_CLAIM_VERB = r"(?:saved|created|wrote|written|generated)"
_CLAIM_WORD_RE = re.compile(rf"\b{_CLAIM_VERB}\b", re.IGNORECASE)
_PATH_RUN_RE = re.compile(r"[A-Za-z0-9_./\\-]+", re.IGNORECASE)
_QUOTED_RUN_RE = re.compile(r"[A-Za-z0-9_.`'\"/\\-]+", re.IGNORECASE)
_SPACE_RUN_RE = re.compile(r"\s+")
# A dot that can end <path>: its extension is followed by a word boundary.
_GOOD_DOT_RE = re.compile(r"\.(?=[A-Za-z0-9]{1,12}\b)", re.IGNORECASE)
_EXT_RE = re.compile(r"[A-Za-z0-9]{1,12}", re.IGNORECASE)
_FILE_NOUN_RE = re.compile(r"(?:file|script|document)\b", re.IGNORECASE)
_GAP_STOP_RE = re.compile(r"[\n.!?]")
# (?<!\s) starts <be> only at the start of a whitespace run, so the search does
# not rescan the rest of a long run from every offset inside it.
_PASSIVE_BE_RE = re.compile(
    r"(?<!\s)\s+(?:was|has been|is)\s+(?:saved|created|written|generated)\b",
    re.IGNORECASE,
)
_BOUNDARY_RE = re.compile(r"\b")
_NO_PREFIX_RE = re.compile(r"no\s+", re.IGNORECASE)
_NEGATED_PREFIX_RE = re.compile(
    r"(?:did\s+not|didn't|haven't|hasn't|never|not|no|once|when|if|after)\s+(?:\w+\s+){0,2}$"
)
_RUN_PATH_RE = re.compile(
    r"\b(?:python(?:\.exe)?|py(?:\.exe)?|node(?:\.exe)?|ruby(?:\.exe)?|bash|sh)"
    r"\s+(?:[\"']([^\"']+)[\"']|(`[^`]+`)|([^\s,;:.!?]+\.[A-Za-z0-9]{1,12}))",
    re.IGNORECASE,
)
_RUN_IT_PATH_RE = re.compile(
    r"\brun\s+(?:it|that|the\s+(?:file|script))\s+(?:with\s+)?"
    r"(?:[\"']([^\"']+)[\"']|(`[^`]+`)|([^\s,;:.!?]+\.[A-Za-z0-9]{1,12}))",
    re.IGNORECASE,
)
_FUTURE_RUN_RE = re.compile(r"\b(?:once|when|if|after)\b[^.?!]{0,40}\brun\b")
_RUN_DIRECT_PATH_RE = re.compile(
    r"\brun\s+(?:(?:python(?:\.exe)?|py(?:\.exe)?)\s+)?"
    r"(?:[\"'`]([^\"'`]+)[\"'`]|"
    r"([A-Za-z]:[\\/][^\s,;!?]+\.[A-Za-z0-9]{1,12})|"
    r"([.\\/A-Za-z0-9_-]+\.[A-Za-z0-9]{1,12}))",
    re.IGNORECASE,
)


class _ClaimScan:
    """Near-linear matcher for the active, path and passive claim patterns.

    Built once per reply and read-only afterwards; owns only lists of offsets
    into ``text``.  Every lookup is a bisect or dict probe, and the scans that
    walk text (word-boundary searches) cover disjoint spans, so the total cost
    is O(len(text) log len(text)) at worst.  Not shared across threads.
    """

    __slots__ = (
        "claim_words", "good_dots", "noun_by_end", "noun_ends", "noun_starts",
        "ok_dots", "ok_starts", "passive_be", "stops", "text", "tok_ends",
        "tok_starts",
    )

    def __init__(self, text: str) -> None:
        self.text = text
        self.claim_words = [m.span() for m in _CLAIM_WORD_RE.finditer(text)]
        self.passive_be = [m.span() for m in _PASSIVE_BE_RE.finditer(text)]
        self.stops: list[int] = []
        self.tok_starts: list[int] = []
        self.tok_ends: list[int] = []
        self.good_dots: list[int] = []
        # Tokens whose last good dot lies after their first character, i.e.
        # tokens where <path> can start at the first character.
        self.ok_starts: list[int] = []
        self.ok_dots: list[int] = []
        self.noun_starts: list[int] = []
        self.noun_ends: list[int] = []
        self.noun_by_end: dict[int, int] = {}
        if not self.claim_words and not self.passive_be:
            return
        self.stops = [m.start() for m in _GAP_STOP_RE.finditer(text)]
        self.good_dots = good = [m.start() for m in _GOOD_DOT_RE.finditer(text)]
        for match in _PATH_RUN_RE.finditer(text):
            start, end = match.span()
            self.tok_starts.append(start)
            self.tok_ends.append(end)
            index = bisect_left(good, end) - 1
            if index >= 0 and good[index] > start:
                self.ok_starts.append(start)
                self.ok_dots.append(good[index])
        for match in _FILE_NOUN_RE.finditer(text):
            start, end = match.span()
            self.noun_starts.append(start)
            self.noun_ends.append(end)
            self.noun_by_end[end] = start

    def _stop_at_or_after(self, index: int) -> int:
        found = bisect_left(self.stops, index)
        return self.stops[found] if found < len(self.stops) else len(self.text)

    def _last_dot_after(self, index: int) -> int:
        """Last good dot after ``index`` in the token holding ``index``, or -1."""

        token = bisect_right(self.tok_starts, index) - 1
        if token < 0 or self.tok_ends[token] <= index:
            return -1
        found = bisect_left(self.good_dots, self.tok_ends[token]) - 1
        if found >= 0 and self.good_dots[found] > index:
            return self.good_dots[found]
        return -1

    def _ext_end(self, dot: int) -> int:
        match = _EXT_RE.match(self.text, dot + 1)
        return match.end() if match is not None else dot + 1

    def _first_boundary(self, start: int, end: int) -> int:
        """First word boundary in [start, end), or -1."""

        if start >= end:
            return -1
        match = _BOUNDARY_RE.search(self.text, start, end)
        return match.start() if match is not None and match.start() < end else -1

    def active_claims(self) -> Iterator[tuple[int, int]]:
        """Spans of the greedy "active" pattern, as finditer yields them."""

        resume = 0
        for start, verb_end in self.claim_words:
            if start < resume:
                continue
            limit = min(verb_end + 120, self._stop_at_or_after(verb_end))
            # Greedy gap: the match ends at the alternative that starts last.
            at = end = -1
            last = bisect_right(self.ok_starts, limit) - 1
            if last >= 0 and self.ok_starts[last] > verb_end:
                dot = self.ok_dots[last]
            else:
                dot = self._last_dot_after(verb_end)
            if dot >= 0:
                at, end = min(dot - 1, limit), self._ext_end(dot)
            noun = bisect_right(self.noun_starts, limit) - 1
            if noun >= 0 and self.noun_starts[noun] >= verb_end and self.noun_starts[noun] > at:
                end = self.noun_ends[noun]
            if end >= 0:
                yield start, end
                resume = end

    def claim_path_spans(self) -> Iterator[tuple[int, int]]:
        """Spans of group 1 of the lazy "paths" pattern, as finditer yields them."""

        resume = 0
        for start, verb_end in self.claim_words:
            if start < resume:
                continue
            # Lazy gap: the first token, from the verb's end on, holding <path>.
            dot = self._last_dot_after(verb_end)
            at = verb_end
            if dot < 0:
                limit = min(verb_end + 100, self._stop_at_or_after(verb_end))
                first = bisect_right(self.ok_starts, verb_end)
                if first >= len(self.ok_starts) or self.ok_starts[first] > limit:
                    continue
                at, dot = self.ok_starts[first], self.ok_dots[first]
            end = self._ext_end(dot)
            yield at, end
            resume = end

    def passive_claims(self) -> Iterator[tuple[int, int]]:
        """Spans of the "passive" pattern, as finditer yields them."""

        if not self.passive_be:
            return
        text = self.text
        spaces = {m.end(): m.start() for m in _SPACE_RUN_RE.finditer(text)}
        quoted = {m.end(): m.start() for m in _QUOTED_RUN_RE.finditer(text)}
        resume = 0
        for be_start, be_end in self.passive_be:
            candidates: list[int] = []
            noun = self.noun_by_end.get(be_start)
            if noun is not None:
                space = spaces.get(noun)
                run = quoted.get(space) if space is not None else None
                if space is not None and run is not None:
                    candidates.append(self._first_boundary(max(run, resume), space))
                if noun >= resume and _BOUNDARY_RE.match(text, noun):
                    candidates.append(noun)
            token = bisect_left(self.tok_ends, be_start)
            if token < len(self.tok_ends) and self.tok_ends[token] == be_start:
                found = bisect_left(self.good_dots, be_start) - 1
                dot = self.good_dots[found] if found >= 0 else -1
                if dot > self.tok_starts[token] and self._ext_end(dot) == be_start:
                    begin = max(self.tok_starts[token], resume)
                    candidates.append(self._first_boundary(begin, dot))
            starts = [candidate for candidate in candidates if candidate >= 0]
            if starts:
                yield min(starts), be_end
                resume = be_end


def _without_code_fences(text: str) -> str:
    """Ignore fenced examples, where imperative text is usually illustrative."""

    return _CODE_FENCE_RE.sub(" ", text)


def _normalise_path(value: str) -> str:
    value = value.strip().strip("`\"'")
    value = value.rstrip(".,;:!?)]}")
    return value.replace("\\", "/").casefold()


def _written(path: str, written_paths: tuple[str, ...]) -> bool:
    candidate = _normalise_path(path)
    if not candidate:
        return False
    for written in written_paths:
        actual = _normalise_path(written)
        if candidate == actual or actual.endswith("/" + candidate):
            return True
    return False


def _run_paths(text: str) -> tuple[str, ...]:
    paths: list[str] = []
    for match in (
        *_RUN_PATH_RE.finditer(text),
        *_RUN_IT_PATH_RE.finditer(text),
        *_RUN_DIRECT_PATH_RE.finditer(text),
    ):
        paths.extend(part for part in match.groups() if part)
    return tuple(paths)


def _has_file_claim(text: str, scan: _ClaimScan | None = None) -> bool:
    scan = scan if scan is not None else _ClaimScan(text)
    spans = chain(
        (match.span() for match in _FILE_CLAIM_RE.finditer(text)),
        scan.active_claims(),
        scan.passive_claims(),
    )
    for start, end in spans:
        if _NO_PREFIX_RE.match(text, start, end):
            continue
        prefix = text[max(0, start - 32) : start].casefold()
        if _NEGATED_PREFIX_RE.search(prefix):
            continue
        return True
    return False


def _claim_paths(text: str, scan: _ClaimScan | None = None) -> tuple[str, ...]:
    scan = scan if scan is not None else _ClaimScan(text)
    paths = [text[start:end] for start, end in scan.claim_path_spans()]
    seen = set(paths)
    for start, end in scan.passive_claims():
        candidate = text[start:end].split()[0].strip("`\"'")
        if "." in candidate and candidate not in seen:
            paths.append(candidate)
            seen.add(candidate)
    return tuple(paths)


def _case_key(char: str) -> str:
    """Key of ``char``'s class under re.IGNORECASE literal matching.

    Two characters match each other under re.IGNORECASE exactly when their
    keys are equal; tests check this over every code point.
    """

    return char.lower()[0].upper().lower()


def _first_occurrences(text: str, needles: Iterable[str]) -> dict[str, int]:
    """Offset of each needle's first case-insensitive occurrence, or -1.

    Gives what re.search(re.escape(needle), text, re.IGNORECASE).start()
    gives for each needle, but in one Aho-Corasick pass over ``text``:
    searching from the start once per needle is quadratic when a reply names
    many distinct paths.  Characters are first mapped to one representative
    per case class, so plain string equality matches re.IGNORECASE.  Cost is
    O(len(text) + total needle length); pure, no shared state.
    """

    distinct = list(dict.fromkeys(needles))
    if not distinct:
        return {}
    chars = set(text)
    for needle in distinct:
        chars.update(needle)
    representative: dict[str, str] = {}
    table: dict[int, str] = {}
    for char in chars:
        rep = representative.setdefault(_case_key(char), char)
        if rep != char:
            table[ord(char)] = rep
    keyed = {needle: needle.translate(table) for needle in distinct}
    first: dict[str, int] = {}
    goto: list[dict[str, int]] = [{}]
    ends: list[str | None] = [None]
    for key in set(keyed.values()):
        if not key:
            first[key] = 0
            continue
        node = 0
        for char in key:
            child = goto[node].get(char)
            if child is None:
                child = len(goto)
                goto.append({})
                ends.append(None)
                goto[node][char] = child
            node = child
        ends[node] = key
    # fail: longest proper suffix in the trie; out: nearest node on the fail
    # chain that ends a needle (0 for none).
    fail = [0] * len(goto)
    out = [0] * len(goto)
    queue = deque(goto[0].values())
    while queue:
        node = queue.popleft()
        for char, child in goto[node].items():
            link = fail[node]
            while link and char not in goto[link]:
                link = fail[link]
            fail[child] = goto[link].get(char, 0)
            out[child] = fail[child] if ends[fail[child]] is not None else out[fail[child]]
            queue.append(child)
    remaining = sum(1 for end in ends if end is not None)
    node = 0
    for index, char in enumerate(text.translate(table)):
        if not remaining:
            break
        while node and char not in goto[node]:
            node = fail[node]
        node = goto[node].get(char, 0)
        hit = node if ends[node] is not None else out[node]
        # Once a needle is found, every needle on its out chain was found at
        # the same time, so the walk stops at the first one already found.
        while hit and ends[hit] not in first:
            found = ends[hit]
            if found is None:
                break
            first[found] = index + 1 - len(found)
            remaining -= 1
            hit = out[hit]
    return {needle: first.get(keyed[needle], -1) for needle in distinct}


def _is_future_run(text: str, marker: int) -> bool:
    if marker < 0:
        return False
    prefix = text[max(0, marker - 40) : marker].casefold()
    return bool(_FUTURE_RUN_RE.search(prefix))


def guard_unsaved_file_claim(
    text: str, *, folder: str, written_paths: Iterable[str] = ()
) -> str:
    """Append a truthful save note when plain chat claims an unperformed write.

    The check is intentionally narrow: past-tense file claims and executable
    file paths are considered, while fenced code and negated/future examples
    are ignored where practical.  It never reads or writes a path.
    """

    if not isinstance(text, str) or not text:
        return text
    if _UNSAVED_NOTE.format(folder=folder) in text:
        return text
    visible = _without_code_fences(text).replace("`", "")
    written = tuple(str(path) for path in written_paths)

    scan = _ClaimScan(visible)
    claim = _has_file_claim(visible, scan)
    claim_paths = _claim_paths(visible, scan)
    unwritten_runs = [path for path in _run_paths(visible) if not _written(path, written)]
    markers = _first_occurrences(visible, unwritten_runs)
    run_path = next(
        (path for path in unwritten_runs if not _is_future_run(visible, markers[path])),
        None,
    )
    if not claim and run_path is None:
        return text
    unverified_claim = any(not _written(path, written) for path in claim_paths)
    if run_path is not None or unverified_claim or (claim and not written):
        return text + "\n\n" + _UNSAVED_NOTE.format(folder=folder)
    return text


_NO_TOOLS_GUIDANCE = (
    "If this request needs local workspace facts or file contents, you cannot "
    "read or change them in this turn: none of Sonder's tools is callable by "
    "you here. The host, not you, may route such a request to a scoped lane: "
    "read-only workspace tools (workspace_inventory, directory_tree, "
    "file_find, text_search, file_read/file_read_range, "
    "repository_symbol_index), or the gated workbench/agent lane for creating "
    "or editing files. Without a tool result in this conversation, do not "
    "claim that a file was read, written, run, or verified. Suggest that the "
    "user ask for it directly in chat so the host can choose the appropriate "
    "scoped lane."
)


def no_tools_system(system: str, prompt: str) -> str:
    """Add guidance to an unrouted default-local file turn.

    The caller must exclude explicit-model and cloud bypasses. The shared
    classifier remains the only authority for recognizing file turns here.
    """

    if not isinstance(system, str) or not isinstance(prompt, str):
        return system
    if not classify_file_intent(prompt):
        return system
    if _NO_TOOLS_GUIDANCE in system:
        return system
    return f"{system}\n\n{_NO_TOOLS_GUIDANCE}" if system else _NO_TOOLS_GUIDANCE


_NO_VERIFICATION_GUIDANCE = (
    "This chat turn has no tools. Never say or imply that you compiled, ran, "
    "tested, linted or verified code, and never show a Passed/Verified/checkmark "
    "results table, unless a tool result in this conversation shows it. If asked "
    "to validate code, say you cannot run it here."
)


_EXECUTION_TOOLS = frozenset({"run_code", "test_run", "build_run", "lint_run", "workspace_run"})
_RUN_CLAIM_RE = re.compile(
    r"(?:\b(?:i\s+(?:just\s+)?(?:ran|executed|tested|compiled|verified)|"
    r"tests?\s+passed|all\s+\d+\s+tests?\s+pass|execution\s+output|"
    r"output\s+was|exit\s+code\s*(?:is|=|:)?\s*0)\b|[\u2705\u2713])",
    re.IGNORECASE,
)
_UNVERIFIED_RUN_NOTE = (
    "\n\n(Note: no code or test tool executed in this turn, so no runtime output was verified.)"
)


def guard_unverified_run_claim(
    reply: str,
    executed_tools: Iterable[object] = (),
) -> str:
    """Append a factual note if a reply claims execution/pass without a matching tool run."""
    if not isinstance(reply, str) or not reply.strip():
        return reply
    for item in executed_tools:
        name = str(getattr(item, "tool", item) or "").strip()
        status = str(getattr(item, "status", "ok") or "").strip()
        if name in _EXECUTION_TOOLS and status in {"ok", "completed", "ran"}:
            return reply
    if _RUN_CLAIM_RE.search(reply) and _UNVERIFIED_RUN_NOTE.strip() not in reply:
        return reply.rstrip() + _UNVERIFIED_RUN_NOTE
    return reply


def no_verification_claims_system(system: str) -> str:
    """Add the no-fabricated-verification rule to a tool-less chat turn.

    Every served chat turn calls no tools, so the rule is unconditional and is
    kept short.  Idempotent: a system prompt that already carries it is
    returned unchanged.
    """

    if not isinstance(system, str):
        return system
    if _NO_VERIFICATION_GUIDANCE in system:
        return system
    return f"{system}\n\n{_NO_VERIFICATION_GUIDANCE}" if system else _NO_VERIFICATION_GUIDANCE


__all__ = [
    "guard_unsaved_file_claim",
    "guard_unverified_run_claim",
    "no_tools_system",
    "no_verification_claims_system",
]

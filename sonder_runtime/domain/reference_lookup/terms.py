"""Search-term handling: FTS5 query building, identifiers, the term filter and parsing.

Ported unchanged from the lookup client.  Every term is emitted as a quoted
FTS5 string so user or model text can never add operators or column filters.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

MAX_QUERY_TERMS = 40
MAX_MODEL_TERMS = 8
MAX_TERM_CHARS = 60
# Identifiers given title priority per question: each costs one title query.
MAX_IDENTIFIERS = 8

_STOP_TEXT = (
    'a an and are as at be but by can do does did for from has have how i if in into is it '
    'its me my of on or our so than that the their them then there these they this to us was '
    'we were what when where which who whom why will with would you your about please tell '
    'explain mean means')
STOP_WORDS = frozenset(_STOP_TEXT.split())

TERM_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]|[A-Za-z0-9]')

EXPAND_PROMPT = (
    'You are choosing search terms for a reference library of Windows security event pages, '
    'MITRE ATT&CK techniques and Sigma detection rules. Do NOT answer the question below and do '
    'not guess identifiers. List up to 8 search terms on ONE line, separated by commas, nothing '
    'else: the key technical words in the question, the usual documentation wording for them '
    '(for example "disabled" for "turned off", "logon" for "signed in"), and any event '
    'IDs or technique IDs that the question itself mentions.\n\nQuestion: {question}')

# Added to the search-terms request when the passages are for an answer: the same
# short call also says whether the question is in the library's subjects at all.
SCOPE_REQUEST = (
    '\n\nFirst line: the search terms. Second line: RELATED if the question is about computers, '
    'networks, accounts, logs or security in any way, or UNRELATED if it is about something else '
    'entirely.')

IDENTIFIER_RE = re.compile(
    r'0x[0-9a-f]+|\b(?:DET|AN|[TMSG])\d{3,4}(?:\.\d{3})?\b|\d{3,}', re.IGNORECASE)

# The verdict word as the model writes it: upper case, standing alone.  Lower-case
# "related" is an ordinary word and stays a search term.
_VERDICT_RE = re.compile(r'(?<![A-Za-z0-9])(UNRELATED|RELATED)(?![A-Za-z0-9])')
_LINE_LABEL_RE = re.compile(r'^[ \t]*(?:first|second)[ \t]+line[ \t]*:[ \t]*',
                            re.IGNORECASE | re.MULTILINE)


def build_expand_prompt(question: str, *, scope: bool = False) -> str:
    """The user turn that asks the model for search terms (sent without a system prompt).

    With ``scope`` the request also asks for the RELATED / UNRELATED line.
    """
    prompt = EXPAND_PROMPT.format(question=question)
    return prompt + SCOPE_REQUEST if scope else prompt


def split_scope(content: str) -> tuple[str, bool]:
    """(the reply without its verdict, True when the verdict is UNRELATED).

    Models put the verdict on its own line, as the last item of the terms line
    or after the last term without a comma; all three are read.  The last
    verdict word decides.  A reply without one is not a verdict of UNRELATED,
    so a model that ran out of room listing terms changes nothing.
    """
    verdicts = _VERDICT_RE.findall(content)
    text = _LINE_LABEL_RE.sub('', _VERDICT_RE.sub('', content))
    return text, bool(verdicts) and verdicts[-1] == 'UNRELATED'


def query_terms(text: str | None, extra_terms: Iterable[object] = ()) -> list[str]:
    """The lower-cased search terms of one query, in order, at most ``MAX_QUERY_TERMS``.

    The words of ``text`` come first.  Each extra term follows as a phrase and
    then as each of its individual words, so "security log cleared" also finds
    "audit log was cleared".  Stop words without a digit and repeats are left out.
    """
    candidates = [m.group(0) for m in TERM_RE.finditer(text or '')]
    for extra in extra_terms or ():
        if isinstance(extra, str) and TERM_RE.search(extra):
            phrase = ' '.join(extra.split())
            candidates.append(phrase)
            if ' ' in phrase:
                candidates.extend(m.group(0) for m in TERM_RE.finditer(phrase))
    terms, seen = [], set()
    for term in candidates:
        term = term.lower()
        if term in seen:
            continue
        if term in STOP_WORDS and not any(c.isdigit() for c in term):
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return terms


def quoted(term: str) -> str:
    """One term as a double-quoted FTS5 string: it can add no operator, column filter or NEAR."""
    return '"' + term.replace('"', '""') + '"'


def fts_query(text: str | None, extra_terms: Iterable[object] = ()) -> str:
    """Build a safe FTS5 MATCH expression: the quoted ``query_terms`` joined by OR, or ''.

    Every term is emitted as a double-quoted string, so user input can never
    add FTS5 operators, column filters or prefix/NEAR syntax.
    """
    return ' OR '.join(quoted(t) for t in query_terms(text, extra_terms))


def identifiers(text: str | None) -> set[str]:
    """Lower-cased identifiers in text: digit runs (3+), hex, ATT&CK style ids."""
    return {m.group(0).lower() for m in IDENTIFIER_RE.finditer(text or '')}


def ordered_identifiers(text: str | None, limit: int | None = None) -> list[str]:
    """Identifiers in text, lower-cased, in order of first appearance.

    With ``limit`` the scan stops once that many distinct identifiers are found.
    """
    found: list[str] = []
    seen: set[str] = set()
    for m in IDENTIFIER_RE.finditer(text or ''):
        if limit is not None and len(found) >= limit:
            break
        ident = m.group(0).lower()
        if ident not in seen:
            seen.add(ident)
            found.append(ident)
    return found


def filter_terms(question: str, terms: list[str]) -> tuple[list[str], list[str]]:
    """Split terms into (kept, dropped); a term naming an identifier absent from the question is dropped.

    IDENTIFIER_RE is deliberately broad: any run of 3+ digits (years such as 2019, ports such
    as 3389), hex values and ATT&CK style ids count as identifiers, as do names containing such
    a run (for example SHA256). A term holding one is dropped unless the question holds it too.
    """
    known = identifiers(question)
    kept: list[str] = []
    dropped: list[str] = []
    for term in terms:
        (kept if identifiers(term) <= known else dropped).append(term)
    return kept, dropped


def parse_terms(content: str) -> list[str]:
    """Leniently parse a comma/line separated keyword list from model output."""
    content = re.sub(r'^\s*(?:search\s+)?(?:keywords?|terms?)\s*:\s*', '', content,
                     flags=re.IGNORECASE)
    terms: list[str] = []
    for item in re.split(r'[,\n]', content):
        item = re.sub(r'^\s*(?:\d+[.)]|[-*•])\s*', '', item)
        item = item.strip().strip('"\'`').strip().rstrip('.;:').strip().strip('"\'`').strip()
        if item and len(item) <= MAX_TERM_CHARS and item.lower() not in {t.lower() for t in terms}:
            terms.append(item)
        if len(terms) >= MAX_MODEL_TERMS:
            break
    return terms

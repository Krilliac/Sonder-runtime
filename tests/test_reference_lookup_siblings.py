"""Reference lookup: same-identifier siblings across sources, and the exact technique-id title match.

In the expanded library an emulation test or a detection rule titled "T1003.001 ..."
repeats the question's words more often than the ATT&CK page it names, so the page
itself fell out of the top passages.  The pure rule is tested on rows; the title query
and the whole retrieve() on small indexes built in tmp_path (never real reference data).
"""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.reference_index import ReferenceIndex
from sonder_runtime.application.chat import reference_lookup as lookup
from sonder_runtime.domain.reference_lookup import selection
from sonder_runtime.domain.reference_lookup.models import IndexState, RankedRow, ReferenceIndexError
from tests._reference_lookup_index import create_index


def _row(doc, title, source, rank, body=None):
    return RankedRow(title=title, heading='', body=body or f'{source} body {doc}', source=source,
                     url='', path=f'{source}/{doc}.md', doc=doc, rank=rank, rowid=doc)


# ---------------------------------------------------------------------------
# lead_identifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('title, expected', [
    ('T1003.001 - OS Credential Dumping: LSASS Memory', 't1003.001'),
    ('T1003.001 OS Credential Dumping: LSASS Memory', 't1003.001'),
    ('t1003.001 lower case', 't1003.001'),
    ('  T1110: Brute Force', 't1110'),
    ('T1110 Brute Force', 't1110'),
    ('M1032 Multi-factor Authentication', 'm1032'),
    ('S0002 Mimikatz', 's0002'), ('G0016 APT29', 'g0016'),
    ('DET0455 Abuse of PowerShell', 'det0455'), ('AN1234 Analytic', 'an1234'),
    ('T0821 Modify Controller Tasking', 't0821'),
    ('LSASS Memory Dump (T1003.001)', None),       # names it, does not open with it
    ('4625(F): An account failed to log on.', None),
    ('CWE-79: Improper Neutralization', None),
    ('T1003.0011 x', None), ('T10031 x', None), ('T12 x', None), ('T1003x', None),
    ('Tcpip', None), ('', None), (None, None),
])
def test_lead_identifier_is_an_attack_style_id_opening_the_title(title, expected):
    assert selection.lead_identifier(title) == expected


# ---------------------------------------------------------------------------
# with_siblings (pure: the title query is a callable)
# ---------------------------------------------------------------------------

ATOMIC = _row(1, 'T1003.001 - OS Credential Dumping: LSASS Memory', 'atomic', -50.0)
RULE_A = _row(2, 'LSASS Memory Dump Creation', 'rules', -49.0)
RULE_B = _row(3, 'Potential Credential Access via LSASS', 'rules', -48.0)
RULE_C = _row(4, 'Suspicious Module Loaded by LSASS', 'rules', -47.0)
ATOMIC_2 = _row(1, 'T1003.001 - OS Credential Dumping: LSASS Memory', 'atomic', -46.0, 'second test')
PAGE = _row(9, 'T1003.001 OS Credential Dumping: LSASS Memory', 'attack', -30.0)
RANKED = [ATOMIC, RULE_A, RULE_B, RULE_C, ATOMIC_2, PAGE]


def _titles(chosen):
    return [(p.source, p.title) for p in chosen]


def test_without_siblings_the_quoting_pages_fill_the_top_five():
    assert PAGE.title not in [p.title for p in selection.select_passages(RANKED, 5)]


def test_the_named_page_of_another_source_follows_the_page_that_opens_with_its_id():
    calls = []

    def fetch(ident):
        calls.append(ident)
        return [ATOMIC, PAGE]          # best row per source, best first: the own source comes back too

    chosen = selection.select_passages(selection.with_siblings(RANKED, 5, fetch), 5)
    assert _titles(chosen)[:2] == [('atomic', ATOMIC.title), ('attack', PAGE.title)]
    assert len(chosen) == 5 and [p.rowid for p in chosen].count(PAGE.rowid) == 1
    assert calls == ['t1003.001']      # one query: the second atomic row has the same id


def test_with_siblings_keeps_every_ranked_row_after_the_new_order():
    out = selection.with_siblings(RANKED, 5, lambda ident: [PAGE])
    assert out[-len(RANKED):] == RANKED
    assert out[:2] == [ATOMIC, PAGE]


def test_only_other_sources_whose_title_opens_with_the_same_id_are_siblings():
    same_source = _row(20, 'T1003.001 - another atomic page', 'atomic', -40.0)
    only_names_it = _row(21, 'Rule about T1003.001', 'sigma', -39.0)
    sub_technique = _row(22, 'T1003.0012 x', 'odd', -38.0)
    other_id = _row(23, 'T1003 OS Credential Dumping', 'attack', -37.0)
    second_source = _row(24, 'T1003.001 OS Credential Dumping: LSASS Memory', 'attack-mobile', -36.0)
    duplicate_source = _row(25, 'T1003.001 OS Credential Dumping: LSASS Memory', 'attack', -35.0)
    fetched = [same_source, only_names_it, sub_technique, other_id, PAGE, second_source, duplicate_source]
    out = selection.with_siblings([ATOMIC], 1, lambda ident: fetched)
    assert out == [ATOMIC, PAGE, second_source, ATOMIC]


def test_only_the_rows_that_would_be_selected_are_expanded():
    calls = []
    plain = [_row(i, f'Plain rule {i}', 'rules', -60.0 + i) for i in range(3)]
    late = _row(30, 'T1059.001 - Command and Scripting Interpreter: PowerShell', 'atomic', -10.0)
    out = selection.with_siblings(plain + [late], 2, lambda ident: calls.append(ident) or [])
    assert calls == [] and out == plain[:2] + plain + [late]
    selection.with_siblings(plain + [late], 4, lambda ident: calls.append(ident) or [])
    assert calls == ['t1059.001']


def test_with_siblings_for_no_rows_or_no_k_queries_nothing():
    def fail(ident):
        pytest.fail('no title query expected')

    assert selection.with_siblings([], 5, fail) == []
    assert selection.with_siblings(RANKED, 0, fail) == RANKED


def test_a_parent_id_in_the_question_gets_the_parent_page_not_its_sub_techniques():
    rows = [_row(1, 'T1110.002 Brute Force: Password Cracking', 'attack', -50.0),
            _row(2, 'T1110.001 Brute Force: Password Guessing', 'attack', -49.0),
            _row(3, 'T1110 Brute Force', 'attack', -20.0)]
    chosen = selection.priority_rows(rows, ['t1110'], lambda ident: [])
    assert [r.title for r in chosen] == ['T1110 Brute Force']


# ---------------------------------------------------------------------------
# The adapter's title query and the whole retrieve()
# ---------------------------------------------------------------------------

QUESTION = 'What is the MITRE ATT&CK ID for OS Credential Dumping: LSASS Memory?'
_QUOTE = ('MITRE ATT&CK technique ID T1003.001 OS Credential Dumping LSASS Memory: dump lsass memory '
          'to read credential material. ')
# (title, heading, body, source, url, path, doc)
ROWS = [
    ('T1003.001 - OS Credential Dumping: LSASS Memory', '', _QUOTE * 3 + 'Atomic test one.',
     'atomic', '', 'atomic/T1003.001.md', 1),
    ('T1003.001 - OS Credential Dumping: LSASS Memory', 'Atomic Test #2', _QUOTE * 3 + 'Atomic test two.',
     'atomic', '', 'atomic/T1003.001.md', 1),
    *[(f'LSASS Memory Dump Rule {n}', '', _QUOTE * 2 + f'Detection rule number {n}.',
       'rules', '', f'rules/{n}.toml', 10 + n) for n in range(5)],
    ('T1003.001 OS Credential Dumping: LSASS Memory', '',
     'Adversaries may attempt to access material stored in the process space of the Local Security '
     'Authority Subsystem Service.', 'attack', 'https://attack.example/T1003/001', 'attack/bundle.json', 50),
    ('T1003.002 OS Credential Dumping: Security Account Manager', '',
     'Adversaries may extract material from the SAM database.', 'attack', '', 'attack/bundle.json', 51),
    ('Unrelated page', '', 'This body mentions T1003.001 but its title does not.', 'notes', '', 'n.md', 60),
]


@pytest.fixture
def quoting(tmp_path) -> ReferenceIndex:
    return ReferenceIndex(create_index(tmp_path / 'idx' / 'quoting.sqlite', ROWS))


class _SearchOnly:
    """An index that offers search only: no siblings."""

    def __init__(self, inner):
        self._inner = inner

    def status(self):
        return self._inner.status()

    def search_rows(self, match, limit, idents=()):
        return self._inner.search_rows(match, limit, idents)


class _RaisingTitles(_SearchOnly):
    def best_title_rows(self, ident):
        raise ReferenceIndexError(IndexState.ERROR, 'title query failed')


def _sources(bundle):
    return [(p.source, p.title) for p in bundle.passages]


def test_best_title_rows_is_one_row_per_source_whose_title_holds_the_id(quoting):
    before = quoting.path.read_bytes()
    rows = quoting.best_title_rows('t1003.001')
    assert sorted(r.source for r in rows) == ['atomic', 'attack']
    assert all('T1003.001' in r.title for r in rows)
    assert [r.rank for r in rows] == sorted(r.rank for r in rows)       # best first
    assert all(r.rowid > 0 for r in rows)
    assert quoting.best_title_rows('') == () and quoting.best_title_rows('zzz9999') == ()
    assert quoting.path.read_bytes() == before


def test_best_title_rows_on_a_missing_index_raises_typed(tmp_path):
    with pytest.raises(ReferenceIndexError) as err:
        ReferenceIndex(tmp_path / 'absent.sqlite').best_title_rows('t1003.001')
    assert err.value.state is IndexState.MISSING


def test_retrieve_puts_the_attack_page_next_to_the_test_that_names_it(quoting):
    plain = lookup.retrieve(QUESTION, None, 5, 100000, index=_SearchOnly(quoting))
    assert plain.applied and ('attack', ROWS[7][0]) not in _sources(plain)
    bundle = lookup.retrieve(QUESTION, None, 5, 100000, index=quoting)
    assert bundle.applied and len(bundle.passages) == 5
    assert _sources(bundle)[:2] == [('atomic', ROWS[0][0]), ('attack', ROWS[7][0])]


def test_a_failing_title_query_keeps_the_plain_ranking(quoting):
    plain = lookup.retrieve(QUESTION, None, 5, 100000, index=_SearchOnly(quoting))
    failing = lookup.retrieve(QUESTION, None, 5, 100000, index=_RaisingTitles(quoting))
    assert failing.applied and failing.reason is lookup.LookupReason.APPLIED
    assert _sources(failing) == _sources(plain)

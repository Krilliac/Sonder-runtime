"""Reference lookup: the read-only index store, retrieval, fail-open states and reply wrapping.

Search-level cases are ported from the standalone lookup client's tests (the same corpora
shapes and expectations) and run against indexes built in tmp_path with the client's own
CREATE statements.  The model call is always a fake.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from sonder_runtime.adapters.reference_index import ReferenceIndex, classify_error
from sonder_runtime.application.chat import reference_lookup as lookup
from sonder_runtime.domain.reference_lookup import selection, terms
from sonder_runtime.domain.reference_lookup.models import (
    IndexState,
    IndexStatus,
    ReferenceIndexError,
)
from sonder_runtime.platform import paths as runtime_paths
from sonder_runtime.platform import reference_library_config as config
from tests._reference_lookup_index import (
    CREATE_META,
    INSERT_PASSAGE,
    SMALL_ROWS,
    create_index,
)

BIG_BUDGET = selection.PASSAGE_CHAR_LIMIT


@pytest.fixture
def small(tmp_path) -> ReferenceIndex:
    return ReferenceIndex(create_index(tmp_path / 'idx' / 'small.sqlite'))


def _search(index, question, k=6, extra=()):
    """The store plus selection steps of ``retrieve`` without the term filter (client ``search``)."""
    match = terms.fts_query(question, extra)
    rows = index.search_rows(match, selection.candidate_limit(k), terms.ordered_identifiers(question))
    return selection.select_passages(rows, k)


def _titles(passages):
    return [p.title for p in passages]


# ---------------------------------------------------------------------------
# Index states
# ---------------------------------------------------------------------------

def test_ready_status_reads_meta(small):
    status = small.status()
    assert status.state is IndexState.READY and status.ready
    assert status.passages == 6
    assert status.sources == (('attack', 2), ('events', 3), ('sigma', 1))
    assert status.built_at == '2026-01-01T00:00:00+00:00'


def _text_file(tmp_path):
    p = tmp_path / 'notes.sqlite'
    p.write_text('this is not a database, just text\n' * 40, encoding='utf-8')
    return p


def _plain_table(tmp_path):
    p = tmp_path / 'plain.sqlite'
    conn = sqlite3.connect(p)
    conn.execute('CREATE TABLE passages(title, heading, body, source, url, path, doc)')
    conn.execute(CREATE_META)
    conn.execute("INSERT INTO meta VALUES ('summary', '{}')")
    conn.commit()
    conn.close()
    return p


def _no_meta(tmp_path):
    return create_index(tmp_path / 'nometa.sqlite', meta=False)


def _no_summary_row(tmp_path):
    p = create_index(tmp_path / 'nosummary.sqlite')
    conn = sqlite3.connect(p)
    conn.execute("DELETE FROM meta WHERE key = 'summary'")
    conn.commit()
    conn.close()
    return p


def _bad_summary(tmp_path):
    p = create_index(tmp_path / 'badsummary.sqlite')
    conn = sqlite3.connect(p)
    conn.execute("UPDATE meta SET value = 'not json' WHERE key = 'summary'")
    conn.commit()
    conn.close()
    return p


def _missing_columns(tmp_path):
    p = tmp_path / 'cols.sqlite'
    conn = sqlite3.connect(p)
    conn.execute('CREATE VIRTUAL TABLE passages USING fts5(title, body)')
    conn.execute(CREATE_META)
    conn.execute("INSERT INTO meta VALUES ('summary', '{}')")
    conn.commit()
    conn.close()
    return p


def _empty_file(tmp_path):
    p = tmp_path / 'empty.sqlite'
    p.write_bytes(b'')
    return p


def _directory(tmp_path):
    p = tmp_path / 'folder.sqlite'
    p.mkdir()
    return p


BAD_INDEXES = [
    ('missing', lambda tmp: tmp / 'absent.sqlite', IndexState.MISSING),
    ('text', _text_file, IndexState.NOT_SQLITE),
    ('plain_table', _plain_table, IndexState.WRONG_SCHEMA),
    ('no_meta', _no_meta, IndexState.WRONG_SCHEMA),
    ('no_summary', _no_summary_row, IndexState.WRONG_SCHEMA),
    ('bad_summary', _bad_summary, IndexState.WRONG_SCHEMA),
    ('missing_columns', _missing_columns, IndexState.WRONG_SCHEMA),
    ('empty_file', _empty_file, IndexState.WRONG_SCHEMA),
    ('directory', _directory, IndexState.UNREADABLE),
]


@pytest.mark.parametrize('name, make, state', BAD_INDEXES, ids=[b[0] for b in BAD_INDEXES])
def test_bad_index_has_a_typed_status_and_search_raises_typed(tmp_path, name, make, state):
    index = ReferenceIndex(make(tmp_path))
    status = index.status()
    assert status.state is state and not status.ready
    assert '\n' not in status.detail
    with pytest.raises(ReferenceIndexError) as err:
        index.search_rows('"logon"', 10)
    assert err.value.state is state


@pytest.mark.parametrize('name, make, state', BAD_INDEXES, ids=[b[0] for b in BAD_INDEXES])
def test_retrieve_fails_open_on_every_bad_index(tmp_path, name, make, state):
    index = ReferenceIndex(make(tmp_path))
    asked = []
    bundle = lookup.retrieve('what is 4625?', lambda p: asked.append(p) or 'logon', 6, BIG_BUDGET,
                             index=index)
    assert bundle.applied is False and bundle.passages == ()
    assert bundle.reason is lookup.LookupReason.INDEX_UNAVAILABLE
    assert bundle.index_state is state and bundle.reason_text == state.value
    assert asked == []                                   # no model call for an unusable index
    gen = lambda prompt, history=None: 'bare answer'
    assert lookup.wrap_generate(gen, bundle, 'abcd1234') is gen


def test_classify_error_maps_sqlite_messages():
    assert classify_error(sqlite3.OperationalError('no such module: fts5')) is IndexState.FTS5_UNAVAILABLE
    assert classify_error(sqlite3.OperationalError('no such table: passages')) is IndexState.WRONG_SCHEMA
    assert classify_error(sqlite3.OperationalError('unable to open database file')) is IndexState.UNREADABLE
    assert classify_error(sqlite3.DatabaseError('file is not a database')) is IndexState.NOT_SQLITE
    assert classify_error(sqlite3.OperationalError('database is locked')) is IndexState.ERROR


def test_status_is_cached_by_path_mtime_and_size(tmp_path):
    path = create_index(tmp_path / 'c.sqlite')
    index = ReferenceIndex(path)
    first = index.status()
    assert index.status() is first                                   # unchanged file: cached object
    path.write_bytes(b'not a database any more' * 10)                # size and mtime change
    changed = index.status()
    assert changed is not first and changed.state is IndexState.NOT_SQLITE
    create_index(tmp_path / 'c2.sqlite').replace(path)               # a good file again
    assert index.status().state is IndexState.READY
    path.unlink()
    assert index.status().state is IndexState.MISSING


# ---------------------------------------------------------------------------
# The store never writes
# ---------------------------------------------------------------------------

def test_store_never_writes_and_its_connection_is_read_only(small):
    path = small.path
    before = path.read_bytes()
    assert small.status().ready
    assert _search(small, 'account logon failed locked', 10)
    assert small.search_rows('"4625"', 50, ['4625'])
    with small.connect() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(INSERT_PASSAGE, ('t', 'h', 'b', 's', 'u', 'p', 99))
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('CREATE TABLE intruder(x)')
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE meta SET value = 'x'")
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ['small.sqlite']   # no journal or wal left


_NO_QUESTION_MARK = pytest.mark.skipif(os.name == 'nt', reason='a folder name cannot contain ? on Windows')


@pytest.mark.parametrize('folder', [
    pytest.param('what?', marks=_NO_QUESTION_MARK), 'a#b', 'a&b=c;d',
    pytest.param('x?y#z', marks=_NO_QUESTION_MARK), 'sp ace %41', 'hash#%23end'])
def test_path_with_uri_special_characters_still_opens_read_only(tmp_path, folder):
    index = ReferenceIndex(create_index(tmp_path / folder / 'r.sqlite'))
    before = index.path.read_bytes()
    assert index.status().ready
    assert 'events/4625.md' in [p.path for p in _search(index, '4625')]
    with index.connect() as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute(INSERT_PASSAGE, ('t', 'h', 'b', 's', 'u', 'p', 99))
    assert index.path.read_bytes() == before
    assert not any(p.name.startswith(('r.sqlite-', 'r.sqlite?')) for p in index.path.parent.iterdir())


def test_stored_path_column_is_only_a_label(tmp_path):
    secret = tmp_path / 'outside.txt'
    secret.write_text('outside content', encoding='utf-8')
    rows = [('Escape 4999', '', 'a passage about escapes', 'x', '', str(secret), 1),
            ('Parent 4998', '', 'another passage about escapes', 'x', '', '../../outside.txt', 2)]
    index = ReferenceIndex(create_index(tmp_path / 'e.sqlite', rows))
    found = _search(index, 'escapes')
    assert sorted(p.path for p in found) == sorted([str(secret), '../../outside.txt'])
    assert secret.read_text(encoding='utf-8') == 'outside content'


# ---------------------------------------------------------------------------
# Search through the store, ported from the client's search tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('query, expected_path', [
    ('4625', 'events/4625.md'), ('0xC000006A', 'events/4625.md'), ('4740', 'events/4740.md')])
def test_identifier_queries_rank_the_right_passage_first(small, query, expected_path):
    results = _search(small, query)
    assert results and results[0].path == expected_path
    if query.startswith('0x'):
        assert '0xC000006A' in results[0].text


def test_technique_id_matches_only_that_subtechnique(small):
    results = _search(small, 'T1110.001')
    assert results and results[0].title == 'T1110.001 Brute Force: Password Guessing'
    assert all('T1110.001' in p.title for p in results)
    spray = _search(small, 'T1110.003')
    assert spray and spray[0].title.startswith('T1110.003') and 'Password Spraying' in spray[0].title


def test_4625_title_hit_beats_4624_body_mention(small):
    results = _search(small, '4625')
    assert len(results) >= 2 and results[0].path == 'events/4625.md'
    assert 'events/4624.md' in [p.path for p in results]


def test_title_match_outranks_body_only_match(tmp_path):
    rows = list(SMALL_ROWS) + [('Field Notes', '', 'We saw kerberoasting twice, kerberoasting again.',
                                'notes', '', 'notes/b.md', 7)]
    results = _search(ReferenceIndex(create_index(tmp_path / 'k.sqlite', rows)), 'kerberoasting')
    assert [p.path for p in results][:2] == ['sigma/a.yml', 'notes/b.md']
    assert results[0].score > results[1].score


def test_title_foo_is_not_a_column_filter(tmp_path):
    rows = [('Bar', '', 'some foo content here', 'notes', '', 'notes/foo-body.md', 1),
            ('Bar2', '', 'nothing relevant at all', 'notes', '', 'notes/bar2.md', 2)]
    index = ReferenceIndex(create_index(tmp_path / 'f.sqlite', rows))
    assert [p.path for p in _search(index, 'title:foo')] == ['notes/foo-body.md']
    assert [p.path for p in _search(index, 'foo')] == ['notes/foo-body.md']


def test_extra_terms_change_the_result_and_unknown_words_find_nothing(tmp_path):
    rows = [('Alpha', '', 'banana banana', 'fruit', '', 'fruit/alpha.md', 1),
            ('Beta', '', 'mango', 'fruit', '', 'fruit/beta.md', 2),
            ('Gardening', '', 'soil and compost', 'filler', '', 'filler/f0.md', 3)]
    index = ReferenceIndex(create_index(tmp_path / 'x.sqlite', rows))
    assert [p.path for p in _search(index, 'banana')] == ['fruit/alpha.md']
    assert {p.path for p in _search(index, 'banana', extra=['mango'])} == {'fruit/alpha.md', 'fruit/beta.md'}
    assert [p.path for p in _search(index, 'zzzzqq', extra=['mango'])] == ['fruit/beta.md']
    assert _search(index, 'zzzzqq') == () and _search(index, 'xyzzyplugh') == ()


def test_scores_positive_sorted_descending_and_k_bounds(small):
    results = _search(small, 'account logon failed locked', 10)
    assert len(results) >= 3
    scores = [p.score for p in results]
    assert all(s > 0 for s in scores) and scores == sorted(scores, reverse=True)
    assert len(_search(small, 'account', k=1)) == 1 and _search(small, 'account', k=0) == ()


def test_search_rows_with_empty_match_or_limit_is_empty(small):
    assert small.search_rows('', 10) == () and small.search_rows('"logon"', 0) == ()


@pytest.mark.parametrize('text', ['"', 'say "hello', '(', '*', ':', '^', 'AND', 'NEAR(a b)', 'title:foo',
                                  'foo*', '{body}: y', "it's", ';DROP TABLE passages;--'])
def test_search_never_raises_on_hostile_input(small, text):
    assert isinstance(_search(small, text), tuple)
    assert isinstance(_search(small, 'logon', extra=[text]), tuple)


@pytest.mark.parametrize('k, expected', [(2, 2), (3, 2), (4, 2), (6, 4), (8, 6), (10, 8)])
def test_per_source_cap_through_the_store(tmp_path, k, expected):
    rows = [(f'Doc {i}', '', f'quokka number {i} lives on the island with unique tail{i}.',
             'only', '', f'only/d{i}.md', i) for i in range(12)]
    results = _search(ReferenceIndex(create_index(tmp_path / 'q.sqlite', rows)), 'quokka', k)
    assert len(results) == expected


def test_per_document_cap_and_duplicate_body_through_the_store(tmp_path):
    body = 'The zebracrossing marker text is identical in these documents here.'
    rows = [('One', '', body, 'a', '', 'a/one.md', 1), ('Two', '', body, 'b', '', 'b/two.md', 2),
            ('Three', '', body + ' Plus an extra distinguishing sentence.', 'c', '', 'c/three.md', 3),
            ('Other', '', 'Unrelated content about gardening and soil and compost.', 'd', '', 'd/o.md', 4)]
    found = [p.path for p in _search(ReferenceIndex(create_index(tmp_path / 'd.sqlite', rows)), 'zebracrossing')]
    assert len(found) == 2 and 'c/three.md' in found and 'd/o.md' not in found
    sections = [('Big', f'S{i}', f'widget alpha{i}', 'notes', '', 'notes/widgets.md', 1) for i in range(5)]
    sections.append(('Other', '', 'One widget here.', 'notes', '', 'notes/widget2.md', 2))
    results = _search(ReferenceIndex(create_index(tmp_path / 'w.sqlite', sections)), 'widget', k=10)
    paths = [p.path for p in results]
    assert paths.count('notes/widgets.md') == 2 and paths.count('notes/widget2.md') == 1 and len(paths) == 3


# ---- identifier-title priority on a corpus that pushes the titles out of the ranking ------------

FLOOD_WORDS = ['gardenia', 'compost', 'mulch', 'trellis', 'perennial', 'topsoil', 'seedling',
               'pruning', 'irrigation', 'rhizome', 'fertiliser', 'greenhouse']
FLOOD = [' '.join(FLOOD_WORDS)]


def _prio_index(tmp_path) -> ReferenceIndex:
    rows, doc = [], 0

    def add(title, body, source, path, heading=''):
        nonlocal doc
        rows.append((title, heading, body, source, '', path, doc))

    for i in range(300):
        doc += 1
        add(f'Neutral {i}', f'Unrelated archive text number {i} about ledgers and receipts.',
            'neutral', f'neutral/n{i}.md')
    for i in range(150):
        doc += 1
        add(f'{" ".join(FLOOD_WORDS)} {i}', f'Plain flood text {i} with other words.',
            f'flood{i % 5}', f'flood/f{i}.md')
    first = 'System audit policy was changed. The event records a change to the audit policy.'
    doc += 1
    sections = [
        first,
        ('Fields include the subject, the category and the subcategory '
         'GUID that changed.'),
        ('Remarks about the audit policy change and how to '
         'review it later.'),
    ]
    for n, text in enumerate(sections):
        add('4719: System audit policy was changed', text, 'events', 'events/e4719.md', f'Part {n}')
    doc += 1
    add('4719: Mirror of the audit event', first, 'events', 'events/e4719_copy.md')
    doc += 1
    add('Notes on 4719', 'Event 4719 shows up when auditing settings are edited by an admin.',
        'events', 'events/e4719_notes.md')
    doc += 1
    add('4720: A user account was created', 'Generated when a user account is created on the computer.',
        'events', 'events/e4720.md')
    for name, title in [('d14719', '14719: Unrelated event'), ('d47190', '47190: Another event'),
                        ('dx4719y', 'x4719y odd name')]:
        doc += 1
        add(title, 'See event 4719 4719 4719 4719 4719 audit audit audit. ' * 3, 'events',
            f'events/{name}.md')
    doc += 1
    add('T1558 Steal or Forge Kerberos Tickets',
        ('Adversaries may attempt to subvert Kerberos '
         'authentication by stealing or forging tickets.'), 'attack', 'attack/t1558.md')
    for sub, name in [('001', 'Golden Ticket'), ('002', 'Silver Ticket'), ('004', 'AS-REP Roasting')]:
        doc += 1
        add(f'T1558.{sub} {name}', f'The {name} sub-technique of Kerberos ticket abuse, for defenders.',
            'attack', f'attack/t1558_{sub}.md')
    doc += 1
    parts = [
        ('Adversaries may abuse a valid Kerberos ticket granting ticket to request '
         'service tickets.'),
        ('Service tickets are encrypted with the service account '
         'password hash and cracked.'),
        ('Detection looks at unusual ticket requests '
         'with the RC4 encryption type.'),
    ]
    for n, text in enumerate(parts):
        add('T1558.003 Kerberoasting', text, 'attack', 'attack/t1558_003.md', f'Part {n}')
    doc += 1
    add('Kerberoasting Detection', 'generic rule', 'sigma', 'sigma/kerb.yml')
    return ReferenceIndex(create_index(tmp_path / 'prio' / 'p.sqlite', rows))


@pytest.fixture
def prio(tmp_path) -> ReferenceIndex:
    return _prio_index(tmp_path)


def _natural(index, question, k, extra=()):
    """The ranking without identifier priority."""
    rows = index.search_rows(terms.fts_query(question, extra), selection.candidate_limit(k), ())
    return selection.select_passages(rows, k)


def _has(passage, ident):
    return selection.title_has(passage.title, ident)


def test_identifier_in_the_question_puts_title_matches_first_despite_extra_terms(prio):
    natural = _natural(prio, 'what is event 4719', 6, FLOOD)
    assert natural and not any(_has(p, '4719') for p in natural)       # the flood pushes them out
    results = _search(prio, 'what is event 4719', 6, FLOOD)
    assert len(results) == 6
    assert _has(results[0], '4719') and _has(results[1], '4719')
    assert sum(_has(p, '4719') for p in results) == 2                  # at most two priority passages
    assert _titles(results[2:]) == _titles(natural[:4])


def test_priority_passages_come_from_titles_only_never_from_a_body_mention(prio):
    natural = _natural(prio, '4719', 8)
    decoy = ('14719', '47190', 'x4719y')
    assert natural[0].title.startswith(decoy)
    results = _search(prio, '4719', 8)
    assert _has(results[0], '4719') and _has(results[1], '4719')
    assert not any(p.title.startswith(decoy) for p in results[:2])


def test_parent_technique_id_in_the_question_prioritises_titles_with_that_token(prio):
    results = _search(prio, 'tell me about T1558', 6)
    assert _has(results[0], 't1558') and _has(results[1], 't1558')
    # pinned client behaviour: sub-technique pages carry the token too and fill the source cap first
    assert not any(p.title.startswith('T1558 Steal') for p in results)
    assert any(p.title.startswith('T1558 Steal') for p in _search(prio, 'tell me about T1558', 8))


def test_subtechnique_id_prioritises_only_that_subtechnique(prio):
    for question in ['what is t1558.003', 'what is T1558.003', 'WHAT IS T1558.003?']:
        results = _search(prio, question, 6, FLOOD)
        assert _titles(results[:2]) == ['T1558.003 Kerberoasting'] * 2, question
        assert not any(p.title.startswith(('T1558 ', 'T1558.001')) for p in results[:2])


def test_identifier_only_in_extra_terms_gets_no_priority(prio):
    extra = ['4719', 'T1558.003']
    rows = prio.search_rows(terms.fts_query('what changed in the policy', extra),
                            selection.candidate_limit(6), terms.ordered_identifiers('what changed in the policy'))
    assert selection.select_passages(rows, 6) == _natural(prio, 'what changed in the policy', 6, extra)
    flooded = _search(prio, 'what is this', 6, FLOOD + ['4719'])
    assert flooded == _natural(prio, 'what is this', 6, FLOOD + ['4719'])
    assert not any(_has(p, '4719') for p in flooded[:2])


@pytest.mark.parametrize('k', range(1, 9))
def test_caps_and_dedup_hold_with_priority_rows_present(prio, k):
    for extra in ((), FLOOD):
        results = _search(prio, 'event 4719 audit policy', k, extra)
        assert 1 <= len(results) <= k and _has(results[0], '4719')
        per_doc, per_source, bodies = {}, {}, set()
        for p in results:
            per_doc[p.path] = per_doc.get(p.path, 0) + 1
            per_source[p.source] = per_source.get(p.source, 0) + 1
            norm = ' '.join(p.text.split()).lower()
            assert norm not in bodies
            bodies.add(norm)
        assert max(per_doc.values()) <= selection.MAX_PER_DOC == 2
        assert max(per_source.values()) <= max(2, k - 2)


def test_priority_is_capped_at_two_per_identifier_and_k_is_respected(prio):
    one = _search(prio, '4719', 1, FLOOD)
    assert len(one) == 1 and _has(one[0], '4719')
    two = _search(prio, '4719', 2, FLOOD)
    assert len(two) == 2 and all(_has(p, '4719') for p in two)
    assert len({' '.join(p.text.split()).lower() for p in two}) == 2   # mirror copy not repeated


def test_two_identifiers_each_get_their_slots_in_question_order(prio):
    forward = _search(prio, 'compare T1558.003 with 4719', 8, FLOOD)
    assert [_has(p, 't1558.003') for p in forward[:2]] == [True, True]
    assert [_has(p, '4719') for p in forward[2:4]] == [True, True]
    backward = _search(prio, 'compare 4719 with T1558.003', 8, FLOOD)
    assert [_has(p, '4719') for p in backward[:2]] == [True, True]
    assert [_has(p, 't1558.003') for p in backward[2:4]] == [True, True]


def test_repeated_identifier_in_the_question_is_prioritised_once(prio):
    once = _search(prio, 'event 4719', 6, FLOOD)
    many = _search(prio, 'event 4719 and again 4719 plus 4719', 6, FLOOD)
    assert sum(_has(p, '4719') for p in many) == 2 and _titles(many[:2]) == _titles(once[:2])


@pytest.mark.parametrize('question', ['what is kerberoasting', 'audit policy changed', 'ledgers and receipts',
                                      'zzz nothing matches this', 'Kerberos tickets'])
@pytest.mark.parametrize('extra', [(), FLOOD, ['4719', 'T1558.003']])
@pytest.mark.parametrize('k', [1, 3, 6, 8])
def test_question_without_identifier_ranks_exactly_as_before(prio, question, extra, k):
    assert _search(prio, question, k, extra) == _natural(prio, question, k, extra)


@pytest.mark.parametrize('question', ['what is event 9999', 'audit policy 9999', 'T9999.999 audit',
                                      'audit policy 0xdeadbeef', 'M9999 kerberoasting'])
@pytest.mark.parametrize('extra', [(), FLOOD])
def test_identifier_with_no_page_in_the_index_changes_nothing(prio, question, extra):
    actual = _search(prio, question, 6, extra)
    assert actual == _natural(prio, question, 6, extra)
    if 'audit' in question or 'kerberoasting' in question:
        assert actual


@pytest.mark.parametrize('question', [
    'what is "4719"', '4719 * audit', 'title:4719 -audit', '(4719) NEAR audit', '4719 AND OR NOT audit',
    '"unbalanced 4719', "4719' OR 1=1 --", 'NEAR(4719 audit, 2)', '4719:', '^4719',
    '{4719} [audit] \\ 4719', 'T1558.003 "*" :', '4719 NEAR/3 policy', 'col:"4719" OR "x', '-4719', 'NOT 4719'])
def test_fts_special_characters_alongside_an_identifier_do_not_raise(prio, question):
    for extra in ((), FLOOD, ['"quoted"', 'a:b*']):
        assert len(_search(prio, question, 6, extra)) <= 6
    wanted = 't1558.003' if 'T1558.003' in question else '4719'
    assert _has(_search(prio, question, 6, FLOOD)[0], wanted)


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------

def test_retrieve_applies_with_terms_and_warnings(small):
    seen = []

    def expand(prompt_text):
        seen.append(prompt_text)
        return '4625, 9999, logon failure'

    bundle = lookup.retrieve('why did 4625 happen', expand, 4, BIG_BUDGET, index=small)
    assert bundle.applied and bundle.reason is lookup.LookupReason.APPLIED
    assert bundle.terms == ('4625', 'logon failure') and bundle.dropped_terms == ('9999',)
    assert bundle.passages[0].path == 'events/4625.md' and bundle.index_state is IndexState.READY
    assert seen == [terms.build_expand_prompt('why did 4625 happen')]


def test_retrieve_expansion_failure_searches_with_question_words_and_warns(small):
    def broken(prompt_text):
        raise RuntimeError('model down\n\x1b[31m')

    bundle = lookup.retrieve('what is 4625?', broken, 6, BIG_BUDGET, index=small)
    assert bundle.applied and bundle.passages[0].path == 'events/4625.md'
    assert len(bundle.warnings) == 1
    assert bundle.warnings[0].startswith('keyword expansion failed: model down')
    assert '\x1b' not in bundle.warnings[0] and '\n' not in bundle.warnings[0]


def test_retrieve_without_usable_terms_warns(small):
    bundle = lookup.retrieve('what is 4625?', lambda p: '9999, 8888', 6, BIG_BUDGET, index=small)
    assert bundle.applied and bundle.terms == () and bundle.dropped_terms == ('9999', '8888')
    assert 'keyword expansion returned no usable terms' in bundle.warnings
    assert lookup.retrieve('what is 4625?', None, 6, BIG_BUDGET, index=small).warnings == ()


def test_retrieve_no_match_is_bare_with_a_warning(small):
    bundle = lookup.retrieve('xyzzyplugh', None, 6, BIG_BUDGET, index=small)
    assert not bundle.applied and bundle.reason is lookup.LookupReason.NO_MATCH
    assert 'no reference passages matched the question' in bundle.warnings


def test_retrieve_drops_passages_that_do_not_fit_the_budget(small):
    full = lookup.retrieve('account logon', None, 6, BIG_BUDGET, index=small)
    assert len(full.passages) >= 3
    first_len = len(full.passages[0].text)
    tight = lookup.retrieve('account logon', None, 6, first_len + 1, index=small)
    assert tight.applied and len(tight.passages) < len(full.passages)
    assert any('left out to fit the prompt size' in w for w in tight.warnings)
    cut = lookup.retrieve('account logon', None, 6, 40, index=small)
    assert cut.applied and len(cut.passages) == 1 and len(cut.passages[0].text) <= 40


@pytest.mark.parametrize('budget', [0, -5])
def test_retrieve_no_room_is_bare_and_makes_no_model_call(small, budget):
    asked = []
    bundle = lookup.retrieve('what is 4625?', lambda p: asked.append(p) or 'x', 6, budget, index=small)
    assert not bundle.applied and bundle.reason is lookup.LookupReason.NO_ROOM
    assert 'no room for references' in bundle.warnings and asked == []


def test_retrieve_disabled_and_empty_question_and_zero_k(small):
    off = lookup.retrieve('what is 4625?', None, 6, BIG_BUDGET, index=small, enabled=False)
    assert not off.applied and off.reason is lookup.LookupReason.DISABLED
    for q in ('', '   \n', None, 42):
        b = lookup.retrieve(q, None, 6, BIG_BUDGET, index=small)
        assert not b.applied and b.reason is lookup.LookupReason.EMPTY_QUESTION
    assert lookup.retrieve('4625', None, 0, BIG_BUDGET, index=small).reason is lookup.LookupReason.NO_MATCH


class _FakeIndex:
    def __init__(self, status=None, error=None):
        self._status = status or IndexStatus(IndexState.READY, 'x')
        self._error = error

    def status(self):
        if isinstance(self._status, BaseException):
            raise self._status
        return self._status

    def search_rows(self, match, limit, idents=()):
        raise self._error


def test_retrieve_never_raises_whatever_the_store_does():
    cases = [
        (_FakeIndex(error=ReferenceIndexError(IndexState.FTS5_UNAVAILABLE, 'no fts5')),
         lookup.LookupReason.INDEX_UNAVAILABLE, IndexState.FTS5_UNAVAILABLE),
        (_FakeIndex(error=RuntimeError('boom')), lookup.LookupReason.SEARCH_FAILED, None),
        (_FakeIndex(error=sqlite3.DatabaseError('x')), lookup.LookupReason.SEARCH_FAILED, None),
        (_FakeIndex(status=OSError('disk')), lookup.LookupReason.SEARCH_FAILED, None),
    ]
    for index, reason, state in cases:
        bundle = lookup.retrieve('what is 4625?', None, 6, BIG_BUDGET, index=index)
        assert not bundle.applied and bundle.reason is reason and bundle.index_state is state


# ---------------------------------------------------------------------------
# wrap_generate and review_reply
# ---------------------------------------------------------------------------

CODE = 'c0de1234'


def _bundle(small, question='what is 4625?'):
    bundle = lookup.retrieve(question, None, 4, BIG_BUDGET, index=small)
    assert bundle.applied
    return bundle


def test_wrap_generate_frames_the_user_turn_and_passes_history_through(small):
    bundle = _bundle(small)
    calls = []

    def gen(prompt_text, history=None, **kw):
        calls.append((prompt_text, history, kw))
        return 'The status means a wrong password [1].'

    reply = lookup.wrap_generate(gen, bundle, CODE)('what is 4625?', [{'role': 'user', 'content': 'hi'}], tag=1)
    assert reply == 'The status means a wrong password [1].'
    ((sent, history, kw),) = calls
    assert sent.startswith('Reference passages:') and f'=== PASSAGE 1 [{CODE}] ===' in sent
    assert '\n\nQuestion: what is 4625?\n\nInstructions:' in sent and 'untrusted reference data' in sent
    assert history == [{'role': 'user', 'content': 'hi'}] and kw == {'tag': 1}


def test_wrap_generate_returns_the_bare_function_when_nothing_was_applied(small):
    gen = lambda prompt_text, history=None: 'x'
    bare = lookup.retrieve('xyzzyplugh', None, 6, BIG_BUDGET, index=small)
    assert lookup.wrap_generate(gen, bare, CODE) is gen
    assert lookup.wrap_generate(gen, _bundle(small), '') is gen


def test_wrap_generate_cleans_echoed_markers_and_control_sequences(small):
    bundle = _bundle(small)
    raw = ('The status means a wrong password [1].\x1b[31m red\x1b[0m\n'
           f'=== END PASSAGE 1 [{CODE}] ===\n'
           '\x1b]0;pwned\x07  === PASSAGE 3 [anything] ===  \n'
           'tail' + chr(0x202e) + chr(0x200b) + '\r\n\n\n')
    out = lookup.wrap_generate(lambda p, h=None: raw, bundle, CODE)('q')
    assert out == 'The status means a wrong password [1]. red\ntail'
    assert '=== ' not in out and '\x1b' not in out and '\x07' not in out and '\r' not in out


def test_wrap_generate_obfuscated_marker_line_is_still_removed(small):
    out = lookup.wrap_generate(lambda p, h=None: 'ok\n\x1b[1m=== END PASSAGE 2 [x] ===\x1b[0m\nmore',
                               _bundle(small), CODE)('q')
    assert out == 'ok\nmore'


def test_wrap_generate_passes_non_text_replies_and_errors_through(small):
    bundle = _bundle(small)
    assert lookup.wrap_generate(lambda p, h=None: None, bundle, CODE)('q') is None

    def failing(p, h=None):
        raise TimeoutError('model timed out')

    with pytest.raises(TimeoutError):
        lookup.wrap_generate(failing, bundle, CODE)('q')


def test_review_reply_flags_citations_and_echoes(small):
    bundle = _bundle(small)
    count = len(bundle.passages)
    review = lookup.review_reply(bundle, f'See [1] and [{count + 5}] and [0].\n=== END PASSAGE 1 [7] ===\n')
    assert review.text == f'See [1] and [{count + 5}] and [0].' and review.echoed_marker
    assert review.cited == {0, 1, count + 5} and review.missing == (0, count + 5)
    assert review.warnings == (
        'the model echoed a passage marker line; it was removed from the answer',
        f'answer cites passage numbers that were not provided: [0], [{count + 5}]')
    clean = lookup.review_reply(bundle, 'See PASSAGE 1 for details [1]; the === PASSAGE 1 [x] === form is inline.')
    assert clean.warnings == () and not clean.echoed_marker and clean.missing == ()


def test_review_reply_marker_digits_are_not_read_as_citations(small):
    review = lookup.review_reply(_bundle(small), 'ok [1].\n=== END PASSAGE 1 [7] ===')
    assert review.cited == {1} and review.missing == ()


def test_new_code_is_eight_hex_digits_and_differs_between_turns():
    codes = {lookup.new_code() for _ in range(8)}
    assert len(codes) > 1 and all(len(c) == 8 and int(c, 16) >= 0 for c in codes)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_index_path_from_environment_wins(tmp_path):
    target = tmp_path / 'chosen' / 'r.sqlite'
    cfg = config.load({config.ENV_INDEX: f'  {target}  '})
    assert cfg.index_path == target and cfg.source is config.IndexPathSource.ENVIRONMENT and cfg.enabled
    assert not target.parent.exists()                       # configuration never creates folders


def test_default_index_path_sits_under_the_state_home_and_creates_nothing(tmp_path):
    home = tmp_path / 'state-home'
    runtime_paths.configure_home(home)
    try:
        path, source = config.index_path({})
        blank, blank_source = config.index_path({config.ENV_INDEX: '   '})
    finally:
        runtime_paths.reset_home()
    assert path == home / 'references' / 'references.sqlite' and source is config.IndexPathSource.DEFAULT
    assert blank == path and blank_source is config.IndexPathSource.DEFAULT
    assert not home.exists()


@pytest.mark.parametrize('value, enabled', [('0', False), (' 0 ', False), ('1', True), ('', True),
                                            ('false', True), ('00', True)])
def test_kill_switch_is_exactly_zero(value, enabled):
    assert config.lookup_enabled({config.ENV_ENABLED: value}) is enabled
    assert config.lookup_enabled({}) is True


def test_config_reads_the_process_environment_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_INDEX, str(tmp_path / 'env.sqlite'))
    monkeypatch.setenv(config.ENV_ENABLED, '0')
    cfg = config.load()
    assert cfg.index_path == tmp_path / 'env.sqlite' and cfg.enabled is False
    assert isinstance(cfg.index_path, Path)

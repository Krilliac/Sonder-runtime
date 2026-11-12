"""Build tiny reference indexes for the reference-lookup tests (never real reference data).

The CREATE statements are those of the standalone lookup client's indexer, so the
fixtures have the exact shape of a real index.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

CREATE_PASSAGES = ("CREATE VIRTUAL TABLE passages USING fts5("
                   "title, heading, body, source UNINDEXED, url UNINDEXED, "
                   "path UNINDEXED, doc UNINDEXED, tokenize='porter unicode61')")
CREATE_META = 'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)'
INSERT_PASSAGE = ('INSERT INTO passages(title, heading, body, source, url, path, doc) '
                  'VALUES (?,?,?,?,?,?,?)')

# (title, heading, body, source, url, path, doc)
SMALL_ROWS = [
    ('4625: An account failed to log on', '', ('This event is generated when a logon request fails. '
     'The status 0xC000006A means the user name is correct but the password is wrong.'),
     'events', 'https://example.test/events/4625', 'events/4625.md', 1),
    ('4624: An account was successfully logged on', '', ('Generated when a logon succeeds. Compare '
     'with the failures that follow repeated attempts. See also 4625.'),
     'events', 'https://example.test/events/4624', 'events/4624.md', 2),
    ('4740: A user account was locked out', '', ('Generated every time a user account is locked out '
     'after too many bad passwords.'),
     'events', 'https://example.test/events/4740', 'events/4740.md', 3),
    ('T1110.001 Brute Force: Password Guessing', '', 'Guess passwords one at a time.',
     'attack', 'https://attack.example/techniques/T1110/001', 'attack/bundle.json', 4),
    ('T1110.003 Brute Force: Password Spraying', '', 'Try one password against many accounts.',
     'attack', 'https://attack.example/techniques/T1110/003', 'attack/bundle.json', 5),
    ('Kerberoasting Detection', '', 'generic rule for kerberoasting',
     'sigma', '', 'sigma/a.yml', 6),
]


def create_index(path: Path, rows=SMALL_ROWS, *, meta: bool = True, built_at: str = '2026-01-01T00:00:00+00:00'
                 ) -> Path:
    """Write a client-shaped index at path and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(CREATE_PASSAGES)
        conn.executemany(INSERT_PASSAGE, rows)
        if meta:
            by_source: dict[str, int] = {}
            for row in rows:
                by_source[row[3]] = by_source.get(row[3], 0) + 1
            summary = {'files_indexed': len(rows), 'files_skipped': {},
                       'documents': len({r[6] for r in rows}), 'passages': len(rows),
                       'by_source': dict(sorted(by_source.items()))}
            conn.execute(CREATE_META)
            conn.execute('INSERT INTO meta VALUES (?, ?)', ('summary', json.dumps(summary)))
            conn.execute('INSERT INTO meta VALUES (?, ?)', ('built_at', built_at))
        conn.execute("INSERT INTO passages(passages) VALUES('optimize')")
        conn.commit()
    finally:
        conn.close()
    return path

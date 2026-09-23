# MEM-004: scoped fact supersession in live indexes

Status: implemented slice; the master MEM-004 checkbox remains open.

The configured authoritative fact source now treats an explicit `supersedes`
link as a same-source, same-project relationship between journaled facts.
The target must exist in authoritative source state. Self links, cycles, and
silent withdrawal of an established link fail inside the fact, source-state,
journal, and index transaction.

Entity and decision reads exclude the predecessor once the successor's
`valid_from` takes effect. Either kind of successor index can invalidate
either kind of predecessor index. A successor tombstone or `valid_until` does
not silently restore the stale predecessor. Index rebuild replays the same
relationship from the digest-checked source journal. The live application
composition and restart path are exercised in
`tests/test_authoritative_memory_source.py`; transaction, scope, cycle,
validity, tombstone, and rebuild cases are in
`tests/test_authoritative_indexes.py`.

This proves only the supported fact/index path. The current indexes are
present-state materializations with validity-time filtering; a later
tombstone does not reconstruct a historical snapshot. Contradiction handling,
source-trust scoring, decay, explicit revalidation, semantic promotion, and
live database adoption remain open under MEM-004 and issues #513/#514.

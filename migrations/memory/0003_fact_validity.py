"""Give asserted facts an optional validity interval (facts.valid_from/valid_to).

An append-only fact store keeps recalling facts that stopped being true.  Each
fact may now carry a half-open interval ``[valid_from, valid_to)`` plus the id
of the explicit successor that closed it (``superseded_by``); recall excludes
facts whose interval does not contain "now".

The work is done by ``memory_store._migrate``, which owns memory.db's schema
and is idempotent; this module is the ledgered, checksummed record that it
happened, plus a ``verify`` that the three columns exist, are nullable and
carry no DEFAULT.

**No backfill.**  A pre-existing fact's start is unknown and it was never
closed, so every legacy row keeps ``NULL``/``NULL`` -- unbounded on both sides,
which is exactly how recall treated it before.  A DEFAULT would silently stamp
an invented start on rows (and on any writer that forgets the column).
"""

manages_own_transaction = True

_COLUMNS = ("valid_from", "valid_to", "superseded_by")


def _db_path(conn) -> str:
    for _, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main":
            return filename
    raise RuntimeError("cannot resolve database path for fact validity")


def apply(conn) -> None:
    from sonder_runtime.adapters import memory_store

    path = _db_path(conn)
    legacy = memory_store.connect(path, check_same_thread=False)
    try:
        memory_store.init_db(legacy)
    finally:
        legacy.close()


def verify(conn) -> None:
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(facts)")}
    for name in _COLUMNS:
        if name not in columns:
            raise RuntimeError("facts.%s was not created" % name)
        # notnull is column 3 of PRAGMA table_info; dflt_value is column 4.
        if columns[name][3]:
            raise RuntimeError("facts.%s must be nullable: NULL means unbounded" % name)
        if columns[name][4] is not None:
            raise RuntimeError("facts.%s must have no DEFAULT" % name)

"""sql_verifier — a verifiers.py-style backend that grounds SQL artifacts
against a real in-memory sqlite3 database instead of eyeballing syntax.

Fits the same seam as verifiers.py: fn(artifact, spec) -> Verdict(passed,
reason, detail). `artifact` is the SQL text (one statement or a small
script); `spec` carries optional schema DDL to set up tables first, bind
params, and execution mode. No network, no external binary, no GPU —
sqlite3 is stdlib and every check runs against a throwaway ":memory:" db
that is created fresh per call and discarded after, so nothing persists
and nothing outside the process is touched.

Designed to be dropped into verifiers.REGISTRY as "sql_valid" with a
one-line addition there — see PITCH.md.
"""
import sqlite3
import traceback
import collections

Verdict = collections.namedtuple("Verdict", ["passed", "reason", "detail"])


def _statements(sql):
    """Naive statement split on ';' (does not understand string-literal
    semicolons — good enough to decide single-statement vs script mode,
    not used for anything correctness-critical)."""
    return [s.strip() for s in (sql or "").split(";") if s.strip()]


def _preview_rows(cur, limit):
    rows = cur.fetchmany(limit)
    cols = [d[0] for d in (cur.description or [])]
    return cols, rows


def sql_valid(artifact, spec=None):
    """Validate `artifact` by preparing/executing it against a fresh
    in-memory sqlite3 database.

    spec (all optional):
      schema     — DDL/DML string run first via executescript() to set up
                   tables/seed data. If the schema itself fails to apply,
                   that is reported distinctly (reason prefixed
                   "schema setup failed") so it isn't confused with the
                   artifact under test being broken.
      params     — sequence of bind values for a single parameterized
                   statement (e.g. "SELECT * FROM t WHERE id = ?", [1]).
                   Only honored in 'statement' mode (see below) — sqlite3's
                   executescript() does not support parameter binding.
      mode       — 'auto' (default), 'statement', or 'script'.
                   'statement' uses conn.execute() (single statement,
                   supports params + a result preview for SELECTs).
                   'script' uses conn.executescript() (runs any number of
                   ';'-separated statements, e.g. CREATE + INSERT + SELECT,
                   no param binding). 'auto' picks 'script' iff the
                   artifact contains more than one non-empty statement,
                   else 'statement'.
      dry_run    — bool, default False. 'statement' mode only. Prefixes
                   the artifact with "EXPLAIN " so sqlite3 compiles the
                   statement without executing it — validates syntax/
                   references without any side effect (useful for
                   checking a destructive INSERT/UPDATE/DELETE is
                   well-formed without actually running it).
      fetch      — bool, default True. If the statement is a SELECT,
                   include a small row/column preview in `detail`.
      fetch_limit — int, default 5. Max preview rows.

    Returns Verdict(passed, reason, detail). `detail` carries either the
    full traceback (on failure) or a short result preview (on success) —
    the traceback is what a self-repair loop would feed back to a model.
    """
    spec = spec or {}
    schema = spec.get("schema")
    params = spec.get("params")
    mode = spec.get("mode", "auto")
    dry_run = spec.get("dry_run", False)
    fetch = spec.get("fetch", True)
    fetch_limit = spec.get("fetch_limit", 5)

    conn = sqlite3.connect(":memory:")
    try:
        if schema:
            try:
                conn.executescript(schema)
            except sqlite3.Error as e:
                return Verdict(False, "schema setup failed: %s" % e, traceback.format_exc())

        stmts = _statements(artifact)
        if mode == "auto":
            use_script = len(stmts) > 1
        else:
            use_script = mode == "script"

        try:
            if use_script:
                conn.executescript(artifact)
                return Verdict(True, "valid (%d statement%s)"
                                % (len(stmts), "" if len(stmts) == 1 else "s"),
                                "script executed cleanly, no rows returned")

            text = artifact
            if dry_run:
                stripped = text.strip()
                if not stripped.upper().startswith("EXPLAIN"):
                    text = "EXPLAIN " + stripped
            cur = conn.execute(text, params or [])
            if fetch and cur.description:
                cols, rows = _preview_rows(cur, fetch_limit)
                detail = "columns=%s rows=%s" % (cols, rows)
            else:
                detail = "rowcount=%s" % cur.rowcount
            reason = "valid (dry_run)" if dry_run else "valid"
            return Verdict(True, reason, detail)
        except sqlite3.Error as e:
            return Verdict(False, str(e), traceback.format_exc())
    finally:
        conn.close()


REGISTRY = {
    "sql_valid": sql_valid,
}


def get(name):
    if name not in REGISTRY:
        raise KeyError("no verifier %r (have %s)" % (name, sorted(REGISTRY)))
    return REGISTRY[name]


def verify(name, artifact, spec=None):
    """Same seam as verifiers.verify — mirrored here so this proposal is
    independently callable before/without merging into verifiers.py."""
    return get(name)(artifact, spec)

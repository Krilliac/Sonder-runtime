import pytest

import sql_verifier as SV


# --- registry plumbing ------------------------------------------------------
def test_get_unknown_raises():
    with pytest.raises(KeyError):
        SV.get("does_not_exist")


def test_registry_covers_documented_backend():
    assert "sql_valid" in SV.REGISTRY
    assert SV.REGISTRY["sql_valid"] is SV.sql_valid


# --- basic valid / invalid single statement --------------------------------
def test_valid_select_with_schema_passes():
    v = SV.verify("sql_valid", "SELECT id, name FROM users WHERE id = 1", {
        "schema": "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);"
                  "INSERT INTO users VALUES (1, 'ada');",
    })
    assert v.passed is True
    assert "columns=" in v.detail
    assert "ada" in v.detail


def test_syntax_error_fails_with_detail():
    v = SV.sql_valid("SELEC * FROM users")
    assert v.passed is False
    assert v.reason  # sqlite3 error message, non-empty
    assert "Traceback" in v.detail


def test_missing_table_fails():
    v = SV.sql_valid("SELECT * FROM no_such_table")
    assert v.passed is False
    assert "no such table" in v.reason.lower()


def test_schema_failure_is_reported_distinctly():
    v = SV.sql_valid("SELECT 1", {"schema": "CREATE TBLE oops(x);"})
    assert v.passed is False
    assert v.reason.startswith("schema setup failed")


# --- params (parameterized single statement) -------------------------------
def test_params_binding_passes():
    v = SV.sql_valid("SELECT name FROM users WHERE id = ?", {
        "schema": "CREATE TABLE users(id INTEGER, name TEXT);"
                  "INSERT INTO users VALUES (1, 'grace');",
        "params": [1],
    })
    assert v.passed is True
    assert "grace" in v.detail


def test_params_type_mismatch_still_valid_sql():
    # sqlite is dynamically typed — a param that matches no row is still a
    # *valid* statement (0 rows), not an error. Confirms we don't conflate
    # "no results" with "invalid SQL".
    v = SV.sql_valid("SELECT name FROM users WHERE id = ?", {
        "schema": "CREATE TABLE users(id INTEGER, name TEXT);",
        "params": [999],
    })
    assert v.passed is True
    assert "rows=[]" in v.detail


# --- multi-statement script (auto mode) ------------------------------------
def test_multi_statement_script_auto_detected():
    v = SV.sql_valid(
        "CREATE TABLE t(x INTEGER); INSERT INTO t VALUES (1); INSERT INTO t VALUES (2);"
    )
    assert v.passed is True
    assert "3 statements" in v.reason


def test_multi_statement_script_with_error_fails():
    v = SV.sql_valid("CREATE TABLE t(x INTEGER); INSERT INTO t VALES (1);")
    assert v.passed is False
    assert "Traceback" in v.detail


def test_forced_script_mode_single_statement():
    v = SV.sql_valid("CREATE TABLE t(x INTEGER)", {"mode": "script"})
    assert v.passed is True
    assert "1 statement" in v.reason


# --- dry_run (EXPLAIN) — validates without side effects --------------------
def test_dry_run_validates_insert_without_executing():
    v = SV.sql_valid("INSERT INTO users VALUES (2, 'bob')", {
        "schema": "CREATE TABLE users(id INTEGER, name TEXT);",
        "dry_run": True,
    })
    assert v.passed is True
    assert v.reason == "valid (dry_run)"


def test_dry_run_still_catches_bad_reference():
    v = SV.sql_valid("INSERT INTO no_such_table VALUES (1)", {"dry_run": True})
    assert v.passed is False
    assert "no such table" in v.reason.lower()


# --- fetch controls ---------------------------------------------------------
def test_fetch_false_skips_row_preview():
    v = SV.sql_valid("SELECT * FROM users", {
        "schema": "CREATE TABLE users(id INTEGER);INSERT INTO users VALUES(1);",
        "fetch": False,
    })
    assert v.passed is True
    assert "rowcount=" in v.detail
    assert "columns=" not in v.detail


def test_fetch_limit_truncates_preview():
    schema = "CREATE TABLE t(x INTEGER);" + "".join(
        "INSERT INTO t VALUES (%d);" % i for i in range(10)
    )
    v = SV.sql_valid("SELECT x FROM t", {"schema": schema, "fetch_limit": 3})
    assert v.passed is True
    # exactly 3 row-tuples previewed, not all 10
    assert v.detail.count("(") == 3

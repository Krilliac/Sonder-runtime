"""Deterministic, execution-grounded SQL gate for model promotion.

The model only produces a single read-only SQL query.  Queries are executed
against disposable in-memory SQLite databases containing data that is never
included in the prompt.  Reports retain bounded reason codes and artifact
hashes, never model responses or generated SQL.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import ollama_endpoint


SUITE_VERSION = "sql-promotion-v2"
REPORT_SCHEMA = "sonder.promotion-evaluation/v1"
INFERENCE_OPTIONS = {
    "temperature": 0,
    "seed": 817_263,
    "num_ctx": 2_048,
    "num_predict": 256,
}
INFERENCE_TIMEOUT_SECONDS = 90

MAX_MODEL_RESPONSE_BYTES = 8 * 1024
MAX_SQL_BYTES = 4 * 1024
MAX_RESULT_ROWS = 200
MAX_RESULT_COLUMNS = 16
MAX_RESULT_BYTES = 32 * 1024
MAX_SQLITE_VALUE_BYTES = 128 * 1024
MAX_VM_STEPS = 50_000
_PROGRESS_GRANULARITY = 100

_FENCED_SQL = re.compile(r"\A```sql[ \t]*\r?\n([\s\S]*?)\r?\n```[ \t]*\Z", re.IGNORECASE)
_FENCED_JSON = re.compile(r"\A```json[ \t]*\r?\n([\s\S]*?)\r?\n```[ \t]*\Z", re.IGNORECASE)
_STARTS_READ_ONLY = re.compile(r"\A\s*(?:SELECT|WITH)\b", re.IGNORECASE)
STRUCTURED_TASK_ID = "dynamic_instruction_transform"


@dataclass(frozen=True)
class _Fixture:
    schema: str
    rows: tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]
    expected_columns: tuple[str, ...]
    expected_rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class _Task:
    task_id: str
    schema_for_prompt: str
    request: str
    fixtures: tuple[_Fixture, _Fixture]


def _fixture(
    schema: str,
    rows: Sequence[tuple[str, Sequence[Sequence[Any]]]],
    columns: Sequence[str],
    expected: Sequence[Sequence[Any]],
) -> _Fixture:
    return _Fixture(
        schema=schema,
        rows=tuple((table, tuple(tuple(row) for row in table_rows)) for table, table_rows in rows),
        expected_columns=tuple(columns),
        expected_rows=tuple(tuple(row) for row in expected),
    )


_SALES_SCHEMA = """CREATE TABLE orders (
  order_id INTEGER PRIMARY KEY,
  region TEXT NOT NULL,
  status TEXT NOT NULL,
  amount INTEGER NOT NULL
);"""
_CUSTOMERS_SCHEMA = """CREATE TABLE customers (
  customer_id INTEGER PRIMARY KEY,
  customer_name TEXT NOT NULL
);
CREATE TABLE payments (
  payment_id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  amount INTEGER NOT NULL
);"""
_READINGS_SCHEMA = """CREATE TABLE readings (
  sensor_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  value INTEGER NOT NULL
);"""
_PRODUCTS_SCHEMA = """CREATE TABLE products (
  product_id INTEGER PRIMARY KEY,
  product_name TEXT NOT NULL
);
CREATE TABLE sale_items (
  sale_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  quantity INTEGER NOT NULL
);"""


TASKS: tuple[_Task, ...] = (
    _Task(
        "completed_revenue_by_region",
        _SALES_SCHEMA,
        "Return one row per region that has a completed order. Include exactly "
        "the columns region and total_revenue, where total_revenue is the sum "
        "of amount for completed orders only. Order by region ascending.",
        (
            _fixture(
                _SALES_SCHEMA,
                (("orders", ((1, "east", "completed", 40), (2, "west", "pending", 99),
                              (3, "east", "completed", 15), (4, "north", "completed", 8),
                              (5, "west", "completed", 21), (6, "east", "refunded", 900),
                              (7, "north", "failed", 700))),),
                ("region", "total_revenue"),
                (("east", 55), ("north", 8), ("west", 21)),
            ),
            _fixture(
                _SALES_SCHEMA,
                (("orders", ((10, "south", "cancelled", 600), (11, "south", "completed", 7),
                              (12, "central", "completed", 25), (13, "central", "completed", 5),
                              (14, "north", "pending", 1))),),
                ("region", "total_revenue"),
                (("central", 30), ("south", 7)),
            ),
        ),
    ),
    _Task(
        "repeat_paid_customers",
        _CUSTOMERS_SCHEMA,
        "Return customers with at least two paid payments. Include exactly the "
        "columns customer_name, paid_count, and paid_total. Ignore non-paid "
        "payments. Order by paid_total descending, then customer_name ascending.",
        (
            _fixture(
                _CUSTOMERS_SCHEMA,
                (
                    ("customers", ((1, "Ada"), (2, "Ada"), (3, "Cy"))),
                    ("payments", ((1, 1, "paid", 20), (2, 1, "paid", 30),
                                  (3, 2, "paid", 80), (4, 2, "failed", 80),
                                  (5, 3, "paid", 10), (6, 3, "paid", 10),
                                  (7, 3, "refunded", 100))),
                ),
                ("customer_name", "paid_count", "paid_total"),
                (("Ada", 2, 50), ("Cy", 2, 20)),
            ),
            _fixture(
                _CUSTOMERS_SCHEMA,
                (
                    ("customers", ((10, "Iris"), (11, "Jae"), (12, "Kai"))),
                    ("payments", ((20, 10, "paid", 5), (21, 10, "paid", 6),
                                  (22, 10, "paid", 7), (23, 11, "paid", 100),
                                  (24, 12, "failed", 9), (25, 12, "paid", 1),
                                  (26, 12, "paid", 2))),
                ),
                ("customer_name", "paid_count", "paid_total"),
                (("Iris", 3, 18), ("Kai", 2, 3)),
            ),
        ),
    ),
    _Task(
        "latest_sensor_reading",
        _READINGS_SCHEMA,
        "Return the latest reading for every sensor. Include exactly the columns "
        "sensor_id, observed_at, and value. observed_at is an ISO timestamp and "
        "is unique within each sensor. Order by sensor_id ascending.",
        (
            _fixture(
                _READINGS_SCHEMA,
                (("readings", (("a", "2026-01-01T08:00:00Z", 2),
                                ("a", "2026-01-01T09:00:00Z", 5),
                                ("b", "2026-01-02T10:00:00Z", 7),
                                ("b", "2026-01-01T10:00:00Z", 9))),),
                ("sensor_id", "observed_at", "value"),
                (("a", "2026-01-01T09:00:00Z", 5), ("b", "2026-01-02T10:00:00Z", 7)),
            ),
            _fixture(
                _READINGS_SCHEMA,
                (("readings", (("x", "2025-12-31T23:59:59Z", 11),
                                ("z", "2026-06-10T01:00:00Z", 3),
                                ("z", "2026-06-10T01:00:01Z", 4),
                                ("y", "2024-01-01T00:00:00Z", 8))),),
                ("sensor_id", "observed_at", "value"),
                (("x", "2025-12-31T23:59:59Z", 11),
                 ("y", "2024-01-01T00:00:00Z", 8),
                 ("z", "2026-06-10T01:00:01Z", 4)),
            ),
        ),
    ),
    _Task(
        "products_without_sales",
        _PRODUCTS_SCHEMA,
        "Return products that have never appeared in sale_items. Include exactly "
        "the columns product_id and product_name. Order by product_id ascending.",
        (
            _fixture(
                _PRODUCTS_SCHEMA,
                (
                    ("products", ((1, "anvil"), (2, "brush"), (3, "clamp"), (4, "drill"))),
                    ("sale_items", ((100, 1, 1), (101, 3, 2), (102, 1, 4))),
                ),
                ("product_id", "product_name"),
                ((2, "brush"), (4, "drill")),
            ),
            _fixture(
                _PRODUCTS_SCHEMA,
                (
                    ("products", ((7, "ink"), (8, "jig"), (9, "knife"))),
                    ("sale_items", ((200, 8, 1),)),
                ),
                ("product_id", "product_name"),
                ((7, "ink"), (9, "knife")),
            ),
        ),
    ),
)


def _suite_hash(tasks=TASKS) -> str:
    payload = []
    for task in tasks:
        payload.append({
            "id": task.task_id,
            "schema": task.schema_for_prompt,
            "request": task.request,
            "fixtures": [
                {
                    "schema": fixture.schema,
                    "rows": fixture.rows,
                    "columns": fixture.expected_columns,
                    "expected": fixture.expected_rows,
                }
                for fixture in task.fixtures
            ],
        })
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


SUITE_HASH = _suite_hash()


def _variant_tasks(challenge: str) -> tuple[_Task, ...]:
    """Nonce table identifiers so a candidate cannot memorize fixed SQL text."""
    if not challenge:
        return TASKS
    suffix = "_v" + hashlib.sha256(challenge.encode("utf-8")).hexdigest()[:10]
    table_names = ("orders", "customers", "payments", "readings", "products", "sale_items")

    def renamed(text):
        for table in table_names:
            text = re.sub(rf"\b{re.escape(table)}\b", table + suffix, text)
        return text

    variants = []
    for task in TASKS:
        fixtures = []
        for fixture in task.fixtures:
            fixtures.append(_Fixture(
                schema=renamed(fixture.schema),
                rows=tuple((table + suffix, rows) for table, rows in fixture.rows),
                expected_columns=fixture.expected_columns,
                expected_rows=fixture.expected_rows,
            ))
        variants.append(_Task(
            task_id=task.task_id,
            schema_for_prompt=renamed(task.schema_for_prompt),
            request=renamed(task.request),
            fixtures=tuple(fixtures),
        ))
    return tuple(variants)


def _structured_challenge(challenge: str):
    digest = hashlib.sha256(("structured:" + challenge).encode("utf-8")).digest()
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    source = "".join(alphabet[value % len(alphabet)] for value in digest[:6])
    left = 10 + digest[6] % 30
    right = 5 + digest[7] % 20
    nonce = hashlib.sha256(("nonce:" + challenge).encode("utf-8")).hexdigest()[:16]
    expected = {
        "nonce": nonce,
        "uppercase": source.upper(),
        "reversed": source[::-1],
        "sum": left + right,
    }
    prompt = (
        "Return only one JSON object, raw or in exactly one ```json fenced block, "
        "with exactly these keys: nonce, uppercase, reversed, sum. Do not add prose. "
        f"Copy nonce {nonce!r} exactly. For source {source!r}, uppercase is its ASCII "
        f"uppercase form and reversed is its characters reversed. sum is {left} + {right}."
    )
    return prompt, expected


def _json_object_no_duplicates(text: str):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=pairs)


def _evaluate_structured(response: Any, expected: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, str):
        return {"id": STRUCTURED_TASK_ID, "passed": False, "reason": "invalid_response_type", "artifact_sha256": None}
    try:
        encoded = response.encode("utf-8")
    except UnicodeEncodeError:
        return {"id": STRUCTURED_TASK_ID, "passed": False, "reason": "invalid_text_encoding", "artifact_sha256": None}
    artifact_hash = hashlib.sha256(encoded).hexdigest()
    if len(encoded) > MAX_MODEL_RESPONSE_BYTES:
        reason = "response_too_large"
    else:
        match = _FENCED_JSON.fullmatch(response)
        if match:
            text = match.group(1).strip()
        elif "```" in response:
            text = ""
        else:
            text = response.strip()
        try:
            value = _json_object_no_duplicates(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = None
        reason = "passed" if value == expected and type(value.get("sum")) is int else "wrong_result" if isinstance(value, dict) else "invalid_json"
    return {
        "id": STRUCTURED_TASK_ID,
        "passed": reason == "passed",
        "reason": reason,
        "artifact_sha256": artifact_hash,
    }


def _prompt(task: _Task) -> str:
    return (
        "Write one SQLite query for this task. Return only raw SQL or exactly one "
        "```sql fenced block. Do not include explanations or comments. Use only a "
        "single SELECT statement (a WITH clause is allowed).\n\n"
        f"Schema:\n{task.schema_for_prompt}\n\nTask:\n{task.request}"
    )


def _local_ollama_origin() -> str:
    try:
        return ollama_endpoint.configured_origin(allow_remote=False)
    except ValueError as error:
        raise ValueError(
            "promotion evaluation requires a valid loopback Ollama host: %s"
            % error
        ) from error


def _local_ollama_url() -> str:
    return _local_ollama_origin() + "/api/generate"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_local(request, timeout):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )
    return opener.open(request, timeout=timeout)


def local_model_digest(model: str) -> str:
    """Return the exact local Ollama manifest digest for one model alias."""
    if not isinstance(model, str) or not model or model != model.strip():
        raise ValueError("model must be a non-empty exact model name")
    request = urllib.request.Request(
        _local_ollama_origin() + "/api/tags", method="GET",
    )
    with _open_local(request, timeout=30) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Ollama tag response exceeds byte limit")
    document = json.loads(raw.decode("utf-8"))
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, list):
        raise ValueError("Ollama tag response has no model list")
    for item in models:
        if isinstance(item, dict) and item.get("name") == model:
            digest = item.get("digest")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
            raise ValueError("Ollama model digest is invalid")
    raise ValueError(f"Ollama model alias is not installed: {model}")


def _default_generate(model: str, prompt: str, *, options: dict[str, int], timeout: int) -> str:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": options,
    }, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        _local_ollama_url(),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _open_local(request, timeout=timeout) as response:
        raw = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
    if len(raw) > MAX_MODEL_RESPONSE_BYTES:
        raise ValueError("model response exceeds byte limit")
    document = json.loads(raw.decode("utf-8"))
    generated = document.get("response") if isinstance(document, dict) else None
    if not isinstance(generated, str):
        raise ValueError("Ollama response has no text response field")
    return generated


def _default_unload(model: str) -> None:
    """Release the just-evaluated model before the next exact alias is loaded."""
    body = json.dumps({
        "model": model,
        "keep_alive": 0,
    }, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        _local_ollama_url(), data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with _open_local(request, timeout=30) as response:
        response.read(1024)


def _extract_sql(response: str) -> tuple[str | None, str, str | None]:
    if not isinstance(response, str):
        return None, "invalid_response_type", None
    try:
        encoded = response.encode("utf-8")
    except UnicodeEncodeError:
        return None, "invalid_text_encoding", None
    artifact_hash = hashlib.sha256(encoded).hexdigest()
    if len(encoded) > MAX_MODEL_RESPONSE_BYTES:
        return None, "response_too_large", artifact_hash

    match = _FENCED_SQL.fullmatch(response)
    if match:
        sql = match.group(1).strip()
    elif "```" in response:
        return None, "invalid_fence", artifact_hash
    else:
        sql = response.strip()

    sql_bytes = sql.encode("utf-8")
    if not sql:
        return None, "empty_sql", artifact_hash
    if len(sql_bytes) > MAX_SQL_BYTES:
        return None, "sql_too_large", artifact_hash
    if not _STARTS_READ_ONLY.match(sql):
        return None, "not_read_only_query", artifact_hash
    if "--" in sql or "/*" in sql or "*/" in sql:
        return None, "comments_not_allowed", artifact_hash
    return sql, "ok", hashlib.sha256(sql_bytes).hexdigest()


_DENIED_ACTIONS = {
    value for name, value in vars(sqlite3).items()
    if name.startswith(("SQLITE_CREATE_", "SQLITE_DROP_")) and isinstance(value, int)
}
_DENIED_ACTIONS.update(
    value for name in (
        "SQLITE_ALTER_TABLE", "SQLITE_ANALYZE", "SQLITE_ATTACH", "SQLITE_DELETE",
        "SQLITE_DETACH", "SQLITE_INSERT", "SQLITE_PRAGMA", "SQLITE_REINDEX",
        "SQLITE_SAVEPOINT", "SQLITE_TRANSACTION", "SQLITE_UPDATE",
    )
    if isinstance((value := getattr(sqlite3, name, None)), int)
)
_ALLOWED_FUNCTIONS = frozenset({
    "abs", "avg", "coalesce", "count", "dense_rank", "ifnull", "length",
    "lower", "max", "min", "nullif", "rank", "replace", "round",
    "row_number", "substr", "substring", "sum", "total", "trim", "upper",
})


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    if action in _DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        function_name = (arg2 or arg1 or "").lower()
        if function_name not in _ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _fixture_authorizer(allowed_tables: frozenset[str]):
    def authorize(action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_READ and (arg1 or "").lower() not in allowed_tables:
            return sqlite3.SQLITE_DENY
        return _authorizer(action, arg1, arg2, database, trigger)

    return authorize


def _normalized_cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    return str(value)


def _run_fixture(sql: str, fixture: _Fixture) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQLITE_VALUE_BYTES)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_RESULT_COLUMNS)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 8)
        connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
        connection.executescript(fixture.schema)
        for table, rows in fixture.rows:
            if not rows:
                continue
            column_count = len(rows[0])
            if any(len(row) != column_count for row in rows):
                return "fixture_error"
            placeholders = ",".join("?" for _ in range(column_count))
            connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
        connection.commit()
        connection.execute("PRAGMA query_only = ON")
        allowed_tables = frozenset(table.lower() for table, _rows in fixture.rows)
        connection.set_authorizer(_fixture_authorizer(allowed_tables))

        steps = 0

        def progress() -> int:
            nonlocal steps
            steps += _PROGRESS_GRANULARITY
            return 1 if steps > MAX_VM_STEPS else 0

        connection.set_progress_handler(progress, _PROGRESS_GRANULARITY)
        try:
            cursor = connection.execute(sql)
        except sqlite3.DatabaseError as error:
            message = str(error).lower()
            if "interrupted" in message:
                return "vm_step_limit"
            if (
                "authorized" in message or "prohibited" in message
                or "readonly" in message or "read-only" in message
            ):
                return "unsafe_sql"
            return "sql_error"

        columns = tuple(description[0] for description in (cursor.description or ()))
        if len(columns) > MAX_RESULT_COLUMNS:
            return "result_column_limit"
        if columns != fixture.expected_columns:
            return "wrong_columns"

        try:
            rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
        except sqlite3.DatabaseError as error:
            if "interrupted" in str(error).lower():
                return "vm_step_limit"
            return "sql_error"
        if len(rows) > MAX_RESULT_ROWS:
            return "result_row_limit"
        normalized = tuple(tuple(_normalized_cell(cell) for cell in row) for row in rows)
        result_bytes = len(json.dumps(normalized, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        if result_bytes > MAX_RESULT_BYTES:
            return "result_byte_limit"
        if normalized != fixture.expected_rows:
            return "wrong_result"
        return "passed"
    finally:
        connection.close()


def _evaluate_task(response: Any, task: _Task) -> dict[str, Any]:
    sql, reason, artifact_hash = _extract_sql(response)
    if sql is None:
        return {"id": task.task_id, "passed": False, "reason": reason, "artifact_sha256": artifact_hash}
    for fixture in task.fixtures:
        reason = _run_fixture(sql, fixture)
        if reason != "passed":
            return {"id": task.task_id, "passed": False, "reason": reason, "artifact_sha256": artifact_hash}
    return {"id": task.task_id, "passed": True, "reason": "passed", "artifact_sha256": artifact_hash}


Generate = Callable[..., str]


def evaluate_model(
    model: str,
    *,
    generate: Generate | None = None,
    task_ids: Iterable[str] | None = None,
    challenge: str = "",
) -> dict[str, Any]:
    """Evaluate one exact local model name and return a bounded report.

    An injected generator is called as
    ``generate(model, prompt, options=dict(...), timeout=90)``.
    """
    if not isinstance(model, str) or not model.strip() or model != model.strip():
        raise ValueError("model must be a non-empty exact model name")
    generator = generate or _default_generate
    if not isinstance(challenge, str) or len(challenge) > 256:
        raise ValueError("challenge must be a bounded string")
    selected = _variant_tasks(challenge)
    include_structured = bool(challenge)
    if task_ids is not None:
        wanted = tuple(task_ids)
        if len(set(wanted)) != len(wanted):
            raise ValueError("task_ids must not contain duplicates")
        by_id = {task.task_id: task for task in selected}
        known_ids = set(by_id)
        if include_structured:
            known_ids.add(STRUCTURED_TASK_ID)
        unknown = [task_id for task_id in wanted if task_id not in known_ids]
        if unknown:
            raise ValueError("unknown promotion task: %s" % unknown[0])
        selected = tuple(by_id[task_id] for task_id in wanted if task_id in by_id)
        include_structured = STRUCTURED_TASK_ID in wanted

    task_reports = []
    try:
        for task in selected:
            try:
                response = generator(
                    model,
                    _prompt(task),
                    options=dict(INFERENCE_OPTIONS),
                    timeout=INFERENCE_TIMEOUT_SECONDS,
                )
                task_report = _evaluate_task(response, task)
            except Exception:
                task_report = {
                    "id": task.task_id,
                    "passed": False,
                    "reason": "generation_error",
                    "artifact_sha256": None,
                }
            task_reports.append(task_report)
        if include_structured:
            prompt, expected = _structured_challenge(challenge)
            try:
                response = generator(
                    model,
                    prompt,
                    options=dict(INFERENCE_OPTIONS),
                    timeout=INFERENCE_TIMEOUT_SECONDS,
                )
                task_report = _evaluate_structured(response, expected)
            except Exception:
                task_report = {
                    "id": STRUCTURED_TASK_ID,
                    "passed": False,
                    "reason": "generation_error",
                    "artifact_sha256": None,
                }
            task_reports.append(task_report)
    finally:
        if generate is None:
            try:
                _default_unload(model)
            except Exception:
                # Evaluation output is already grounded. An unload failure must not
                # turn a valid score into an unbounded backend exception.
                pass

    return {
        "model": model,
        "score": sum(1 for result in task_reports if result["passed"]),
        "total": len(task_reports),
        "tasks": task_reports,
    }


def evaluate_pair(
    base_model: str,
    candidate_model: str,
    *,
    generate: Generate | None = None,
    challenge: str = "",
) -> dict[str, Any]:
    """Evaluate base, then candidate, with the exact same fixed suite/options."""
    base = evaluate_model(base_model, generate=generate, challenge=challenge)
    candidate = evaluate_model(candidate_model, generate=generate, challenge=challenge)
    return {
        "schema": REPORT_SCHEMA,
        "suite_version": SUITE_VERSION,
        "suite_hash": SUITE_HASH,
        "challenge_hash": hashlib.sha256(challenge.encode("utf-8")).hexdigest(),
        "options": dict(INFERENCE_OPTIONS),
        "base": base,
        "candidate": candidate,
    }


def validate_model_report(report, *, expected_model=None, challenge="") -> tuple[bool, str]:
    """Validate a standalone model report before a published alias is trusted."""
    try:
        if not isinstance(report, dict):
            raise ValueError("report is not an object")
        if expected_model is not None and report.get("model") != expected_model:
            return False, "model_mismatch"
        expected_ids = {task.task_id for task in TASKS}
        if challenge:
            expected_ids.add(STRUCTURED_TASK_ID)
        tasks = report.get("tasks")
        if not isinstance(tasks, list) or report.get("total") != len(expected_ids):
            return False, "incomplete_suite"
        results = {}
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("task is not an object")
            task_id = task.get("id")
            passed = task.get("passed")
            reason = task.get("reason")
            artifact_hash = task.get("artifact_sha256")
            if not isinstance(task_id, str) or task_id in results or type(passed) is not bool:
                raise ValueError("invalid task result")
            if not isinstance(reason, str) or not reason or len(reason) > 128:
                raise ValueError("invalid task reason")
            if passed is not (reason == "passed"):
                raise ValueError("task pass/reason mismatch")
            if artifact_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
                raise ValueError("invalid artifact hash")
            if passed and artifact_hash is None:
                raise ValueError("passing task has no artifact")
            if reason in {"generation_error", "fixture_error"}:
                return False, "evaluation_infrastructure_error"
            results[task_id] = passed
        if set(results) != expected_ids:
            return False, "incomplete_suite"
        if type(report.get("score")) is not int or report["score"] != sum(results.values()):
            raise ValueError("invalid score")
    except (AttributeError, KeyError, TypeError, ValueError):
        return False, "invalid_report"
    return True, "valid_report"


def promotion_decision(
    report: dict[str, Any], *, expected_base=None, expected_candidate=None,
    expected_challenge=None,
) -> tuple[bool, str]:
    """Apply SQL floor, dynamic instruction, no-regression, and lift rules."""
    try:
        include_structured = bool(expected_challenge)
        expected_ids = {task.task_id for task in TASKS}
        if include_structured:
            expected_ids.add(STRUCTURED_TASK_ID)
        expected_count = len(expected_ids)
        if (
            report.get("schema") != REPORT_SCHEMA
            or report.get("suite_version") != SUITE_VERSION
            or report.get("suite_hash") != SUITE_HASH
            or report.get("options") != INFERENCE_OPTIONS
        ):
            return False, "suite_mismatch"
        if expected_challenge is not None:
            if not isinstance(expected_challenge, str) or len(expected_challenge) > 256:
                return False, "challenge_mismatch"
            if report.get("challenge_hash") != hashlib.sha256(
                expected_challenge.encode("utf-8")
            ).hexdigest():
                return False, "challenge_mismatch"
        base = report["base"]
        candidate = report["candidate"]
        if expected_base is not None and base.get("model") != expected_base:
            return False, "model_mismatch"
        if expected_candidate is not None and candidate.get("model") != expected_candidate:
            return False, "model_mismatch"
        if base.get("total") != expected_count or candidate.get("total") != expected_count:
            return False, "incomplete_suite"
        if len(base["tasks"]) != expected_count or len(candidate["tasks"]) != expected_count:
            return False, "incomplete_suite"

        def checked_results(model_report: dict[str, Any]) -> dict[str, bool]:
            if not isinstance(model_report.get("model"), str) or not model_report["model"]:
                raise ValueError("invalid model")
            results: dict[str, bool] = {}
            for task_result in model_report["tasks"]:
                task_id = task_result["id"]
                passed = task_result["passed"]
                reason = task_result["reason"]
                artifact_hash = task_result["artifact_sha256"]
                if not isinstance(task_id, str) or task_id in results or type(passed) is not bool:
                    raise ValueError("invalid task result")
                if not isinstance(reason, str) or not reason or len(reason) > 128:
                    raise ValueError("invalid task reason")
                if artifact_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
                    raise ValueError("invalid artifact hash")
                if passed and artifact_hash is None:
                    raise ValueError("passed task has no artifact")
                if passed is not (reason == "passed"):
                    raise ValueError("task pass/reason mismatch")
                results[task_id] = passed
            score = model_report.get("score")
            if type(score) is not int or score != sum(results.values()):
                raise ValueError("invalid score")
            return results

        base_by_id = checked_results(base)
        candidate_by_id = checked_results(candidate)
        if set(base_by_id) != expected_ids or set(candidate_by_id) != expected_ids:
            return False, "incomplete_suite"
        base_score = sum(base_by_id.values())
        candidate_score = sum(candidate_by_id.values())
    except (AttributeError, KeyError, TypeError, ValueError):
        return False, "invalid_report"

    if any(
        task["reason"] in {"generation_error", "fixture_error"}
        for model_report in (base, candidate)
        for task in model_report["tasks"]
    ):
        return False, "evaluation_infrastructure_error"

    candidate_sql_score = sum(
        candidate_by_id[task.task_id] for task in TASKS
    )
    if candidate_sql_score < 3:
        return False, "candidate_below_floor"
    if include_structured and not candidate_by_id[STRUCTURED_TASK_ID]:
        return False, "candidate_failed_instruction_probe"
    regressions = sorted(task_id for task_id in expected_ids if base_by_id[task_id] and not candidate_by_id[task_id])
    if regressions:
        return False, "task_regression:" + ",".join(regressions)
    if base_score < expected_count and candidate_score <= base_score:
        return False, "candidate_has_no_lift"
    if base_score == expected_count and candidate_score != expected_count:
        return False, "candidate_not_perfect"
    return True, "promotion_passed"

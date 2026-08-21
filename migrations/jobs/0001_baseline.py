"""Adoption baseline for the durable job registry schema."""

manages_own_transaction = True


def apply(conn) -> None:
    from sonder_runtime.adapters.persistence.sqlite.job_registry import initialize_schema

    initialize_schema(conn)


def verify(conn) -> None:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(durable_job)")
    }
    required = {
        "job_id", "kind", "operation_id", "idempotency_key", "status",
        "revision", "created_at", "updated_at", "worker_id", "lease_until",
    }
    if not required <= columns:
        raise RuntimeError(
            "durable-job baseline verify failed: required columns missing"
        )

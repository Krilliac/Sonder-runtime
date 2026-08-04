"""Baseline schema for updates.db (SPEC-4 section 6)."""


def apply(conn) -> None:
    conn.execute(
        """
        CREATE TABLE installed_release (
            release_id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            platform TEXT NOT NULL,
            architecture TEXT NOT NULL,
            install_path TEXT NOT NULL,
            activated_at_utc TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
              'active','previous','staged','failed','removed'
            )),
            manifest_sha256 TEXT NOT NULL,
            state_schema_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX one_active_release_idx"
        " ON installed_release(status) WHERE status = 'active'"
    )
    conn.execute(
        """
        CREATE TABLE update_plan (
            update_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            channel TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('online','offline')),
            source_ref TEXT NOT NULL,
            from_release_id TEXT NOT NULL,
            target_version TEXT NOT NULL,
            target_manifest_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
              'planned','checking','available','downloading','verified',
              'staged','preflight','backing_up','draining','installing',
              'migrating','health_check','committed','rolling_back',
              'rolled_back','blocked','failed','cancelled'
            )),
            revision INTEGER NOT NULL DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            backup_id TEXT,
            error_code TEXT,
            error_detail_redacted TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE update_step (
            update_id TEXT NOT NULL,
            step_no INTEGER NOT NULL,
            step_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at_utc TEXT,
            completed_at_utc TEXT,
            attempt INTEGER NOT NULL DEFAULT 1,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT,
            PRIMARY KEY(update_id, step_no, attempt),
            FOREIGN KEY(update_id) REFERENCES update_plan(update_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE trusted_root (
            version INTEGER PRIMARY KEY,
            metadata_json TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            accepted_at_utc TEXT NOT NULL,
            source TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE update_channel (
            channel TEXT PRIMARY KEY,
            metadata_base_url TEXT,
            targets_base_url TEXT,
            automatic_check INTEGER NOT NULL DEFAULT 1,
            automatic_download INTEGER NOT NULL DEFAULT 0,
            automatic_install INTEGER NOT NULL DEFAULT 0,
            last_checked_at_utc TEXT
        )
        """
    )


def verify(conn) -> None:
    for table in (
        "installed_release",
        "update_plan",
        "update_step",
        "trusted_root",
        "update_channel",
    ):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"updates baseline verify failed: {table} missing")

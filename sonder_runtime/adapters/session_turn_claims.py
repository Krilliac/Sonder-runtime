"""Database-backed session turn claims for the persistent chat session.

Only one turn may read and extend a remembered session at a time; the claim
is held in the memory database with the owning process identity so a
crashed owner can be recognized, and released with a bounded retry before
the claim is abandoned. It opens the database and probes process liveness,
so it lives with the adapters. Moved from ``server.py`` in the WP1
Three-Hundred-Thirty-Second Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import os
import time

import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.adapters import process_liveness


def acquire_session_turn(session_id, *, open_db, claim_wait_seconds):
    """Acquire a DB-backed session claim before reading remembered history.

    ``open_db()`` opens the memory database and ``claim_wait_seconds`` bounds
    the wait for a contended session; both are injected so the root delegates
    keep the database seam and the environment-derived wait.
    """
    owner_state, owner_identity = process_liveness.probe_process(os.getpid())
    if (
        owner_state != process_liveness.PROCESS_ALIVE
        or not owner_identity
    ):
        return None, "ERROR: session owner identity is unavailable."
    try:
        conn = open_db()
    except Exception:
        return None, "ERROR: session turn coordination is unavailable."
    claim_token = memory_store.new_id()
    deadline = time.monotonic() + claim_wait_seconds
    while True:
        try:
            claimed = memory_store.claim_session_turn(
                conn,
                session_id,
                claim_token,
                owner_pid=os.getpid(),
                owner_identity=owner_identity,
            )
        except Exception:
            conn.close()
            return None, "ERROR: session turn coordination is unavailable."
        if claimed:
            return {
                "conn": conn,
                "session_id": session_id,
                "claim_token": claim_token,
                "owner_pid": os.getpid(),
                "owner_identity": owner_identity,
            }, ""
        if time.monotonic() >= deadline:
            conn.close()
            session_label = str(session_id).replace("\r", " ").replace("\n", " ")[:120]
            return None, (
                "ERROR: session '%s' already has a turn in progress; retry shortly."
                % session_label
            )
        time.sleep(0.05)


def release_session_turn_claim(claim, *, open_db):
    """Release a session claim, retrying with a fresh connection before abandoning it."""
    if not claim:
        return
    conn = claim["conn"]
    for attempt in range(3):
        try:
            memory_store.release_session_turn(
                conn, claim["session_id"], claim["claim_token"],
            )
            break
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            if attempt == 2:
                memory_store.abandon_session_turn_claim(
                    claim["session_id"], claim["claim_token"],
                    claim["owner_pid"], claim["owner_identity"],
                )
                return
            time.sleep(0.05)
            try:
                conn = open_db()
            except Exception:
                continue
    try:
        conn.close()
    except Exception:
        pass

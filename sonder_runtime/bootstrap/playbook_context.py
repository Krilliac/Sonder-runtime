"""Compose playbook context without touching models or importing the server."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import sqlite3

from sonder_runtime.adapters.playbook_telemetry import log_usage
from sonder_runtime.application.context import current_operation_context
from sonder_runtime.application.memory.playbook_context import PlaybookContext, PlaybookSelection, frame_owner_notes
from sonder_runtime.bootstrap.playbooks import get_store

_CONTEXT = PlaybookContext(get_store)
_SESSION = ContextVar("playbook_session", default=None)


def _session():
    context = current_operation_context()
    return str(_SESSION.get() or (context.session_id if context else "") or "")


@contextmanager
def session_scope(session_id=None):
    token = _SESSION.set(session_id or _session())
    try:
        yield
    finally:
        _SESSION.reset(token)


def configure(store=None):
    _CONTEXT.set_store(store)


def reload_index():
    return _CONTEXT.reload_index(_session())


def stable_index():
    return _CONTEXT.stable_index(_session())


def select(query, *, cloud=False):
    return PlaybookSelection() if cloud else _CONTEXT.select(query, _session())


def augment(system, query, *, cloud=False):
    selection = select(query, cloud=cloud)
    return (system + "\n\n" + selection.text if system and selection.text else system or selection.text), selection


def record_usage(conn, selection, interaction_id):
    if not interaction_id or not selection.topics:
        return
    try:
        log_usage(conn, selection.topics, interaction_id)
        conn.commit()
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        logging.getLogger(__name__).warning("Playbook usage attribution unavailable")


def metrics():
    return _CONTEXT.metrics()


__all__ = ["configure", "frame_owner_notes", "reload_index", "select", "stable_index", "augment", "record_usage", "metrics", "session_scope"]

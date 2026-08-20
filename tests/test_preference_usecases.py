"""Typed preference use cases and exact legacy-surface compatibility."""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import server
from sonder_runtime.adapters.preference_adapters import (
    LegacyPreferenceCodec,
    LegacyPreferenceRepository,
    NullPreferenceEventSink,
)
from sonder_runtime.adapters.preference_codec import PreferenceCodecAdapter
from sonder_runtime.application.ports.tool_executor import ToolResult
from sonder_runtime.application.preferences import (
    PreferenceService,
    render_preference_result,
)


class FakeRepository:
    def __init__(self):
        self.calls = []
        self.rows = [{"text": "User prefers concise replies.", "enabled": 1}]

    def upsert_and_list(self, **kwargs):
        self.calls.append(("upsert", kwargs))
        return self.rows

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return self.rows

    def set_enabled(self, target, **kwargs):
        self.calls.append(("set_enabled", target, kwargs))
        return 2


class FakeCodec:
    def extract(self, text):
        return ["User prefers concise replies."] if text else []

    def normalize(self, text):
        return str(text or "").strip()

    def key(self, text):
        return "preference-key"

    def is_stable(self, text, source_text=None):
        return bool(text)

    def format(self, rows):
        return "\n".join(row["text"] for row in rows)


class CapturingEvents:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def changed(self, operation, count):
        self.calls.append((operation, count))
        if self.fail:
            raise RuntimeError("event sink unavailable")


def _service(events=None):
    repository = FakeRepository()
    event_sink = events or CapturingEvents()
    return PreferenceService(repository, FakeCodec(), event_sink), repository, event_sink


def test_typed_service_preserves_success_text_and_metadata_only_events():
    service, repository, events = _service()

    learned = service.learn("I prefer concise replies", "team")
    disabled = service.disable("preference-key", "team")

    assert learned == ToolResult(
        ok=True,
        output=(
            "Learned preference: User prefers concise replies.\n\n"
            "User prefers concise replies."
        ),
    )
    assert disabled == ToolResult(
        ok=True, output="forgot 2 matching preference(s)"
    )
    assert repository.calls == [
        (
            "upsert",
            {
                "scope": "team",
                "key": "preference-key",
                "text": "User prefers concise replies.",
                "confidence": 0.8,
                "limit": 20,
            },
        ),
        ("set_enabled", "preference-key", {"enabled": False, "scope": "team"}),
    ]
    assert events.calls == [("learned", 1), ("disabled", 2)]
    assert "concise" not in repr(events.calls)
    assert "team" not in repr(events.calls)


def test_status_bounds_match_legacy_and_event_failures_do_not_change_success():
    events = CapturingEvents(fail=True)
    service, repository, _ = _service(events)

    assert service.learn("preference").ok is True
    assert service.status(True, "invalid").output == (
        "learned preferences\nUser prefers concise replies."
    )
    service.status(False, 999999)
    service.status(False, -10)
    service.status("false", True)

    assert repository.calls[-4:] == [
        ("list", {"limit": 50, "include_disabled": True}),
        ("list", {"limit": 200, "include_disabled": False}),
        ("list", {"limit": 1, "include_disabled": False}),
        ("list", {"limit": 50, "include_disabled": False}),
    ]


def test_empty_and_storage_failures_are_typed_with_exact_wire_rendering():
    service, repository, _ = _service()
    empty = service.learn("")
    assert empty.ok is False
    assert empty.error_code == "INVALID_INPUT"
    assert render_preference_result(empty) == "ERROR: preference text is empty."

    def fail(**kwargs):
        raise OSError("database unavailable at C:/private/preferences.db")

    repository.list = fail
    failed = service.status()
    assert failed.ok is False
    assert failed.error_code == "PREFERENCE_STORAGE_ERROR"
    assert failed.output == "preference storage is unavailable."
    assert render_preference_result(failed) == (
        "ERROR: preference storage is unavailable."
    )
    assert "C:/private" not in repr(failed)


def test_codec_failures_are_distinct_and_never_disclose_input_or_exception():
    service, _, _ = _service()
    secret = "private preference text"

    def fail(_value):
        raise UnicodeError("codec rejected %s" % secret)

    service._codec.extract = fail
    result = service.learn(secret)
    assert result == ToolResult(
        ok=False,
        output="preference processing failed.",
        error_code="PREFERENCE_CODEC_ERROR",
    )
    assert secret not in repr(result)
    assert render_preference_result(result) == (
        "ERROR: preference processing failed."
    )


def test_successful_error_prefixed_content_is_not_reclassified():
    result = ToolResult(ok=True, output="ERROR: legitimate stored content")
    assert render_preference_result(result) == "ERROR: legitimate stored content"

    service, _, _ = _service()
    service._codec.format = lambda _rows: "ERROR: legitimate stored content"
    status = service.status()
    assert status.ok is True
    assert status.error_code == ""
    assert render_preference_result(status) == (
        "learned preferences\nERROR: legitimate stored content"
    )


def test_legacy_repository_closes_connection_and_service_sanitizes_db_failure(
    monkeypatch,
):
    class Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = Connection()
    repository = LegacyPreferenceRepository(lambda: connection)

    def fail(*_args, **_kwargs):
        raise OSError("C:/private/preferences.db contains secret-user-text")

    monkeypatch.setattr(server.memory_store, "all_preferences", fail)
    service = PreferenceService(repository, FakeCodec(), CapturingEvents())
    result = service.status()
    assert result.error_code == "PREFERENCE_STORAGE_ERROR"
    assert result.output == "preference storage is unavailable."
    assert connection.closed is True
    assert "private" not in repr(result)
    assert "secret-user-text" not in repr(result)


def test_server_wire_uses_typed_status_not_error_prefix_heuristics(monkeypatch):
    service, repository, _ = _service()
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server, "_APP_GRAPH", SimpleNamespace(preferences=service),
    )
    service._codec.format = lambda _rows: "ERROR: legitimate stored preference"
    assert server.preferences_status() == (
        "learned preferences\nERROR: legitimate stored preference"
    )

    repository.list = lambda **_kwargs: (_ for _ in ()).throw(
        OSError("C:/private/preferences.db")
    )
    assert server.preferences_status() == (
        "ERROR: preference storage is unavailable."
    )


def test_legacy_codec_resolves_the_injected_live_module_identity():
    replacement = SimpleNamespace(
        extract_preferences=lambda text: ["extracted"],
        normalize_preference=lambda text: "normalized",
        preference_key=lambda text: "key",
        is_stable_preference=lambda text, source_text=None: True,
        format_preferences=lambda rows: "formatted",
    )
    codec = LegacyPreferenceCodec(lambda: replacement)
    assert codec.extract("raw") == ["extracted"]
    assert codec.normalize("raw") == "normalized"
    assert codec.key("raw") == "key"
    assert codec.format([]) == "formatted"
    assert NullPreferenceEventSink().changed("learned", 1) is None


def test_preference_codec_has_canonical_owner_and_legacy_identity():
    assert LegacyPreferenceCodec is PreferenceCodecAdapter


def test_server_mcp_signatures_docstrings_and_policies_are_unchanged():
    assert str(inspect.signature(server.learn_preference)) == (
        "(text: str, scope: str = 'global') -> str"
    )
    assert inspect.getdoc(server.learn_preference) == (
        "Teach Sonder a durable user preference immediately.\n\n"
        "This is for behavior, style, workflow defaults, names, and recurring user\n"
        "expectations. Learned preferences are injected into future local-model\n"
        "prompts and apply without restarting."
    )
    assert str(inspect.signature(server.preferences_status)) == (
        "(include_disabled: bool = False, limit: int = 50) -> str"
    )
    assert inspect.getdoc(server.preferences_status) == (
        "List learned user preferences that shape future responses."
    )
    assert "preferences_status" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "preferences_status" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "preferences_status" not in server._PROJECT_SCOPED_PATH_TOOLS
    assert "preferences_status" not in server._WORK_MUTATION_TOOLS
    assert "learn_preference" not in server.REPOSITORY_READ_ONLY_TOOLS
    assert "learn_preference" not in server._WORK_MUTATION_TOOLS


def test_server_integration_learns_lists_and_disables_without_restart(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "preferences.db"))
    monkeypatch.setattr(server, "_APP_GRAPH", None)

    learned = server.learn_preference("I prefer short direct answers")
    status = server.preferences_status()
    connection = server._open_db()
    try:
        row = connection.execute(
            "SELECT key FROM preferences WHERE enabled=1"
        ).fetchone()
    finally:
        connection.close()
    forgotten = server.preference_command("forget %s" % row["key"])
    including_disabled = server.preferences_status(include_disabled=True)
    enabled_only = server.preferences_status()

    assert "Learned preference" in learned
    assert "User prefers short direct answers." in status
    assert forgotten == "forgot 1 matching preference(s)"
    assert "User prefers short direct answers." in including_disabled
    assert "User prefers short direct answers." not in enabled_only


def test_live_reload_keeps_tool_identity_and_updates_service_codec(monkeypatch):
    original_tool = server.learn_preference
    original_module = server.preference_learning
    replacement = SimpleNamespace()
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    app = server._application()
    monkeypatch.setattr(
        server.live_reload,
        "reload_changed_modules",
        lambda names: {"preference_learning": replacement},
    )

    try:
        server._maybe_live_reload()

        assert server.learn_preference is original_tool
        assert server.preference_learning is replacement
        assert app.preferences._codec._module_provider() is replacement
    finally:
        server.preference_learning = original_module


def test_legacy_adapter_has_no_server_dependency_or_raw_activity_payload():
    from sonder_runtime.adapters import preference_adapters as preferences

    source = Path(preferences.__file__).read_text(encoding="utf-8")
    assert "import server" not in source
    assert "activity_tracker" not in source
    assert "preference text" not in source.lower()


def test_preference_repository_default_connection_uses_packaged_paths(monkeypatch):
    from sonder_runtime.adapters import preference_adapters as preferences

    captured = {}

    class Connection:
        def close(self):
            pass

    monkeypatch.setattr(
        preferences.sonder_paths,
        "memory_db_path",
        lambda: "packaged-memory.db",
    )
    monkeypatch.setattr(
        "sonder_runtime.adapters.memory_store.connect",
        lambda path, **kwargs: captured.update(path=path, kwargs=kwargs)
        or Connection(),
    )

    connection = preferences._default_connection()

    assert isinstance(connection, Connection)
    assert captured == {
        "path": "packaged-memory.db",
        "kwargs": {"check_same_thread": True},
    }

    source = Path(preferences.__file__).read_text(encoding="utf-8")
    assert "import sonder_paths" not in source
    assert "sonder_runtime.platform" in source

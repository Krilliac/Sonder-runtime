"""The command surface the app talks to: /v1/commands, completion, help.

The three routes and the /help text all read command_catalog, which derives
itself from the dispatch chains and the live tool registry -- so these tests
assert the shape of the contract rather than a frozen list of command names,
which would fail the moment a tool is added.
"""
import http.client
import itertools
import json
import threading
from contextlib import contextmanager

import command_catalog
import permission_modes
import sonder_serve as ts


@contextmanager
def _http_server(monkeypatch):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    response = conn.getresponse()
    raw = response.read()
    status = response.status
    conn.close()
    return status, json.loads(raw.decode("utf-8"))


def _assert_command_shape(entry):
    assert set(entry) == {
        "name", "aliases", "tool", "category", "risk", "summary", "native",
        "usage", "params",
    }
    assert entry["name"].startswith("/")
    assert isinstance(entry["aliases"], list)
    assert isinstance(entry["native"], bool)
    # Derived from the enforcing module rather than typed out. The literal
    # ("safe", "ask", "mutation", "dangerous") stood here, and it pinned the
    # wire to the one vocabulary the gate does NOT decide on -- it has no
    # ``execution`` -- so it passed happily while 27 commands reached the app
    # under a class ``_MATRIX`` has no row for, and it would have rejected the
    # fix. Every mode's row has the same key set; MANUAL is just a witness.
    assert entry["risk"] in permission_modes._MATRIX[permission_modes.MANUAL]
    for param in entry["params"]:
        assert set(param) == {"name", "type", "required", "default"}


# --- GET /v1/commands -----------------------------------------------------


def test_commands_index_returns_catalog_categories_and_popular(monkeypatch):
    with _http_server(monkeypatch) as port:
        status, payload = _get(port, "/v1/commands")

    assert status == 200
    assert set(payload) == {"commands", "categories", "popular"}
    assert len(payload["commands"]) == len(command_catalog.http_catalog())
    # The whole point of the catalog: far more than the legacy registry's 59.
    assert len(payload["commands"]) > 150
    for entry in payload["commands"]:
        _assert_command_shape(entry)
    assert payload["categories"] == dict(command_catalog.CATEGORIES)
    assert payload["popular"] == list(command_catalog.POPULAR)
    assert "/help" in payload["popular"]


def test_commands_index_entries_match_to_dict_exactly(monkeypatch):
    with _http_server(monkeypatch) as port:
        _, payload = _get(port, "/v1/commands")

    by_name = {entry["name"]: entry for entry in payload["commands"]}
    for command in command_catalog.http_catalog():
        assert by_name[command.name] == command.to_dict()


def test_commands_index_omits_console_only_native_controls(monkeypatch):
    """The app sends slash choices to the HTTP dispatcher, not the REPL."""
    assert command_catalog.by_name("/mode") is not None
    assert "/mode" not in command_catalog.http_native_names()
    assert ts._handle_slash("/mode plan") is None

    with _http_server(monkeypatch) as port:
        _, payload = _get(port, "/v1/commands")
        _, completion = _get(port, "/v1/commands/complete?q=mode&limit=50")
        _, help_payload = _get(port, "/v1/commands/help?topic=/mode")

    names = {entry["name"] for entry in payload["commands"]}
    assert "/mode" not in names
    assert "/model" not in names
    assert "/project" not in names
    assert "/mode" not in {entry["name"] for entry in completion["matches"]}
    assert "no HTTP command" in help_payload["text"]


def test_every_advertised_native_spelling_is_handled_by_http_dispatcher(monkeypatch):
    with _http_server(monkeypatch) as port:
        _, payload = _get(port, "/v1/commands")

    handled = command_catalog.http_native_names()
    for entry in payload["commands"]:
        if entry["native"]:
            assert entry["name"] in handled
            assert set(entry["aliases"]) <= handled


# --- GET /v1/commands/complete -------------------------------------------


def test_complete_returns_matches_in_command_shape(monkeypatch):
    with _http_server(monkeypatch) as port:
        status, payload = _get(port, "/v1/commands/complete?q=/file")

    assert status == 200
    assert set(payload) == {"matches"}
    assert payload["matches"], "expected /file to match something"
    for entry in payload["matches"]:
        _assert_command_shape(entry)


def test_complete_narrows_as_the_query_grows(monkeypatch):
    with _http_server(monkeypatch) as port:
        _, broad = _get(port, "/v1/commands/complete?q=f&limit=50")
        _, narrow = _get(port, "/v1/commands/complete?q=file_&limit=50")
        _, narrowest = _get(port, "/v1/commands/complete?q=file_read&limit=50")

    broad_names = [c["name"] for c in broad["matches"]]
    narrow_names = [c["name"] for c in narrow["matches"]]
    narrowest_names = [c["name"] for c in narrowest["matches"]]

    assert len(broad_names) > len(narrow_names) > len(narrowest_names)
    assert narrowest_names[0] == "/file_read"
    # Prefix matches lead; anything after them still earned its place, by a
    # match further inside the name or in the summary -- which is how "file_"
    # keeps /system_pro-file_-text on the end of the list rather than first.
    leading = list(
        itertools.takewhile(lambda name: name.startswith("/file_"), narrow_names)
    )
    assert "/file_read" in leading and len(leading) >= 8
    for entry in narrow["matches"][len(leading):]:
        assert "file_" in (entry["name"] + entry["summary"]).lower()


def test_complete_without_query_offers_the_popular_set(monkeypatch):
    with _http_server(monkeypatch) as port:
        _, payload = _get(port, "/v1/commands/complete")

    names = [c["name"] for c in payload["matches"]]
    assert names and names[0] == "/help"
    assert set(names) <= set(command_catalog.POPULAR)


def test_complete_limit_is_honoured_and_clamped(monkeypatch):
    with _http_server(monkeypatch) as port:
        _, three = _get(port, "/v1/commands/complete?q=/&limit=3")
        _, zero = _get(port, "/v1/commands/complete?q=file&limit=0")
        _, huge = _get(port, "/v1/commands/complete?q=file&limit=9999")

    assert len(three["matches"]) == 3
    assert len(zero["matches"]) == 1        # clamped up to the 1..50 floor
    assert len(huge["matches"]) <= 50       # clamped down to the ceiling


def test_complete_ignores_a_junk_limit_instead_of_failing(monkeypatch):
    """A bad limit must not 500 -- it blanks the menu the user is typing into."""
    with _http_server(monkeypatch) as port:
        status, junk = _get(port, "/v1/commands/complete?q=&limit=banana")
        empty_status, empty = _get(port, "/v1/commands/complete?q=&limit=")

    assert status == 200 and empty_status == 200
    assert len(junk["matches"]) == ts.COMPLETE_DEFAULT_LIMIT
    assert empty["matches"] == junk["matches"]


# --- GET /v1/commands/help ------------------------------------------------


def test_help_route_returns_the_overview_text(monkeypatch):
    with _http_server(monkeypatch) as port:
        status, payload = _get(port, "/v1/commands/help")

    assert status == 200
    assert set(payload) == {"text"}
    assert payload["text"] == command_catalog.help_text("")
    assert "categories" in payload["text"]


def test_help_route_topic_selects_a_category_then_a_command(monkeypatch):
    with _http_server(monkeypatch) as port:
        _, overview = _get(port, "/v1/commands/help")
        _, category = _get(port, "/v1/commands/help?topic=filesystem")
        _, command = _get(port, "/v1/commands/help?topic=/file_read")

    assert category["text"] != overview["text"]
    assert category["text"].startswith("filesystem --")
    assert "/file_read" in category["text"]
    assert command["text"].startswith("/file_read")
    assert "category: filesystem" in command["text"]


def test_unknown_commands_subroute_is_still_a_404(monkeypatch):
    with _http_server(monkeypatch) as port:
        status, payload = _get(port, "/v1/commands/nope")

    assert status == 404
    assert payload["error"]["type"] == "not_found"


# --- /help over the chat surface -----------------------------------------


def test_help_slash_uses_the_catalog_not_a_frozen_string():
    bare = ts._handle_slash("/help")
    category = ts._handle_slash("/help memory")
    command = ts._handle_slash("/help /file_read")

    assert bare == command_catalog.help_text("")
    assert category != bare
    assert category.startswith("memory --")
    assert command.startswith("/file_read")


def test_help_slash_for_an_unknown_topic_suggests_rather_than_raises():
    out = ts._handle_slash("/help filereed")

    assert "no exact command" in out or "no command" in out


# --- the tool fallback ----------------------------------------------------


def test_uncatalogued_slash_still_falls_through_to_the_model():
    assert ts._handle_slash("/definitely-not-a-command here") is None


def test_fallback_dispatches_a_catalogued_read_only_tool(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ts.server,
        "tool_manifest",
        lambda **kwargs: calls.append(kwargs) or "manifest text",
    )

    assert ts._handle_slash("/tool_manifest") == "manifest text"
    assert calls == [{}]


def test_fallback_binds_named_and_positional_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ts.server,
        "text_search",
        lambda **kwargs: calls.append(kwargs) or "searched",
    )

    assert ts._handle_slash("/text_search query=needle") == "searched"
    assert calls[0]["query"] == "needle"


def test_fallback_reports_an_unknown_key_instead_of_500ing(monkeypatch):
    def explode(**kwargs):
        raise AssertionError("the tool must not run with a dropped argument")

    monkeypatch.setattr(ts.server, "tool_manifest", explode)

    out = ts._handle_slash("/tool_manifest bogus=1")

    assert "does not take bogus" in out
    assert "/tool_manifest" in out


def test_fallback_surfaces_a_tool_fault_as_text(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("ollama is not running")

    monkeypatch.setattr(ts.server, "tool_manifest", boom)

    out = ts._handle_slash("/tool_manifest")

    assert "tool_manifest failed" in out
    assert "ollama is not running" in out


def test_fallback_never_shadows_an_explicit_branch(monkeypatch):
    """/read has its own branch; the fallback must not reach file_read first."""
    monkeypatch.setattr(
        ts.server, "file_read", lambda **kwargs: "explicit branch: %s" % kwargs["path"]
    )

    assert ts._handle_slash("/read README.md") == "explicit branch: README.md"

"""The app's host tool inventory fixtures match what the server really sends.

``app/test/fixtures/server/tool_inventory_*.json`` feed the Flutter client and
panel tests.  The 200/413/503 bodies are rebuilt here through the real
application service and HTTP facade; the 403/429 bodies are produced by
``serve.py`` itself and are checked against a live test server.  A wire change
therefore fails this test until the fixtures are regenerated with::

    python -m tests.test_app_tool_inventory_fixtures --write
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

from sonder_runtime.application.host_tools.service import HostToolInventoryService
from sonder_runtime.domain.host_tools.model import (
    DiscoverySource,
    ToolCategory,
    ToolRecord,
    VersionStatus,
    build_snapshot,
)
from sonder_runtime.interfaces.http.facades import host_tools as facade
from tests.test_compute_snapshot_http import _get, http_server  # noqa: F401 - fixture

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "app" / "test" / "fixtures" / "server"
_CREATED_AT = 1790334000.0
_NOW = _CREATED_AT + 754.0
_HOME = "/home/devon"


def _record(name, category, path, source, version, status, *, on_path=True,
            alternatives=(), details=()):
    return ToolRecord(name=name, category=category, path=path, source=source,
                      on_path=on_path, version=version, version_status=status,
                      identity="1:%d" % len(name), alternatives=tuple(alternatives),
                      details=tuple(details))


def _snapshot():
    C, S, V = ToolCategory, DiscoverySource, VersionStatus
    tools = [
        _record("gcc", C.COMPILER, "/usr/bin/gcc", S.PATH, "13.2.0", V.OK,
                alternatives=("/usr/local/bin/gcc",)),
        _record("clang", C.COMPILER, "/usr/bin/clang", S.PATH, "", V.TIMEOUT),
        _record("cmake", C.BUILD_SYSTEM, _HOME + "/.local/bin/cmake", S.PATH, "3.28.3", V.OK),
        _record("ninja", C.BUILD_SYSTEM, "/usr/bin/ninja", S.PATH, "", V.DEFERRED),
        _record("pytest", C.TEST_RUNNER, _HOME + "/project/.venv/bin/pytest", S.KNOWN_PREFIX,
                "", V.PROJECT_LOCAL, on_path=False),
        _record("gdb", C.DEBUGGER_PROFILER, "/usr/bin/gdb", S.PATH, "", V.FAILED),
        _record("python3", C.RUNTIME, "/usr/bin/python3", S.PATH, "3.12.3", V.OK,
                details=(("py:3.11", "/usr/bin/python3.11"),)),
        _record("git", C.VCS, "/usr/bin/git", S.PATH, "2.43.0", V.OK),
    ]
    return build_snapshot(os="Linux", os_release="6.8.0", machine="x86_64",
                          created_at=_CREATED_AT, duration_ms=412, tools=tools,
                          notes=["skipped 1 relative or invalid PATH entries"])


class _Discovery:
    def discover(self, *, previous, full):
        return _snapshot()


class _Store:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def load(self):
        return self.snapshot

    def save(self, snapshot):
        self.snapshot = snapshot


def _service(ttl_seconds=3600, now=_NOW):
    return HostToolInventoryService(
        _Discovery(), _Store(_snapshot()), clock=lambda: now, ttl_seconds=ttl_seconds,
        redact_path=lambda text: text.replace(_HOME, "~"), executable_guard=lambda path: True,
    )


def facade_fixtures() -> dict[str, tuple[int, dict]]:
    """Fixture name -> (status, body) produced by the real facade."""
    ok = lambda: _service()  # noqa: E731 - a factory, as serve.py passes one
    stale = lambda: _service(ttl_seconds=600)  # noqa: E731
    fixtures = {
        "tool_inventory_200.json": facade.dispatch_tool_inventory(ok, {}),
        "tool_inventory_compiler_200.json": facade.dispatch_tool_inventory(
            ok, {"category": ["compiler"]}),
        "tool_inventory_stale_200.json": facade.dispatch_tool_inventory(stale, {}),
        "tool_inventory_unavailable_503.json": facade.dispatch_tool_inventory(lambda: None, {}),
    }
    saved = facade.MAX_RESPONSE_BYTES
    facade.MAX_RESPONSE_BYTES = 64
    try:
        fixtures["tool_inventory_too_large_413.json"] = facade.dispatch_tool_inventory(ok, {})
    finally:
        facade.MAX_RESPONSE_BYTES = saved
    return fixtures


# Bodies serve.py writes itself before the facade runs; verified over HTTP below.
SERVE_FIXTURES: dict[str, tuple[int, dict]] = {
    "tool_inventory_forbidden_403.json": (403, {"error": {"code": "FORBIDDEN"}}),
    "tool_inventory_busy_429.json": (429, {"error": {"code": "TOOL_INVENTORY_BUSY"}}),
}


def _read(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def write_fixtures() -> None:
    for name, (_status, body) in {**facade_fixtures(), **SERVE_FIXTURES}.items():
        (FIXTURE_DIR / name).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("name", sorted(facade_fixtures()))
def test_facade_fixture_matches_committed_file(name):
    status, body = facade_fixtures()[name]
    assert _read(name) == body, f"regenerate {name}: python -m tests.test_app_tool_inventory_fixtures --write"
    assert int(name.rsplit("_", 1)[1].split(".")[0]) == status


def test_full_fixture_covers_the_statuses_the_panel_renders():
    body = _read("tool_inventory_200.json")
    assert body["object"] == "tool_inventory" and body["stale"] is False
    statuses = {tool["version_status"] for tool in body["tools"]}
    assert {"ok", "timeout", "failed", "deferred", "project_local_not_probed"} <= statuses
    assert "/home/devon" not in json.dumps(body)
    assert _read("tool_inventory_stale_200.json")["stale"] is True
    assert [t["name"] for t in _read("tool_inventory_compiler_200.json")["tools"]] == ["clang", "gcc"]


def _as_admin(monkeypatch, role):
    from sonder_runtime.interfaces.http import serve

    monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda self: {
        "authorized": True, "mode": "account", "account": {"role": role}, "api_key": False})
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app",
                        lambda: SimpleNamespace(developer_tools=SimpleNamespace(inventory=_service())))


def test_serve_fixtures_match_live_responses(http_server, monkeypatch):
    from threading import BoundedSemaphore

    _as_admin(monkeypatch, "user")
    status, body = _get(http_server, "/v1/tools/inventory")
    expected_status, expected = SERVE_FIXTURES["tool_inventory_forbidden_403.json"]
    assert (status, body) == (expected_status, expected) == (403, _read("tool_inventory_forbidden_403.json"))

    _as_admin(monkeypatch, "admin")
    gate = BoundedSemaphore(1)
    monkeypatch.setattr(facade, "_TOOL_INVENTORY_SLOTS", gate)
    gate.acquire()
    try:
        status, body = _get(http_server, "/v1/tools/inventory")
    finally:
        gate.release()
    expected_status, expected = SERVE_FIXTURES["tool_inventory_busy_429.json"]
    assert (status, body) == (expected_status, expected) == (429, _read("tool_inventory_busy_429.json"))

    # The same service over HTTP returns the committed 200 fixture byte-for-value.
    status, body = _get(http_server, "/v1/tools/inventory")
    assert status == 200 and body == _read("tool_inventory_200.json")


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        sys.exit("usage: python -m tests.test_app_tool_inventory_fixtures --write")
    write_fixtures()

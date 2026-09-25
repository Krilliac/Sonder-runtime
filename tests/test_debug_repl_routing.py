"""Plain-language routes to ``/crash`` and ``/profile``, and their catalog grades."""
from __future__ import annotations

import pytest

import permission_modes as pm
from sonder_runtime.adapters import command_catalog
from sonder_runtime.interfaces.repl import command_router as cr


@pytest.mark.parametrize("phrase, expected", [
    ("analyze the crash dump dumps/x.dmp", "/crash dumps/x.dmp"),
    ("analyse this crash core.4411", "/crash core.4411"),
    ("analyze the crash dump build/game.core", "/crash build/game.core"),
    ("analyze crash asan.log", "/crash asan.log"),
    ("why did the game crash core.1234?", "/crash core.1234"),
    ("why did it crash core", "/crash core"),
    ("why did this crash memcheck.xml", "/crash memcheck.xml"),
    ("summarize the trace out.json", "/profile out.json"),
    ("analyze the capture perf.data", "/profile perf.data"),
    ("summarise this profile callgrind.out.4242", "/profile callgrind.out.4242"),
    ("analyze the profile hotspots.csv", "/profile hotspots.csv"),
    ("fix that crash", "/crash fix last"),
    ("fix the crash!", "/crash fix last"),
])
def test_whole_turn_phrases_route(phrase, expected):
    assert cr.resolve(phrase) == expected
    assert cr.explain(phrase)["source"] == "rule"


@pytest.mark.parametrize("phrase", [
    "profile startup",
    "profile the startup path and make it faster",
    "why is the game slow",
    "analyze the crash dump dumps/x.dmp and fix the bug",
    "fix that crash in the renderer by adding a null check",
    "summarize the trace out.json then email it",
    "analyze the crash",
])
def test_broader_requests_are_not_hijacked(phrase):
    resolved = cr.resolve(phrase)
    assert resolved is None or not resolved.startswith(("/crash", "/profile"))


def test_catalog_categories():
    assert command_catalog._CATEGORY_BY_SLASH["/crash"] == "dev"
    assert command_catalog._CATEGORY_BY_SLASH["/profile"] == "dev"
    for tool in ("crash_triage", "crash_digest", "profile_digest",
                 "profile_capture_digest", "debug_run_result"):
        assert command_catalog._CATEGORY_BY_TOOL[tool] == "dev"


def test_console_branches_carry_their_execution_stand_ins():
    work = command_catalog._UNREGISTERED_BRANCH_WORK
    assert work["/crash"] == "crash_digest"
    assert work["/profile"] == "profile_capture_digest"
    console = command_catalog.console_tools()
    assert "crash_digest" in console["/crash"]
    assert "profile_capture_digest" in console["/profile"]


def test_execution_command_parity_for_the_debug_stand_ins():
    """Both stand-ins must grade ``execution`` (lane C's permission_modes hunk)."""
    if "crash_digest" not in pm.EXECUTION_TOOLS:
        pytest.skip("lane C's permission_modes hunk (EXECUTION_TOOLS) is not merged yet")
    for name in ("crash_digest", "profile_capture_digest"):
        assert pm.risk_of(name) == "execution", name
        assert name not in pm.EXECUTION_COMMANDS, (
            "a typed tool is graded by EXECUTION_TOOLS, not EXECUTION_COMMANDS")

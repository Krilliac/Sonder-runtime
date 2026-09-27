"""Plain-language routes to ``/tools``, ``/test`` and ``/digest``."""
from __future__ import annotations

import pytest

from sonder_runtime.interfaces.repl import command_router as cr


@pytest.mark.parametrize("phrase, expected", [
    ("show installed tools", "/tools"),
    ("list available tools", "/tools"),
    ("show me the installed developer tools", "/tools"),
    ("list my available dev tools by category", "/tools"),
    ("show the tool inventory", "/tools"),
    ("tool inventory", "/tools"),
    ("run the tests", "/test"),
    ("run tests", "/test"),
    ("run all the tests!", "/test"),
    ("run the project's tests", "/test"),
    ("run pytest", "/test pytest"),
    ("run ctest", "/test ctest"),
    ("run cargo test", "/test cargo"),
    ("run go test", "/test go"),
    ("run dotnet test", "/test dotnet"),
    ("run npm test", "/test npm"),
    ("run pnpm test", "/test pnpm"),
    ("run yarn test", "/test yarn"),
    ("run gradle test", "/test gradle"),
    ("run mvn test", "/test maven"),
    ("run make test", "/test make"),
    ("summarize the output of job test-run-1a2b", "/digest test-run-1a2b"),
    ("summarise the log from build/out.log", "/digest build/out.log"),
    ("summarize output for lane-test-9", "/digest lane-test-9"),
])
def test_new_phrases_route_to_the_intended_command(phrase, expected):
    assert cr.resolve(phrase) == expected
    assert cr.explain(phrase)["source"] == "rule"


@pytest.mark.parametrize("phrase, expected", [
    ("which toolchains are installed", "/env"),
    ("which tools are installed?", "/env"),
    ("what version is cargo?", "/toolstatus cargo"),
    ("show the tool status", "/toolstatus"),
    ("show me the tools", "/tool_manifest"),
    ("what tools do you have", "/tool_manifest"),
    ("tool activity", "/activity"),
])
def test_existing_neighbours_keep_their_routes(phrase, expected):
    assert cr.resolve(phrase) == expected


@pytest.mark.parametrize("phrase", [
    "run the tests and then fix whatever fails",
    "run pytest on the auth module with coverage",
    "summarize the output of job 12 and email it",
    "how do I run the tests in CI",
])
def test_longer_work_requests_are_not_hijacked(phrase):
    resolved = cr.resolve(phrase)
    assert resolved is None or not resolved.startswith(("/test ", "/digest", "/tools"))
    assert resolved != "/test"


def test_tool_inventory_phrase_resolves_to_the_command_that_fronts_the_tool():
    # ``/tools`` renders the same host inventory service the registered
    # ``tool_inventory`` tool reads, so the catalog must record that the
    # routed slash fronts that tool rather than the single-probe
    # ``toolchain_status``.
    from sonder_runtime.adapters import command_catalog

    resolved = cr.resolve("tool inventory")
    assert resolved == "/tools"
    assert command_catalog.console_tools()[resolved] == ("tool_inventory",)
    assert command_catalog.by_name("/tool_inventory").tool == "tool_inventory"

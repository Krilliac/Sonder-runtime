"""The read-only reference_search tool and its registration."""
from __future__ import annotations

import inspect
import re
import sqlite3

import pytest

from sonder_runtime.adapters import reference_lookup_service as service
from sonder_runtime.adapters import reference_lookup_state as state
from sonder_runtime.adapters.command_catalog import _READ_ONLY
from sonder_runtime.bootstrap import reference_agent_tools as tools
from sonder_runtime.domain.reference_lookup.prompt import MARKER_LINE_RE
from sonder_runtime.domain.tools.builtin_traits import READ_ONLY_TOOLS
from sonder_runtime.platform import reference_library_config as config
from tests._reference_lookup_index import CREATE_META, CREATE_PASSAGES, create_index, rule_cache_cleared


# No applies rule is carried from one test to the next.
_forget_rule_cache = pytest.fixture(autouse=True)(rule_cache_cleared)


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


@pytest.fixture
def tool(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(config.ENV_INDEX, str(tmp_path / "absent" / "refs.sqlite"))
    monkeypatch.delenv(config.ENV_ENABLED, raising=False)
    monkeypatch.setattr(state, "_INDEX", None)
    monkeypatch.setattr(state, "_LAST_ERROR", "")
    fake = _FakeMcp()
    recorded = []
    tools.register(fake, lambda *a, **kw: recorded.append((a, kw)))
    fn = fake.tools["reference_search"]
    fn.recorded = recorded
    return fn


@pytest.fixture
def good_index(monkeypatch, tmp_path):
    path = create_index(tmp_path / "idx" / "refs.sqlite")
    monkeypatch.setenv(config.ENV_INDEX, str(path))
    return path


def test_hits_come_back_framed_with_sources(tool, good_index):
    text = tool("failed logon 4625", 3)
    lines = text.splitlines()
    assert any(MARKER_LINE_RE.match(line) and "=== PASSAGE 1 [" in line for line in lines)
    assert "4625: An account failed to log on" in text
    assert "(events: https://example.test/events/4625)" in text
    assert "Sources:" in text
    assert "untrusted reference data" in text
    assert not text.startswith("ERROR")
    (args, kwargs), = tool.recorded
    assert args[0] == "reference_search" and kwargs["ok"] is True


def test_marker_code_is_fresh_for_each_call(tool, good_index):
    first = re.search(r"PASSAGE 1 \[(\w+)\]", tool("4625", 2)).group(1)
    second = re.search(r"PASSAGE 1 \[(\w+)\]", tool("4625", 2)).group(1)
    assert first != second


def test_no_match_is_plain_text_not_an_error(tool, good_index):
    assert tool("zzzqqq", 5) == "no reference passages matched the query"


@pytest.mark.parametrize("given, expected", [
    (0, 1), (-5, 1), (1, 1), (5, 5), (8, 8), (9, 8), (1000, 8), ("3", 3), ("9", 8), ("abc", 5),
    (None, 5), (True, 5), (2.9, 2), ("", 5),
])
def test_k_is_clamped(given, expected):
    assert service.clamp_k(given) == expected


def _passage_count(text):
    return len(re.findall(r"^=== END PASSAGE \d+ \[\w+\] ===$", text, re.MULTILINE))


def test_tool_applies_the_clamp(tool, good_index, monkeypatch):
    seen = []
    real = service.retrieve_for

    def spy(question, expand_fn, k, char_budget):
        seen.append(k)
        return real(question, expand_fn, k, char_budget)

    monkeypatch.setattr(service, "retrieve_for", spy)
    for given in (0, 9, "7", "abc", 100):
        tool("password", given)
    assert seen == [1, 8, 7, 5, 8]
    assert _passage_count(tool("password logon account brute force", 100)) <= 8
    assert _passage_count(tool("password logon account brute force", 0)) == 1


def test_only_query_and_k_are_accepted(tool):
    assert list(inspect.signature(tool).parameters) == ["query", "k"]
    with pytest.raises(TypeError):
        tool("4625", 3, path="C:/evil.sqlite")           # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tool(query="4625", index="C:/evil.sqlite")       # type: ignore[call-arg]


def test_extra_argument_never_changes_which_index_is_read(tool, good_index, tmp_path):
    other = create_index(tmp_path / "other.sqlite",
                         rows=[("OTHER only", "", "unique zebra text", "x", "", "p", 1)])
    with pytest.raises(TypeError):
        tool("zebra", 3, path=str(other))                # type: ignore[call-arg]
    assert "zebra" not in tool("zebra", 3)


def _text_file(tmp_path):
    path = tmp_path / "plain.txt"
    path.write_text("this is not a database", encoding="utf-8")
    return path


def _no_fts5(tmp_path):
    path = tmp_path / "plain.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE passages(title, heading, body, source, url, path, doc)")
    conn.execute(CREATE_META)
    conn.commit()
    conn.close()
    return path


def _no_meta(tmp_path):
    path = tmp_path / "nometa.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(CREATE_PASSAGES)
    conn.commit()
    conn.close()
    return path


@pytest.mark.parametrize("make, reason", [
    (lambda p: p / "absent.sqlite", "missing"),
    (_text_file, "not_sqlite"),
    (_no_fts5, "wrong_schema"),
    (_no_meta, "wrong_schema"),
])
def test_bad_index_states_return_the_unavailable_text(tool, tmp_path, monkeypatch, make, reason):
    monkeypatch.setenv(config.ENV_INDEX, str(make(tmp_path)))
    text = tool("failed logon 4625", 5)
    assert text == "reference lookup unavailable: " + reason
    assert "ERROR" not in text and "Traceback" not in text
    assert tool.recorded[-1][1]["ok"] is False
    assert reason in service.status().last_error


def test_directory_as_index_and_kill_switch_are_unavailable(tool, tmp_path, monkeypatch, good_index):
    monkeypatch.setenv(config.ENV_INDEX, str(tmp_path))
    assert tool("4625", 3) == "reference lookup unavailable: unreadable"
    monkeypatch.setenv(config.ENV_INDEX, str(good_index))
    monkeypatch.setenv(config.ENV_ENABLED, "0")
    assert tool("4625", 3) == "reference lookup unavailable: disabled"


def test_non_text_query_never_raises(tool, good_index):
    assert tool(None, 3) == "reference lookup unavailable: empty_question"      # type: ignore[arg-type]
    assert tool("   ", 3) == "reference lookup unavailable: empty_question"


def test_search_failure_inside_the_service_never_escapes(tool, good_index, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("search exploded\x1b[31m")

    monkeypatch.setattr(service, "retrieve_for", boom)
    text = tool("4625", 3)
    assert text.startswith("reference lookup unavailable: RuntimeError: search exploded")
    assert "\x1b" not in text


def test_the_tool_is_read_only_everywhere_it_is_registered():
    assert "reference_search" in READ_ONLY_TOOLS
    assert "reference_search" in _READ_ONLY
    from sonder_runtime.adapters.mcp_tool_manifest import MCP_TOOL_MANIFEST
    assert any("reference_search" in key.split("/") for key in MCP_TOOL_MANIFEST)


def test_registrar_is_discovered_without_a_server_line():
    from sonder_runtime.bootstrap.agent_help_tools import discover_agent_tool_registrars

    assert "reference_agent_tools" in dict(discover_agent_tool_registrars())

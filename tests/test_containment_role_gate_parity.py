"""Containment: the ``user`` role's tool allowlist holds on every path a model reaches.

Boundary: a ``user``-role caller, and any model acting for one, may use only
the tools on the user allowlist (``adapters/data/user_tool_allowlist.json``);
developer and admin are unrestricted.

Functions under test: ``public_policy.decide_tool`` / ``parse_allowlist`` /
``parse_untooled_allowlist``; ``role_tool_policy.refusal_reason`` /
``agent_refusal`` / ``bind_caller_role`` / ``user_tool_allowlist``;
``serve._role_tool_refusal_reason`` / ``_restricted_tool_role`` /
``_dispatch_catalogued_tool`` / ``_loop_global_operation_refusal``; and
``server._agent_dispatch`` (canonicalize, then ``agent_refusal``).

Classification of every finding (BUG / GAP / BY-DESIGN) is in the test id or
docstring. Tests named ``test_gap_*`` PIN CURRENT BEHAVIOUR of a gap nobody has
decided to close; they are expected to be edited when the gap is fixed.

Seeded part: spellings of a tool name are mutated by a ``random.Random``
seeded from ``FUZZ_SEED`` (default below); the seed is in every assertion
message so a failure replays with ``FUZZ_SEED=<n>``.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import permission_modes  # noqa: F401  (fixtures below patch it)
import server
from sonder_runtime.adapters import command_catalog
from sonder_runtime.adapters.security import role_tool_policy
from sonder_runtime.application import model_batching
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.domain import public_policy
from sonder_runtime.domain.agents.tool_naming import (
    AGENT_TOOL_ALIASES,
    canonical_agent_tool_name,
)
from sonder_runtime.interfaces.http import serve
from sonder_runtime.interfaces.http.work_runs import WorkRunner
from sonder_runtime.platform import runtime_threads
from tests.test_role_tool_gate import HOST_ACTING, _context
from tests.test_role_tool_gate_agent import (  # noqa: F401  (fixtures)
    _record,
    auto_mode,
    user_caller,
)

FUZZ_SEED = int(os.environ.get("FUZZ_SEED", "20261116"))
USER_CTX = _context("user")
HOST_POLICY = "ERROR: HOST POLICY"


# --- helpers -----------------------------------------------------------------

def _fake_allowlist(monkeypatch, *names):
    """Replace the cached shipped allowlist with ``names`` (the gate calls it by module global)."""
    monkeypatch.setattr(role_tool_policy, "user_tool_allowlist", lambda: frozenset(names))


def _agent_decision(spelling):
    """The agent path's verdict for a raw spelling: exactly what server.py:18181 does."""
    name = canonical_agent_tool_name(str(spelling or "").strip())
    return name, bind_and_refuse(name)


def _server_gate_refuses_by_role(spelling):
    """The REAL server.py gate (:18181, canonicalize then agent_refusal) for a raw spelling."""
    token = role_tool_policy._CALLER_ROLE.set("user")
    try:
        return "not available to this account role" in server._agent_permission_gate_error(spelling, {}, record=False)
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)


def bind_and_refuse(name):
    token = role_tool_policy._CALLER_ROLE.set("user")
    try:
        return role_tool_policy.agent_refusal(name)
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)


def _spellings(base):
    """Named spellings of ``base`` an attacker may try (ASCII ``base`` with letters)."""
    first = base[0]
    homoglyph = base.replace("i", "\u0456", 1) if "i" in base else base.replace("e", "\u0435", 1)
    return {
        "exact": base,
        "upper": base.upper(),
        "title": base.title(),
        "mixed-case": "".join(c.upper() if i % 2 else c for i, c in enumerate(base)),
        "leading-space": " " + base,
        "trailing-space": base + " ",
        "tab-newline-pad": "\t" + base + "\n",
        "leading-slash": "/" + base,
        "zero-width-space-inside": base[:3] + "\u200b" + base[3:],
        "zero-width-joiner-trailing": base + "\u200d",
        "bom-leading": "\ufeff" + base,
        "cyrillic-homoglyph": homoglyph,
        "fullwidth-nfkc": "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in base),
        "nul-trailing": base + "\x00",
        "double-slash": "//" + base,
        "backslash": "\\" + base,
        "dot-suffix": base + ".",
        "first-upper-only": first.upper() + base[1:],
    }


def _recorder(called, name):
    return lambda *a, **k: (called.append(name), "ran")[1]


@pytest.fixture
def all_handlers_recorded(monkeypatch):
    """Replace every tool handler the tests could reach with a recorder."""
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    names |= set(HOST_ACTING) | set(AGENT_TOOL_ALIASES.values())
    called = []
    for name in names:
        monkeypatch.setattr(
            server, name,
            _recorder(called, name),
            raising=False,
        )
    return called


# --- 1. name parity ----------------------------------------------------------

@pytest.mark.parametrize("tool", ("file_write", "run_code", "script_run"))
@pytest.mark.parametrize("label", sorted(_spellings("file_write")))
def test_unlisted_tool_never_runs_under_any_spelling_on_either_path(
    monkeypatch, auto_mode, all_handlers_recorded, tool, label,  # noqa: F811
):
    """Allowlist = {status}; B = ``tool``. No spelling of B may run B, by HTTP or agent path."""
    _fake_allowlist(monkeypatch, "status")
    spelling = _spellings(tool)[label]

    # HTTP: the raw-name helper (:3167) and the real catalogued dispatch.
    assert serve._role_tool_refusal_reason(spelling, USER_CTX), (
        f"HTTP gate allowed raw spelling {spelling!r}")
    line = f"/{spelling} path=x content=y code=1"
    result = serve._dispatch_catalogued_tool(line, serve.ConversationState(), context=USER_CTX)
    assert result is None or "refused" in str(result) or "unknown" in str(result).lower() \
        or "not " in str(result), (spelling, result)
    assert all_handlers_recorded == [], f"HTTP path ran {all_handlers_recorded} for spelling {spelling!r}"

    # Agent: canonicalize, role gate, then dispatch. Bind the role the way serve.py:4960 does.
    token = role_tool_policy._CALLER_ROLE.set("user")
    try:
        with server._managed_agent_admission_scope():
            observation = server._agent_dispatch(spelling, {"path": "x", "content": "y", "code": "1"})
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)
    assert str(observation).startswith(HOST_POLICY), (spelling, observation)
    assert all_handlers_recorded == [], f"agent path ran {all_handlers_recorded} for spelling {spelling!r}"


def test_http_catalog_resolution_is_wider_than_the_raw_gate_but_gates_the_resolved_tool(
    monkeypatch, auto_mode, all_handlers_recorded,  # noqa: F811
):
    """BY-DESIGN: the catalog folds case/slash, so the raw-name helper sees a different string than runs.

    The gate that matters runs on the catalog-RESOLVED tool name
    (serve.py:3725), so both outcomes below are consistent: an allowlisted
    tool runs under a case variant, an unlisted one is refused under it.
    """
    _fake_allowlist(monkeypatch, "status")
    assert command_catalog.by_name("/STATUS").tool == "status"
    assert command_catalog.by_name("/FILE_WRITE").tool == "file_write"
    assert serve._role_tool_refusal_reason("STATUS", USER_CTX)  # raw helper is stricter
    assert serve._role_tool_refusal_reason("status", USER_CTX) == ""
    allowed = serve._dispatch_catalogued_tool("/STATUS", serve.ConversationState(), context=USER_CTX)
    assert "status" in all_handlers_recorded, allowed
    all_handlers_recorded.clear()
    refused = serve._dispatch_catalogued_tool("/FILE_WRITE path=x content=y", serve.ConversationState(),
                                              context=USER_CTX)
    assert str(refused).startswith("refused"), refused
    assert all_handlers_recorded == []


def test_agent_path_is_exact_match_stricter_than_http_for_case_variants(monkeypatch):
    """BY-DESIGN: agent names are strip-only; ``STATUS`` is refused although HTTP ``/STATUS`` runs."""
    _fake_allowlist(monkeypatch, "status")
    assert _agent_decision("status")[1] == ""
    assert _agent_decision("  status\n")[1] == ""  # strip is applied before the gate
    for spelling in ("STATUS", "Status", "/status", "status\x00", "stat\u200bus", "\ufeffstatus",
                     "\uff53tatus", "st\u0430tus"):
        assert _agent_decision(spelling)[1].startswith(HOST_POLICY), spelling


@pytest.mark.parametrize("alias,canonical", sorted(AGENT_TOOL_ALIASES.items()))
def test_alias_allowance_rides_only_on_its_own_canonical_tool(monkeypatch, alias, canonical):
    """Allowlisting X permits spellings canonicalizing to X and nothing else.

    allowlist {canonical}: the alias spelling runs (same tool, intended).
    allowlist {alias}: dead on the agent path (canonicalized away first); the
    alias name is never a free ticket to the tool it points at (GAP: such an
    entry silently does nothing, it fails closed).
    """
    _fake_allowlist(monkeypatch, canonical)
    assert not _server_gate_refuses_by_role(alias)
    assert _server_gate_refuses_by_role(alias + "x")
    assert _agent_decision(alias) == (canonical, "")
    assert _agent_decision(canonical) == (canonical, "")
    others = {k: v for k, v in AGENT_TOOL_ALIASES.items() if v != canonical}
    for other_alias, other_canonical in others.items():
        assert _agent_decision(other_alias)[1].startswith(HOST_POLICY), (other_alias, other_canonical)
    _fake_allowlist(monkeypatch, alias)
    assert _server_gate_refuses_by_role(alias)
    assert _agent_decision(alias)[1].startswith(HOST_POLICY)
    assert _agent_decision(canonical)[1].startswith(HOST_POLICY)


def test_alias_names_are_not_registered_tools_except_through_the_catalog():
    """The HTTP path can only execute catalog-resolved tools; an alias key that were a real tool name
    would make the raw-name gate and the agent gate disagree on what an allowlist entry means."""
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert not (set(AGENT_TOOL_ALIASES) & registered)
    for alias, canonical in AGENT_TOOL_ALIASES.items():
        command = command_catalog.by_name("/" + alias)
        # '' = a hand-routed chain command (gated by _http_slash_refusal on its mapped tools)
        assert command is None or command.tool in (canonical, "", None), (alias, command.tool)


def _mutate(rng, base):
    """One seeded hostile spelling of ``base``."""
    out = list(base)
    for _ in range(rng.randint(1, 4)):
        kind = rng.randrange(8)
        pos = rng.randrange(len(out) + 1)
        if kind == 0 and out:
            i = min(pos, len(out) - 1)
            out[i] = out[i].swapcase()
        elif kind == 1:
            out.insert(pos, rng.choice(" \t\n\r\x0b\x0c\u00a0\u2003"))
        elif kind == 2:
            out.insert(pos, rng.choice("\u200b\u200c\u200d\u2060\ufeff\u00ad"))
        elif kind == 3:
            out.insert(0, rng.choice("/\\.~"))
        elif kind == 4 and out:
            i = min(pos, len(out) - 1)
            out[i] = {"e": "\u0435", "i": "\u0456", "o": "\u043e", "a": "\u0430", "l": "\u04cf"}.get(out[i], out[i])
        elif kind == 5 and out:
            i = min(pos, len(out) - 1)
            out[i] = chr(ord(out[i]) + 0xFEE0) if "!" <= out[i] <= "~" else out[i]
        elif kind == 6:
            out.append(rng.choice("\x00.:;,"))
        elif kind == 7 and out:
            del out[min(pos, len(out) - 1)]
    return "".join(out)


def test_seeded_spellings_obey_the_exact_gate_oracle(monkeypatch, auto_mode, all_handlers_recorded):  # noqa: F811
    """Oracle: the agent path permits a spelling iff strip+alias-canonicalization lands ON the allowlist,
    and what it would execute is exactly that gated name (the dispatcher is an exact-match if-chain)."""
    rng = random.Random(FUZZ_SEED)
    allow = frozenset({"status", "tool_help"})
    _fake_allowlist(monkeypatch, *allow)
    bases = ("file_write", "status", "run_code", "tool_help", "assetgen", "agent_status", "master")
    started = time.perf_counter()
    permitted_executed = set()
    for iteration in range(300):
        spelling = _mutate(rng, rng.choice(bases))
        where = f"seed={FUZZ_SEED} iteration={iteration} spelling={spelling!r}"
        executed = canonical_agent_tool_name(spelling.strip())
        refusal = role_tool_policy.refusal_reason(executed, "user")
        assert bool(refusal) == (executed not in allow), where
        assert bool(serve._role_tool_refusal_reason(spelling, USER_CTX)) == (spelling not in allow), where
        assert _server_gate_refuses_by_role(spelling) == bool(refusal), where
        if not refusal:
            permitted_executed.add(executed)
        # HTTP: whatever the catalog resolves must itself be gated by the real helper.
        try:
            resolved = command_catalog.by_name("/" + spelling.strip().lstrip("/"))
        except Exception as error:  # noqa: BLE001 - report input, not a crash
            pytest.fail(f"by_name raised {error!r} for {where}")
        if resolved is not None and resolved.tool:
            gate = serve._http_tool_refusal((resolved.tool,), "/x", USER_CTX)
            assert bool(gate) == (resolved.tool not in allow), where
    assert permitted_executed <= allow
    assert time.perf_counter() - started < 8, f"seed={FUZZ_SEED} too slow"
    assert all_handlers_recorded == []


# --- 1b. loop actions (serve.py:3100) ---------------------------------------

def test_loop_precheck_refuses_every_spelling_the_executor_would_fold_to_a_host_tool():
    """The pre-check lowercases/strips exactly as ``server._loop_dispatch`` does, so spelling cannot dodge it."""
    for spelling in ("FILE_WRITE", " file_write ", "File_Write", "RUN_CODE", "Code", "PROJECT", "\tscript_run\n",
                     "game_campaign", "AGENT", "Work", "assetgen"):
        refusal = serve._loop_global_operation_refusal(json.dumps([{"type": spelling}]), USER_CTX)
        assert refusal.startswith("refused /loop"), spelling
        refusal = serve._loop_global_operation_refusal(json.dumps([{"action": spelling}]), USER_CTX)
        assert refusal.startswith("refused /loop"), spelling


@pytest.mark.parametrize("action", ({}, {"type": ""}, {"type": None}, {"action": ""}), ids=repr)
def test_loop_precheck_treats_an_action_with_no_type_as_code_like_the_executor(action):
    """``_loop_dispatch`` runs a typeless action as ``code`` (run_code); the pre-check applies the same
    default, so a user is refused at the pre-check as well as at the primary ``loop`` gate."""
    defaulted = (action.get("type") or action.get("action") or "code").strip().lower()
    assert defaulted == "code"
    assert server._loop_action_tool(defaulted) == "run_code"
    refusal = serve._loop_global_operation_refusal(json.dumps([action]), USER_CTX)
    assert refusal.startswith("refused /loop")


def test_loop_tool_itself_is_refused_for_user_so_the_precheck_gap_is_not_reachable(monkeypatch, auto_mode,  # noqa: F811
                                                                                   all_handlers_recorded):
    for line in ("/loop actions_json=[{}]", '/loop actions_json=[{"type":"code","code":"1"}]'):
        result = serve._dispatch_catalogued_tool(line, serve.ConversationState(), context=USER_CTX)
        assert str(result).startswith("refused"), (line, result)
    assert all_handlers_recorded == []


# --- 2. role values ----------------------------------------------------------

@pytest.mark.parametrize("role", (
    "user", "User", "USER ", " user", "", "guest", "root", "superuser", "Developer", "DEVELOPER",
    "developer ", " developer", "Admin", "ADMIN", "admin ", "admin\x00", "developer\n", "admin\u200b",
    "\u0430dmin", "\uff41dmin", "developer\x00admin", 0, 1, b"admin", b"developer", ("admin",), frozenset({"admin"}),
    None, True,
), ids=repr)
def test_every_role_except_the_exact_strings_developer_and_admin_is_restricted(role):
    """decide_tool is an exact string-set membership test: no normalisation, so no case/whitespace/NUL trick
    reaches the unrestricted path (BY-DESIGN: pinned exactly; ``None`` here is a role VALUE, not 'unbound')."""
    decision = public_policy.decide_tool("file_write", role, frozenset({"status"}))
    assert isinstance(decision, public_policy.ToolRefused), role
    assert role_tool_policy.refusal_reason("file_write", role)
    assert role_tool_policy.untooled_refusal_reason("/prompts", role)


@pytest.mark.parametrize("role", ("developer", "admin"))
def test_only_the_two_exact_unrestricted_roles_pass(role):
    assert role_tool_policy.refusal_reason("file_write", role) == ""
    assert public_policy.UNRESTRICTED_ROLES == frozenset({"developer", "admin"})


@pytest.mark.parametrize("role", (["admin"], {"role": "admin"}), ids=repr)
def test_gap_unhashable_role_raises_typeerror_instead_of_a_refusal(role):
    """GAP: ``role in frozenset`` hashes the role. Unreachable today (``bind_caller_role`` only ever receives
    ``_restricted_tool_role``'s 'user'/None), and it fails closed by raising, never by allowing."""
    with pytest.raises(TypeError):
        public_policy.decide_tool("file_write", role, frozenset())


@pytest.mark.parametrize("role,expected_runs", (
    ("developer", True), ("admin", True), (None, True),  # None = unbound = no restriction (module docstring)
    ("user", False), ("User", False), ("USER ", False), ("Developer", False), ("developer ", False),
    ("admin\x00", False), ("", False), ("root", False), (5, False), (b"admin", False),
), ids=repr)
def test_agent_refusal_for_each_bound_role_value(role, expected_runs):
    token = role_tool_policy._CALLER_ROLE.set(role)
    try:
        refusal = role_tool_policy.agent_refusal("file_write")
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)
    assert (refusal == "") is expected_runs, (role, refusal)
    if not expected_runs:
        assert refusal.startswith(HOST_POLICY)


def test_gap_empty_string_role_is_restricted_but_none_is_not():
    """``''`` is falsy, ``None`` means 'unrestricted'. A buggy binder that passes a falsy role for a user would
    silently unrestrict only if it passes None; '' stays restricted. Pin the asymmetry."""
    token = role_tool_policy._CALLER_ROLE.set("")
    try:
        assert role_tool_policy.agent_refusal("file_write")
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)
    token = role_tool_policy._CALLER_ROLE.set(None)
    try:
        assert role_tool_policy.agent_refusal("file_write") == ""
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)


@pytest.mark.parametrize("tool", (None, "", 0, False, [], b"file_write"), ids=repr)
def test_non_string_or_empty_tool_names_are_refused_for_user(tool):
    assert role_tool_policy.refusal_reason(tool, "user")
    assert serve._role_tool_refusal_reason(tool, USER_CTX)


_ACCOUNT_ROLES = (
    "developer", "admin", "Developer", "Admin", "developer ", "admin\x00", "user", "", None, "root", 7,
    ["admin"], {"admin": 1}, b"admin",
)


@pytest.mark.parametrize("mode", ("account", "both", "either"))
@pytest.mark.parametrize("role", _ACCOUNT_ROLES, ids=repr)
def test_served_account_is_unrestricted_only_for_the_exact_developer_or_admin_role(mode, role):
    context = {"mode": mode, "authorized": True, "api_key": False, "account": {"username": "u", "role": role}}
    restricted = serve._restricted_tool_role(context)
    if role in ("developer", "admin") and isinstance(role, str):
        assert restricted is None
    else:
        assert restricted == "user", (mode, role)


@pytest.mark.parametrize("context,expected", (
    (None, None),  # trusted local surface (BY-DESIGN, docstring of _restricted_tool_role)
    ({"mode": "local-open", "authorized": False, "account": None}, None),  # BY-DESIGN local-open
    ({}, "user"),
    ({"mode": "account"}, "user"),
    ({"mode": "account", "authorized": False, "account": {"role": "admin"}}, "user"),
    ({"mode": "account", "authorized": True, "account": None}, "user"),
    ({"mode": "account", "authorized": True, "account": {}}, "user"),
    ({"mode": "account", "authorized": True, "account": {"role": "admin"}, "api_key": False}, None),
    ({"mode": "both", "authorized": True, "account": {"role": "user"}, "api_key": True}, "user"),
    ({"mode": "Local-Open", "authorized": True, "account": None}, "user"),  # mode is exact-match too
    ({"mode": "local-open ", "authorized": True, "account": None}, "user"),
    ({"mode": "api_key", "authorized": True, "api_key": True, "account": None}, None),  # owner key = developer
), ids=lambda v: repr(v)[:70])
def test_restricted_tool_role_for_malformed_contexts_fails_toward_user(context, expected):
    assert serve._restricted_tool_role(context) == expected


@pytest.mark.parametrize("account", ("admin", 7, ["admin"]), ids=repr)
def test_non_mapping_account_fails_toward_restricted_user(account):
    """A non-mapping ``account`` (AttributeError inside the authorization check) is restricted, never raised."""
    context = {"mode": "account", "authorized": True, "api_key": False, "account": account}
    assert serve._restricted_tool_role(context) == "user"


# --- 3. ContextVar propagation -----------------------------------------------

def _run_in_thread(fn):
    out = []
    thread = threading.Thread(target=lambda: out.append(fn()))
    thread.start()
    thread.join(10)
    return out[0]


def _probe():
    return role_tool_policy.agent_refusal("file_write")


INHERITS = bool(getattr(sys.flags, "thread_inherit_context", 0))


def test_role_is_seen_on_the_same_thread(user_caller):  # noqa: F811
    assert _probe().startswith(HOST_POLICY)


def test_gap_plain_thread_loses_the_role_and_runs_unrestricted(user_caller):  # noqa: F811
    """GAP: ``threading.Thread`` starts with an empty context (CPython <= 3.13), so ``agent_refusal`` sees None.
    Safe today only because every in-repo spawn site below is reached through a tool a user cannot call."""
    seen = _run_in_thread(_probe)
    assert (seen != "") is INHERITS, seen


def test_gap_executor_without_copy_context_loses_the_role(user_caller):  # noqa: F811
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_probe).result(10) == ""


def test_executor_with_copy_context_keeps_the_role(user_caller):  # noqa: F811
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(contextvars.copy_context().run, _probe).result(10).startswith(HOST_POLICY)


def test_asyncio_to_thread_keeps_the_role(user_caller):  # noqa: F811
    assert asyncio.run(asyncio.to_thread(_probe)).startswith(HOST_POLICY)


def test_gap_runtime_threads_helpers_do_not_carry_the_role(user_caller):  # noqa: F811
    """GAP: ``runtime_threads.Thread`` / ``ThreadPoolExecutor`` (what server.py's fan-out tools and
    interactive_lanes use) copy nothing. Only a tool outside the user allowlist reaches them (next test)."""
    assert (_run_in_thread(lambda: runtime_threads.Thread and _probe()) != "") is INHERITS
    pool = runtime_threads.ThreadPoolExecutor(max_workers=1)
    try:
        assert pool.submit(_probe).result(10) == ""
    finally:
        pool.shutdown()


def test_model_batching_pool_carries_the_role_to_every_worker(user_caller):  # noqa: F811
    seen = []

    class Provider:
        def generate(self, request, _context):
            seen.append(_probe())
            return ModelResponse(request.prompt, "test/model", "code")

    model_batching.generate_batch(
        Provider(), [ModelRequest("a", "code"), ModelRequest("b", "code"), ModelRequest("c", "code")],
        local_owner_context(correlation_id="role-batch"), max_workers=3,
    )
    assert len(seen) == 3 and all(s.startswith(HOST_POLICY) for s in seen), seen


def test_work_runner_thread_carries_the_role(user_caller):  # noqa: F811
    """work_runs.py:230 copies the context, so a narrated/HTTP work run keeps the caller's role."""
    from tests.test_http_work_narration import _Effects, _Store

    seen = []
    runner = WorkRunner(store=_Store(), effects=_Effects(), wait_seconds=5, thread_factory=threading.Thread)
    outcome = runner.run("owner", lambda: seen.append(_probe()), classify=lambda r: ("returned", "ok"))
    assert outcome.finished
    assert len(seen) == 1 and seen[0].startswith(HOST_POLICY), seen


_SPAWNING_TOOLS = (
    # server.py owned_runtime_pool sites (no copy_context): 7656, 7783, 7988, 8305, 15188, 24708's caller.
    "parallel_generate_run", "parallel_generate_run_languages", "campaign_generate_compile_execute_record",
    "campaign_repo_repair", "game_generation_campaign", "game_campaign",
    # agent loops that run on their own worker: interactive_lanes._pool, master/mission/offload/loop.
    "agent_lane", "master_orchestrate", "mission_start", "offload", "loop", "workflow_run", "workbench_agent",
    "parallel_run_code", "computer_task", "ensemble_codegen_build_loop",
)


@pytest.mark.parametrize("tool", _SPAWNING_TOOLS)
def test_every_tool_that_spawns_a_context_losing_worker_is_refused_for_user_on_both_paths(
    monkeypatch, auto_mode, all_handlers_recorded, tool,  # noqa: F811
):
    """Trace verdict: GAP, not BUG. The role is lost on these workers, but a user-role caller cannot start them:
    the tool is not on the allowlist, so the HTTP gate (:3167) and the agent gate both refuse before any spawn.
    If someone allowlists one of these tools this test fails and the ContextVar loss becomes a BUG."""
    assert tool not in role_tool_policy.user_tool_allowlist()
    assert role_tool_policy.refusal_reason(tool, "user")
    assert serve._http_tool_refusal((tool,), "/" + tool, USER_CTX).startswith("refused")
    token = role_tool_policy._CALLER_ROLE.set("user")
    try:
        with server._managed_agent_admission_scope():
            observation = server._agent_dispatch(tool, {})
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)
    assert str(observation).startswith(HOST_POLICY), observation
    assert all_handlers_recorded == []


def test_shipped_allowlist_contains_none_of_the_spawning_tools():
    assert not set(_SPAWNING_TOOLS) & role_tool_policy.user_tool_allowlist()


def test_request_binding_is_per_context_and_does_not_leak_between_requests(monkeypatch):
    """serve.py:4960 sets the var on the handler thread: a user request then a developer request on a reused
    keep-alive thread must end unrestricted, and the reverse restricted (each request rebinds)."""
    import types

    def request(ctx):
        monkeypatch.setattr(serve, "_auth_context", lambda *_a, **_k: ctx)
        serve.Handler._request_auth_context(types.SimpleNamespace(headers={}))
        return role_tool_policy._CALLER_ROLE.get()

    def sequence():
        return [request(USER_CTX), request(_context("developer")), request(USER_CTX), request(_context("local-open"))]

    assert contextvars.copy_context().run(sequence) == ["user", None, "user", None]
    assert role_tool_policy._CALLER_ROLE.get() is None  # nothing leaked into this test's own context


# --- 4. allowlist parsing ----------------------------------------------------

def _doc(*entries, key="tools", version=1):
    return {"schema_version": version, key: list(entries)}


def _e(name="status", why="read-only"):
    return {"name": name, "why": why}


@pytest.mark.parametrize("parser,key", (
    (public_policy.parse_allowlist, "tools"),
    (public_policy.parse_untooled_allowlist, "untooled_commands"),
))
@pytest.mark.parametrize("payload_factory", (
    lambda key: None, lambda key: [], lambda key: "tools", lambda key: 1, lambda key: b"{}", lambda key: {},
    lambda key: {"schema_version": 1}, lambda key: {"schema_version": 2, key: []},
    lambda key: {"schema_version": "1", key: []}, lambda key: {"schema_version": None, key: []},
    lambda key: {"schema_version": 1, key: None}, lambda key: {"schema_version": 1, key: {}},
    lambda key: {"schema_version": 1, key: "status"}, lambda key: {"schema_version": 1, key: ["status"]},
    lambda key: {"schema_version": 1, key: [None]}, lambda key: {"schema_version": 1, key: [[]]},
    lambda key: {"schema_version": 1, key: [{}]},
    lambda key: {"schema_version": 1, key: [{"name": "/x"}]},  # missing why
    lambda key: {"schema_version": 1, key: [{"why": "w"}]},  # missing name
    lambda key: {"schema_version": 1, key: [{"name": "/x", "why": ""}]},
    lambda key: {"schema_version": 1, key: [{"name": "/x", "why": "   \t\n"}]},
    lambda key: {"schema_version": 1, key: [{"name": "/x", "why": None}]},
    lambda key: {"schema_version": 1, key: [{"name": "/x", "why": ["because"]}]},
    lambda key: {"schema_version": 1, key: [{"name": ["/x"], "why": "w"}]},
    lambda key: {"schema_version": 1, key: [{"name": {"a": 1}, "why": "w"}]},
    lambda key: {"schema_version": 1, key: [{"name": "", "why": "w"}]},
    lambda key: {"schema_version": 1, key: [{"name": "   ", "why": "w"}]},
    lambda key: {"schema_version": 1, key: [{"name": 5, "why": "w"}]},
    lambda key: {"schema_version": 1, key: [{"name": b"/x", "why": "w"}]},
    lambda key: {"schema_version": 1, key: [{"name": "/x ", "why": "w"}]},
    lambda key: {"schema_version": 1, key: [{"name": " /x", "why": "w"}]},
    lambda key: {"schema_version": 1, key: [{"name": "/x", "why": "w"}, {"name": "/x", "why": "other"}]},
    lambda key: {"schema_version": 1, key: [{"name": "/x", "why": "w"}] * 3},
    lambda key: {"schema_version": 1, key: [{"name": "/x", "why": "w"}, "tail"]},
), ids=lambda f: "payload")
def test_malformed_documents_raise_only_valueerror(parser, key, payload_factory):
    with pytest.raises(ValueError):
        parser(payload_factory(key))


def test_nested_and_huge_payloads_raise_only_valueerror_in_bounded_time():
    deep = current = {}
    for _ in range(5000):
        nxt = {}
        current["x"] = nxt
        current = nxt
    started = time.perf_counter()
    for payload in (
        {"schema_version": 1, "tools": [{"name": deep, "why": "w"}]},
        {"schema_version": 1, "tools": [{"name": "a", "why": deep}]},
        {"schema_version": 1, "tools": [[[[[[]]]]]]},
        {"schema_version": 1, "tools": [{"name": "x" * 2_000_000, "why": ""}]},
    ):
        with pytest.raises(ValueError):
            public_policy.parse_allowlist(payload)
        with pytest.raises(ValueError):
            public_policy.parse_untooled_allowlist(payload)
    assert time.perf_counter() - started < 2


def test_huge_valid_allowlist_parses_linearly_and_is_exact():
    names = [f"tool_{i}" for i in range(50_000)]
    started = time.perf_counter()
    parsed = public_policy.parse_allowlist(_doc(*[_e(n) for n in names]))
    assert time.perf_counter() - started < 2
    assert parsed == frozenset(names) and len(parsed) == 50_000
    assert public_policy.decide_tool("tool_49999", "user", parsed) == public_policy.ToolAllowed()
    assert isinstance(public_policy.decide_tool("tool_50000", "user", parsed), public_policy.ToolRefused)


@pytest.mark.parametrize("entry", ("*", "file_*", "?", "**", ".*", "[a-z]*", "file_write|*"))
def test_wildcard_looking_entries_are_rejected_as_names(entry):
    """No wildcard is documented, so none may exist: a glob-looking name does not match the name pattern."""
    with pytest.raises(ValueError):
        public_policy.parse_allowlist(_doc(_e(entry)))


def test_upper_case_names_are_rejected_so_a_case_typo_cannot_silently_grant_nothing():
    """``Status`` is not a valid entry (no case folding exists, so it could never match a tool)."""
    with pytest.raises(ValueError):
        public_policy.parse_allowlist(_doc(_e("status"), _e("Status")))
    allowed = public_policy.parse_allowlist(_doc(_e("status")))
    assert isinstance(public_policy.decide_tool("STATUS", "user", allowed), public_policy.ToolRefused)


@pytest.mark.parametrize("name", (
    "\u200b", "status\u200b", "\ufeffstatus", "st\u0430tus", "\uff53tatus", "a\nb", "a\x00b",
    "Status", "file_*", "*", "1abc", "_abc", "a-b", "a b", "a.b", "", "a\n",
))
def test_tool_names_outside_the_strict_pattern_are_rejected(name):
    """Names must match ``[a-z][a-z0-9_]*``: invisible, homoglyph, upper-case and glob-looking entries raise."""
    with pytest.raises(ValueError):
        public_policy.parse_allowlist(_doc(_e(name)))


@pytest.mark.parametrize("version", (True, 1.0))
def test_schema_version_true_and_one_point_zero_are_rejected(version):
    """``True == 1`` and ``1.0 == 1``, but the version must be an int, so both raise (either parser)."""
    with pytest.raises(ValueError):
        public_policy.parse_allowlist(_doc(_e(), version=version))
    with pytest.raises(ValueError):
        public_policy.parse_untooled_allowlist(_doc(_e("/help"), key="untooled_commands", version=version))


@pytest.mark.parametrize("name", ("/Help", "/help ", " /help", "help", "", "/HELP", "\\help", "//\u0130"))
def test_untooled_names_must_be_exact_lowercase_slash_commands(name):
    with pytest.raises(ValueError):
        public_policy.parse_untooled_allowlist(_doc(_e(name), key="untooled_commands"))


@pytest.mark.parametrize("name", ("/", "/*", "/a b", "/help\u200b", "/a/b", "/help\n"))
def test_untooled_parser_rejects_degenerate_slash_names(name):
    """A slash command is ``/`` plus one or more of ``[a-z0-9_-]``; ``/``, ``/*`` and spaces raise."""
    with pytest.raises(ValueError):
        public_policy.parse_untooled_allowlist(_doc(_e(name), key="untooled_commands"))


def test_every_shipped_allowlist_file_parses_under_the_strict_rules():
    """A shipped entry the strict parsers reject would fail closed to an empty allowlist and lock users out."""
    payload = json.loads(role_tool_policy.ALLOWLIST_PATH.read_text(encoding="utf-8"))
    tools = public_policy.parse_allowlist(payload)
    commands = public_policy.parse_untooled_allowlist(payload)
    assert tools and commands
    role_tool_policy.user_tool_allowlist.cache_clear()
    role_tool_policy.user_untooled_commands.cache_clear()
    try:
        assert role_tool_policy.user_tool_allowlist() == tools
        assert role_tool_policy.user_untooled_commands() == commands
    finally:
        role_tool_policy.user_tool_allowlist.cache_clear()
        role_tool_policy.user_untooled_commands.cache_clear()


def test_each_parser_ignores_the_other_parsers_section():
    document = {"schema_version": 1, "tools": [_e("status")], "untooled_commands": [_e("/help")]}
    assert public_policy.parse_allowlist(document) == frozenset({"status"})
    assert public_policy.parse_untooled_allowlist(document) == frozenset({"/help"})
    assert not any(n.startswith("/") for n in public_policy.parse_allowlist(document))


def test_seeded_hostile_documents_obey_the_reference_oracle():
    rng = random.Random(FUZZ_SEED + 1)
    names = ["status", "Status", " status", "status ", "", "  ", "*", "file_*", "\u200b", "a\u0430", 5, None, [], {},
             "x" * 300, "/help", "/Help", "tool_help", b"b"]
    whys = ["ok", "", "   ", None, 3, [], "why\u200b", "\u200b", "x" * 5000]
    versions = [1, 1, 1, 2, "1", None, True, 0]

    def oracle(doc):
        if not isinstance(doc, dict) or type(doc.get("schema_version")) is not int or doc.get("schema_version") != 1 or not isinstance(doc.get("tools"), list):
            return None
        out = []
        for entry in doc["tools"]:
            if not isinstance(entry, dict):
                return None
            name, why = entry.get("name"), entry.get("why")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                return None
            if not isinstance(why, str) or not why.strip():
                return None
            out.append(name)
        return None if len(set(out)) != len(out) else frozenset(out)

    started = time.perf_counter()
    for iteration in range(600):
        entries = []
        for _ in range(rng.randint(0, 5)):
            entry = {"name": rng.choice(names), "why": rng.choice(whys)}
            if rng.random() < 0.1:
                del entry[rng.choice(("name", "why"))]
            entries.append(entry if rng.random() > 0.05 else rng.choice([None, "s", [], 1]))
        doc = {"schema_version": rng.choice(versions), "tools": entries}
        if rng.random() < 0.05:
            doc = rng.choice([[], None, "x", {"tools": entries}])
        where = f"seed={FUZZ_SEED + 1} iteration={iteration} doc={doc!r}"
        expected = oracle(doc)
        try:
            got = public_policy.parse_allowlist(doc)
        except ValueError:
            assert expected is None, where
        except Exception as error:  # noqa: BLE001
            pytest.fail(f"non-ValueError {error!r} for {where}")
        else:
            assert expected == got, where
            assert all(isinstance(n, str) and n == n.strip() and n for n in got), where
    assert time.perf_counter() - started < 5


# --- 5. cache staleness ------------------------------------------------------

@pytest.fixture
def fresh_allowlist_cache():
    role_tool_policy.user_tool_allowlist.cache_clear()
    role_tool_policy.user_untooled_commands.cache_clear()
    yield
    role_tool_policy.user_tool_allowlist.cache_clear()
    role_tool_policy.user_untooled_commands.cache_clear()


def _write_allowlist(path, *names):
    path.write_text(json.dumps({
        "schema_version": 1,
        "tools": [{"name": n, "why": "test"} for n in names],
        "untooled_commands": [],
    }), encoding="utf-8")


def test_gap_allowlist_is_cached_for_the_life_of_the_process(monkeypatch, tmp_path, fresh_allowlist_cache):
    """GAP/BY-DESIGN (pins current): the module docstring says 'loaded once'; no doc in docs/ or SECURITY.md
    mentions that editing the data file needs a restart. Revoking a tool from the file does NOT take effect
    until restart (or an explicit cache_clear), and a user keeps the revoked tool meanwhile."""
    assert "loaded once" in role_tool_policy.__doc__
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    _write_allowlist(first, "status", "tool_help")
    _write_allowlist(second, "status")
    monkeypatch.setattr(role_tool_policy, "ALLOWLIST_PATH", first)
    assert role_tool_policy.refusal_reason("tool_help", "user") == ""
    monkeypatch.setattr(role_tool_policy, "ALLOWLIST_PATH", second)  # file "edited": tool_help revoked
    assert role_tool_policy.refusal_reason("tool_help", "user") == "", "cache expected to be stale"
    role_tool_policy.user_tool_allowlist.cache_clear()
    assert role_tool_policy.refusal_reason("tool_help", "user")


def test_gap_a_transient_read_failure_is_cached_as_an_empty_allowlist_until_restart(
    monkeypatch, tmp_path, fresh_allowlist_cache,
):
    """GAP (pins current): the fail-closed empty result is cached too, so one unreadable moment at first use
    locks every user out (``status`` included) even after the file is fixed. Fails closed, never open."""
    path = tmp_path / "late.json"
    monkeypatch.setattr(role_tool_policy, "ALLOWLIST_PATH", path)
    assert role_tool_policy.user_tool_allowlist() == frozenset()
    _write_allowlist(path, "status")
    assert role_tool_policy.user_tool_allowlist() == frozenset()
    assert role_tool_policy.refusal_reason("status", "user")
    role_tool_policy.user_tool_allowlist.cache_clear()
    assert role_tool_policy.user_tool_allowlist() == frozenset({"status"})


def test_tool_and_untooled_caches_are_independent(monkeypatch, tmp_path, fresh_allowlist_cache):
    """GAP (pins current): two separate lru_caches. Clearing one leaves the other stale, so a half-reload can
    leave the tool list and the slash-command list from different file versions."""
    path = tmp_path / "x.json"
    path.write_text(json.dumps({
        "schema_version": 1, "tools": [_e("status")], "untooled_commands": [_e("/help")],
    }), encoding="utf-8")
    monkeypatch.setattr(role_tool_policy, "ALLOWLIST_PATH", path)
    assert role_tool_policy.user_tool_allowlist() == frozenset({"status"})
    assert role_tool_policy.user_untooled_commands() == frozenset({"/help"})
    path.write_text(json.dumps({
        "schema_version": 1, "tools": [_e("tool_help")], "untooled_commands": [_e("/trace")],
    }), encoding="utf-8")
    role_tool_policy.user_tool_allowlist.cache_clear()
    assert role_tool_policy.user_tool_allowlist() == frozenset({"tool_help"})
    assert role_tool_policy.user_untooled_commands() == frozenset({"/help"})


def test_shipped_allowlist_loads_and_matches_the_gate_exactly():
    allowed = role_tool_policy.user_tool_allowlist()
    assert allowed >= {"status", "tool_help"}
    for name in allowed:
        assert role_tool_policy.refusal_reason(name, "user") == ""
    for name in HOST_ACTING:
        assert role_tool_policy.refusal_reason(name, "user")

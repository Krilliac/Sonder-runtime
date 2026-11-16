"""Containment: a model or an untrusted HTTP caller never gains authority.

Boundary under test. A model (through ``server._agent_dispatch``, the one
terminal dispatcher every agent, workbench and autopilot tool call reaches) or
an untrusted HTTP caller on an authenticated deployment (through
``sonder_serve._handle_slash`` with a served auth context) must not be able to

* gain operator or developer authority (``elevate``, ``admin_*``, the
  ``permission_*`` controls, a developer token it read somewhere);
* approve its own calls (``permission_approve``, ``/approve``, a replayed call
  id or digest, an approval spent for a different call);
* turn on cloud egress (``cloud_opt_in`` writes ``SONDER_ALLOW_CLOUD``);
* reach the operator-only tools (``server._AGENT_SYSTEM_OPERATOR_TOOLS``,
  ``generic_agent_dispatch.DIRECT_ONLY_NAMES`` / ``ADMIN_NAMES``), whatever
  spelling it uses, and even with the unsafe lab active.

Threat-model sources read before classifying anything:
``docs/wiki/09-security-model.md`` (one-shot approvals, the env routes
``SONDER_ALLOW_PERMISSION_EDITS`` and ``SONDER_FILE_BYPASS``, "a model's
string token or approval is dropped from every agent proposal"),
``docs/security/REVIEW.md`` D2/D6 (the developer gate is inert on
``local-open`` by definition), the ``permission_modes`` module docstring
(``DURABLE_AUTHORITY_TOOLS``, unattended callers), the comment on
``server._AGENT_SYSTEM_OPERATOR_TOOLS`` ("an agent is intentionally never that
operator, even in unsafe lab mode") and ``cloud_opt_in``'s docstring
("enabling is explicit consent").

Each test id says what the attacker tries. Tests named ``test_gap_*`` pin a
current behaviour that no document forbids but that a model could use; tests
named ``test_by_design_*`` pin behaviour the documents accept. Neither kind is
a failure today; both name the behaviour so a change to it is a decision.

Seeded variation (``FUZZ_SEED``, printed in every failure message) is used
only where one shape has many spellings: operator tool names and the one
changed argument of an approved call.

No network, no model: every operator tool body is replaced by a recorder, both
as the ``server`` attribute the hand-written branches call and as the MCP
registry callable the generic fallback calls, so "refused" is checked as "the
body never ran", not only as the text of the reply. A positive control proves
those recorders and the generated arguments do reach the bodies once the
exclusions are removed.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import random
from pathlib import Path

import pytest

import admin_auth
import memory_store
import permission_modes as pm
import reloadable_mcp
import server
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.security import approval_ledger as ledger_module
from sonder_runtime.adapters.security import role_tool_policy, unsafe_lab
from sonder_runtime.bootstrap import generic_agent_dispatch as generic
from sonder_runtime.domain.agents import tool_naming
from sonder_runtime.domain.agents.tool_args import normalize_tool_args

pytestmark = pytest.mark.unit

SEED = int(os.environ.get("FUZZ_SEED", "20261116"))
SECRET = f"CONTAINMENT-SECRET-{SEED}"
# A fixed session HMAC key: the account tokens minted once per module must
# still authenticate after a test points the runtime home somewhere else.
AUTH_SECRET = f"containment-authority-test-hmac-key-{SEED}-0123456789abcdef"
CALL = {"path": "notes.txt", "content": "hello", "mode": "create"}

_REGISTRY = generic.registered_tools(server.mcp)
# Every name an agent must never run: the server's operator set, the generic
# fallback's admin and direct-only sets, and every registered ``admin_*`` tool
# (the fallback excludes that prefix wholesale).
OPERATOR_NAMES = tuple(sorted(
    set(server._AGENT_SYSTEM_OPERATOR_TOOLS)
    | generic.ADMIN_NAMES
    | generic.DIRECT_ONLY_NAMES
    | {name for name in _REGISTRY if name.startswith("admin_")}
))
_ROLES = ("user", "developer", "admin", "api-key", "local-open")
_HOMOGLYPHS = {
    "a": "\u0430", "e": "\u0435", "o": "\u043e", "p": "\u0440", "c": "\u0441",
    "x": "\u0445", "y": "\u0443", "i": "\u0456", "s": "\u0455",
}
_INVISIBLES = ("\u200d", "\u200b", "\u200c", "\ufeff", "\u2060", "\u00ad")
_PREFIXES = ("/", "//", "sonder.", "mcp__sonder__", "functions.", "sonder:", "tools/")


def _fail(message):
    return f"{message} [FUZZ_SEED={SEED}]"


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def accounts_db(tmp_path_factory):
    """A real account store: owner (admin), dev1 (developer), usr1 (user)."""
    path = tmp_path_factory.mktemp("authority-accounts") / "accounts.db"
    previous = os.environ.get("SONDER_AUTH_SECRET")
    os.environ["SONDER_AUTH_SECRET"] = AUTH_SECRET
    conn = memory_store.connect(str(path))
    try:
        admin_auth.register(conn, "owner", "password123")
        admin_auth.register(conn, "dev1", "password123")
        admin_auth.set_account(conn, "dev1", role="developer")
        admin_auth.register(conn, "usr1", "password123")
        tokens = {
            "admin": admin_auth.login(conn, "owner", "password123")[0],
            "developer": admin_auth.login(conn, "dev1", "password123")[0],
            "user": admin_auth.login(conn, "usr1", "password123")[0],
        }
    finally:
        conn.close()
        if previous is None:
            os.environ.pop("SONDER_AUTH_SECRET", None)
        else:
            os.environ["SONDER_AUTH_SECRET"] = previous
    return str(path), tokens


@pytest.fixture
def gate(tmp_path, monkeypatch, accounts_db):
    """A private ledger, mode state and account store; nothing ambient."""
    store = ledger_module.ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    saved, saved_loaded = dict(pm._STATE), pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.MANUAL, elevated=False, elevation_reason="")
    pm._LOADED = True
    pm.reset_unattended_for_tests()
    pm.forget_spent_approval()
    role_token = role_tool_policy._CALLER_ROLE.set(None)
    monkeypatch.setattr(server, "_DB_PATH", accounts_db[0])
    monkeypatch.setenv("SONDER_AUTH_SECRET", AUTH_SECRET)
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "0")
    for name in ("SONDER_ALLOW_PERMISSION_EDITS", "SONDER_FILE_BYPASS",
                 "SONDER_FILE_APPROVAL_CODE", "SONDER_AUTH_MODE", "SONDER_API_KEY",
                 "SONDER_REQUIRE_ACCOUNT", unsafe_lab.ACK_ENV):
        monkeypatch.delenv(name, raising=False)
    try:
        yield store
    finally:
        role_tool_policy._CALLER_ROLE.reset(role_token)
        pm.forget_spent_approval()
        pm.reset_unattended_for_tests()
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


def _set_mode(mode):
    with pm._LOCK:
        pm._STATE["mode"] = mode


def _allow_every_tool(monkeypatch):
    """The broadest written operator grant: an ``allow`` rule for every name."""
    monkeypatch.setattr(
        pm, "_rule_lookup", lambda tool: {"pattern": "*", "action": pm.ALLOW, "note": "test"},
    )


def _recorder(original, sink, entry, reply):
    """A stand-in body that records its call and keeps the real signature, so
    the generic fallback's strict ``inspect.signature(...).bind`` still sees
    exactly the parameters the real tool accepts."""
    def body(*args, **kwargs):
        sink.append(entry(kwargs))
        return reply
    if original is not None:
        body.__signature__ = inspect.signature(original)
    return body


@pytest.fixture
def tripwires(monkeypatch):
    """Replace every operator tool body with a recorder, on both dispatch routes."""
    reached = []
    for name in OPERATOR_NAMES + ("cloud_opt_in",):
        tool = _REGISTRY.get(name)
        original = tool.fn if tool is not None else getattr(server, name, None)
        body = _recorder(original, reached, lambda _kwargs, _n=name: _n, f"TRIPWIRE {name} RAN")
        monkeypatch.setattr(server, name, body, raising=False)
        if tool is not None:
            monkeypatch.setattr(tool, "fn", body)
    return reached


@pytest.fixture
def strongest(monkeypatch, gate):
    """Everything else that could let a call through is already open.

    ``auto`` and an allow rule for every tool: only the operator-tool boundary
    and exact name resolution are left to refuse. The unsafe lab is added per
    test where it matters; it is not "more open" for the generic fallback,
    which then refuses every registry-only tool as hosted-agent policy.
    """
    _set_mode(pm.AUTO)
    _allow_every_tool(monkeypatch)
    return gate


def _valid_args(name):
    """Schema-valid arguments for a registered tool, authority arguments left out."""
    tool = _REGISTRY[name]
    properties = tool.parameters.get("properties") or {}
    by_type = {"string": "x", "integer": 1, "number": 1.0, "boolean": False,
               "array": [], "object": {}}
    args = {}
    for key in tool.parameters.get("required") or ():
        if key in generic.AUTHORITY_ARGUMENTS:
            continue
        spec = properties.get(key, {})
        kind = spec.get("type")
        if kind is None:
            kind = next((item.get("type") for item in spec.get("anyOf", ())
                         if item.get("type") not in (None, "null")), "string")
        args[key] = by_type.get(kind, "x")
    return args


def _assert_nothing_gained(store, reached, label, *, cloud="0"):
    assert reached == [], _fail(f"{label}: an operator tool body ran: {reached!r}")
    assert pm.elevated() is False, _fail(f"{label}: elevation was turned on")
    assert store.approvals() == [], _fail(f"{label}: an approval was issued")
    assert os.environ.get("SONDER_ALLOW_CLOUD") == cloud, _fail(f"{label}: cloud consent changed")


def _activate_unsafe_lab(monkeypatch, tmp_path):
    """The real activation path (exact acknowledgement, loopback, unprivileged).

    The per-process audit-once set is swapped for a private one so this test
    neither depends on nor changes whether another test already audited.
    """
    monkeypatch.setenv(unsafe_lab.ACK_ENV, unsafe_lab.ACKNOWLEDGEMENT)
    monkeypatch.setenv(unsafe_lab.AUDIT_PATH_ENV, str(tmp_path / "unsafe-lab.jsonl"))
    monkeypatch.delenv("SONDER_HOST", raising=False)
    monkeypatch.setattr(unsafe_lab, "is_privileged", lambda: False)
    monkeypatch.setattr(unsafe_lab, "_audited_processes", set())
    assert unsafe_lab.active() is True
    assert (tmp_path / "unsafe-lab.jsonl").is_file(), "the activation was audited"


def _http_context(role):
    if role == "local-open":
        return {"mode": "local-open", "authorized": True, "api_key": False, "account": None}
    if role == "api-key":
        return {"mode": "api-key", "authorized": True, "api_key": True, "account": None}
    return {"mode": "account", "authorized": True, "api_key": False,
            "account": {"username": role, "role": role}}


# --- 1. the operator tools, under every spelling -----------------------------------


def _first_mapped(name, table):
    for index, char in enumerate(name):
        if char in table:
            return name[:index] + table[char] + name[index + 1:]
    return name + "\u0430"


def _fullwidth(name):
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in name)


SPELLINGS = {
    "exact": lambda n: n,
    "upper-case": str.upper,
    "title-case": lambda n: n[:1].upper() + n[1:],
    "trailing-space": lambda n: n + " ",
    "leading-tab-trailing-newline": lambda n: "\t" + n + "\n",
    "leading-slash": lambda n: "/" + n,
    "sonder-dot-prefix": lambda n: "sonder." + n,
    "mcp-sonder-prefix": lambda n: "mcp__sonder__" + n,
    "zero-width-joiner-inside": lambda n: n[:1] + "\u200d" + n[1:],
    "zero-width-space-trailing": lambda n: n + "\u200b",
    "byte-order-mark-leading": lambda n: "\ufeff" + n,
    "cyrillic-homoglyph": lambda n: _first_mapped(n, _HOMOGLYPHS),
    "fullwidth-letters": _fullwidth,
}


# The unsafe lab changes only which layer refuses (hosted-agent policy closes
# every registry-only tool), so it gets a representative subset of shapes; the
# seeded test below covers it with random spellings too.
_UNSAFE_SPELLINGS = ("exact", "upper-case", "trailing-space", "leading-slash",
                     "zero-width-joiner-inside", "cyrillic-homoglyph")
SPELLING_CASES = [
    pytest.param(spelling, config, id=f"{spelling}-{config}")
    for spelling in SPELLINGS
    for config in ("default-gate", "auto-allow-all", "auto-allow-all-unsafe-lab")
    if config != "auto-allow-all-unsafe-lab" or spelling in _UNSAFE_SPELLINGS
]


@pytest.mark.parametrize("spelling, config", SPELLING_CASES)
def test_agent_never_reaches_an_operator_tool_by_any_spelling(
    spelling, config, monkeypatch, gate, tripwires,
):
    """Canonicalization and the deny check agree: no spelling runs the body."""
    if config != "default-gate":
        _set_mode(pm.AUTO)
        _allow_every_tool(monkeypatch)
    if config.endswith("unsafe-lab"):
        monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    mode_before = pm.current_mode()
    for name in OPERATOR_NAMES:
        proposed = SPELLINGS[spelling](name)
        out = server._agent_dispatch(proposed, _valid_args(name))
        label = f"{config} {spelling} {proposed!r}"
        assert isinstance(out, str) and out.startswith("ERROR:"), _fail(f"{label} answered {out!r}")
        _assert_nothing_gained(gate, tripwires, label)
    assert pm.current_mode() == mode_before


@pytest.mark.parametrize("unsafe", [False, True], ids=["auto-allow-all", "auto-allow-all-unsafe-lab"])
def test_agent_operator_names_seeded_spellings(unsafe, strongest, tripwires, monkeypatch):
    """Many random spellings of every operator name; none may run a body."""
    if unsafe:
        monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    rng = random.Random(SEED)
    registered = set(_REGISTRY)
    for name in OPERATOR_NAMES:
        for _ in range(4):
            chars = list(name)
            for _ in range(rng.randint(1, 3)):
                index = rng.randrange(len(chars))
                kind = rng.randrange(4)
                if kind == 0:
                    chars[index] = chars[index].swapcase()
                elif kind == 1:
                    chars.insert(index, rng.choice(_INVISIBLES))
                elif kind == 2:
                    chars[index] = _HOMOGLYPHS.get(chars[index], chars[index])
                else:
                    chars[index] = _fullwidth(chars[index])
            proposed = (rng.choice(("",) + _PREFIXES) + "".join(chars)
                        + rng.choice(("", " ", "\t", "\u200b")))
            if proposed.strip() in registered and proposed.strip() not in OPERATOR_NAMES:
                continue  # a mutation that happens to name an ordinary tool
            out = server._agent_dispatch(proposed, _valid_args(name))
            label = f"seeded {proposed!r} from {name}"
            assert isinstance(out, str) and out.startswith("ERROR:"), _fail(f"{label} answered {out!r}")
            _assert_nothing_gained(strongest, tripwires, label, cloud="0")


def test_an_alias_to_an_operator_tool_is_refused_after_canonicalization(
    strongest, tripwires, monkeypatch,
):
    """A canonicalizer alias that resolves to an operator tool must hit the deny.

    No shipped alias targets an operator tool today; this adds one so the
    order (canonicalize, then deny) is what is tested, not the alias table.
    """
    assert not set(tool_naming.AGENT_TOOL_ALIASES.values()) & set(OPERATOR_NAMES)
    monkeypatch.setitem(tool_naming.AGENT_TOOL_ALIASES, "raise_privilege", "elevate")
    monkeypatch.setitem(tool_naming.AGENT_TOOL_ALIASES, "approve_it", "permission_approve")
    for alias in ("raise_privilege", " raise_privilege ", "approve_it"):
        out = server._agent_dispatch(alias, {"on": "on", "reason": "r"})
        assert "is a system operation and cannot be called by an agent" in out, _fail(repr(out))
    _assert_nothing_gained(strongest, tripwires, "alias")
    # The help/admission predicate canonicalizes the same way.
    assert server._agent_run_tool_refusal("raise_privilege") == "system operation"
    assert server._agent_run_tool_refusal("approve_it") == "system operation"


def test_no_registry_alias_exposes_an_operator_body_under_another_name():
    """The generic fallback resolves through the MCP registry, which may hold
    aliases. No registered name outside the exclusions may carry an operator
    tool's callable."""
    excluded = generic.excluded_names(server.mcp, server._AGENT_SYSTEM_OPERATOR_TOOLS)
    operator_bodies = {id(_REGISTRY[name].fn): name for name in OPERATOR_NAMES if name in _REGISTRY}
    assert len(operator_bodies) >= 30
    leaks = sorted(
        (name, operator_bodies[id(tool.fn)]) for name, tool in _REGISTRY.items()
        if id(tool.fn) in operator_bodies and name not in excluded
    )
    assert leaks == []


def test_positive_control_the_recorders_are_reachable_without_the_exclusions(
    strongest, tripwires, monkeypatch,
):
    """Mutation proof in-file: drop the two exclusion lists and the bodies run.

    Without this, the tests above could pass because the generated arguments
    never validate or because the recorders sit where nothing looks.
    """
    monkeypatch.setattr(server, "_AGENT_SYSTEM_OPERATOR_TOOLS", frozenset())
    monkeypatch.setattr(generic, "excluded_names", lambda *_a, **_k: frozenset())
    for name in OPERATOR_NAMES:
        server._agent_dispatch(name, _valid_args(name))
    # ``admin_set_account`` requires ``token``, which the dispatcher strips
    # from every proposal, so its body stays unreachable for a second reason.
    assert set(tripwires) == set(OPERATOR_NAMES) - {"admin_set_account"}


# --- 2. authority arguments smuggled through an agent call -------------------------


@pytest.fixture
def outside(tmp_path, monkeypatch):
    """A workspace, and a secret in a directory outside every configured root."""
    root = tmp_path / "ws"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text(SECRET, encoding="utf-8")
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(server.file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.platform import paths as runtime_paths

    previous = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    monkeypatch.setattr(server, "_APP_GRAPH", bootstrap_app.build_application())
    try:
        yield elsewhere
    finally:
        if previous is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous)


def _smuggle_rows():
    def rows(path, roots, dev):
        return {
            "extra-roots-top-level": {"path": path, "extra_roots": roots},
            "developer-token-top-level": {"path": path, "extra_roots": roots, "token": dev},
            "developer-token-as-approval": {"path": path, "extra_roots": roots, "approval": dev},
            "token-capitalised": {"path": path, "extra_roots": roots, "Token": dev},
            "approval-upper-case": {"path": path, "extra_roots": roots, "APPROVAL": dev},
            "extra-roots-upper-case": {"path": path, "EXTRA_ROOTS": roots, "token": dev},
            "nested-in-args": {"path": path, "args": {"token": dev, "extra_roots": roots}},
            "nested-in-arguments": {"path": path, "arguments": {"token": dev, "extra_roots": roots}},
            "token-as-list": {"path": path, "extra_roots": roots, "token": [dev]},
            "token-as-object": {"path": path, "extra_roots": roots, "token": {"token": dev}},
            "arguments-envelope-json-string": normalize_tool_args("file_read", {
                "arguments": json.dumps({"path": path, "extra_roots": roots, "token": dev}),
            })[0],
            "args-envelope-object": normalize_tool_args("file_read", {
                "args": {"path": path, "extra_roots": roots, "approval": dev},
            })[0],
        }
    return rows


SMUGGLE_IDS = list(_smuggle_rows()("p", "r", "t"))


@pytest.mark.parametrize("shape", SMUGGLE_IDS)
def test_smuggled_authority_arguments_are_never_honoured(
    shape, gate, outside, accounts_db,
):
    """The model knows a real developer token and a directory outside its roots."""
    dev = accounts_db[1]["developer"]
    secret = str(outside / "secret.txt")
    # Positive control: the same token handed over by a person does open it.
    assert SECRET in server.file_read(secret, extra_roots=str(outside), token=dev)
    args = _smuggle_rows()(secret, str(outside), dev)[shape]
    out = server._agent_dispatch("file_read", args)
    assert SECRET not in str(out), _fail(f"{shape} leaked the secret: {out!r}")
    assert str(out).startswith("ERROR:"), _fail(f"{shape}: {out!r}")


def test_whole_arguments_as_a_json_string_are_refused(gate, outside, accounts_db):
    dev = accounts_db[1]["developer"]
    raw = json.dumps({"path": str(outside / "secret.txt"), "token": dev,
                      "extra_roots": str(outside)})
    out = server._agent_dispatch("file_read", raw)
    assert out == "ERROR: tool args must be a JSON object"


@pytest.mark.parametrize("smuggled", [
    {"token": "dev"}, {"approval": "dev"}, {"extra_roots": "C:/"},
    {"Token": "dev"}, {"APPROVAL": "dev"}, {"Extra_Roots": "C:/"},
], ids=["token", "approval", "extra-roots", "Token", "APPROVAL", "Extra_Roots"])
def test_generic_fallback_never_hands_a_body_an_authority_argument(
    smuggled, strongest, monkeypatch, accounts_db,
):
    """Registry-backed tools have no hand-written branch to drop the knobs.

    A lower-case string ``token``/``approval`` is dropped by ``_agent_dispatch``
    before the fallback, so the body runs without it; ``extra_roots`` and every
    other spelling are refused (authority check, or the strict signature
    binding). Either way no body ever receives one.
    """
    received = []
    for name in ("vision_analyze", "fetch_artifact"):
        monkeypatch.setattr(_REGISTRY[name], "fn", _recorder(
            _REGISTRY[name].fn, received, lambda kwargs, _n=name: (_n, kwargs), "ran"))
    value = {key: (accounts_db[1]["developer"] if item == "dev" else item)
             for key, item in smuggled.items()}
    for name in ("vision_analyze", "fetch_artifact"):
        out = server._agent_dispatch(name, {**_valid_args(name), **value})
        if out != "ran":
            assert out.startswith("ERROR:"), _fail(f"{name} {smuggled!r} answered {out!r}")
    for name, kwargs in received:
        carried = {key for key in kwargs if key.casefold() in generic.AUTHORITY_ARGUMENTS}
        assert carried == set(), _fail(f"{name} received {carried!r}")
    if "extra_roots" in smuggled or set(smuggled) - {"token", "approval"}:
        assert received == [], _fail(f"{smuggled!r} reached a body")


# --- 3. self-approval ---------------------------------------------------------------


def _pending_call(store):
    refused = server._agent_dispatch("file_write", dict(CALL))
    assert "pending call" in refused
    pending = store.pending()
    assert len(pending) == 1
    return pending[0].call_id, pending[0].digest, refused


REPLAYS = {
    "agent-permission-approve-call-id": lambda cid, dg, dev: server._agent_dispatch(
        "permission_approve", {"call_id": cid}),
    "agent-permission-approve-full-digest": lambda cid, dg, dev: server._agent_dispatch(
        "permission_approve", {"call_id": dg}),
    "agent-permission-approve-with-developer-token": lambda cid, dg, dev: server._agent_dispatch(
        "permission_approve", {"call_id": cid, "token": dev}),
    "agent-permission-approve-tool-and-arguments": lambda cid, dg, dev: server._agent_dispatch(
        "permission_approve", {"tool": "file_write", "arguments_json": json.dumps(CALL)}),
    "agent-permission-rule-set-allow": lambda cid, dg, dev: server._agent_dispatch(
        "permission_rule_set", {"pattern": "file_write", "action": "allow"}),
    "agent-permission-mode-auto": lambda cid, dg, dev: server._agent_dispatch(
        "permission_mode", {"mode": "auto"}),
    "direct-call-id-as-token": lambda cid, dg, dev: server.permission_approve(call_id=cid, token=cid),
    "direct-digest-as-token": lambda cid, dg, dev: server.permission_approve(call_id=cid, token=dg),
    "direct-forged-console-label": lambda cid, dg, dev: server.permission_approve(
        call_id=cid, token="console operator"),
    "direct-forged-env-label": lambda cid, dg, dev: server.permission_approve(
        call_id=cid, token="env:SONDER_ALLOW_PERMISSION_EDITS"),
    "control-chain-approve": lambda cid, dg, dev: server.control_command(f"/approve {cid}"),
    "control-chain-approve-call": lambda cid, dg, dev: server.control_command(
        f"/approve call file_write {json.dumps(CALL)}"),
}


@pytest.mark.parametrize("replay", list(REPLAYS))
def test_a_model_cannot_approve_its_own_pending_call(replay, gate, accounts_db):
    """The refusal text hands the model its call id; replaying it grants nothing."""
    cid, digest, refusal = _pending_call(gate)
    assert cid in refusal, "the model really does see the call id"
    out = REPLAYS[replay](cid, digest, accounts_db[1]["developer"])
    assert str(out).startswith(("ERROR:", "refused")), _fail(f"{replay} answered {out!r}")
    assert gate.approvals() == []
    assert pm.current_mode() == pm.MANUAL
    assert server._agent_permission_gate_error("file_write", dict(CALL)).startswith("ERROR:")


@pytest.mark.parametrize("permission_edits_env", [False, True])
def test_mcp_and_http_callers_cannot_approve_a_replayed_call_id(
    permission_edits_env, gate, monkeypatch,
):
    """Unattended protocol callers: the durable-authority class binds first."""
    if permission_edits_env:
        monkeypatch.setenv("SONDER_ALLOW_PERMISSION_EDITS", "1")
    cid, _digest, _ = _pending_call(gate)
    with pytest.raises(reloadable_mcp.ToolError):
        reloadable_mcp._refuse_if_gated("permission_approve", {"call_id": cid})
    for role in _ROLES:
        for line in (f"/permission_approve call_id={cid}", f"/approve {cid}"):
            out = ts._handle_slash(line, context=_http_context(role))
            assert out is None or out.startswith("refused"), _fail(f"{role} {line}: {out!r}")
    assert gate.approvals() == []


def _one_changed(rng, key):
    value = CALL[key]
    kind = rng.randrange(5)
    if kind == 0:
        changed = value + rng.choice(("x", " ", "\n", "\u200b"))
    elif kind == 1:
        changed = value.upper() if value != value.upper() else value.lower()
    elif kind == 2:
        changed = value[:-1]
    elif kind == 3:
        changed = rng.choice(("overwrite", "append")) if key == "mode" else "./" + value
    else:
        return {**CALL, rng.choice(("Token", "APPROVAL", "extra_roots", "note")): "x"}
    return {**CALL, key: changed}


def test_an_approval_covers_exactly_one_call_once(gate, monkeypatch):
    """Changed arguments never spend it; the exact call spends it once; replay is refused."""
    ran = []
    monkeypatch.setattr(server, "file_write", lambda **kwargs: ran.append(kwargs) or "wrote")
    issued = gate.issue("file_write", pm.call_digest("file_write", CALL), approver="console operator")
    rng = random.Random(SEED)
    for index in range(24):
        changed = _one_changed(rng, sorted(CALL)[index % len(CALL)])
        # Reference oracle, independent of ``call_digest``: every mutation
        # changes a value or adds a key that is not a documented credential.
        assert changed != CALL, _fail("mutation produced the approved call")
        out = server._agent_dispatch("file_write", changed)
        assert out.startswith("ERROR:"), _fail(f"changed call {changed!r} ran: {out!r}")
        assert gate.get(issued.nonce).state() == "open", _fail(f"changed call {changed!r} spent it")
    assert ran == []
    assert server._agent_dispatch("file_write", dict(CALL)) == "wrote"
    assert len(ran) == 1 and gate.get(issued.nonce).state() == "spent"
    again = server._agent_dispatch("file_write", dict(CALL))
    assert again.startswith("ERROR:") and len(ran) == 1


def test_gap_slash_prefixed_agent_name_spends_an_approval_but_runs_nothing(gate, monkeypatch):
    """GAP (low): the gate and the dispatcher read ``/file_write`` differently.

    ``permission_modes.call_digest``/``_decide`` strip a leading ``/``, so the
    gate spends the approval issued for ``file_write``; ``_agent_dispatch``
    does not, so the call then resolves to no tool. Nothing runs and nothing is
    widened, but a model can burn an operator's one-shot approval. Pinned so a
    fix (strip ``/`` in ``_agent_dispatch`` or refuse it before the gate) is a
    visible change.
    """
    ran = []
    monkeypatch.setattr(server, "file_write", lambda **kwargs: ran.append(kwargs) or "wrote")
    issued = gate.issue("file_write", pm.call_digest("file_write", CALL), approver="console operator")
    out = server._agent_dispatch("/file_write", dict(CALL))
    assert out == "ERROR: unknown tool '/file_write'."
    assert ran == []
    assert gate.get(issued.nonce).state() == "spent"


# --- 4. env-only authority on an authenticated deployment ---------------------------


POSTURES = {"auth-mode-account": ("SONDER_AUTH_MODE", "account"),
            "api-key-set": ("SONDER_API_KEY", "k" * 32)}


@pytest.mark.parametrize("posture", list(POSTURES))
def test_permission_edits_env_reaches_no_untokened_caller_on_authenticated_deployment(
    posture, gate, monkeypatch,
):
    monkeypatch.setenv(*POSTURES[posture])
    monkeypatch.setenv("SONDER_ALLOW_PERMISSION_EDITS", "1")
    assert server._deployment_authenticates_callers()
    cid, _digest, _ = _pending_call(gate)
    assert server._agent_dispatch("permission_approve", {"call_id": cid}).startswith("ERROR:")
    with pytest.raises(reloadable_mcp.ToolError):
        reloadable_mcp._refuse_if_gated("permission_approve", {"call_id": cid})
    for role in ("user", "developer", "admin", "api-key"):
        out = ts._handle_slash(f"/permission_approve call_id={cid}", context=_http_context(role))
        assert out.startswith("refused"), _fail(f"{role}: {out!r}")
    assert gate.approvals() == []


@pytest.mark.parametrize("posture", list(POSTURES))
def test_by_design_permission_edits_env_grants_an_in_process_approval(posture, gate, monkeypatch):
    """BY-DESIGN: 09-security-model.md lists ``SONDER_ALLOW_PERMISSION_EDITS=1``
    as an approval authority. It is a launch-time operator setting (no tool
    writes it; see the env-writer scan below), and every surface gate refuses
    ``permission_approve`` unattended, so only an attended or internal caller
    can use it -- on any deployment posture."""
    monkeypatch.setenv(*POSTURES[posture])
    monkeypatch.setenv("SONDER_ALLOW_PERMISSION_EDITS", "1")
    cid, _digest, _ = _pending_call(gate)
    assert server.permission_approve(call_id=cid).startswith("approved file_write call")
    assert gate.approvals()[0].approver == "env:SONDER_ALLOW_PERMISSION_EDITS"


@pytest.mark.parametrize("posture", list(POSTURES))
def test_by_design_file_bypass_env_lifts_agent_containment_but_not_for_user_role(
    posture, gate, outside, monkeypatch,
):
    """BY-DESIGN: ``SONDER_FILE_BYPASS=1`` is the documented process-wide env
    bypass (``file_ops.policy_text``: "env bypass"; 09-security-model.md keeps
    credential stores denied even under it). It is set at launch by the
    operator and binds every caller, a developer-role agent included. A
    user-role HTTP caller's agent still meets the role allowlist first."""
    monkeypatch.setenv(*POSTURES[posture])
    monkeypatch.setenv("SONDER_FILE_BYPASS", "1")
    secret = str(outside / "secret.txt")
    assert SECRET in server._agent_dispatch("file_read", {"path": secret})
    token = role_tool_policy._CALLER_ROLE.set("user")
    try:
        out = server._agent_dispatch("file_read", {"path": secret})
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)
    assert SECRET not in out and "not available to this account role" in out
    reply = ts._handle_slash(f"/file_read path={secret}", context=_http_context("user"))
    assert SECRET not in str(reply) and str(reply).startswith("refused")


def test_no_server_code_writes_an_authority_env_variable():
    """Env-only authority is operator authority only while nothing a caller
    reaches can write it. ``server.py`` writes exactly two variables."""
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    written = set()
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else []
        for target in targets:
            if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "environ"):
                key = target.slice
                written.add(key.value if isinstance(key, ast.Constant) else ast.dump(key))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("putenv", "update", "setdefault")
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"):
            written.add(f"<{node.func.attr}>")
    assert written == {"SONDER_ALLOW_CLOUD", "SONDER_SELFMOD_ACTIVE"}


# --- 5. authenticates_callers() false while accounts exist --------------------------


def test_by_design_local_open_developer_gate_is_inert_even_with_accounts(
    gate, accounts_db, tripwires,
):
    """BY-DESIGN (REVIEW.md D2, ``admin_private_chain_of_thought`` docstring):
    on ``local-open`` the developer gate passes every caller, accounts or not,
    and a non-loopback bind is refused in that mode. The model still cannot
    reach ``elevate``: the agent and MCP gates refuse it before that gate."""
    assert not server._deployment_authenticates_callers()
    assert server._developer_gate("elevate", "", 0.0) is None
    assert server._agent_dispatch("elevate", {"on": "on", "reason": "r"}).startswith("ERROR: HOST POLICY")
    with pytest.raises(reloadable_mcp.ToolError):
        reloadable_mcp._refuse_if_gated("elevate", {"on": "on", "reason": "r"})
    _assert_nothing_gained(gate, tripwires, "local-open elevate")


@pytest.mark.parametrize("posture", list(POSTURES))
def test_authenticated_developer_gate_needs_a_developer_token(posture, gate, monkeypatch, accounts_db):
    monkeypatch.setenv(*POSTURES[posture])
    tokens = accounts_db[1]
    for supplied in ("", "garbage", tokens["user"], tokens["developer"] + "x"):
        assert server._developer_gate("elevate", supplied, 0.0), _fail(repr(supplied))
    assert server._developer_gate("elevate", tokens["developer"], 0.0) is None
    assert server._developer_gate("elevate", tokens["admin"], 0.0) is None
    assert server.elevate(on="on", reason="r", token="").startswith("refused")
    assert pm.elevated() is False


# --- 6. admin_auth.require with an unknown required role ----------------------------


@pytest.mark.parametrize("typo", ["admn", "Admin", "admin ", "developr", "root"])
def test_gap_require_with_an_unknown_role_is_satisfied_by_any_logged_in_account(typo):
    """GAP (latent fail-open): ``ranks.get(role, 0)`` ranks an unknown
    *required* role as ``user``, so a typo at a call site would let every
    logged-in account through. Every call site passes a known literal today
    (next test), so nothing is reachable now."""
    assert admin_auth.require({"username": "usr1", "role": "user"}, typo) == (True, "")
    assert admin_auth.require(None, typo) == (False, "login required")


def test_every_require_call_site_names_a_known_role_literal():
    known = {"user", "developer", "admin"}
    root = Path(server.__file__).resolve().parent
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(("tests/", "venv/", ".venv/", "build/")) or "/tests/" in relative:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "require(" not in text or ("admin_auth" not in text and "_admin_require" not in text):
            continue
        tree = ast.parse(text)
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                continue
            for node in ast.iter_child_nodes(scope) if isinstance(scope, ast.Module) else ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_require = (
                    isinstance(func, ast.Attribute) and func.attr == "require"
                    and isinstance(func.value, ast.Name) and func.value.id == "admin_auth"
                ) or (isinstance(func, ast.Name) and func.id == "_admin_require")
                if not is_require:
                    continue
                role = node.args[1] if len(node.args) > 1 else next(
                    (kw.value for kw in node.keywords if kw.arg == "role"), None)
                owner = getattr(scope, "name", "<module>")
                if owner == "_admin_require" and isinstance(role, ast.Name):
                    continue  # the forwarding helper itself; its callers are checked
                ok = role is None or (isinstance(role, ast.Constant) and role.value in known)
                found.append((relative, node.lineno, owner, ok))
    assert len(found) >= 10, found
    assert [item for item in found if not item[3]] == []


# --- 7. cloud_opt_in ----------------------------------------------------------------


@pytest.mark.parametrize("unsafe", [False, True], ids=["normal", "unsafe-lab"])
@pytest.mark.parametrize("mode", list(pm.MODES))
def test_agent_cannot_turn_on_cloud_egress_in_any_mode(mode, unsafe, gate, monkeypatch):
    """The agent gate is always unattended (``interactive=False``)."""
    if unsafe:
        monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    _set_mode(mode)
    for action in ("on", "enable", "1", "true", "yes", " ON ", "Enabled"):
        out = server._agent_dispatch("cloud_opt_in", {"action": action})
        assert out.startswith("ERROR: HOST POLICY"), _fail(f"{mode} {action!r}: {out!r}")
        assert os.environ.get("SONDER_ALLOW_CLOUD") == "0", _fail(f"{mode} {action!r} flipped cloud")
    assert "cloud_opt_in" not in server._agent_help_advertised_tools(
        server._agent_tool_help(cloud=True, unsafe=unsafe))


@pytest.mark.parametrize("mode", list(pm.MODES))
@pytest.mark.parametrize("role", _ROLES)
def test_http_caller_cannot_turn_on_cloud_egress(role, mode, gate):
    _set_mode(mode)
    for line in ("/cloud_opt_in on", "/cloud_opt_in action=on"):
        out = ts._handle_slash(line, context=_http_context(role))
        assert str(out).startswith("refused /cloud_opt_in"), _fail(f"{role} {mode}: {out!r}")
        assert os.environ.get("SONDER_ALLOW_CLOUD") == "0"


def test_user_role_agent_cannot_turn_on_cloud_even_under_an_allow_rule(gate, monkeypatch):
    _set_mode(pm.AUTO)
    _allow_every_tool(monkeypatch)
    token = role_tool_policy._CALLER_ROLE.set("user")
    try:
        out = server._agent_dispatch("cloud_opt_in", {"action": "on"})
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)
    assert "not available to this account role" in out
    assert os.environ.get("SONDER_ALLOW_CLOUD") == "0"


def test_gap_agent_cloud_opt_in_runs_under_an_explicit_allow_rule(gate, monkeypatch):
    """GAP: ``cloud_opt_in`` is not in ``_AGENT_SYSTEM_OPERATOR_TOOLS`` nor in
    the generic fallback's exclusions. Only the permission gate (graded
    ``dangerous``, refused unattended) stands in front of it, so an operator's
    broad ``allow`` rule -- written to stop prompts, not to consent to cloud --
    lets a model write ``SONDER_ALLOW_CLOUD=1`` for the whole process. Its
    docstring calls enabling "explicit consent" and the operator-tools comment
    says process-wide controls need a human operator, but no document names
    this tool as agent-refused. A one-shot approval of exactly this call is
    the documented route and is not what this pins."""
    _set_mode(pm.MANUAL)
    _allow_every_tool(monkeypatch)
    out = server._agent_dispatch("cloud_opt_in", {"action": "on"})
    assert out.startswith("hosted/cloud tiers: ENABLED")
    assert os.environ.get("SONDER_ALLOW_CLOUD") == "1"


def test_real_unsafe_lab_agent_cannot_opt_into_cloud_even_under_an_allow_rule(
    gate, monkeypatch, tmp_path,
):
    """With the real unsafe-lab activation the GAP above is closed, but by a
    side effect: unsafe mode applies hosted-agent policy to every registry-only
    tool (``_agent_run_tool_refusal``), and ``cloud_opt_in`` is registry-only.
    Were it ever flipped, ``unsafe_lab.active()`` would refuse every later
    dispatch, because the acknowledgement refuses cloud consent."""
    _activate_unsafe_lab(monkeypatch, tmp_path)
    _set_mode(pm.AUTO)
    _allow_every_tool(monkeypatch)
    out = server._agent_dispatch("cloud_opt_in", {"action": "on"})
    assert out.startswith("ERROR: HOST POLICY") and "hosted agent policy" in out, out
    assert os.environ.get("SONDER_ALLOW_CLOUD") == "0"
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    with pytest.raises(unsafe_lab.UnsafeLabError, match="cloud"):
        server._agent_dispatch("status", {})


# --- 8. the unsafe lab never makes an agent an operator -----------------------------


def test_unsafe_lab_never_makes_an_agent_an_operator(gate, monkeypatch, tmp_path, tripwires):
    """server.py: "An agent is intentionally never that operator, even in
    unsafe lab mode". Real activation, ``auto``, an allow rule for everything."""
    _activate_unsafe_lab(monkeypatch, tmp_path)
    assert server._file_bypass_allowed() is True, "the lab really is active"
    _set_mode(pm.AUTO)
    _allow_every_tool(monkeypatch)
    for name in OPERATOR_NAMES:
        out = server._agent_dispatch(name, _valid_args(name))
        assert out.startswith("ERROR:"), _fail(f"{name}: {out!r}")
    for name in server._AGENT_SYSTEM_OPERATOR_TOOLS:
        assert server._agent_run_tool_refusal(name, unsafe=True) == "system operation"
    advertised = set(server._agent_help_advertised_tools(server._agent_tool_help(unsafe=True)))
    assert advertised & set(OPERATOR_NAMES) == set()
    _assert_nothing_gained(gate, tripwires, "unsafe lab")
    assert pm.current_mode() == pm.AUTO

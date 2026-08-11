"""Grading of native slash commands (#47) and the /login escalation (#51).

The catalog used to decide what tool a native slash command fronts by string
identity between the slash name and a registered tool name::

    tool = next((n.lstrip("/") for n in group if n.lstrip("/") in tools_by_name), "")
    risk = _risk_for(tool, server) if tool else (hit.get("risk", "safe") if hit else "safe")

Every command whose branch calls a tool under a *different* name therefore
resolved to ``tool == ""`` and fell into the "fronts no tool, so it cannot
mutate on its own, so it is safe" default -- including ``/setaccount`` and
``/register``, whose tools are named explicitly in ``_DANGEROUS``.

``console_tools()`` already resolves each branch to the tools it really calls;
it is what the console permission gate reads. These tests pin the catalog to
that same derivation so the help surface and the gate cannot disagree, and so
a command added tomorrow is graded by what its branch does rather than by
whether its author happened to name it after its tool.
"""
from __future__ import annotations

import pytest

import command_catalog
import permission_modes


_ORDER = {
    "safe": 0,
    "ask": 1,
    "mutation": 2,
    "execution": 2,
    "dangerous": 3,
    permission_modes.UNCLASSIFIED: 3,
}


def _severity(risk: str) -> int:
    return _ORDER.get(risk, 3)


def _native_commands():
    return [c for c in command_catalog.catalog() if c.native]


def _branch_tools(command) -> set:
    mapped = command_catalog.console_tools()
    tools: set = set()
    for name in (command.name,) + tuple(command.aliases):
        tools |= set(mapped.get(name, ()))
    return tools


def _by_name(slash: str):
    hits = [c for c in command_catalog.catalog() if c.name == slash]
    assert hits, "%s is not in the catalog" % slash
    return hits[0]


@pytest.fixture
def fixture_account():
    """A local developer account in the hermetic test home, and its token.

    No network and no real credentials: ``conftest`` has already repointed
    ``SONDER_HOME``/``SONDER_DB`` at a temporary directory, so this registers
    into throwaway state. The token is produced by ``server.admin_login`` and
    parsed exactly the way ``sonder_repl`` parses it into ``CURRENT_TOKEN``.
    """
    import admin_auth
    import server

    conn = server._open_db()
    try:
        admin_auth.init(conn)
        try:
            admin_auth.register(conn, "gradingfixture", "gradingfixturepw1")
        except Exception:
            pass  # already present from an earlier test in the session
        admin_auth.set_account(conn, "gradingfixture", role="developer")
    finally:
        conn.close()

    out = server.admin_login("gradingfixture", "gradingfixturepw1")
    marker = "token: "
    assert marker in out and not out.startswith("ERROR:"), out
    return out.split(marker, 1)[1].strip().splitlines()[0]


# --- #51: /login elevates file-write privilege --------------------------------


def test_admin_login_grants_developer_file_authority(fixture_account):
    """The reproduction, as a test: the same write is refused, then allowed.

    This is the fact that makes ``admin_login`` an elevation primitive rather
    than an ordinary read: nothing about the *login call* mutates a guarded
    path, but every later file op handed the token it produces does.
    """
    import server

    token = fixture_account
    assert server._file_developer_allowed("") is False
    assert server._file_developer_allowed(token) is True


def test_login_is_not_graded_safe():
    """`/login` is the console's elevation gesture and must not read as safe."""
    assert _by_name("/login").risk != "safe"


def test_admin_login_is_graded_as_elevation():
    """`_DANGEROUS` already carries `elevate` for widening later authority.

    ``admin_login`` does exactly that on the console path: it is the only way
    ``CURRENT_TOKEN`` becomes non-empty, and that token is threaded into every
    guarded file op as ``developer_authorized=``.
    """
    assert permission_modes.risk_of("admin_login") == "dangerous"


# --- #47: the tool-less default, in both directions ---------------------------


@pytest.mark.parametrize("slash,tool", [
    ("/setaccount", "admin_set_account"),
    ("/register", "admin_register"),
])
def test_dangerous_tool_is_never_downgraded_by_its_slash_name(slash, tool):
    """A command fronting a `_DANGEROUS` tool is graded dangerous.

    These two are a different bug from the tool-less default: the danger is
    marked explicitly and a name-matching heuristic overrode it.
    """
    assert tool in command_catalog._DANGEROUS
    assert _by_name(slash).risk == "dangerous"


@pytest.mark.parametrize("slash,tool", [
    ("/setaccount", "admin_set_account"),
    ("/register", "admin_register"),
    ("/login", "admin_login"),
])
def test_slash_name_grades_the_same_as_the_tool_it_fronts(slash, tool):
    """`risk_of` must not answer differently for two names of one command."""
    assert permission_modes.risk_of(slash.lstrip("/")) == permission_modes.risk_of(tool)


def _disarmed(command) -> set:
    # getattr rather than a direct call so that at the parent commit -- where
    # the de-escalation does not exist yet -- these tests fail on the grade
    # they are actually asserting about, not on a missing attribute. A test
    # that RED-fails with AttributeError has not demonstrated the defect.
    resolve = getattr(command_catalog, "console_disarmed_tools", dict)
    mapped = resolve()
    out: set = set()
    for name in (command.name,) + tuple(command.aliases):
        out |= set(mapped.get(name, ()))
    return out


def test_no_native_command_reaching_a_dangerous_tool_is_graded_below_dangerous():
    """The security half of the property, over every native command.

    Deliberately written against `_DANGEROUS` -- the explicit, hand-maintained
    danger marking -- rather than against any derivation, so it cannot pass
    merely because the derivation and the grade share a bug. This is what has
    to hold for command 272: a command added tomorrow that calls a dangerous
    tool is graded dangerous the moment it is written, with nobody updating a
    table.
    """
    under = []
    for command in _native_commands():
        reaching = (_branch_tools(command) & set(command_catalog._DANGEROUS)) - _disarmed(command)
        if reaching and command.risk != "dangerous":
            under.append((command.name, command.risk, sorted(reaching)))
    assert not under, "commands reaching a dangerous tool but not graded dangerous: %r" % (under,)


def test_no_native_command_is_graded_below_the_tools_its_branch_calls():
    """The general property, in the catalog's own risk vocabulary.

    Compared against `_risk_for`, not `permission_modes.risk_of`, because the
    two speak different vocabularies: `risk_of` layers on a synthetic
    `execution` class for tools that start a host process, and the catalog has
    no such class to store. `test_execution_class_is_absent_from_the_catalog`
    below pins that gap rather than letting this assertion quietly absorb it.
    """
    import server

    under = []
    for command in _native_commands():
        tools = _branch_tools(command) - _disarmed(command)
        graded = [
            command_catalog._risk_for(t, server) for t in tools
            if t in {r.name for r in server.mcp._tool_manager.list_tools()}
        ]
        if not graded:
            continue
        worst = max(graded, key=_severity)
        if _severity(command.risk) < _severity(worst):
            under.append((command.name, command.risk, worst, sorted(tools)))
    assert not under, "under-graded native commands: %r" % (under,)


def test_execution_class_is_published_when_a_native_command_reaches_it():
    """Execution-backed native commands publish the gate's actual class.

    `/run`, `/runscript`, `/forge` and `/train` reach tools that
    `permission_modes.risk_of` grades `execution`; the catalog stores `ask` for
    all of them, because `_risk_for` has no `execution` branch. Measured before
    and after the #47 fix: unchanged in both. It does not reach the enforcement
    path -- the console gate grades the TOOLS, so it sees `execution` correctly,
    and `command_router._RISKY` contains neither class -- so it is a display and
    classification drift, not a hole. Recorded here so that if a future change
    makes the catalog able to express `execution`, this test fails and someone
    removes the exemption above rather than discovering it by accident.
    """
    import server

    execution_backed = {
        c.name for c in _native_commands()
        if any(permission_modes.risk_of(t) == "execution"
               for t in _branch_tools(c) - _disarmed(c))
    }
    assert execution_backed, "no command reaches an execution tool -- did the derivation break?"
    published = {c.name for c in command_catalog.catalog() if c.risk == "execution"}
    assert execution_backed <= published
    del server


def test_delete_is_not_graded_dangerous_when_the_branch_disarms_it():
    """The inverse error: `/delete` is graded dangerous but cannot delete.

    The console branch is ``server.file_delete(path=..., dry_run=True, ...)``
    with the literal hard-coded; there is no console spelling that reaches a
    real delete. Grading it dangerous is the same kind of wrong answer as
    grading ``/setaccount`` safe, and it is the reason "grade tool-less
    commands stricter" is not the fix.
    """
    assert _by_name("/delete").risk != "dangerous"


def test_de_escalation_is_scoped_to_the_disarmed_branch_only():
    """`/delete` is de-escalated; the tool it calls is NOT.

    The de-escalation has to be a property of one *call site* that passes a
    disarming literal, never of the tool. ``/file_delete`` -- the direct MCP
    spelling, which takes ``dry_run`` from the caller -- must stay dangerous,
    or the fix for the inverse error would have punched a hole straight
    through the real guard.
    """
    assert _by_name("/delete").risk != "dangerous"
    assert permission_modes.risk_of("file_delete") == "dangerous"
    assert _by_name("/file_delete").risk == "dangerous"

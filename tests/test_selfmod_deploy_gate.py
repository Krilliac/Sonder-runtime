"""``/selfmod deploy|rollback`` must not run with nobody present to approve it.

``/selfmod`` is graded ``dangerous`` (``command_registry``), which ``plan``
denies at every surface -- that half already holds. What did not hold is every
other mode. ``dangerous`` maps to ``ask`` under ``manual``, ``acceptEdits`` and
``auto``, and every surface that reaches ``_selfmod_command`` decides with
``interactive=False``, where ``ask`` degrades to ``allow``. So the gate was
consulted, correctly graded the command as the most dangerous class it has,
and then let it through anyway:

    deploy   manual  control_command    REACHED-TOOL
    deploy   auto    http _handle_slash REACHED-TOOL

That degrade is gone for every effect class -- with nobody to ask,
``permission_modes`` now refuses file changes, host programs and destructive
tools outright ("Unattended callers" there) -- and this file keeps the
guarantee that predates it and never leaned on the mode table:
``_selfmod_command`` refuses its two source-writing actions itself unless a
console operator approved or a written rule allows, so a later mode or rule
change cannot quietly reopen this. ``selfmod.deploy`` ``os.replace``s
Sonder's own source tree, including ``selfmod.py`` itself, so a wrong
``allow`` here is not recoverable by the mechanism that would normally
recover it. Every other ``dangerous`` tool can be undone by running Sonder;
this one can overwrite the Sonder that would do the undoing.

The way out is kept actionable, because a refusal nobody can act on trains
operators to route around the gate: a console operator who answers the prompt
still deploys, and an explicit ``allow`` rule still satisfies the ask
unattended. Only "nobody was asked and nobody said yes" is refused.

Nothing here runs the real deploy. ``selfmod.deploy``/``rollback`` are
replaced by probes that record having been reached and raise; reaching one is
the failure this file exists to detect.
"""
from __future__ import annotations

import pytest

import permission_modes as pm
import selfmod
import server
import sonder_runtime.interfaces.http.serve as ts

pytestmark = pytest.mark.unit


class _Reached(AssertionError):
    """Raised by the write path a refusal was supposed to prevent."""


@pytest.fixture(autouse=True)
def no_real_write_path(monkeypatch):
    """The self-modifying writes, replaced by probes.

    ``deploy`` and ``rollback`` are the two functions in this repository that
    rewrite the interpreter running the tests. They are stubbed for the whole
    module rather than per test, so a test added later cannot reach the real
    ones by forgetting to.
    """
    def _deploy(*_args, **_kwargs):
        raise _Reached("selfmod.deploy was reached with nobody asked")

    def _rollback(*_args, **_kwargs):
        raise _Reached("selfmod.rollback was reached with nobody asked")

    monkeypatch.setattr(selfmod, "deploy", _deploy)
    monkeypatch.setattr(selfmod, "rollback", _rollback)


@pytest.fixture(autouse=True)
def mode_sandbox(tmp_path, monkeypatch):
    """A tmp state file and a known starting mode for every test here."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    saved = dict(pm._STATE)
    saved_loaded = pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    pm._LOADED = True
    try:
        yield
    finally:
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


@pytest.fixture(autouse=True)
def no_rule(monkeypatch):
    """No per-tool rule unless a test asks for one, so these assertions do not
    depend on whose machine they run on."""
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)


def _rules(action):
    """A rule lookup that answers ``action`` for every tool."""
    return lambda tool: {"pattern": tool, "action": action, "note": "test"}


UNATTENDED_MODES = ("manual", "acceptEdits", "auto")


# --- the hole: ask degraded to allow at every unattended surface ----------


@pytest.mark.parametrize("mode", UNATTENDED_MODES)
@pytest.mark.parametrize("action", ("deploy", "rollback"))
def test_self_modification_is_refused_when_nobody_can_be_asked(mode, action):
    """The reproduction, as an assertion.

    ``control_command`` is reached with the user's raw prompt from
    ``answer_with_history`` -- the ordinary chat path and the one an MCP client
    reaches through the ``sonder`` tool -- so a chat line reading
    ``/selfmod deploy <id>`` rewrote Sonder's own source in three of the four
    modes.
    """
    pm.set_mode(mode)
    out = server.control_command("/selfmod %s RUN1" % action)
    assert out.startswith("refused /selfmod"), (
        "%s in mode %r was not refused; got %r" % (action, mode, out)
    )


@pytest.mark.parametrize("mode", UNATTENDED_MODES)
@pytest.mark.parametrize("action", ("deploy", "rollback"))
def test_self_modification_is_refused_through_the_apps_chain(mode, action):
    """The same hole at the surface the Flutter app talks to.

    ``sonder_serve._handle_slash`` forwards ``/selfmod`` to
    ``server.control_command``, so the app inherited the same degrade. An HTTP
    request is the clearest case of nobody being present to approve.
    """
    pm.set_mode(mode)
    out = ts._handle_slash("/selfmod %s RUN1" % action)
    assert out.startswith("refused /selfmod"), (
        "%s in mode %r was not refused at the app chain; got %r"
        % (action, mode, out)
    )


def test_the_refusal_says_what_would_make_it_run():
    """A refusal that names no way forward is one operators learn to bypass.

    Both routes out are named, because both are real: approving at the console,
    and writing an explicit rule.
    """
    pm.set_mode("manual")
    out = server.control_command("/selfmod deploy RUN1")
    assert "console" in out.lower(), out
    assert "rule" in out.lower() or "/permissions" in out, out


# --- what must keep working ----------------------------------------------


@pytest.mark.parametrize("mode", ("plan",) + UNATTENDED_MODES)
@pytest.mark.parametrize("form", ("/selfmod", "/selfmod status"))
def test_reading_selfmod_state_is_never_blocked_by_this(mode, form):
    """The refusal is scoped to the two write actions, not to ``/selfmod``.

    Letting the new refusal reach ``status`` would be the over-refusal this
    gate is explicitly built to avoid, so this pins the scope of *this* change.

    ``plan`` is included. The chain gate grades a named command by the
    strictest tool it can front, but ``command_catalog.narrow_branch_tools``
    first narrows the read forms the branch grammar recognises -- the bare
    form shows status -- to the read they are, so the mode that only reads
    reads this too.
    """
    pm.set_mode(mode)
    out = server.control_command(form)
    assert not out.startswith("refused"), out


@pytest.mark.parametrize("action", ("deploy", "rollback"))
def test_an_explicit_allow_rule_still_satisfies_the_ask(monkeypatch, action):
    """The unattended escape hatch an operator can actually write.

    An explicit ``allow`` rule resolves the mode's ``ask`` before the degrade
    is ever consulted, so an operator who wants unattended self-deployment can
    still have it -- deliberately, in writing, per tool.
    """
    pm.set_mode("manual")
    monkeypatch.setattr(pm, "_rule_lookup", _rules(pm.ALLOW))
    with pytest.raises(_Reached):
        server.control_command("/selfmod %s RUN1" % action)


@pytest.mark.parametrize("mode", UNATTENDED_MODES)
@pytest.mark.parametrize("action", ("deploy", "rollback"))
def test_the_console_operator_who_answered_the_prompt_still_deploys(mode, action):
    """The other half of the fix, and the half that makes it a gate not a wall.

    ``sonder_repl`` prompts at ``_named_command_gate`` and then forwards to
    ``control_command``, which re-decides. Without ``operator_approved`` that
    re-decision would refuse the person who had just approved -- turning
    ``/selfmod deploy`` off entirely rather than gating it. Reaching the probe
    is success here.

    This one cannot be RED at the parent commit: it exercises the keyword the
    fix introduces, so before the fix it fails on the signature rather than on
    behaviour. It is a guard, not a reproduction, and is labelled as such.
    """
    pm.set_mode(mode)
    with pytest.raises(_Reached):
        server.control_command(
            "/selfmod %s RUN1" % action, operator_approved=True,
        )


def test_a_piped_console_is_not_an_operator(monkeypatch):
    """What the console passes is "is somebody there", not "am I the console".

    ``sonder < script.txt`` reaches the same branch, but ``_gate_tools``
    degraded rather than prompted, so nobody approved anything. The repl reads
    ``_console_has_operator()`` for exactly this reason; a hardcoded ``True``
    there would have handed every piped script the approval a person never
    gave.
    """
    import sonder_runtime.interfaces.repl.repl as sonder_repl

    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    assert sonder_repl._console_has_operator() is False
    pm.set_mode("auto")
    out = server.control_command(
        "/selfmod deploy RUN1",
        operator_approved=sonder_repl._console_has_operator(),
    )
    assert out.startswith("refused /selfmod"), out


def test_the_console_never_hardcodes_the_operator_approval():
    """The repl's wiring, checked in the source, because behaviour cannot see it.

    Added after a mutation survived. Replacing
    ``operator_approved=_console_has_operator()`` with ``operator_approved=True``
    in ``sonder_repl`` left all 28 tests here green, because every one of them
    builds its own call to ``control_command`` and none reads what the repl
    actually passes. That is the "guard nobody is holding" shape: the refusal
    was proven, the thing that decides whether the refusal applies was not.

    A literal there would hand ``sonder < script.txt`` -- a console session with
    nobody at the keyboard -- the approval a person never gave, which is exactly
    the case ``_console_has_operator`` exists to distinguish. So the rule is
    that this argument is never a constant: it must be computed per call.
    """
    import ast
    import pathlib

    import sonder_runtime.interfaces.repl.repl as sonder_repl

    source = pathlib.Path(sonder_repl.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    seen = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "operator_approved":
                continue
            seen += 1
            assert not isinstance(keyword.value, ast.Constant), (
                "sonder_repl line %d passes operator_approved as the literal "
                "%r; it must be computed from _console_has_operator() so a "
                "piped session cannot inherit an approval nobody gave"
                % (node.lineno, getattr(keyword.value, "value", None))
            )
    assert seen, (
        "no call in sonder_repl passes operator_approved at all, so the "
        "console can no longer approve /selfmod deploy and this check is "
        "watching nothing"
    )


def test_control_command_is_not_reachable_as_a_tool():
    """``operator_approved`` is only safe because no model can pass it.

    The argument is a self-authorization if anything the model drives can set
    it, which is the trap ``_TRUSTED_REPOSITORY_APPROVAL`` and
    ``harness_tools.authorized_root_scope`` both exist to avoid. It is safe
    here for a checkable reason rather than an argued one: ``control_command``
    is not a registered tool and the catalog has never heard of it, so the
    catalogued fall-throughs cannot resolve it and neither can agent dispatch.
    """
    import command_catalog

    assert command_catalog.by_name("/control_command") is None
    assert command_catalog.parse_invocation(
        "/control_command prompt=/selfmod operator_approved=true") is None


@pytest.mark.parametrize("action", ("deploy", "rollback"))
def test_a_deny_rule_and_plan_still_refuse(monkeypatch, action):
    """Neither route out can be reached by a caller the operator denied."""
    pm.set_mode("auto")
    monkeypatch.setattr(pm, "_rule_lookup", _rules(pm.DENY))
    assert server.control_command(
        "/selfmod %s RUN1" % action).startswith("refused /selfmod")
    pm.set_mode("plan")
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    assert server.control_command(
        "/selfmod %s RUN1" % action).startswith("refused /selfmod")

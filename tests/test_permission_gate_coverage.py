"""The completeness floor: every dispatch branch must be *in the map*.

Three rounds of review each closed the doors that were named and left the doors
that were not, and the reason was always the same one layer down. The gate is
not the thing that fails. The gate is consulted, it receives ``()`` for a
branch the derivation could not resolve, and it answers "allowed" -- because an
empty tool set has nothing in it to refuse. Every previous fix put a gate in
front of another door while the door itself went on reporting that there was
nothing behind it.

So this file does not test a gate. It tests the *map*, and it tests it for
completeness rather than for correctness:

    enumerate every ``cmd ==`` / ``cmd in (...)`` branch across all three
    dispatch chains, subtract an explicit allow-list of branches that front no
    work the gate exists to protect, and require that everything left has a
    non-empty entry.

An entry may be wrong -- that is what the other files check. What it may not be
is *absent*, because absence is indistinguishable from "allowed" at the moment
of the decision and is invisible at every other moment.

The allow-list is the load-bearing part and is deliberately awkward: each entry
carries the reason it is there, `test_the_allow_list_has_no_dead_entries` stops
it collecting names that no longer exist, and adding to it is a visible edit in
a file called "coverage". A blanket "these are fine" set would reproduce the
defect one level up.
"""
from __future__ import annotations

import ast
import os

import pytest

import command_catalog
import permission_modes as pm
import server

pytestmark = pytest.mark.unit


# (source file, dispatch function, the map that has to cover it).
#
# Three chains, two maps: `console_tools()` covers the console's own chain and
# the `control_command` chain it forwards to, `http_slash_tools()` covers the
# app's. They are kept apart because the same slash name can mean different
# work on different surfaces -- see `http_slash_tools`' docstring.
_CHAINS = (
    ("server.py", "control_command", "console"),
    ("sonder_repl.py", "main", "console"),
    ("sonder_serve.py", "_handle_slash", "http"),
)


@pytest.fixture(scope="module", autouse=True)
def _guard_process_state():
    """Fail this module if it leaks ``permission_modes`` state into other suites.

    This file reads the map and asks ``decide()`` about a mode by argument; it
    has no session of its own to change. It got this fixture the hard way: a
    stray ``set_mode(PLAN)`` here left the whole process in ``plan`` and took
    twenty tests in other files down with it, none of which named the cause.
    """
    before_state = dict(pm._STATE)
    before_loaded = pm._LOADED
    before_rule_lookup = pm._rule_lookup
    yield
    assert dict(pm._STATE) == before_state, (
        "permission_modes._STATE leaked out of this module: %r -> %r"
        % (before_state, dict(pm._STATE))
    )
    assert pm._LOADED == before_loaded, "permission_modes._LOADED leaked"
    assert pm._rule_lookup is before_rule_lookup, "_rule_lookup monkeypatch leaked"


def _maps():
    return {
        "console": command_catalog.console_tools(),
        "http": command_catalog.http_slash_tools(),
    }


# Branches that front no work this gate exists to protect. Every entry is a
# claim about a branch body, checked by reading it, and the string is the
# claim -- not a label.
#
# The bar is "changes what this session shows or which model answers, and
# touches nothing outside the process". Anything that runs a program, writes a
# file, changes what a later call may do, or spends the machine is NOT display
# only, however harmless the default argument looks -- that is exactly how
# `/selfmod` came to be graded by its most harmless sibling.
_DISPLAY_ONLY_BRANCHES = {
    "/help": "renders command_catalog text",
    "/exit": "ends the session; empty branch body",
    "/quit": "alias of /exit",
    "/q": "alias of /exit",
    "/project": "sets the session's project scope; assignment only",
    "/trace": "session output toggle (apply_trace)",
    "/strict": "session output toggle (apply_strict)",
    "/model": "chooses which model answers; no tool call",
    "/persona": "chooses the persona; reads personas.names()",
    "/route": "reports which tier would answer (tier_router.route); routes nothing",
    "/new": "starts a new session id (memory_store.new_id)",
    "/sessions": "lists past sessions (memory_store.list_sessions)",
    "/resume": "looks a session up (memory_store.find_session)",
    "/facts": "prints stored facts",
    "/lessons": "prints stored lessons",
    "/location": "reads the location-consent env flag",
    "/report": "formats activity_tracker.latest(); no writer beneath it",
    "/endreport": "alias of /report",
}


def _branch_names_in(path, function):
    """Every slash name one dispatch chain compares ``cmd`` against.

    Deliberately re-derived here rather than imported from
    ``command_catalog``: this is the check on that module's derivation, so
    sharing its walk would let one bug hide the other. It uses only
    ``_branch_names``, which reads a single ``if`` test and cannot be the
    source of a missing branch.
    """
    with open(os.path.join(os.path.dirname(server.__file__), path),
              encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    scope = next(
        (node for node in ast.walk(tree)
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name == function),
        None,
    )
    assert scope is not None, "%s.%s no longer exists" % (path, function)
    names = []
    for node in ast.walk(scope):
        if isinstance(node, ast.If):
            names.extend(command_catalog._branch_names(node.test))
    return sorted(set(names))


def test_every_dispatch_branch_is_in_the_map_or_declared_display_only():
    """The floor. A branch the map cannot see is a branch the gate allows.

    This is the check none of the three fix rounds had, and each of those
    rounds was spent finding by hand what it finds by construction:
    `/runwindow` launching host code that `/run` is refused for, `/training`
    reaching the whole training CLI, `/emotion` writing over HTTP while the
    console refuses it.
    """
    maps = _maps()
    missing = []
    for path, function, which in _CHAINS:
        for name in _branch_names_in(path, function):
            if name in _DISPLAY_ONLY_BRANCHES:
                continue
            if not maps[which].get(name):
                missing.append("%s (%s, %s)" % (name, path, which))

    assert not missing, (
        "%d dispatch branch(es) resolve to no tool, so the gate is consulted, "
        "receives an empty set, and allows them:\n  %s"
        % (len(missing), "\n  ".join(sorted(missing)))
    )


def test_the_allow_list_has_no_dead_entries():
    """An allow-list that outlives its branches becomes a blanket exemption.

    Without this, deleting `/lessons` leaves its entry behind, and the next
    command to be named `/lessons` is exempt on arrival.
    """
    live = set()
    for path, function, _which in _CHAINS:
        live |= set(_branch_names_in(path, function))

    assert set(_DISPLAY_ONLY_BRANCHES) - live == set(), (
        "allow-list names that are no longer branches anywhere"
    )


def test_every_allow_list_entry_gives_a_reason():
    """The reason is the check. A bare set would be a place to hide things."""
    for name, reason in _DISPLAY_ONLY_BRANCHES.items():
        assert reason and len(reason) > 12, name


def test_a_name_defined_on_two_surfaces_is_mapped_on_both_or_neither():
    """Divergence is the same hole wearing the other surface's clothes.

    `/emotion` mapped to `tune_emotion_vectors` at the console -- which `plan`
    refuses -- and to nothing over HTTP, because the app's branch calls a
    wrapper (`server.emotion_command`) instead of the tools. Same command,
    same write, refused for the operator and allowed for the app.

    This does not demand the two entries be *equal*: `/todo` reaches
    `task_delete` at the console and does not over HTTP, and flattening that
    would gate each surface at the other's blast radius. It demands only that
    neither surface resolve a shared name to nothing.
    """
    maps = _maps()
    console_names = set(_branch_names_in("server.py", "control_command"))
    console_names |= set(_branch_names_in("sonder_repl.py", "main"))
    http_names = set(_branch_names_in("sonder_serve.py", "_handle_slash"))

    diverged = []
    for name in sorted(console_names & http_names):
        if name in _DISPLAY_ONLY_BRANCHES:
            continue
        if bool(maps["console"].get(name)) != bool(maps["http"].get(name)):
            diverged.append("%s: console=%r http=%r" % (
                name, maps["console"].get(name), maps["http"].get(name),
            ))

    assert not diverged, (
        "the same command resolves to work on one surface and to nothing on "
        "the other:\n  %s" % "\n  ".join(diverged)
    )


def test_the_floor_is_not_vacuous():
    """Guard against the walk finding nothing and the floor passing on air.

    A `_branch_names_in` that silently returned `[]` -- a renamed function, a
    changed comparison idiom -- would make every assertion above pass while
    checking nothing at all. This is the control.
    """
    counts = {
        "%s.%s" % (path, function): len(_branch_names_in(path, function))
        for path, function, _which in _CHAINS
    }
    for where, count in counts.items():
        assert count > 50, "%s yielded only %d branches" % (where, count)

    maps = _maps()
    assert len(maps["console"]) > 100
    assert len(maps["http"]) > 100


# --- the three graded branches this floor forced into the map -------------


def test_the_console_window_runners_are_graded_as_execution():
    """`/run` was refused under `plan` while `/runwindow` ran the same block.

    Both reach host code -- `/run` through the `run_code` tool, `/runwindow`
    through `code_runner.run_code_window` directly, which fronts no tool and
    so resolved to nothing. Launching a detached console is not less execution
    than running the block inline.
    """
    maps = _maps()
    for name in ("/runwindow", "/runnew", "/runconsole"):
        assert maps["console"].get(name), name
        assert pm.risk_of(maps["console"][name][0]) == "execution", name

    assert pm.decide(
        maps["console"]["/runwindow"][0],
        mode=pm.PLAN, rule_lookup=lambda _tool: None,
    ).action == pm.DENY


def test_the_training_command_is_graded_by_the_cli_it_reaches():
    """`/training` fronts no tool and reached the whole training CLI.

    `_training_command` passes its argument to `adaptive_training.command_text`,
    which runs the CLI's own `main()` -- `start`, `deploy`, `rollback`,
    `adopt-legacy`, `release-alias`. Deploying an adapter changes which weights
    every later call uses, which is the reasoning `_DANGEROUS` already applies
    to `runtime_policy_update`.
    """
    maps = _maps()
    for name in ("/training", "/weighttraining"):
        assert maps["console"].get(name), name
    assert pm.risk_of("training") == "dangerous"


def test_the_hardware_report_is_graded_safe_rather_than_exempted():
    """`/hardware` reaches the same CLI, but only ever with a literal argument.

    It is a read, so it is recorded in the map *as* a read instead of being
    put on the allow-list. A recorded `safe` verdict and an exemption behave
    identically today and differ entirely tomorrow: the verdict is visible to
    `/permissions`, can be overridden by a rule, and shows up in the map that
    this file checks.

    The claim that makes `safe` true -- that the argument is a constant and
    the branch therefore cannot reach `start`/`deploy` -- is asserted from the
    source, not assumed.
    """
    maps = _maps()
    assert maps["console"].get("/hardware") == ("hardware",)
    assert pm.risk_of("hardware") == "safe"

    with open(os.path.join(os.path.dirname(server.__file__), "server.py"),
              encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    scope = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)
                 and node.name == "control_command")
    branch = next(
        node for node in ast.walk(scope)
        if isinstance(node, ast.If) and "/hardware" in command_catalog._branch_names(node.test)
    )
    calls = [
        node for node in ast.walk(branch)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_training_command"
    ]
    assert calls, "/hardware no longer calls _training_command -- recheck the grade"
    for call in calls:
        assert all(isinstance(a, ast.Constant) for a in call.args), (
            "/hardware now passes a variable to the training CLI; it can reach "
            "deploy/rollback and is no longer safe"
        )

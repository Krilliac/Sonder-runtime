"""admin_private_chain_of_thought: refused by default, opt-in takes two acts.

The tool refused unconditionally for its whole life, and the refusal predates
``reasoning_show`` -- so it had come to refuse exactly what a sibling tool
already serves under a gate. Opting in unifies the two surfaces; it does not
create a new one. There is no hidden chain-of-thought store behind this tool,
and these tests exist partly to keep that true: opted in, it must serve the
same reasoning record ``reasoning_show`` serves, and nothing else.
"""
import pathlib
import re

import activity_tracker as at
import permission_rules
import server
import sonder_serve as ts
from sonder_runtime.domain.execution import policy

import pytest

# --- consent-gate documentation coverage ----------------------------------

_CONSENT_GATE_RE = re.compile(r"SONDER_ALLOW_[A-Z0-9_]+")

# Where this runtime enumerates its default-off consent gates.
_CONSENT_GATE_INVENTORIES = (
    "docs/wiki/09-security-model.md",
    "docs/wiki/03-configuration.md",
    "docs/security/REVIEW.md",
)

# (gate, inventory) pairs already undocumented at this branch's base commit
# 9f377f1. This branch does not fix them -- they are other people's gates and
# other people's docs. Registering them keeps the check's teeth for anything
# NEW while leaving the pre-existing debt visible instead of invisible, which
# is the whole difference between a silenced check and an honest one.
_UNDOCUMENTED_AT_BRANCH_POINT = frozenset(
    (gate, inventory)
    for gate in ("SONDER_ALLOW_CPU_OFFLOAD", "SONDER_ALLOW_PERMISSION_EDITS",
                 "SONDER_ALLOW_REGISTRATION")
    for inventory in _CONSENT_GATE_INVENTORIES
) | frozenset({
    # REVIEW.md's D2 entry describes this gate in prose and by its config key
    # `allow_remote` rather than by env-var name. Pre-existing; not mine.
    ("SONDER_ALLOW_REMOTE_OLLAMA", "docs/security/REVIEW.md"),
})


def _repo_root():
    return pathlib.Path(__file__).resolve().parent.parent


def _declared_consent_gates():
    """Every SONDER_ALLOW_* gate name the runtime source actually reads."""
    root = _repo_root()
    sources = list(root.glob("*.py")) + list((root / "sonder_runtime").rglob("*.py"))
    found = set()
    for path in sources:
        found.update(
            _CONSENT_GATE_RE.findall(path.read_text(encoding="utf-8", errors="replace"))
        )
    return found


def _undocumented_gates(gate_names, root=None):
    """{inventory: [gate, ...]} for gates absent from it, minus known debt.

    Pure and separately callable so the check can be shown to have teeth
    rather than merely claimed to.
    """
    root = root or _repo_root()
    missing = {}
    for rel in _CONSENT_GATE_INVENTORIES:
        text = (root / rel).read_text(encoding="utf-8")
        absent = sorted(
            gate for gate in gate_names
            if gate not in text and (gate, rel) not in _UNDOCUMENTED_AT_BRANCH_POINT
        )
        if absent:
            missing[rel] = absent
    return missing


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """Neutral deployment: no opt-in, no reasoning, isolated home, empty tracker."""
    monkeypatch.delenv("SONDER_ALLOW_PRIVATE_COT", raising=False)
    monkeypatch.delenv("SONDER_EXPOSE_REASONING", raising=False)
    monkeypatch.delenv("SONDER_AUTH_MODE", raising=False)
    monkeypatch.delenv("SONDER_API_KEY", raising=False)
    monkeypatch.delenv("SONDER_REQUIRE_ACCOUNT", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    at.reset_for_tests()
    yield
    at.reset_for_tests()


def _allow_rule():
    """The operator's half of the opt-in: an explicit allow rule for this tool."""
    permission_rules.add_rule(
        server.sonder_paths.default_home(),
        "admin_private_chain_of_thought",
        "allow",
        "operator opt-in",
    )


# --- the flag itself ------------------------------------------------------


def test_flag_is_off_by_default():
    assert server.private_cot_opt_in_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_flag_enables_on_truthy_value(monkeypatch, value):
    monkeypatch.setenv("SONDER_ALLOW_PRIVATE_COT", value)
    assert server.private_cot_opt_in_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_flag_stays_off_on_falsy_value(monkeypatch, value):
    monkeypatch.setenv("SONDER_ALLOW_PRIVATE_COT", value)
    assert server.private_cot_opt_in_enabled() is False


def test_flag_is_distinct_from_reasoning_exposure(monkeypatch):
    """The two switches decide different things and must not be one switch.

    SONDER_EXPOSE_REASONING decides whether Sonder asks the model to think at
    all; this flag decides whether the surface that has always refused may
    reveal it. Either alone must leave the other untouched.
    """
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "1")
    assert server.private_cot_opt_in_enabled() is False
    monkeypatch.delenv("SONDER_EXPOSE_REASONING")
    monkeypatch.setenv("SONDER_ALLOW_PRIVATE_COT", "1")
    assert server.reasoning_exposure_enabled() is False


# --- the refusal, byte for byte -------------------------------------------


REFUSAL = (
    "DENIED: hidden private chain-of-thought cannot be exposed. "
    "Use /trace, /debug, /agents, master_status, debug_inspect, "
    "tool call logs, prompts, retrieved lessons, and final rationale summaries "
    "instead.\n"
    "  Refused here, and refused by default everywhere. Opting in takes two\n"
    "  independent acts and either one alone leaves this refused: set\n"
    "  SONDER_ALLOW_PRIVATE_COT=1, and add an explicit permission rule\n"
    "  allowing admin_private_chain_of_thought (the built-in rule denies it).\n"
    "  Opted in it serves the same record reasoning_show serves -- the model's\n"
    "  own thinking channel for the current turn -- and no hidden state beyond\n"
    "  it, because there is none."
)


def test_refusal_is_pinned_when_nothing_is_opted_in():
    assert server.admin_private_chain_of_thought() == REFUSAL
    assert server.admin_private_chain_of_thought("some-token") == REFUSAL


def test_flag_alone_is_not_enough(monkeypatch):
    """The flag without an operator rule must still refuse."""
    monkeypatch.setenv("SONDER_ALLOW_PRIVATE_COT", "1")
    assert server.admin_private_chain_of_thought() == REFUSAL


def test_rule_alone_is_not_enough():
    """An operator rule without the flag must still refuse."""
    _allow_rule()
    assert server.admin_private_chain_of_thought() == REFUSAL


def test_built_in_rule_still_denies_this_tool():
    """The default deny rule is what makes the second act deliberate."""
    rule = policy.evaluate(policy.default_rules(), "admin_private_chain_of_thought")
    assert rule["action"] == "deny"


def test_refusal_is_not_an_error_signal():
    """check_error_signals.py greps for a leading ERROR:; this must not add one."""
    assert not server.admin_private_chain_of_thought().startswith("ERROR:")
    assert server.admin_private_chain_of_thought().startswith("DENIED:")


# --- opted in -------------------------------------------------------------


def _opt_in(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_PRIVATE_COT", "1")
    _allow_rule()


def test_opted_in_says_nothing_is_captured_when_reasoning_is_off(monkeypatch):
    """"Nothing captured" and "something withheld" are different claims."""
    _opt_in(monkeypatch)
    out = server.admin_private_chain_of_thought()
    assert not out.startswith("DENIED:")
    assert "SONDER_EXPOSE_REASONING" in out
    assert "nothing withheld" in out
    assert "nothing captured" in out


def test_opted_in_says_nothing_recorded_when_enabled_but_no_turn_ran(monkeypatch):
    _opt_in(monkeypatch)
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "1")
    out = server.admin_private_chain_of_thought()
    assert "nothing is recorded for this turn" in out
    assert "withheld" in out


def test_opted_in_shows_the_recorded_reasoning(monkeypatch):
    _opt_in(monkeypatch)
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "1")
    with at.response_span("t", "p"):
        at.record_reasoning("weighing two options", model="m")
        out = server.admin_private_chain_of_thought()
    assert "weighing two options" in out
    assert "model reasoning" in out
    assert "(m)" in out


def test_opted_in_serves_exactly_what_reasoning_show_serves(monkeypatch):
    """One data source, one rendering -- the two surfaces cannot drift apart."""
    _opt_in(monkeypatch)
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "1")
    with at.response_span("t", "p"):
        at.record_reasoning("shared deliberation", model="m")
        assert server.admin_private_chain_of_thought() == server.reasoning_show()


def test_opted_in_still_refuses_a_caller_without_a_developer_token(monkeypatch):
    """Opting the deployment in does not open the tool to every caller."""
    _opt_in(monkeypatch)
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "1")
    monkeypatch.setenv("SONDER_REQUIRE_ACCOUNT", "1")
    with at.response_span("t", "p"):
        at.record_reasoning("private deliberation", model="m")
        out = server.admin_private_chain_of_thought("")
    assert out.startswith("refused:")
    assert "private deliberation" not in out


def test_reasoning_show_never_falls_back_to_another_developers_record(monkeypatch):
    """The private latest-record cache must remain principal-scoped."""
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "1")
    monkeypatch.setenv("SONDER_REQUIRE_ACCOUNT", "1")
    accounts = {
        "alice-token": {"username": "alice", "role": "developer"},
        "bob-token": {"username": "bob", "role": "developer"},
    }
    monkeypatch.setattr(server, "_admin_account_from_token", accounts.get)

    with at.response_span(
        "alice-turn", "private", reasoning_owner=server.reasoning_owner_for_token("alice-token"),
    ):
        at.record_reasoning("ALICE_PRIVATE_REASONING", model="m")

    assert "ALICE_PRIVATE_REASONING" in server.reasoning_show("alice-token")
    bob = server.reasoning_show("bob-token")
    assert "ALICE_PRIVATE_REASONING" not in bob
    assert "nothing is recorded" in bob


def test_http_and_direct_reasoning_owner_keys_match_for_an_account(monkeypatch):
    """A developer can inspect their own HTTP turn, never another account's."""
    monkeypatch.setenv("SONDER_REQUIRE_ACCOUNT", "1")
    account = {"username": "alice", "role": "developer"}
    monkeypatch.setattr(server, "_admin_account_from_token", lambda token: account)

    assert ts._reasoning_request_owner({"account": account}) == (
        server.reasoning_owner_for_token("alice-token")
    )


# --- the surfaces that describe this behaviour ----------------------------


def test_no_surface_still_calls_the_refusal_unconditional():
    """A display asserting behaviour the code does not have is the real defect.

    Every string below described a refusal that could never be lifted. Now it
    can be, so each must name the condition instead.
    """
    import command_registry
    import sonder_repl

    surfaces = {
        "tool docstring": server.admin_private_chain_of_thought.__doc__,
        "reasoning_exposure_enabled docstring": (
            server.reasoning_exposure_enabled.__doc__
        ),
        "reasoning_show docstring": server.reasoning_show.__doc__,
        "reasoning_show off-message": server.reasoning_show(),
        "refusal text": server.admin_private_chain_of_thought(),
        "manifest blurb": server.tool_manifest(),
        "repl help": sonder_repl.HELP,
        "policy rule note": policy.evaluate(
            policy.default_rules(), "admin_private_chain_of_thought"
        )["note"],
        "command registry": str(command_registry.COMMANDS),
        # The note this change actually rewrote. Pinning the surfaces I left
        # alone while omitting one I edited is the wrong way round.
        "debug_inspect note": "\n".join(server._DEBUG_INSPECT_REASONING_NOTE),
    }
    for name, text in surfaces.items():
        assert "never exposed" not in (text or ""), name
        assert "stays refused" not in (text or ""), name
        assert "safely deny private chain-of-thought" not in (text or ""), name


def test_cot_is_developer_gated_on_the_http_surface():
    """It was left out of the HTTP gate because it could only ever refuse.

    /debug and /inspect are in DANGEROUS_HTTP_SLASH_COMMANDS; /cot was not,
    because it returned one fixed string to every caller. Now that an opted-in
    deployment can return a turn's reasoning it belongs with them. An omission
    justified by a refusal outlives the refusal, silently.
    """
    import sonder_serve as ts

    for name in ("/cot", "/chainofthought", "/thoughts"):
        assert ts._dangerous_http_slash(name), name


def test_every_consent_gate_in_the_source_is_documented():
    """Every SONDER_ALLOW_* gate the source reads must appear in all three
    inventories -- derived from the code, not from a name typed here.

    The first version of this test hardcoded one flag, so a *fourth* gate added
    with no documentation would have passed green. That repeats the very shape
    this change exists to remove: you cannot search for a name that does not
    exist yet, and you equally cannot assert on one. The gate list is now read
    out of the source, so a new SONDER_ALLOW_* gate fails here by default and
    its author has to either document it or add it to the debt register below
    on purpose.

    Honest limit: this catches the SONDER_ALLOW_* naming family. A consent gate
    named outside that convention -- SONDER_EXPOSE_REASONING, SONDER_WEB_TOOLS,
    SONDER_LOCATION_CONSENT -- is not derivable this way and still escapes.
    """
    gates = _declared_consent_gates()
    # The derivation must actually be finding things, or an empty set would
    # make every assertion below vacuously true.
    assert "SONDER_ALLOW_PRIVATE_COT" in gates
    assert len(gates) >= 5
    assert _undocumented_gates(gates) == {}


def test_the_inventory_check_has_teeth():
    """Prove it fails on an undocumented gate instead of quietly passing.

    The claim "a new gate now fails loudly" is worth exactly as much as the
    demonstration that it does. This is that demonstration.
    """
    bogus = "SONDER_ALLOW_NOT_A_REAL_GATE"
    missing = _undocumented_gates({bogus})
    assert set(missing) == set(_CONSENT_GATE_INVENTORIES)
    assert all(names == [bogus] for names in missing.values())


def test_the_documentation_debt_register_cannot_rot():
    """A silenced name that no longer exists silences nothing and hides that.

    Every entry must still name a gate the source really reads, so the register
    shrinks when the debt is paid instead of quietly outliving it.
    """
    gates = _declared_consent_gates()
    for gate, inventory in _UNDOCUMENTED_AT_BRANCH_POINT:
        assert gate in gates, gate
        assert inventory in _CONSENT_GATE_INVENTORIES, inventory


def test_no_surface_calls_permission_rule_set_the_only_way_in():
    """Act 2 is a state on disk, not one tool. Saying otherwise overstates it.

    `permissions.json` can be edited by hand -- this file's own `_allow_rule`
    helper goes through `permission_rules.add_rule`, not the MCP tool. The
    security property is that an *environment variable* cannot supply act 2,
    not that one developer-gated tool is the sole route. That distinction was
    already flagged as deferred in one docstring and I carried it into three
    operator-facing documents before catching it.
    """
    surfaces = {
        "_private_cot_rule_allows docstring": server._private_cot_rule_allows.__doc__,
        "tool docstring": server.admin_private_chain_of_thought.__doc__,
    }
    root = _repo_root()
    for rel in _CONSENT_GATE_INVENTORIES:
        surfaces[rel] = (root / rel).read_text(encoding="utf-8")
    for name, text in surfaces.items():
        if "permission_rule_set" in (text or ""):
            assert "by hand" in text, name


def test_review_does_not_claim_the_refusal_is_recorded_unconditionally():
    """activity_tracker.record_tool_result no-ops without a bound response span.

    Pre-existing and uniform across every tool -- but my sentence in REVIEW.md
    asserted the recording flatly, which is a new absolute that is not absolute.
    """
    text = (_repo_root() / "docs/security/REVIEW.md").read_text(encoding="utf-8")
    assert "response span" in text


def test_the_opt_in_refusal_is_recorded(monkeypatch):
    """A gate nobody can see being probed is a gate with no alarm on it.

    The refusal returned before any activity record, so an operator could not
    tell a deployment nobody had touched from one being probed repeatedly --
    on the one tool where that difference is most worth knowing.
    """
    seen = []
    monkeypatch.setattr(
        server,
        "_record_direct_tool",
        lambda name, args=None, ok=True, started=None, **kw: seen.append((name, ok)),
    )
    server.admin_private_chain_of_thought()
    # ok=False, not ok=True: a blocked attempt that logs as a success is
    # indistinguishable from a served one, which defeats recording it.
    assert seen == [("admin_private_chain_of_thought", False)]


def test_docstring_admits_the_token_gate_is_inert_on_local_open():
    """Naming three gates without saying one is off by default overstates them.

    On the default deployment _deployment_authenticates_callers() is False, so
    the developer-token gate passes for everybody. An operator counting gates
    from this docstring must not be given three when they have two.
    """
    doc = server.admin_private_chain_of_thought.__doc__ or ""
    assert "local-open" in doc


def test_opt_in_flag_is_named_on_every_surface_that_refuses():
    """An operator must be able to find the switch from the refusal itself."""
    import sonder_repl

    assert "SONDER_ALLOW_PRIVATE_COT" in server.admin_private_chain_of_thought()
    assert "SONDER_ALLOW_PRIVATE_COT" in (
        server.admin_private_chain_of_thought.__doc__ or ""
    )
    line = next(l for l in sonder_repl.HELP.splitlines() if l.strip().startswith("/cot"))
    assert "opt-in" in line.lower()

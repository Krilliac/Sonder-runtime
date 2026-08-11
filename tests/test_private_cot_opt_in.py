"""admin_private_chain_of_thought: refused by default, opt-in takes two acts.

The tool refused unconditionally for its whole life, and the refusal predates
``reasoning_show`` -- so it had come to refuse exactly what a sibling tool
already serves under a gate. Opting in unifies the two surfaces; it does not
create a new one. There is no hidden chain-of-thought store behind this tool,
and these tests exist partly to keep that true: opted in, it must serve the
same reasoning record ``reasoning_show`` serves, and nothing else.
"""
import pathlib

import activity_tracker as at
import permission_rules
import server
from sonder_runtime.domain.execution import policy

import pytest


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


def test_the_flag_is_listed_in_every_consent_gate_inventory():
    """A new consent gate must appear wherever consent gates are enumerated.

    Searching the docs for this feature's own name could only ever return zero
    -- nothing containing it existed yet, so the search was guaranteed to
    reassure. The searchable thing is the *category*: these three files are
    where this runtime lists its default-off consent gates, and a gate missing
    from them is a gate an operator auditing the runtime will never learn about.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    inventories = (
        "docs/wiki/09-security-model.md",
        "docs/wiki/03-configuration.md",
        "docs/security/REVIEW.md",
    )
    for rel in inventories:
        text = (root / rel).read_text(encoding="utf-8")
        assert "SONDER_ALLOW_PRIVATE_COT" in text, rel


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

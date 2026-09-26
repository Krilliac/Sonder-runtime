"""The principal-keyed build line in the model-context brief (F22)."""
from __future__ import annotations

import pytest

import sonder_runtime.platform.environment_probe as environment_probe
from sonder_runtime.bootstrap import build_tools
from sonder_runtime.bootstrap.build_tools import (
    build_brief_line,
    build_brief_principal,
    install_build_brief,
    uninstall_build_brief,
)
from tests.test_build_executor import fake_services

pytestmark = pytest.mark.unit


class SpyInventory:
    def __init__(self):
        self.calls = []

    def capability_summary(self, *, max_chars=480):
        self.calls.append(max_chars)
        return ("compilers: gcc 13, clang 18; build: cmake 3.28, ninja 1.11; " * 20)[:max_chars]

    def view(self, *args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("the brief must not discover or read")


@pytest.fixture
def installed(monkeypatch):
    services = fake_services()
    services.model.summaries = {
        "owner": "cmake/Ninja sparklite: 5 targets (game, core, crash_test, shadergen*, deploy!); "
                 "Debug; gcc 13; PCH core" + " x" * 200,
        "account:a": "cmake/Ninja secret-engine: 900 targets",
    }

    def fail_view(*args, **kwargs):
        raise AssertionError("the brief must never read a build tree")

    services.model.view = fail_view
    inventory = SpyInventory()
    previous = environment_probe._capability_summary_provider
    # A process-wide flag the HTTP host sets; each test starts as a local process.
    monkeypatch.setattr(build_tools, "_BRIEF_DECLARATION_REQUIRED", False)
    install_build_brief(services, inventory)
    yield services, inventory
    uninstall_build_brief()
    environment_probe.set_capability_summary_provider(previous)


def test_no_declared_principal_means_no_build_line(installed):
    services, _ = installed
    assert build_brief_line(services) == ""
    summary = environment_probe._capability_summary()
    assert "| build: " not in summary
    assert "sparklite" not in summary and "secret-engine" not in summary


def test_the_owner_sees_their_model_and_another_principal_never_does(installed):
    services, _ = installed
    with build_brief_principal("owner"):
        owner = environment_probe._capability_summary()
    with build_brief_principal("account:b"):
        other = environment_probe._capability_summary()
    with build_brief_principal("account:a"):
        account_a = environment_probe._capability_summary()
    assert "sparklite" in owner and "secret-engine" not in owner
    assert "sparklite" not in other and "secret-engine" not in other
    assert "secret-engine" in account_a and "sparklite" not in account_a
    assert ("account:b", "") in services.model.summary_calls


def test_the_brief_is_bounded_and_does_no_reads(installed):
    services, inventory = installed
    with build_brief_principal("owner", project_label="sparklite"):
        summary = environment_probe._capability_summary()
        brief = environment_probe.agent_brief()
    assert len(summary) <= 480
    assert "| build: " in summary and summary.startswith("compilers:")
    assert len(summary.split(" | build: ", 1)[1]) <= build_tools.BUILD_BRIEF_MAX_CHARS
    assert "capabilities: " in brief and len(brief.split("capabilities: ", 1)[1]) <= 480
    assert len(brief) <= 1100
    assert services.model.views == []  # zero reads at brief time
    assert ("owner", "sparklite") in services.model.summary_calls
    assert all(value <= 480 for value in inventory.calls)


def test_install_is_idempotent_and_survives_a_failing_summary(installed, monkeypatch):
    services, inventory = installed
    install_build_brief(services, inventory)

    def boom(*args, **kwargs):
        raise RuntimeError("cache broken")

    monkeypatch.setattr(services.model, "cached_summary", boom)
    with build_brief_principal("owner"):
        summary = environment_probe._capability_summary()
    assert summary.startswith("compilers:") and "| build: " not in summary


def _agent_turn_system(monkeypatch, *, project="", cloud=False):
    """Run one ``server._agent_turn`` over a scripted model; return its system text."""
    import types

    import server

    seen = {}
    monkeypatch.setattr(server, "_application", lambda: types.SimpleNamespace())
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_serve_target",
                        lambda *args, **kwargs: ("fixture", cloud, False, "code"))
    monkeypatch.setattr(server, "_build_system",
                        lambda text, *args, **kwargs: seen.setdefault("system", text))
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(server.unsafe_lab, "active", lambda: False)
    monkeypatch.setenv("SONDER_SPECULATION", "0")

    def make_generate(model, system, *args, **kwargs):
        seen.setdefault("system", system)
        return lambda prompt, history=None: '{"final":"done"}'

    monkeypatch.setattr(server, "_make_generate", make_generate)
    assert server._agent_turn("inspect", max_steps=2, read_only=True, project=project) == "done"
    return seen["system"]


def test_a_local_agent_turn_declares_the_owner_and_shows_the_build_line(installed, monkeypatch,
                                                                         tmp_path):
    services, _ = installed
    system = _agent_turn_system(monkeypatch)
    capabilities = system.split("capabilities: ", 1)[1]
    assert "| build: cmake/Ninja sparklite" in capabilities
    assert len(capabilities.split(" | build: ", 1)[1].split("\n", 1)[0]) \
        <= build_tools.BUILD_BRIEF_MAX_CHARS
    assert "secret-engine" not in system
    assert ("owner", "") in services.model.summary_calls
    assert services.model.views == []

    # A turn scoped to a project asks for that project's model by its label.
    project = tmp_path / "sparklite"
    project.mkdir()
    _agent_turn_system(monkeypatch, project=str(project))
    assert ("owner", "sparklite") in services.model.summary_calls
    # The declaration ends with the turn: the next reader sees no principal.
    assert build_brief_line(services) == ""


def test_the_build_line_is_the_owners_only_and_never_reaches_a_hosted_agent(installed,
                                                                           monkeypatch):
    services, _ = installed
    services.model.summaries = {"account:a": "cmake/Ninja secret-engine: 900 targets"}
    system = _agent_turn_system(monkeypatch)
    assert "| build: " not in system and "secret-engine" not in system

    services.model.summaries = {"owner": "cmake/Ninja sparklite: 5 targets"}
    calls = len(services.model.summary_calls)
    hosted = _agent_turn_system(monkeypatch, cloud=True)
    assert "sparklite" not in hosted and "capabilities: " not in hosted
    assert len(services.model.summary_calls) == calls


def _in_fresh_context(function, *args, **kwargs):
    """Run in a new context, as a fresh request-handler or worker thread does."""
    import contextvars

    return contextvars.Context().run(function, *args, **kwargs)


def test_a_served_account_turn_shows_its_own_build_line_never_the_owners(installed,
                                                                         monkeypatch):
    """A developer account's natural work reaches ``_agent_turn`` on the HTTP
    handler (or a work thread that copied its context): the request declares
    the account's own principal, so the owner's project model never appears."""
    import hashlib

    from sonder_runtime.interfaces.http import serve

    services, _ = installed
    principal = "account:" + hashlib.sha256(b"alice").hexdigest()
    services.model.summaries = {"owner": "cmake/Ninja sparklite: 5 targets",
                                principal: "cmake/Make alice-tool: 2 targets"}

    def served_turn(context):
        serve._bind_request_build_principal(context)
        return _agent_turn_system(monkeypatch)

    account = {"authorized": True, "account": {"username": "alice", "role": "developer"}}
    system = _in_fresh_context(served_turn, account)
    assert "alice-tool" in system and "sparklite" not in system
    assert (principal, "") in services.model.summary_calls
    assert ("owner", "") not in services.model.summary_calls

    # The owner (API key or local-open) still sees the owner's line.
    owner = _in_fresh_context(served_turn, {"authorized": True, "account": None})
    assert "sparklite" in owner and "alice-tool" not in owner

    # An unauthorized or malformed request declares nobody; in the HTTP host
    # nobody means no build line at all.
    monkeypatch.setattr(build_tools, "_BRIEF_DECLARATION_REQUIRED", True)
    for context in ({"authorized": False, "account": None},
                    {"authorized": True, "account": {"username": ""}}):
        refused = _in_fresh_context(served_turn, context)
        assert "| build: " not in refused


def test_the_http_host_shows_no_build_line_for_an_undeclared_turn(installed, monkeypatch):
    """A fleet worker or autopilot run in the HTTP host inherited no request
    declaration: it cannot say whose turn it runs, so it gets no build line.
    The same undeclared turn in a local process (REPL, stdio MCP) is the owner's."""
    services, _ = installed
    services.model.summaries = {"owner": "cmake/Ninja sparklite: 5 targets"}
    local = _in_fresh_context(_agent_turn_system, monkeypatch)
    assert "sparklite" in local
    monkeypatch.setattr(build_tools, "_BRIEF_DECLARATION_REQUIRED", True)
    calls = len(services.model.summary_calls)
    hosted = _in_fresh_context(_agent_turn_system, monkeypatch)
    assert "| build: " not in hosted and "sparklite" not in hosted
    assert len(services.model.summary_calls) == calls


def test_the_http_host_requires_a_declared_principal(monkeypatch):
    from sonder_runtime.interfaces.http import serve

    monkeypatch.setattr(build_tools, "_BRIEF_DECLARATION_REQUIRED", False)

    class Stop(Exception):
        pass

    def configure(config):
        raise Stop()

    monkeypatch.setattr(serve, "configure_typed_config", configure)
    with pytest.raises(Stop):
        serve.main(config=object(), _close_default_resources=False)
    assert build_tools._BRIEF_DECLARATION_REQUIRED is True

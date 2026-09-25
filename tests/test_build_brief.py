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

"""agent_brief() capability suffix installed from the host tool inventory."""
import pytest

from sonder_runtime.application.host_tools.service import HostToolInventoryService
from sonder_runtime.bootstrap import host_tools as bootstrap
from sonder_runtime.domain.host_tools.model import (
    DiscoverySource,
    ToolCategory,
    ToolRecord,
    VersionStatus,
    build_snapshot,
)
from sonder_runtime.platform import environment_probe


@pytest.fixture(autouse=True)
def _clean_provider():
    bootstrap.uninstall_agent_brief_summary()
    yield
    bootstrap.uninstall_agent_brief_summary()


class Store:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def load(self):
        return self.snapshot

    def save(self, snapshot):
        self.snapshot = snapshot


class NoDiscovery:
    def discover(self, *, previous, full):
        raise AssertionError("the brief must never trigger discovery")


def _service(count=3):
    records = [
        ToolRecord(name=f"tool{i}", category=ToolCategory.COMPILER, path=f"/usr/bin/tool{i}",
                   source=DiscoverySource.PATH, on_path=True, version="1.2.3",
                   version_status=VersionStatus.OK, identity="1:1")
        for i in range(count)
    ]
    snapshot = build_snapshot(os="Linux", os_release="x", machine="x", created_at=1.0,
                              duration_ms=1, tools=records)
    return HostToolInventoryService(NoDiscovery(), Store(snapshot), clock=lambda: 10.0 ** 9,
                                    redact_path=lambda p: p, executable_guard=lambda p: True)


def test_without_provider_the_brief_is_the_base_brief():
    base = environment_probe.agent_brief()
    assert " | capabilities: " not in base
    assert environment_probe.agent_brief() == base


def test_installed_summary_is_appended_single_line_and_bounded():
    base = environment_probe.agent_brief()
    bootstrap.install_agent_brief_summary(_service(300))
    brief = environment_probe.agent_brief()
    assert brief.startswith(base + " | capabilities: compilers: tool0 1.2")
    suffix = brief[len(base + " | capabilities: "):]
    assert "\n" not in brief and len(suffix) <= 480
    bootstrap.install_agent_brief_summary(_service(300))  # idempotent
    assert environment_probe.agent_brief() == brief


def test_provider_output_is_flattened_and_truncated():
    base = environment_probe.agent_brief()
    environment_probe.set_capability_summary_provider(lambda: "a\nb\r\n" + "x" * 1000)
    brief = environment_probe.agent_brief()
    assert "\n" not in brief and "\r" not in brief
    assert len(brief) - len(base + " | capabilities: ") == 480


def test_raising_or_empty_provider_adds_nothing():
    base = environment_probe.agent_brief()

    def boom():
        raise RuntimeError("no")

    environment_probe.set_capability_summary_provider(boom)
    assert environment_probe.agent_brief() == base
    environment_probe.set_capability_summary_provider(lambda: "")
    assert environment_probe.agent_brief() == base
    environment_probe.set_capability_summary_provider(lambda: 42)
    assert environment_probe.agent_brief() == base
    with pytest.raises(TypeError):
        environment_probe.set_capability_summary_provider("not callable")


def test_uninstall_restores_the_base_brief():
    base = environment_probe.agent_brief()
    bootstrap.install_agent_brief_summary(_service())
    assert environment_probe.agent_brief() != base
    bootstrap.uninstall_agent_brief_summary()
    assert environment_probe.agent_brief() == base


def test_format_profile_is_unchanged_by_the_provider():
    profile = environment_probe.format_profile()
    bootstrap.install_agent_brief_summary(_service())
    assert environment_probe.format_profile() == profile
    assert "capabilities" not in profile

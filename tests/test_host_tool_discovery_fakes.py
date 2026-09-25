"""Linux discovery against fake probes, plus a real planted-binary guard test."""
import os
import stat
import sys

import pytest

from sonder_runtime.adapters.host_tools import guards
from sonder_runtime.adapters.host_tools.discovery import HostToolDiscovery
from sonder_runtime.adapters.host_tools.probes import default_host_probes
from sonder_runtime.domain.host_tools.model import DiscoverySource, VersionStatus
from sonder_runtime.domain.host_tools.registry import spec_for
from tests.test_tools_inventory_fakes import FakeHost


def _specs(*names):
    return tuple(spec_for(name) for name in names)


def _by_name(snapshot):
    return {record.name: record for record in snapshot.tools}


def test_path_order_dedupe_and_version_parsing():
    host = FakeHost(path="/usr/local/bin:/usr/bin:relative/bin:/usr/bin:.")
    host.add_exe("/usr/local/bin/gcc", "5:5", "gcc (Local) 14.1.0")
    host.add_exe("/usr/bin/gcc", "6:6", "gcc (Ubuntu 13.2.0-23ubuntu4) 13.2.0")
    host.add_exe("/usr/bin/git", "7:7", "git version 2.43.0")
    snapshot = HostToolDiscovery(host.probes(), specs=_specs("gcc", "git")).discover(previous=None, full=False)
    tools = _by_name(snapshot)
    assert tools["gcc"].path == "/usr/local/bin/gcc"
    assert tools["gcc"].version == "14.1.0" and tools["gcc"].version_status is VersionStatus.OK
    assert tools["gcc"].alternatives == ("/usr/bin/gcc",)
    assert tools["gcc"].on_path and tools["gcc"].source is DiscoverySource.PATH
    assert tools["git"].version == "2.43.0"
    assert any("relative" in note for note in snapshot.notes)
    # Only fixed argv was launched, with a scrubbed and pinned environment.
    assert sorted(host.runs) == [("/usr/bin/git", "--version"), ("/usr/local/bin/gcc", "--version")]
    assert all(env["NO_COLOR"] == "1" and env["CI"] == "1" for env in host.envs)


def test_symlinked_duplicates_are_not_alternatives():
    host = FakeHost(path="/bin:/usr/bin")
    host.add_exe("/bin/make", "1:1", "GNU Make 4.3")
    host.add_exe("/usr/bin/make", "1:1", "GNU Make 4.3")
    host.realpaths["/bin/make"] = "/usr/bin/make"
    snapshot = HostToolDiscovery(host.probes(), specs=_specs("make")).discover(previous=None, full=False)
    assert _by_name(snapshot)["make"].alternatives == ()


def test_known_prefix_dirs_and_alternatives_cap():
    host = FakeHost(path="/usr/bin")
    host.add_exe("/usr/bin/node", "1:1", "v20.11.0")
    host.dirs.update({"/home/alice/.nvm/versions/node", "/opt"})
    for index, version in enumerate(["v18.0.0", "v19.0.0", "v21.0.0", "v22.0.0", "v23.0.0"]):
        host.add_exe(f"/home/alice/.nvm/versions/node/{version}/bin/node", f"{index}:9")
    host.add_exe("/home/alice/.cargo/bin/cargo", "3:3", "cargo 1.75.0 (1d8b05cdd 2023-11-20)")
    snapshot = HostToolDiscovery(host.probes(), specs=_specs("node", "cargo")).discover(previous=None, full=False)
    tools = _by_name(snapshot)
    assert len(tools["node"].alternatives) == 4
    assert tools["node"].alternatives[0].endswith("v23.0.0/bin/node")  # newest nvm first
    assert tools["cargo"].source is DiscoverySource.KNOWN_PREFIX and not tools["cargo"].on_path
    assert tools["cargo"].version == "1.75.0"


def test_version_cache_reused_by_identity_and_full_reprobes():
    host = FakeHost(path="/usr/bin")
    host.add_exe("/usr/bin/cmake", "9:9", "cmake version 3.28.3")
    discovery = HostToolDiscovery(host.probes(), specs=_specs("cmake"))
    first = discovery.discover(previous=None, full=False)
    assert len(host.runs) == 1
    second = discovery.discover(previous=first, full=False)
    assert len(host.runs) == 1 and second.digest == first.digest
    discovery.discover(previous=first, full=True)
    assert len(host.runs) == 2
    host.files["/usr/bin/cmake"] = "9:10"  # binary replaced: identity changed
    discovery.discover(previous=first, full=False)
    assert len(host.runs) == 3


def test_budget_exhaustion_yields_deferred():
    host = FakeHost(path="/usr/bin")
    for name in ("gcc", "git", "make"):
        host.add_exe(f"/usr/bin/{name}", "1:1", f"{name} 1.2.3")
    ticks = iter(range(0, 10_000, 50))
    discovery = HostToolDiscovery(host.probes(), specs=_specs("gcc", "git", "make"),
                                  budget_seconds=30, max_workers=1, monotonic=lambda: next(ticks))
    snapshot = discovery.discover(previous=None, full=False)
    assert {r.version_status for r in snapshot.tools} == {VersionStatus.DEFERRED}
    assert host.runs == []
    assert any("budget" in note for note in snapshot.notes)


def test_probe_cap_defers_extra_tools():
    host = FakeHost(path="/usr/bin")
    for name in ("gcc", "git", "make"):
        host.add_exe(f"/usr/bin/{name}", "1:1", f"{name} 1.2.3")
    snapshot = HostToolDiscovery(host.probes(), specs=_specs("gcc", "git", "make"),
                                 max_probes=2).discover(previous=None, full=False)
    statuses = sorted(r.version_status.value for r in snapshot.tools)
    assert statuses == ["deferred", "ok", "ok"] and len(host.runs) == 2


def test_project_local_binary_is_recorded_but_never_run():
    host = FakeHost(path="/work/project/bin:/usr/bin")
    host.add_exe("/work/project/bin/python3", "1:1", "Python 3.99.0")
    host.add_exe("/usr/bin/git", "2:2", "git version 2.43.0")
    host.local.add("/work/project")
    snapshot = HostToolDiscovery(host.probes(), specs=_specs("python3", "git")).discover(previous=None, full=False)
    tools = _by_name(snapshot)
    assert tools["python3"].version_status is VersionStatus.PROJECT_LOCAL
    assert tools["python3"].version == ""
    assert host.runs == [("/usr/bin/git", "--version")]


def test_presence_only_specs_are_never_run():
    host = FakeHost(path="/usr/bin")
    host.add_exe("/usr/bin/code", "1:1", "1.90.0")
    snapshot = HostToolDiscovery(host.probes(), specs=_specs("code")).discover(previous=None, full=False)
    assert _by_name(snapshot)["code"].version_status is VersionStatus.NOT_PROBED
    assert host.runs == []


def _planted_script(directory, marker):
    script = directory / "python3"
    script.write_text(
        "#!/bin/sh\n"
        f"echo planted > '{marker}'\n"
        "echo 'Python 3.99.0'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shebang script")
def test_real_planted_binary_in_allowed_root_is_not_executed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "bin").mkdir(parents=True)
    marker = tmp_path / "marker-inside"
    _planted_script(project / "bin", marker)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))
    monkeypatch.setenv("PATH", f"{project / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    snapshot = HostToolDiscovery(default_host_probes(), specs=_specs("python3"), budget_seconds=20,
                                 probe_timeout_seconds=5).discover(
        previous=None, full=False)
    record = _by_name(snapshot)["python3"]
    assert record.path == str(project / "bin" / "python3")
    assert record.version_status is VersionStatus.PROJECT_LOCAL
    assert not marker.exists(), "a project-local binary was executed"
    assert guards.project_local(record.path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shebang script")
def test_real_same_script_outside_roots_is_probed_control(tmp_path, monkeypatch):
    outside = tmp_path / "outside-bin"
    outside.mkdir()
    marker = tmp_path / "marker-outside"
    _planted_script(outside, marker)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path / "unrelated-root"))
    monkeypatch.setenv("PATH", f"{outside}{os.pathsep}{os.environ.get('PATH', '')}")
    snapshot = HostToolDiscovery(default_host_probes(), specs=_specs("python3"), budget_seconds=20,
                                 probe_timeout_seconds=5).discover(
        previous=None, full=False)
    record = _by_name(snapshot)["python3"]
    assert record.path == str(outside / "python3")
    assert record.version_status is VersionStatus.OK and record.version == "3.99.0"
    assert marker.exists()
    assert not guards.project_local(record.path)

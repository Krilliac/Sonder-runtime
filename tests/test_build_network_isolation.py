"""Network policy of build jobs: unshare enforcement, launcher downgrade,
enforce refusals, advisory platforms, and a real namespace probe."""
from __future__ import annotations

import shutil
import sys
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.build.network import (
    ADVISORY_OFF,
    ALLOWED,
    ENFORCED_OFF,
    NetworkIsolation,
    launcher_names,
)
from sonder_runtime.domain.common.errors import SonderError

pytestmark = pytest.mark.unit


class Lookup:
    def lookup(self, name):
        return SimpleNamespace(path="/usr/bin/" + name) if name in ("unshare", "setpriv") else None


def fake_run(results):
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return SimpleNamespace(outcome=results.get(argv[0].rsplit("/", 1)[-1], "ok"), output="")

    run.calls = calls
    return run


def isolation(mode="default", platform="linux", results=None, uid=1000):
    run = fake_run(results or {})
    decider = NetworkIsolation(mode=mode, platform=platform, lookup=Lookup(), run=run,
                               geteuid=lambda: uid, executable_guard=lambda path: path)
    return decider, run


def test_enforced_off_prefixes_unshare_and_probes_once():
    decider, run = isolation()
    first = decider.decide(allow_network=False)
    second = decider.decide(allow_network=False)
    assert first.policy == ENFORCED_OFF and first.prefix == ("/usr/bin/unshare", "-rn", "--")
    assert first.checked_executables == ("/usr/bin/unshare",)
    assert second == first and len(run.calls) == 1
    assert run.calls[0][1]["env"]["PATH"].startswith("/usr/sbin")  # a fixed probe env


def test_allow_network_is_allowed_without_prefix():
    decider, run = isolation()
    decision = decider.decide(allow_network=True)
    assert decision.policy == ALLOWED and decision.prefix == () and not run.calls


@pytest.mark.parametrize("value", ["/usr/bin/sccache", r"C:\tools\sccache.exe", "distcc", "icecc;x"])
def test_a_daemon_launcher_downgrades_under_default(value):
    decider, _ = isolation()
    decision = decider.decide(allow_network=False, launchers=(value,))
    assert decision.policy == ADVISORY_OFF and decision.prefix == ()
    assert "needs a local daemon" in decision.notes[0]


def test_a_daemon_launcher_is_refused_under_enforce():
    decider, _ = isolation(mode="enforce")
    with pytest.raises(SonderError) as excinfo:
        decider.decide(allow_network=False, launchers=("/usr/bin/sccache",))
    assert excinfo.value.code == "NETWORK_ISOLATION_UNAVAILABLE"


def test_ccache_is_not_a_daemon_launcher():
    assert launcher_names(["/usr/bin/ccache"]) == ()
    decider, _ = isolation()
    assert decider.decide(allow_network=False, launchers=("/usr/bin/ccache",)).policy == ENFORCED_OFF


def test_a_failed_probe_is_advisory_by_default_and_refused_under_enforce():
    decider, _ = isolation(results={"unshare": "error"})
    assert decider.decide(allow_network=False).policy == ADVISORY_OFF
    strict, _ = isolation(mode="enforce", results={"unshare": "error"})
    with pytest.raises(SonderError) as excinfo:
        strict.decide(allow_network=False)
    assert excinfo.value.code == "NETWORK_ISOLATION_UNAVAILABLE"


@pytest.mark.parametrize("platform", ["windows", "darwin"])
def test_windows_and_macos_are_advisory(platform):
    decider, run = isolation(platform=platform)
    assert decider.decide(allow_network=False).policy == ADVISORY_OFF and not run.calls
    strict, _ = isolation(mode="enforce", platform=platform)
    with pytest.raises(SonderError):
        strict.decide(allow_network=False)


def test_advisory_mode_never_probes():
    decider, run = isolation(mode="advisory")
    assert decider.decide(allow_network=False).policy == ADVISORY_OFF and not run.calls


def test_a_root_runtime_also_probes_the_unprivileged_uid_for_reporting():
    decider, run = isolation(uid=0)
    probe = decider.probe()
    assert probe.enforceable and probe.unprivileged_uid == 65534 and probe.unprivileged_enforceable
    assert run.calls[1][0][:4] == ("/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups")
    assert any("runtime is root" in note for note in probe.notes)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        NetworkIsolation(mode="off")


@pytest.mark.integration
@pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("unshare") is None,
                    reason="needs Linux with unshare")
def test_real_probe_on_this_host():
    decider = NetworkIsolation(mode="default", executable_guard=lambda path: path)
    probe = decider.probe()
    decision = decider.decide(allow_network=False)
    if probe.enforceable:
        assert decision.policy == ENFORCED_OFF and decision.prefix[1:] == ("-rn", "--")
    else:
        assert decision.policy == ADVISORY_OFF

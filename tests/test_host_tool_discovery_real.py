"""Real discovery on this host (Linux, real processes, per-tool skips)."""
import os
import shutil
import sys
import time

import pytest

from sonder_runtime.adapters.host_tools.discovery import HostToolDiscovery
from sonder_runtime.adapters.host_tools.probes import default_host_probes
from sonder_runtime.domain.host_tools.model import VersionStatus
from sonder_runtime.domain.host_tools.registry import HOST_TOOL_SPECS, spec_for

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux host discovery")

CORE = ("python3", "git", "gcc", "cmake", "make")


def _discover(specs, previous=None):
    discovery = HostToolDiscovery(default_host_probes(), specs=specs, budget_seconds=30,
                                  probe_timeout_seconds=3)
    return discovery.discover(previous=previous, full=False)


def test_core_tools_are_found_with_versions_matching_which(monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", "")
    specs = tuple(spec_for(name) for name in CORE)
    snapshot = _discover(specs)
    tools = {record.name: record for record in snapshot.tools}
    checked = 0
    for name in CORE:
        which = shutil.which(name)
        if which is None:
            continue
        checked += 1
        record = tools[name]
        assert os.path.realpath(record.path) == os.path.realpath(which), name
        assert record.version_status is VersionStatus.OK, (name, record)
        assert record.version and record.version[0].isdigit(), (name, record.version)
    assert checked >= 3, "too few core tools on this host to prove discovery"


def test_full_registry_discovery_is_bounded_and_digest_stable_with_cache():
    started = time.monotonic()
    first = _discover(HOST_TOOL_SPECS)
    elapsed = time.monotonic() - started
    assert elapsed < 30 + 3 + 3
    assert first.tools, "no tools discovered at all"
    assert len(first.tools) <= 512
    # Re-run over the tools whose versions were cached: identical digest.
    stable_names = {r.name for r in first.tools if r.version_status in (VersionStatus.OK,)}
    stable = tuple(spec for spec in HOST_TOOL_SPECS if spec.name in stable_names)
    subset = _discover(stable)
    again = _discover(stable, previous=subset)
    # Every record probed OK is reused verbatim from the cache (no relaunch).
    # A probe that timed out under CPU contention is re-probed and may change,
    # so the whole-snapshot digest is compared only when all records were OK.
    subset_by_name = {record.name: record for record in subset.tools}
    again_by_name = {record.name: record for record in again.tools}
    cached = [r for r in subset.tools if r.version_status is VersionStatus.OK]
    assert cached, "no tool was probed OK twice on this host"
    for record in cached:
        assert again_by_name[record.name] == record, record.name
    if all(r.version_status is VersionStatus.OK for r in subset_by_name.values()):
        assert again.digest == subset.digest

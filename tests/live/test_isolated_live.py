"""Opt-in live checks of the isolated runner against a real Docker/Podman engine.

The root conftest scrubs every SONDER_* variable at import, so it captures these
first and the ``live_isolated_environment`` fixture restores them per test.

Skipped unless ``SONDER_ISOLATED_LIVE=1``, ``SONDER_ISOLATED_RUNTIME`` (docker or
podman) and ``SONDER_ISOLATED_ROOTS`` (a directory the engine can bind-mount)
are all set; the skip reason names the one that is missing. The image must
already be installed (the runner never pulls)::

    podman pull docker.io/library/alpine:3.22
    SONDER_ISOLATED_LIVE=1 SONDER_ISOLATED_RUNTIME=podman \\
        SONDER_ISOLATED_ROOTS=/path/to/scratch \\
        pytest tests/live/test_isolated_live.py

``SONDER_ISOLATED_IMAGE`` overrides the default alpine:3.22. The OCI-volume
refusal check additionally needs ``SONDER_ISOLATED_VOLUME_IMAGE`` naming an
installed image that declares a ``VOLUME``; without it that one check skips.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution import isolated_runner as ir

DEFAULT_IMAGE = "docker.io/library/alpine:3.22"


@pytest.fixture(autouse=True)
def _live_gate(live_isolated_environment):
    """Skip unless every opt-in variable (restored by conftest) is present."""
    if live_isolated_environment.get("SONDER_ISOLATED_LIVE") != "1":
        pytest.skip("live isolated-runner test: SONDER_ISOLATED_LIVE=1 is not set")
    for name in (ir.RUNTIME_ENV, ir.ROOTS_ENV):
        if not live_isolated_environment.get(name, "").strip():
            pytest.skip("live isolated-runner test: %s is not set" % name)


def _image():
    return os.environ.get("SONDER_ISOLATED_IMAGE", "").strip() or DEFAULT_IMAGE


@pytest.fixture
def project():
    root = ir.authorized_roots()[0]
    path = root / ("sonder-live-" + uuid.uuid4().hex[:12])
    path.mkdir()
    (path / "hello.txt").write_text("hello-from-host\n", encoding="utf-8")
    os.chmod(path, 0o755)
    os.chmod(path / "hello.txt", 0o644)
    try:
        yield str(path.resolve())
    finally:
        for child in path.iterdir():
            child.unlink()
        path.rmdir()


def _run(project, script, **kwargs):
    return ir.run_isolated(
        _image(), json.dumps(["/bin/sh", "-c", script]), project, **kwargs
    )


def test_runtime_is_detected():
    assert ir.detect_runtime() is not None, "no ready local engine detected"


def test_runs_as_unprivileged_uid(project):
    out = _run(project, "id")
    assert "uid=65534" in out["stdout"] and "gid=65534" in out["stdout"], out


def test_network_is_none(project):
    out = _run(
        project,
        "cut -d: -f1 /proc/net/dev | tail -n +3; "
        "wget -T 3 -q -O- http://1.1.1.1 >/dev/null 2>&1 "
        "&& echo NET_OPEN || echo NET_BLOCKED",
    )
    assert "NET_BLOCKED" in out["stdout"] and "eth0" not in out["stdout"], out
    assert out["stdout"].split()[0] == "lo", out


def test_root_filesystem_is_read_only(project):
    out = _run(project, "touch /pwned 2>/dev/null && echo ROOT_WRITE_OK || echo ROOT_WRITE_DENIED")
    assert "ROOT_WRITE_DENIED" in out["stdout"], out


def test_workspace_is_read_only_by_default(project):
    out = _run(
        project,
        "cat /workspace/hello.txt; touch /workspace/x 2>/dev/null "
        "&& echo WS_WRITE_OK || echo WS_WRITE_DENIED",
    )
    assert "hello-from-host" in out["stdout"] and "WS_WRITE_DENIED" in out["stdout"], out


def test_capabilities_dropped_and_no_new_privs(project):
    out = _run(project, "grep -E '^(CapEff|NoNewPrivs):' /proc/self/status")
    assert "CapEff:\t0000000000000000" in out["stdout"], out
    assert "NoNewPrivs:\t1" in out["stdout"], out


def test_tmp_is_noexec(project):
    out = _run(
        project,
        "cp /bin/busybox /tmp/bb 2>/dev/null; /tmp/bb true 2>/dev/null "
        "&& echo TMP_EXEC_OK || echo TMP_EXEC_DENIED",
    )
    assert "TMP_EXEC_DENIED" in out["stdout"], out


def test_cgroup_limits_applied(project):
    out = _run(
        project,
        "echo mem=$(cat /sys/fs/cgroup/memory.max) pids=$(cat /sys/fs/cgroup/pids.max) "
        "cpu=$(cat /sys/fs/cgroup/cpu.max)",
    )
    assert "mem=536870912" in out["stdout"], out
    assert "pids=64" in out["stdout"], out
    assert "cpu=100000 100000" in out["stdout"], out


def test_timeout_kills_container_and_cleanup_is_verified(project):
    out = _run(project, "sleep 60", timeout=3)
    assert not out.get("ok"), out
    assert "timed out after 3s" in str(out.get("error")), out
    assert out.get("cleanup") == "verified-absent", out


def test_writable_workspace_is_opt_in(project):
    marker = Path(project) / "written_by_container.txt"
    try:
        _run(project, "echo hi > /workspace/written_by_container.txt", writable_workspace=True)
        assert marker.exists()
    finally:
        if marker.exists():
            marker.unlink()


def test_image_declaring_a_volume_is_refused(project):
    image = os.environ.get("SONDER_ISOLATED_VOLUME_IMAGE", "").strip()
    if not image:
        pytest.skip("SONDER_ISOLATED_VOLUME_IMAGE is not set (needs an installed image with a VOLUME)")
    with pytest.raises(ValueError, match="OCI volumes are not allowed"):
        ir.run_isolated(image, json.dumps(["true"]), project)

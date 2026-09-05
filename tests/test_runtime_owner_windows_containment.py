import os
import subprocess
import sys

import pytest


def test_failed_job_handle_close_retains_the_owned_handle():
    from sonder_runtime.adapters.extensions.memory_limits import (
        _WindowsJobToken,
        ExtensionMemoryLimitError,
    )

    token = _WindowsJobToken(123, lambda handle: False)
    with pytest.raises(ExtensionMemoryLimitError):
        token.close()
    assert token._handle == 123
    token._close_handle = lambda handle: True
    token.close()
    assert token._handle is None


from sonder_runtime.adapters.extensions.memory_limits import (
    NativeExtensionMemoryLimiter,
)


@pytest.mark.skipif(os.name != "nt", reason="actual Windows Job Object required")
def test_native_job_proves_live_then_empty_without_pid_census():
    limiter = NativeExtensionMemoryLimiter()
    argv = (sys.executable, "-c", "import time; time.sleep(30)")
    prepared = limiter.prepare_process_job("owner-fixture", argv, None, 4)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=prepared.launch_options["creationflags"]
        | subprocess.CREATE_NO_WINDOW,
    )
    token = None
    try:
        token = limiter.apply_process_limits(process, None, 4)
        limiter.resume(process)
        assert not token.quiesce(force=False).complete
        proof = token.quiesce(force=True)
        assert proof.complete and proof.forced
        process.wait(timeout=3)
        assert token.quiesce(force=False).complete
    finally:
        if token is not None:
            token.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.mark.skipif(os.name != "nt", reason="actual Windows descendants required")
def test_job_cleanup_includes_actual_descendant(tmp_path):
    import time
    from sonder_runtime.adapters.process_liveness import (
        process_identity,
        probe_process,
        PROCESS_DEAD,
    )

    marker = tmp_path / "descendant.txt"
    script = "import subprocess,sys,time; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    argv = (sys._base_executable, "-c", script, str(marker))
    limiter = NativeExtensionMemoryLimiter()
    prepared = limiter.prepare_process_job("descendant-fixture", argv, None, 4)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=prepared.launch_options["creationflags"]
        | subprocess.CREATE_NO_WINDOW,
    )
    token = None
    try:
        token = limiter.apply_process_limits(process, None, 4)
        limiter.resume(process)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(marker.read_text())
        child_identity = process_identity(child_pid)
        assert child_identity
        assert token.quiesce(force=True).complete
        process.wait(timeout=3)
        assert probe_process(child_pid, child_identity)[0] == PROCESS_DEAD
    finally:
        if token is not None:
            token.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)

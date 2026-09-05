import os
import socket
import urllib.request

import pytest

from sonder_runtime.bootstrap.runtime_owner import DisposableRuntimeOwner
from sonder_runtime.application.ports.runtime_owner import (
    OwnerRefused,
    OwnerUnsupported,
)


@pytest.mark.skipif(os.name != "nt", reason="actual Windows contained runtime required")
def test_owned_http_runtime_clean_stop_and_exact_replay(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    owner = DisposableRuntimeOwner(tmp_path / "owner", writable_roots=lambda: ())
    try:
        selection = owner.prepare("select", "select", {"config": {"port": port}})
        owner.execute(selection)
        launch = owner.prepare("launch", "launch", {})
        receipt = owner.execute(launch)
        readers = owner._process._readers
        assert readers
        assert receipt["state"] == "RUNNING"
        assert owner.execute(launch) == receipt
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/live", timeout=3
        ) as response:
            assert response.status == 200
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/admin/drain",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request, timeout=3)
        assert denied.value.code in (401, 403)
        assert owner._process.alive("launch")
        assert owner.private_source_paths == (str(owner.path),)
        stop = owner.prepare("stop", "stop", {})
        stopped = owner.execute(stop)
        assert stopped["state"] == "STOPPED_CLEAN"
        assert stopped["result"]["containment_empty"]
        assert not any(reader.is_alive() for reader in readers)
        assert owner.execute(stop) == stopped
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        owner.close()


def test_unknown_existing_namespace_never_becomes_an_owner(tmp_path):
    with pytest.raises(OwnerUnsupported):
        DisposableRuntimeOwner(tmp_path, writable_roots=lambda: ())


@pytest.mark.skipif(os.name != "nt", reason="Windows owner composition required")
def test_live_private_root_revocation_blocks_admission_without_effects(tmp_path):
    roots = []
    owner = DisposableRuntimeOwner(tmp_path / "owner", writable_roots=lambda: roots)
    try:
        before = owner.journal.status()
        roots.append(tmp_path)
        with pytest.raises(OwnerRefused, match="overlaps"):
            owner.prepare("select", "select", {"config": {"port": 12345}})
        assert owner.journal.status() == before
    finally:
        roots.clear()
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="actual Windows owned cleanup required")
def test_late_cleanup_retains_pending_identity_for_effect_free_reconciliation(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    import sonder_runtime.bootstrap.runtime_owner as module

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    owner = DisposableRuntimeOwner(tmp_path / "owner", writable_roots=lambda: ())
    try:
        owner.execute(owner.prepare("select", "select", {"config": {"port": port}}))
        owner.execute(owner.prepare("launch", "launch", {}))
        stop = owner.prepare("stop", "stop", {})
        original = owner._process.wait
        clock = module.time
        calls = []
        with monkeypatch.context() as patch:

            def late(*args):
                result = original(*args)
                calls.append(result)
                patch.setattr(
                    module,
                    "time",
                    SimpleNamespace(
                        monotonic=lambda: clock.monotonic() + 60, sleep=clock.sleep
                    ),
                )
                return result

            patch.setattr(owner._process, "wait", late)
            with pytest.raises(OwnerRefused, match="deadline"):
                owner.execute(stop)
        assert owner.journal.pending() == stop
        assert len(calls) == 1
        with monkeypatch.context() as patch:
            patch.setattr(
                owner._process,
                "wait",
                lambda *args: pytest.fail("cleanup effects repeated"),
            )
            assert owner.execute(stop)["state"] == "STOPPED_CLEAN"
    finally:
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="actual Windows contained runtime required")
def test_forced_runtime_stop_remains_unclean_and_cannot_restart(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    owner = DisposableRuntimeOwner(tmp_path / "owner", writable_roots=lambda: ())
    try:
        owner.execute(owner.prepare("select", "select", {"config": {"port": port}}))
        owner.execute(owner.prepare("launch", "launch", {}))
        assert owner._process.force_stop("launch").cleanup_completed
        stopped = owner.execute(owner.prepare("stop", "stop", {}))
        assert stopped["state"] == "STOPPED_UNCLEAN"
        assert not stopped["result"]["application_closed"]
        with pytest.raises(OwnerRefused):
            owner.prepare("unsafe-restart", "launch", {})
    finally:
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="actual Windows owner crash required")
def test_owner_crash_closes_containment_and_reopen_cannot_launch(tmp_path):
    import json
    from pathlib import Path
    import subprocess
    import sys
    import time
    from sonder_runtime.adapters.persistence.runtime_owner import (
        SQLiteRuntimeOwnerJournal,
    )
    from sonder_runtime.application.ports.runtime_owner import prepare_owner_operation

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = tmp_path / "owner"
    marker = tmp_path / "observed.json"
    script = """
import json,os,sys
from pathlib import Path
from sonder_runtime.bootstrap.runtime_owner import DisposableRuntimeOwner
owner=DisposableRuntimeOwner(Path(sys.argv[1]),writable_roots=lambda:())
owner.execute(owner.prepare('select','select',{'config':{'port':int(sys.argv[3])}}))
owner.execute(owner.prepare('launch','launch',{}))
Path(sys.argv[2]).write_text(json.dumps({'namespace':owner.namespace}))
os._exit(23)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(Path(__file__).resolve().parents[1]),
            str(Path(sys.prefix) / "Lib" / "site-packages"),
        )
    )
    result = subprocess.run(
        [sys._base_executable, "-c", script, str(root), str(marker), str(port)],
        env=environment,
        capture_output=True,
        timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 23
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                break
        time.sleep(0.05)
    else:
        pytest.fail("exact disposable listener survived owner crash")
    namespace = json.loads(marker.read_text())["namespace"]
    journal = SQLiteRuntimeOwnerJournal(root / "owner.sqlite", namespace=namespace)
    assert journal.status()["state"] == "RUNNING"
    with pytest.raises(OwnerRefused):
        journal.prepare(
            prepare_owner_operation(
                "unsafe", "launch", journal.status()["revision"], {}
            )
        )
    with pytest.raises(OwnerUnsupported):
        DisposableRuntimeOwner(root, writable_roots=lambda: ())

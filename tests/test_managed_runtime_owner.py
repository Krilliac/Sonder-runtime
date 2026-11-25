import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from sonder_runtime.bootstrap.managed_runtime_owner import ManagedRuntimeOwner
from sonder_runtime.application.ports.runtime_owner import OwnerRefused, OwnerUnsupported
from tests._managed_runtime_layout import require_bounded_real_runtime_closure


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]



# A managed launch validates the full payload manifest twice and then starts a
# child that compiles the runtime without writing bytecode (-B), which takes
# ~20-27s on an idle workstation. The owner bounds each command at 30s. For a
# slower start its contract is "readiness is unresolved; retain launch
# identity": executing the same prepared launch again resumes the readiness
# wait for the process already started, without starting another. PR #673's
# CI hit that bound on a loaded runner. This helper is that resume, bounded
# only against a hang.
_LAUNCH_HANG_SECONDS = 180.0


def _execute_launch(owner, launch):
    deadline = time.monotonic() + _LAUNCH_HANG_SECONDS
    while True:
        try:
            return owner.execute(launch)
        except OwnerRefused as error:
            if "readiness is unresolved" not in str(error) or time.monotonic() >= deadline:
                raise
            # Exactly one launch: the identity is retained, nothing relaunched.
            assert owner._launch_id == launch.operation_id


def _capture_managed_owner_failure(owner, output_path, *, iteration, phase,
                                   launch_job_id, receipt, injection_completed,
                                   error):
    """Failure-only bounded reads; never change or wait on the child lifecycle."""
    import json
    import re

    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(item) for item in value]
        if isinstance(value, str):
            value = re.sub(r"(?i)\b[0-9a-f]{64}\b", "[redacted opaque value]", value)
            return re.sub(
                r"(?im)^.*(?:authorization|bearer|api[-_ ]?key|token|secret|password|credential).*$",
                "[redacted credential-labelled line]", value)
        return value

    diagnostic = {
        "iteration": iteration, "phase": phase, "launch_job_id": launch_job_id,
        "stop_receipt": receipt,
        "injected_completion_reached": injection_completed,
        "original_exception": {"type": type(error).__name__, "message": str(error)},
    }
    def capture(key, read):
        try:
            diagnostic[key] = read()
        except BaseException as exc:
            diagnostic[key] = {"read_error": type(exc).__name__, "message": str(exc)}

    def terminal_evidence():
        with (owner.path / ("runtime-" + launch_job_id + ".json")).open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            return {"oversized": True, "read_bytes": len(raw)}
        return json.loads(raw)

    def job_record():
        view = owner._process.registry.view(launch_job_id)
        record = view.record
        return {"status": record.status.value, "result": record.result,
                "error": record.error, "revision": record.revision,
                "created_at": record.created_at, "updated_at": record.updated_at,
                "process_id": view.process_id, "process_group_id": view.process_group_id}

    def retained_output():
        page = owner._process.registry.stream(launch_job_id,
                                             max_events=256, max_bytes=65536)
        return {"events": [{"sequence": event.watermark.sequence,
                            "stream": event.stream.value, "data": event.data}
                           for event in page.events],
                "next_watermark": page.next_watermark.sequence,
                "has_more": page.has_more, "truncated": page.truncated}

    capture("terminal_evidence", terminal_evidence)
    capture("job_record", job_record)
    capture("retained_output", retained_output)
    # Publish only this bounded, redacted failure receipt. Do not collect
    # environment/configuration files or the private runtime directory.
    try:
        destination = os.environ.get("PYTEST_MANAGED_OWNER_DIAGNOSTIC_DIR")
        if destination:
            directory = Path(destination)
            directory.mkdir(parents=True, exist_ok=True)
            output_path = directory / "owner-diagnostic.json"
        output_path.write_text(json.dumps(redact(diagnostic), indent=2, default=str) + "\n",
                               encoding="utf-8")
    except BaseException:
        pass


@pytest.mark.skipif(os.name != "nt", reason="actual Windows containment required")
def test_full_manifest_owned_http_and_relaunch(tmp_path, monkeypatch):
    require_bounded_real_runtime_closure()
    owner = ManagedRuntimeOwner(tmp_path / "owner", writable_roots=lambda: ())
    iteration = None
    phase = "select"
    launch_job_id = None
    receipt = None
    injection_completed = False
    try:
        configuration = owner.register_configuration(port=port())
        selected = owner.prepare("select", "select", {"config": configuration})
        owner.execute(selected)
        for index in range(2):
            iteration = index
            phase = "launch"
            receipt = None
            injection_completed = False
            launch = owner.prepare(f"launch{index}", "launch", {})
            launch_job_id = launch.operation_id
            assert _execute_launch(owner, launch)["state"] == "RUNNING"
            with pytest.raises(OwnerRefused):
                owner.selected_store
            configured_port = owner._config(configuration)["port"]
            with urllib.request.urlopen(f"http://127.0.0.1:{configured_port}/live", timeout=3) as response:
                assert response.status == 200
            phase = "prepare-stop"
            stop = owner.prepare(f"stop{index}", "stop", {})
            if index == 0:
                complete = owner.journal.complete
                def lose_stop_response(command, result, state):
                    nonlocal injection_completed
                    complete(command, result, state)
                    injection_completed = True
                    raise OSError("injected durable stop response loss")
                with monkeypatch.context() as patch:
                    patch.setattr(owner.journal, "complete", lose_stop_response)
                    with pytest.raises(OSError):
                        owner.execute(stop)
                assert owner._launch_id is not None
            phase = "execute-stop"
            receipt = owner.execute(stop)
            phase = "assert-clean-stop"
            assert receipt["state"] == "STOPPED_CLEAN"
            assert owner.execute(stop) == receipt
            assert owner.selected_store.path == owner.path / "children.sqlite"
        phase = "migration"
        from sonder_runtime.adapters.persistence.child_migration import SQLiteChildMigrationStore
        from sonder_runtime.adapters.filesystem.child_migration_bundle import ChildMigrationBundle
        from sonder_runtime.application.subagents.child_migration import export_snapshot, stage_snapshot
        source = owner.selected_store
        target = SQLiteChildMigrationStore(owner.path / "next.sqlite")
        with ChildMigrationBundle(tmp_path / "bundle", writable_roots=lambda: ()) as bundle:
            export_snapshot(source, bundle, target_identity=target.identity)
            stage_snapshot(bundle, target)
            reference = owner.register_configuration(port=port(), target=target)
            with pytest.raises(OwnerRefused):
                owner.prepare("bypass", "select", {"config": reference})
            activation = owner.prepare_activation("activate", bundle, target, reference)
            record_phase = bundle.record_phase
            def fail_complete(phase, manifest):
                if phase == "COMPLETE":
                    raise OSError("injected incomplete activation")
                return record_phase(phase, manifest)
            with monkeypatch.context() as patch:
                patch.setattr(bundle, "record_phase", fail_complete)
                with pytest.raises(OSError):
                    owner.execute(activation)
            with pytest.raises(OwnerRefused):
                owner.prepare("unsafe-launch", "launch", {})
            with pytest.raises(OwnerRefused):
                owner.selected_store
            assert owner.execute(activation)["state"] == "STOPPED_CLEAN"
            assert owner.selected_store.identity == target.identity
            iteration = "migrated"
            phase = "migrated-launch"
            receipt = None
            injection_completed = False
            launch = owner.prepare("migrated-launch", "launch", {})
            launch_job_id = launch.operation_id
            assert _execute_launch(owner, launch)["state"] == "RUNNING"
            phase = "migrated-stop"
            receipt = owner.execute(owner.prepare("migrated-stop", "stop", {}))
            phase = "migrated-assert-clean-stop"
            assert receipt["state"] == "STOPPED_CLEAN"
        with pytest.raises(OwnerUnsupported):
            ManagedRuntimeOwner(owner.path, writable_roots=lambda: ())
    except BaseException as error:
        try:
            _capture_managed_owner_failure(owner, tmp_path / "owner-diagnostic.json",
                iteration=iteration, phase=phase, launch_job_id=launch_job_id,
                receipt=receipt, injection_completed=injection_completed, error=error)
        except BaseException:
            pass
        raise
    finally:
        owner.close()


@pytest.mark.skipif(os.name != "nt", reason="native Windows managed process required")
def test_workstation_profile_launches_with_only_its_pinned_site(tmp_path):
    from sonder_runtime.adapters.execution.runtime_profile import PROFILE_NAME

    checkout = Path(__file__).resolve().parents[1]
    profile = checkout / "venv-managed"
    if not (profile / PROFILE_NAME).is_file():
        if os.environ.get("SONDER_REQUIRE_MANAGED_RUNTIME_PROFILE") == "1":
            pytest.fail("managed runtime installer did not provision its profile")
        pytest.skip("run install_workstation_local.ps1 -ManagedRuntime first")
    owner = ManagedRuntimeOwner.workstation_local(
        tmp_path / "owner", writable_roots=lambda: ()
    )
    try:
        manifest = owner._payload.manifest
        assert manifest["profile"] == str(profile)
        assert str(profile / "Lib" / "site-packages") in manifest["paths"]
        host_site = Path(sys.prefix).resolve() / "Lib" / "site-packages"
        if host_site != profile / "Lib" / "site-packages":
            assert str(host_site) not in manifest["paths"]
        selected = owner.register_configuration(port=port())
        owner.execute(owner.prepare("select", "select", {"config": selected}))
        assert owner.execute(owner.prepare("launch", "launch", {}))["state"] == "RUNNING"
        assert owner.execute(owner.prepare("stop", "stop", {}))["state"] == "STOPPED_CLEAN"
    finally:
        owner.close()


def test_journal_read_only_queries_do_not_contend_with_writer_begin_immediate(tmp_path):
    """Child control polling must not take a write lock while the owner writes."""
    import sqlite3
    from sonder_runtime.adapters.persistence.runtime_owner import (
        SQLiteManagedRuntimeOwnerJournal,
    )
    from sonder_runtime.application.ports.managed_runtime_owner import managed_operation

    db_path = tmp_path / "owner.sqlite"
    journal = SQLiteManagedRuntimeOwnerJournal(db_path, namespace="ns", create=True)
    select = managed_operation(
        "select",
        "select",
        journal.status(),
        {"config": {"generation": 1, "digest": "a" * 64, "selector_revision": 0}},
    )
    journal.prepare(select)
    journal.complete(select, {"selected": True}, "STOPPED_CLEAN")
    launch = managed_operation("launch0", "launch", journal.status(), {})
    journal.prepare(launch)
    journal.phase(launch, "STARTING", {"manifest": "m"})

    reader = SQLiteManagedRuntimeOwnerJournal(db_path, namespace="ns")
    writer = sqlite3.connect(db_path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert reader.status()["state"] == "STARTING"
        assert reader.pending() == launch
        assert reader.selected_config()["generation"] == 1
        assert reader.phase_evidence(launch, "STARTING") == {"manifest": "m"}
    finally:
        writer.rollback()
        writer.close()


def test_journal_distinguishes_transient_busy_from_permanent_storage_error(
    tmp_path, monkeypatch
):
    import sqlite3
    import sonder_runtime.adapters.persistence.runtime_owner as persistence_owner

    db_path = tmp_path / "owner.sqlite"
    journal = persistence_owner.SQLiteManagedRuntimeOwnerJournal(
        db_path, namespace="ns", create=True
    )

    def raise_busy(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(persistence_owner, "owned_sqlite_connect", raise_busy)
        with pytest.raises(persistence_owner.OwnerStorageBusy, match="unavailable"):
            journal.pending()

    def raise_corrupt(*_args, **_kwargs):
        raise sqlite3.DatabaseError("file is not a database")

    with monkeypatch.context() as patch:
        patch.setattr(persistence_owner, "owned_sqlite_connect", raise_corrupt)
        with pytest.raises(OwnerRefused, match="unavailable") as caught:
            journal.pending()
        assert type(caught.value) is OwnerRefused


def test_managed_control_loop_retries_bounded_transient_busy_and_fails_closed():
    from threading import Event
    from types import SimpleNamespace
    from sonder_runtime.adapters.persistence.runtime_owner import OwnerStorageBusy
    from sonder_runtime.bootstrap import managed_http_runtime as managed

    # 1. Transient SQLite busy spikes (< budget) recover cleanly and process stop.
    outcomes = iter([
        OwnerStorageBusy("owner storage is unavailable"),
        OwnerStorageBusy("owner storage is unavailable"),
        None,
        OwnerStorageBusy("owner storage is unavailable"),
        SimpleNamespace(action="stop"),
    ])
    ready_marks = []
    drains = []
    errors = []

    def read_next():
        item = next(outcomes)
        if isinstance(item, BaseException):
            raise item
        return item

    managed._run_control_loop(
        stopped=Event(),
        is_ready=lambda: True,
        mark_ready=lambda: ready_marks.append("READY"),
        read_pending=read_next,
        drain=drains.append,
        on_error=lambda err: errors.append(type(err).__name__),
        poll_interval=0.0,
        max_transient_busy=3,
    )
    assert ready_marks == ["READY"]
    assert drains == ["owned runtime stop"]
    assert errors == []

    # 2. Persistent busy hitting the bound still fails closed.
    persistent_drains = []
    persistent_errors = []
    managed._run_control_loop(
        stopped=Event(),
        is_ready=lambda: False,
        mark_ready=lambda: None,
        read_pending=lambda: (_ for _ in ()).throw(
            OwnerStorageBusy("owner storage is unavailable")
        ),
        drain=persistent_drains.append,
        on_error=lambda err: persistent_errors.append(type(err).__name__),
        poll_interval=0.0,
        max_transient_busy=3,
    )
    assert persistent_errors == ["OwnerStorageBusy"]
    assert persistent_drains == ["owned control unavailable"]

    # 3. Non-busy OwnerRefused (e.g. missing journal or integrity error) fails closed immediately.
    immediate_calls = []
    immediate_drains = []
    immediate_errors = []

    def missing_journal():
        immediate_calls.append(1)
        raise OwnerRefused("owner journal is missing")

    managed._run_control_loop(
        stopped=Event(),
        is_ready=lambda: False,
        mark_ready=lambda: None,
        read_pending=missing_journal,
        drain=immediate_drains.append,
        on_error=lambda err: immediate_errors.append(type(err).__name__),
        poll_interval=0.0,
        max_transient_busy=5,
    )
    assert immediate_calls == [1]
    assert immediate_errors == ["OwnerRefused"]
    assert immediate_drains == ["owned control unavailable"]


def test_finish_managed_runtime_includes_bounded_error_reason():
    from types import SimpleNamespace
    from sonder_runtime.bootstrap import managed_http_runtime as managed

    recorded = []
    with pytest.raises(
        OwnerRefused, match=r"managed application cleanup is incomplete.*control-failed:OwnerRefused"
    ):
        managed._finish_managed_runtime(
            errors=["control-failed:OwnerRefused"],
            cleanup_errors=[],
            receipt=SimpleNamespace(clean=True, components=()),
            evidence=lambda phase, receipt: recorded.append((phase, receipt)),
            primary_error=None,
            primary_traceback=None,
        )
    assert len(recorded) == 1 and recorded[0][0] == "UNCLEAN"

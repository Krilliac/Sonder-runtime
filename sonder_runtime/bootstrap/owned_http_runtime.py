"""Fixed child of the disposable owner; no external control/authentication API."""

from dataclasses import replace
import os
from pathlib import Path
import secrets
import sys
from threading import Event, Thread


def run(root, namespace, job_id):
    from ..application.compute_fabric.artifact_spool import PrivateDirectoryAnchor

    root = Path(root).absolute()
    workspace = root.parent / (root.name + "-workspace")
    # The live owner created and pinned this required-new directory before launch.
    # The child only validates/holds it; it must never create or adopt a redirect.
    with PrivateDirectoryAnchor(workspace) as workspace_anchor:
        workspace_anchor.validate()
        return _run(root, namespace, job_id, workspace)


def _run(root, namespace, job_id, workspace):
    # Only process-created environment and fixed namespace data are consumed.
    # Before importing the HTTP stack, prove this actual process was attached
    # to the canonical job record while its launch was durably pending.
    from ..adapters.persistence.runtime_owner import SQLiteRuntimeOwnerJournal
    from ..adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from ..adapters.process_liveness import process_identity
    from ..application.ports.runtime_owner import OwnerRefused

    root = Path(root).absolute()
    journal = SQLiteRuntimeOwnerJournal(root / "owner.sqlite", namespace=namespace)
    command = journal.pending()
    registry = SQLiteDurableJobRegistry(root / "processes.sqlite")
    record = registry.view(job_id)
    if (
        command is None
        or command.action != "launch"
        or command.operation_id != job_id
        or record.process_id != os.getpid()
    ):
        raise OwnerRefused("actual owned process admission is missing")
    identity = process_identity(os.getpid())
    if (
        not identity
        or dict(registry.view(job_id).metadata).get("process_instance_identity")
        != identity
    ):
        raise OwnerRefused("actual owned process identity does not match")
    selected = journal.selected_config()
    from .runtime_owner import DisposableRuntimeOwner

    DisposableRuntimeOwner._config(selected)

    from ..platform.config import SonderConfig, Secrets
    from ..adapters.filesystem.atomic_json import write_json_atomic
    from ..adapters.inference import ollama_endpoint

    config = SonderConfig()
    config = replace(
        config,
        server=replace(
            config.server, host="127.0.0.1", port=selected["port"], auth_mode="api-key"
        ),
        state=replace(
            config.state, home=str(root / "state"), workspace_roots=(str(workspace),)
        ),
        ollama=replace(config.ollama, url=f"http://127.0.0.1:{selected['port']}"),
        secrets=Secrets(api_key=secrets.token_hex(32)),
        private_source_paths=(str(root),),
    )
    # This endpoint belongs to this disposable runtime; no installed model
    # service, configured remote worker or model call is contacted.
    ollama_endpoint.configure_typed_endpoint(config.ollama.url)
    from ..platform import paths

    paths.configure_home(config.state.home)
    # Fixed disposable compatibility settings, not an entrypoint import or a
    # general environment/config bridge. Typed HTTP authority is bound below.
    os.environ.update(
        {
            "SONDER_HOST": "127.0.0.1",
            "SONDER_PORT": str(selected["port"]),
            "SONDER_AUTH_MODE": "api-key",
            "SONDER_API_KEY": config.secrets.api_key,
            "OLLAMA_HOST": config.ollama.url,
            "SONDER_OLLAMA_WORKERS": "",
            "SONDER_ALLOW_REMOTE_OLLAMA": "0",
        }
    )
    from ..adapters.persistence import migrations

    migrations.migrate_all(busy_timeout_ms=1000)
    from .app import default_app

    application = default_app(config=config)
    application.delegation_service()
    from ..interfaces.http import serve
    from ..adapters.web import lifecycle

    serve.configure_typed_config(config)
    lifecycle.configure(config)
    stopped = Event()
    watcher_failure = []

    def evidence(phase):
        write_json_atomic(
            root / ("runtime-" + job_id + ".json"),
            {
                "namespace": namespace,
                "job_id": job_id,
                "pid": os.getpid(),
                "process_identity": identity,
                "phase": phase,
                "selection": journal.status()["selection"],
            },
        )

    def control():
        ready = False
        try:
            while not stopped.wait(0.1):
                if not ready and serve.BOUND_PORT == selected["port"]:
                    evidence("READY")
                    ready = True
                pending = journal.pending()
                if pending is not None and pending.action == "stop":
                    lifecycle.get().drain("owned runtime stop")
                    return
        except BaseException as error:
            watcher_failure.append(type(error).__name__)
            lifecycle.get().drain("owned control unavailable")

    watcher = Thread(target=control, name="owned-runtime-control", daemon=True)
    watcher.start()
    try:
        serve.main(config=config)
    finally:
        stopped.set()
        watcher.join(2)
        application.close_providers(timeout=5)
    if watcher.is_alive() or watcher_failure:
        raise OwnerRefused("owned control cleanup is incomplete")
    evidence("CLEAN")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run(*sys.argv[1:]))
    except Exception as error:
        import traceback

        frames = traceback.extract_tb(error.__traceback__)
        location = ";".join(
            Path(frame.filename).name + ":" + str(frame.lineno) for frame in frames[-3:]
        )
        print(
            "owned runtime failed: " + type(error).__name__ + " " + location,
            file=sys.stderr,
        )
        raise SystemExit(1)

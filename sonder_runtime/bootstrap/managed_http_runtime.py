"""Fixed contained child for ManagedRuntimeOwner; no external capability issuer."""

from dataclasses import asdict, replace
import os
from pathlib import Path
import secrets
import sys
from threading import Event


def run(root, namespace, job_id):
    from ..application.compute_fabric.artifact_spool import PrivateDirectoryAnchor

    root = Path(root).absolute()
    workspace = root.parent / (root.name + "-workspace")
    with PrivateDirectoryAnchor(root) as anchor, PrivateDirectoryAnchor(
        workspace
    ) as workspace_anchor:
        anchor.validate()
        workspace_anchor.validate()
        return _run(root, workspace, namespace, job_id, anchor)


def _run(root, workspace, namespace, job_id, anchor):
    from ..adapters.persistence.owned_sqlite import (
        OwnedSQLiteConnections,
        install_disposable_owner,
    )

    sqlite = OwnedSQLiteConnections((str(root),), validate=anchor.validate)
    install_disposable_owner(sqlite)
    from .thread_resources import SQLiteThreadCleanup, install_disposable_thread_owner
    from ..platform.runtime_threads import OwnedRuntimeThreads

    cleanup = SQLiteThreadCleanup(sqlite)
    workers = OwnedRuntimeThreads(cleanup=cleanup)
    install_disposable_thread_owner(workers)
    from ..adapters.persistence.runtime_owner import SQLiteManagedRuntimeOwnerJournal
    from ..adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from ..adapters.process_liveness import process_identity
    from ..application.ports.runtime_owner import OwnerRefused
    from .managed_configuration import (
        read_configuration,
        COMPONENTS,
        CLOSE_ORDER,
        MANIFEST_DIGEST,
    )

    journal = SQLiteManagedRuntimeOwnerJournal(
        root / "owner.sqlite", namespace=namespace
    )
    pending = journal.pending()
    record = SQLiteDurableJobRegistry(root / "processes.sqlite").view(job_id)
    identity = process_identity(os.getpid())
    status = journal.status()
    if (
        pending is None
        or pending.action != "launch"
        or pending.operation_id != job_id
        or pending.namespace != namespace
        or pending.incarnation != status["incarnation"]
        or record.process_id != os.getpid()
        or not identity
        or dict(record.metadata).get("process_instance_identity") != identity
    ):
        raise OwnerRefused("exact managed process admission required")
    selected = journal.selected_config()
    descriptor = read_configuration(
        anchor,
        selected,
        root=root,
        namespace=namespace,
        incarnation=status["incarnation"],
    )
    from ..adapters.execution.runtime_payload import RuntimePayload

    artifact = RuntimePayload(root)
    try:
        artifact.validate((str(workspace),), expected=descriptor["artifact_digest"])
        if (
            pending.payload
            != __import__("json")
            .dumps(
                {"artifact_digest": artifact.digest},
                sort_keys=True,
                separators=(",", ":"),
            )
            .encode()
            or dict(record.metadata).get("runtime_artifact_digest") != artifact.digest
            or Path(sys.executable).resolve() != Path(artifact.manifest["executable"])
        ):
            raise OwnerRefused("actual runtime artifact admission differs")
    finally:
        artifact.close()
    from ..platform.config import SonderConfig, Secrets
    from ..platform.child_storage_config import ChildStorageConfig

    config = SonderConfig()
    config = replace(
        config,
        server=replace(
            config.server,
            host="127.0.0.1",
            port=descriptor["port"],
            auth_mode="api-key",
            request_timeout_seconds=descriptor["request_timeout_seconds"],
            stream_idle_timeout_seconds=descriptor["stream_idle_timeout_seconds"],
        ),
        state=replace(
            config.state, home=str(root / "state"), workspace_roots=(str(workspace),)
        ),
        ollama=replace(config.ollama, url=f"http://127.0.0.1:{descriptor['port']}"),
        secrets=Secrets(api_key=secrets.token_hex(32)),
        private_source_paths=(str(root),)
        + (
            (str(Path(descriptor["child_storage"]["binding_file"]).parent),)
            if descriptor["child_storage"]["backend"] == "postgresql"
            else ()
        ),
        child_storage=ChildStorageConfig(**descriptor["child_storage"]),
    )
    from ..platform import paths
    from ..adapters.inference import ollama_endpoint

    paths.configure_home(config.state.home)
    ollama_endpoint.configure_typed_endpoint(config.ollama.url)
    os.environ.update(
        {
            "SONDER_HOST": "127.0.0.1",
            "SONDER_PORT": str(descriptor["port"]),
            "SONDER_AUTH_MODE": "api-key",
            "SONDER_API_KEY": config.secrets.api_key,
            "OLLAMA_HOST": config.ollama.url,
            "SONDER_OLLAMA_WORKERS": "",
            "SONDER_ALLOW_REMOTE_OLLAMA": "0",
        }
    )
    from ..application.runtime_resources import (
        ApplicationResourceOwners,
        ComponentCloseProof,
    )

    resources = ApplicationResourceOwners(COMPONENTS, close_order=CLOSE_ORDER)

    def proof(name, closed, evidence):
        return ComponentCloseProof(name, bool(closed), evidence)

    def close_sqlite(resource, timeout):
        resource.stop_admissions()
        return proof(
            "sqlite", cleanup() and resource.snapshot().clean, "exact-sqlite-handles"
        )

    resources.initialize("sqlite", lambda: sqlite, close_sqlite)
    resources.initialize(
        "workers",
        lambda: workers,
        lambda resource, timeout: proof(
            "workers", resource.close(timeout=timeout).clean, "exact-worker-handles"
        ),
    )
    from ..adapters.persistence import migrations

    migrations.migrate_all(busy_timeout_ms=1000)
    from .app import (
        build_application,
        install_owned_application,
        stop_owned_application,
    )
    from .child_storage import HostChildRepositoryFactory
    from ..adapters.persistence.durable_continuation import (
        SQLiteDurableContinuationRepository,
    )

    def create_children():
        if config.child_storage.backend == "sqlite":
            return SQLiteDurableContinuationRepository(Path(descriptor["child_path"]))
        from ..adapters.persistence.postgres_binding import PostgresPrivateBinding
        from ..adapters.persistence.postgres_continuation import (
            PostgreSQLDurableContinuationRepository,
        )

        def roots():
            from ..adapters.filesystem.file_ops import allowed_roots

            return tuple(allowed_roots()) + config.state.workspace_roots

        binding = PostgresPrivateBinding(
            config.child_storage.binding_file, writable_roots=roots
        )
        try:
            return PostgreSQLDurableContinuationRepository(
                config.child_storage,
                binding,
                expected_storage_identity=descriptor["child_identity"],
            )
        except BaseException:
            binding.close()
            raise

    def construct():
        application = build_application(
            config=config,
            child_repository_factory=HostChildRepositoryFactory(
                config.child_storage.backend, create_children
            ),
        )
        install_owned_application(application)
        return application

    def close_application(application, timeout):
        return proof(
            "application",
            application.session_repository().close(timeout=timeout),
            "session-connections-closed",
        )

    application = resources.initialize("application", construct, close_application)
    from .managed_app_work import install_owned_app_work_slot, seal_owned_app_work

    install_owned_app_work_slot(application, resources, workers)
    application.session_repository()

    def close_children(application, timeout):
        application.close_delegation(timeout=timeout)
        return proof("child-storage", True, "runner-repository-close-proof")

    resources.initialize("child-storage", lambda: application, close_children)
    application.delegation_service()

    def close_providers(application, timeout):
        from time import monotonic

        deadline = monotonic() + timeout
        application.close_compute(timeout=timeout)
        # The concrete bundle unregisters each provider only after its typed
        # CleanupResult proves quiescence and release, and raises on any failure.
        application.specialized_providers.close(timeout=max(0, deadline - monotonic()))
        return proof("providers", True, "typed-provider-unregister")

    resources.initialize("providers", lambda: application, close_providers)
    from ..interfaces.http import serve
    from ..adapters.web import lifecycle
    from .managed_http import ManagedHTTPServer
    from ..adapters.filesystem.atomic_json import write_json_atomic

    serve.configure_typed_config(config)
    lifecycle.configure(config)
    stopped = Event()
    errors = []
    listener = []

    def factory(address, handler):
        seal_owned_app_work(application)
        server = resources.initialize(
            "http-sockets",
            lambda: ManagedHTTPServer(
                address,
                handler,
                workers=workers,
                request_timeout_seconds=descriptor["request_timeout_seconds"],
            ),
            lambda resource, timeout: proof(
                "http-sockets",
                resource.sockets_closed,
                "exact-listener-request-sockets",
            ),
        )
        listener.append(server)
        return server

    def evidence(phase, receipt=None):
        value = dict(
            namespace=namespace,
            incarnation=status["incarnation"],
            epoch=status["epoch"],
            job_id=job_id,
            pid=os.getpid(),
            process_identity=identity,
            phase=phase,
            selection=status["selection"],
            manifest=MANIFEST_DIGEST,
            artifact_digest=descriptor["artifact_digest"],
        )
        if receipt is not None:
            value["components"] = [asdict(item) for item in receipt.components]
        write_json_atomic(root / ("runtime-" + job_id + ".json"), value)

    def control():
        ready = False
        try:
            while not stopped.wait(0.1):
                if not ready and listener and serve.BOUND_PORT == descriptor["port"]:
                    evidence("READY")
                    ready = True
                command = journal.pending()
                if command is not None and command.action == "stop":
                    lifecycle.get().drain("owned runtime stop")
                    return
        except BaseException:
            errors.append("control-failed")
            lifecycle.get().drain("owned control unavailable")

    watcher = workers.thread(
        target=control, name="managed-runtime-control", daemon=True
    )
    watcher.start()
    try:
        serve.main(
            config=config, _server_factory=factory, _close_default_resources=False
        )
    finally:
        stopped.set()
        lifecycle.get().stop_probe()
        stop_owned_application(application)
        receipt = resources.close(timeout=15)
    if errors or not receipt.clean:
        evidence("UNCLEAN", receipt)
        raise OwnerRefused("managed application cleanup is incomplete")
    evidence("CLEAN", receipt)
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
            "managed runtime failed: " + type(error).__name__ + " " + location,
            file=sys.stderr,
        )
        raise SystemExit(1)

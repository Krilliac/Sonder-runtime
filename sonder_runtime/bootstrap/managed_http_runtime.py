"""Fixed contained child for ManagedRuntimeOwner; no external capability issuer."""

from dataclasses import asdict, replace
import os
from pathlib import Path
import secrets
import sys
from threading import Event
from time import monotonic


def _configure_legacy_http_boundary(application, config, *, serve):
    """Bind the compatibility handlers to the already-owned application.

    The contained HTTP child uses the canonical ``serve`` adapter, but that
    adapter still exposes a few legacy route implementations through its
    explicit injection seam.  Leaving the seam empty makes the listener look
    healthy while every chat/model route fails closed on first use.  Bind it
    only after the exact owned :class:`Application` exists; this mirrors the
    normal ``cmd_serve`` bootstrap and does not create another application or
    widen any permission surface.
    """
    from .legacy_interfaces import (
        configure_legacy_application,
        configure_legacy_capacity,
        configure_legacy_interfaces,
    )

    configure_legacy_interfaces()
    configure_legacy_application(application)
    configure_legacy_capacity(
        autopilot_runs=config.capacity.autopilot_runs,
        fleet_workers=config.capacity.fleet_workers,
        training_jobs=config.capacity.training_jobs,
    )
    from ..interfaces.http.handlers import RecallHandler, OutcomeHandler

    serve.configure_thin_handlers(
        {
            "/v1/recall": RecallHandler(application.memory),
            "/v1/outcome": OutcomeHandler(application.memory),
        }
    )


def _install_owned_app_work_if_enabled(application, config, *, serve):
    """Compose the app-work dispatcher only for an explicitly enabled child.

    ``configure_typed_config`` owns the app-control binding, while the child
    owns the application and the app-work slot.  Keeping this composition in
    the managed startup path makes the identity handoff explicit: a missing
    control binding or an installer that does not publish the exact returned
    service aborts before listener publication.  The default managed profile
    keeps app control disabled, so this hook cannot silently add a new
    externally reachable authority.
    """
    if not config.app_control.enabled:
        return None
    control = getattr(serve, "_APP_CONTROL_BINDING", None)
    if control is None:
        from ..application.ports.runtime_owner import OwnerRefused

        raise OwnerRefused("typed app-control binding unavailable for managed work")
    from .app_managed_work_http import install_owned_work_http
    from .legacy_interfaces import legacy_runtime
    from ..adapters.security.permission_policy import PermissionPolicyProvider

    binding = install_owned_work_http(
        control,
        application=application,
        runtime=legacy_runtime(),
        permission_engine=PermissionPolicyProvider(),
    )
    if getattr(control, "_work_binding", None) is not binding:
        from ..application.ports.runtime_owner import OwnerRefused

        raise OwnerRefused("owned app-work binding publication was not exact")
    return binding


def _close_managed_provider_graph(application, *, timeout):
    """Close graph-owned resources while child storage owns delegation.

    The managed resource ledger closes durable child storage first, so calling
    ``Application.close_providers`` here would repeat delegation shutdown.
    Keep the remaining graph lifecycle in its authoritative order and attempt
    every closer even when an earlier one fails.
    """
    started = monotonic()

    def remaining():
        return max(0, timeout - (monotonic() - started))

    try:
        artifact_close = getattr(application, "close_artifact_mobility", None)
        if artifact_close is not None:
            artifact_close()
    finally:
        try:
            compute_close = getattr(application, "close_compute", None)
            if compute_close is not None:
                compute_close(timeout=remaining())
        finally:
            try:
                replication = getattr(application, "memory_replication", None)
                if replication is not None:
                    replication.close()
            finally:
                try:
                    # Membership shutdown stops refresh, not request admission.
                    # Drain the exact graph pool first so a failing owned child
                    # cannot retain a live inference authority during teardown.
                    pool = getattr(application, "inference_pool", None)
                    drain = getattr(pool, "drain", None)
                    if callable(drain) and not drain(timeout_seconds=min(30, remaining())):
                        raise TimeoutError("inference worker pool has not drained")
                finally:
                    try:
                        controller = getattr(application, "inference_membership", None)
                        if controller is not None and not controller.close(
                            timeout=min(30, remaining())
                        ):
                            raise TimeoutError("inference membership refresh has not stopped")
                    finally:
                        application.specialized_providers.close(timeout=remaining())


def _stop_configured_lifecycle_probe(lifecycle_module) -> bool:
    """Stop the selected lifecycle probe and report whether it actually ended.

    A managed owner writes a clean resource receipt only after every resource
    it owns has stopped.  Older compatibility lifecycle doubles return
    ``None`` from ``stop_probe``; retain that successful convention while a
    modern explicit ``False`` remains an unclean, observable result.
    """
    instance = getattr(lifecycle_module, "_instance", None)
    if instance is None:
        return True
    stop_probe = getattr(instance, "stop_probe", None)
    if not callable(stop_probe):
        return True
    return stop_probe() is not False


def _finish_managed_runtime(
    *,
    errors,
    cleanup_errors,
    receipt,
    evidence,
    primary_error,
    primary_traceback,
) -> int:
    """Write a terminal receipt before preserving the primary startup error.

    A managed child must leave durable evidence for both ordinary cleanup
    failures and a startup/bind exception that triggered an otherwise clean
    rollback.  Re-raising the original exception keeps the caller's causal
    error and traceback authoritative; the receipt records the separate
    runtime ownership outcome.
    """
    unclean = (
        primary_error is not None
        or bool(errors)
        or bool(cleanup_errors)
        or receipt is None
        or not receipt.clean
    )
    if unclean:
        try:
            evidence("UNCLEAN", receipt)
        except BaseException as error:
            cleanup_errors.append("evidence-" + type(error).__name__)
        if primary_error is not None:
            if primary_traceback is not None:
                raise primary_error.with_traceback(primary_traceback)
            raise primary_error
        from ..application.ports.runtime_owner import OwnerRefused

        raise OwnerRefused("managed application cleanup is incomplete")
    evidence("CLEAN", receipt)
    return 0


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
    from ..adapters.filesystem.atomic_json import write_json_atomic
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
    application = None
    owned_application = False
    stopped = None
    watcher = None
    watcher_started = False
    lifecycle = None
    lifecycle_configured = False
    receipt = None
    errors = []
    cleanup_errors = []
    primary_error = None
    primary_traceback = None

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

    # The ledger exists before any resource can be admitted.  Keep one cleanup
    # owner around every startup step, including migration and listener setup:
    # a child that fails before serve.main must not strand a graph or SQLite
    # handle merely because the normal serving finalizer was never entered.
    try:
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
            graph = build_application(
                config=config,
                child_repository_factory=HostChildRepositoryFactory(
                    config.child_storage.backend, create_children
                ),
            )
            try:
                install_owned_application(graph)
            except BaseException:
                # initialize() records a failed factory as unresolved and
                # cannot call its closer, so this fresh graph owns its own
                # rollback until installation has been published.
                graph.close_providers(timeout=5)
                raise
            return graph

        def close_application(graph, timeout):
            return proof(
                "application",
                graph.session_repository().close(timeout=timeout),
                "session-connections-closed",
            )

        application = resources.initialize("application", construct, close_application)
        owned_application = True
        from .managed_app_work import install_owned_app_work_slot, seal_owned_app_work

        def close_children(graph, timeout):
            graph.close_delegation(timeout=timeout)
            return proof("child-storage", True, "runner-repository-close-proof")

        def close_providers(graph, timeout):
            _close_managed_provider_graph(graph, timeout=timeout)
            return proof("providers", True, "typed-provider-unregister")

        # Register every graph closer before any late startup work can fail.
        resources.initialize("child-storage", lambda: application, close_children)
        resources.initialize("providers", lambda: application, close_providers)
        install_owned_app_work_slot(application, resources, workers)
        application.session_repository()
        application.delegation_service()

        from ..interfaces.http import serve
        from ..adapters.web import lifecycle
        from .managed_http import ManagedHTTPServer

        _configure_legacy_http_boundary(application, config, serve=serve)
        lifecycle.configure(config)
        lifecycle_configured = True
        stopped = Event()
        listener = []

        def finish_http_configuration(graph):
            if graph is not application:
                raise OwnerRefused("managed HTTP graph selection changed during startup")
            _install_owned_app_work_if_enabled(application, config, serve=serve)

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
        watcher_started = True
        serve.main(
            config=config,
            _server_factory=factory,
            _close_default_resources=False,
            _after_configure=finish_http_configuration,
        )
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__
    finally:
        if stopped is not None:
            stopped.set()
        if watcher_started:
            try:
                watcher.join(2)
            except BaseException as error:
                cleanup_errors.append("watcher-" + type(error).__name__)
        if lifecycle_configured:
            try:
                if not _stop_configured_lifecycle_probe(lifecycle):
                    cleanup_errors.append("probe-incomplete")
            except BaseException as error:
                cleanup_errors.append("probe-" + type(error).__name__)
        if owned_application:
            try:
                stop_owned_application(application)
            except BaseException as error:
                cleanup_errors.append("owned-" + type(error).__name__)
        try:
            receipt = resources.close(timeout=15)
        except BaseException as error:
            cleanup_errors.append("resources-" + type(error).__name__)

    return _finish_managed_runtime(
        errors=errors,
        cleanup_errors=cleanup_errors,
        receipt=receipt,
        evidence=evidence,
        primary_error=primary_error,
        primary_traceback=primary_traceback,
    )


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

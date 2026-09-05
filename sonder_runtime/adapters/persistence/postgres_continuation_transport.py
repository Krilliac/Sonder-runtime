"""Optional PostgreSQL driver ownership and externally bounded transactions."""

from threading import BoundedSemaphore, Event, Lock, Thread, local
import time

from ...application.ports.continuation_mutations import (
    ContinuationStorageFailure,
    ContinuationCommitAmbiguous,
    ContinuationReceiptCapacity,
)


class PostgresAdmissionUnavailable(ContinuationStorageFailure):
    pass


class PostgresContinuationTransport:
    def __init__(self, config, binding):
        try:
            import psycopg
            import psycopg_pool
            from psycopg_pool import ConnectionPool
        except ImportError:
            raise ContinuationStorageFailure(
                "optional PostgreSQL driver is unavailable"
            ) from None
        if not psycopg.capabilities.has_cancel_safe():
            raise ContinuationStorageFailure(
                "PostgreSQL requires bounded safe cancellation support"
            )
        if psycopg.__version__ != "3.3.5" or psycopg_pool.__version__ != "3.3.0":
            raise ContinuationStorageFailure(
                "PostgreSQL driver versions require reviewed lifecycle compatibility"
            )
        self.driver = psycopg
        self.config = config
        self.binding = binding
        self._slots = BoundedSemaphore(config.pool_size)
        self._lock = Lock()
        self._active = set()
        self._inflight = {}
        self._reserved = set()
        self._closed = False
        self._operation_context = local()

        class PrivateConnection(psycopg.Connection):
            @classmethod
            def connect(cls, *args, **kwargs):
                try:
                    return super().connect(*args, **kwargs)
                except psycopg.Error:
                    raise psycopg.OperationalError(
                        "configured PostgreSQL connection unavailable"
                    ) from None

        self.connection_class = PrivateConnection
        self.pool = ConnectionPool(
            connection_class=PrivateConnection,
            kwargs=lambda: binding.connection_kwargs(config),
            min_size=0,
            max_size=config.pool_size,
            max_waiting=1,
            timeout=config.operation_timeout_seconds,
            num_workers=1,
            open=False,
        )
        self.pool.open()
        # Pinned psycopg_pool 3.3.0 has no public thread-termination result.
        # Capture only this pool's exact handles, before exposing the transport.
        workers = getattr(self.pool, "_workers", None)
        scheduler = getattr(self.pool, "_sched_runner", None)
        if (
            not isinstance(workers, list)
            or len(workers) != 1
            or not isinstance(scheduler, Thread)
            or not all(isinstance(worker, Thread) for worker in workers)
            or scheduler is workers[0]
        ):
            self.pool.close(timeout=0)
            raise ContinuationStorageFailure(
                "PostgreSQL pool ownership structure is unsupported"
            )
        self._pool_threads = tuple(workers) + (scheduler,)

    def run(
        self, function, *, prepared=None, connection=None, shutdown=False, timeout=None
    ):
        operation_timeout = self.config.operation_timeout_seconds
        if timeout is not None:
            # Leave room for exact-session cancellation and cleanup observation.
            operation_timeout = min(
                operation_timeout, timeout - 2 * self.config.cancel_timeout_seconds
            )
            if operation_timeout <= 0:
                raise PostgresAdmissionUnavailable(
                    "PostgreSQL shutdown deadline has no transaction budget"
                )
        with self._lock:
            if (
                (self._closed and not shutdown)
                or (prepared is not None and prepared.operation_id in self._reserved)
                or not self._slots.acquire(blocking=False)
            ):
                raise PostgresAdmissionUnavailable(
                    "PostgreSQL operation admission unavailable"
                )
            if prepared is not None:
                self._reserved.add(prepared.operation_id)
        deadline = time.monotonic() + operation_timeout
        done, cancel_done = Event(), Event()
        state_lock = Lock()
        state = {
            "connection": connection,
            "expired": False,
            "finished": False,
            "cancel_active": False,
        }
        outcome = {}

        def work():
            leased = connection
            failed = False
            notices = []

            def boundary():
                with state_lock:
                    expired = state["expired"]
                if expired or notices or time.monotonic() >= deadline:
                    raise ContinuationStorageFailure(
                        "PostgreSQL transaction boundary requires reconciliation"
                    )
                self.binding.validate()

            self._operation_context.boundary = boundary

            def notice(value):
                if (value.sqlstate or "").startswith(
                    "01"
                ) or value.severity_nonlocalized == "WARNING":
                    notices.append(True)

            try:
                self.binding.validate()
                if leased is None:
                    leased = self.pool.getconn(
                        timeout=max(0.001, deadline - time.monotonic())
                    )
                with state_lock:
                    state["connection"] = leased
                    expired = state["expired"]
                if expired or time.monotonic() >= deadline:
                    raise ContinuationStorageFailure(
                        "PostgreSQL operation deadline expired"
                    )
                leased.add_notice_handler(notice)
                outcome["value"] = function(leased)
                if notices:
                    raise ContinuationStorageFailure(
                        "PostgreSQL replication acknowledgement unresolved"
                    )
            except BaseException as error:
                failed = True
                outcome["error"] = error
            finally:
                with state_lock:
                    expired = state["expired"]
                    wait_cancel = state["cancel_active"]
                    if not wait_cancel:
                        state["finished"] = True
                if wait_cancel:
                    cancel_done.wait()  # Slot remains occupied until cancellation ends.
                cleanup_proven = True
                if leased is not None:
                    try:
                        leased.remove_notice_handler(notice)
                    except Exception:
                        failed = True
                    try:
                        if (
                            failed
                            or expired
                            or leased.info.transaction_status
                            != self.driver.pq.TransactionStatus.IDLE
                        ):
                            leased.close()
                            cleanup_proven = leased.closed
                    except Exception:
                        cleanup_proven = False
                        outcome["error"] = ContinuationStorageFailure(
                            "PostgreSQL connection cleanup unresolved"
                        )
                    finally:
                        if connection is None and cleanup_proven:
                            try:
                                self.pool.putconn(leased)
                            except Exception:
                                cleanup_proven = False
                                outcome["error"] = ContinuationStorageFailure(
                                    "PostgreSQL pool cleanup unresolved"
                                )
                with state_lock:
                    state["finished"] = True
                if cleanup_proven:
                    with self._lock:
                        self._active.discard(done)
                        if prepared is not None:
                            self._inflight.pop(prepared.operation_id, None)
                            self._reserved.discard(prepared.operation_id)
                        self._slots.release()
                    done.set()

        with self._lock:
            self._active.add(done)
            if prepared is not None:
                self._inflight[prepared.operation_id] = done
        try:
            Thread(target=work, name="child-postgres-operation", daemon=True).start()
        except Exception:
            with self._lock:
                self._active.discard(done)
                if prepared is not None:
                    self._inflight.pop(prepared.operation_id, None)
                    self._reserved.discard(prepared.operation_id)
                self._slots.release()
            raise ContinuationStorageFailure(
                "PostgreSQL worker admission unavailable"
            ) from None
        if not done.wait(max(0, deadline - time.monotonic())):
            with state_lock:
                state["expired"] = True
                leased = None if state["finished"] else state["connection"]
                state["cancel_active"] = leased is not None
            try:
                if leased is not None:
                    leased.cancel_safe(timeout=self.config.cancel_timeout_seconds)
            except Exception:
                pass  # Unknown cancellation is never success or connection release.
            finally:
                cancel_done.set()
            done.wait(self.config.cancel_timeout_seconds)
            self._failure(
                prepared, "PostgreSQL operation deadline requires reconciliation"
            )
        error = outcome.get("error")
        if error is not None:
            from ...application.ports.subagents import InvalidSubagentRequest

            if isinstance(
                error,
                (
                    InvalidSubagentRequest,
                    ContinuationCommitAmbiguous,
                    ContinuationReceiptCapacity,
                ),
            ):
                raise error
            if prepared is None and isinstance(error, ContinuationStorageFailure):
                raise error
            self._failure(prepared, "PostgreSQL storage operation unavailable")
        return outcome["value"]

    def require_effect_boundary(self):
        """Prevent another transaction after cancellation of an earlier COMMIT."""
        boundary = getattr(self._operation_context, "boundary", None)
        if boundary is None:
            raise ContinuationStorageFailure(
                "PostgreSQL operation context is unavailable"
            )
        boundary()

    @staticmethod
    def _failure(prepared, message):
        if prepared is not None:
            raise ContinuationCommitAmbiguous(prepared) from None
        raise ContinuationStorageFailure(message) from None

    def quiescent(self):
        with self._lock:
            return not self._active

    def require_reconcilable(self, prepared):
        with self._lock:
            if prepared.operation_id in self._reserved:
                raise ContinuationStorageFailure(
                    "original PostgreSQL operation cleanup is unresolved"
                )

    def stop_admissions(self):
        with self._lock:
            self._closed = True

    def close(self, timeout=5):
        with self._lock:
            self._closed = True
            active = tuple(self._active)
        deadline = time.monotonic() + max(0, timeout)
        for done in active:
            done.wait(max(0, deadline - time.monotonic()))
        if not self.quiescent():
            return False
        self.pool.close(timeout=0)
        for worker in self._pool_threads:
            worker.join(max(0, deadline - time.monotonic()))
        return not any(worker.is_alive() for worker in self._pool_threads)

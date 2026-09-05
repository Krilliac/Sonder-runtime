"""Single child-owned dispatcher slot, installed before HTTP publication."""

from threading import RLock
from time import monotonic

from ..application.ports.runtime_owner import OwnerRefused, OwnerUnsupported
from ..application.runtime_resources import (
    ApplicationResourceOwners,
    ComponentCloseProof,
)
from ..platform.runtime_threads import OwnedRuntimeThreads

_LOCK = RLock()
_SLOT = None


def _current(application):
    from .app import Application, _owned_default_application, _application_lifecycle

    if (
        type(application) is not Application
        or application is not _owned_default_application
    ):
        raise OwnerRefused("exact owned Application required")
    try:
        observed = _application_lifecycle.get()
    except RuntimeError:
        raise OwnerRefused("owned Application admission is closed") from None
    if observed is not application:
        raise OwnerRefused("owned Application admission changed")


class _AppWorkSlot:
    def __init__(self, application, workers):
        self.application, self.workers = application, workers
        self.dispatcher = None
        self.recovery = self._recovery_lease = None
        self._recovery_thread = None
        self._recovery_done = False
        self.sealed = self.closed = False
        self.lock = RLock()
        self._drain_thread = None
        self._drain_succeeded = False
        self._lease = None

    def register(self, dispatcher):
        from .app_managed_work import AppManagedWorkDispatcher

        with self.lock:
            _current(self.application)
            if self.closed or self.sealed or self.dispatcher is not None:
                raise OwnerRefused("owned app work registration is closed")
            if (
                type(dispatcher) is not AppManagedWorkDispatcher
                or dispatcher.application is not self.application
            ):
                raise OwnerRefused("dispatcher Application identity differs")
            if dispatcher._closed:
                raise OwnerRefused("dispatcher admission is already closed")
            if not self.workers.owns_pool(dispatcher._executor):
                raise OwnerRefused("dispatcher executor is not owned by this runtime")
            self.dispatcher = dispatcher
            self._lease = OwnedAppWorkRegistration(self, dispatcher)
            return self._lease

    def register_recovery(self, registry):
        from .app_work_recovery_registry import AppWorkRecoveryRegistry

        with self.lock:
            dispatcher = self.require()
            if self.sealed or self.recovery is not None:
                raise OwnerRefused("recovery registration is closed")
            if (
                type(registry) is not AppWorkRecoveryRegistry
                or registry.application is not self.application
                or registry._authority is not dispatcher.authority
                or registry._closed
                or not self.workers.owns_pool(registry._executor)
                or registry._executor is dispatcher._executor
            ):
                raise OwnerRefused(
                    "exact independently owned recovery registry required"
                )
            self.recovery = registry
            self._recovery_lease = OwnedAppRecoveryRegistration(self, registry)
            return self._recovery_lease

    def require_recovery(self):
        with self.lock:
            self.require()
            if self.recovery is None:
                raise OwnerUnsupported("owned app recovery is not configured")
            if self.recovery._closed:
                raise OwnerRefused("owned app recovery is closed")
            return self.recovery

    def _close_recovery(self):
        if self.recovery is not None:
            self.recovery.close()

    def require(self):
        with self.lock:
            _current(self.application)
            if self.closed:
                raise OwnerRefused("owned app work admission is closed")
            if self.dispatcher is None:
                raise OwnerUnsupported("owned app work is not configured")
            if self.dispatcher._closed:
                raise OwnerRefused("dispatcher admission is closed")
            if self.dispatcher.application is not self.application:
                raise OwnerRefused("dispatcher Application identity changed")
            return self.dispatcher

    def _drain(self, timeout):
        from .app_managed_work import AppManagedWorkDispatcher

        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError("bounded drain timeout required")
        deadline = monotonic() + timeout
        with self.lock:
            dispatcher = self.dispatcher
            if dispatcher is None:
                return True
            if dispatcher.application is not self.application:
                raise OwnerRefused("dispatcher cleanup Application identity changed")
            AppManagedWorkDispatcher.stop_admissions(dispatcher)
            if self.recovery is not None:
                self.recovery.stop_admissions()
            if self._drain_thread is None:

                def drain():
                    try:
                        self._close_recovery()
                        AppManagedWorkDispatcher.close(dispatcher, cancel_pending=True)
                    except BaseException:
                        return
                    self._drain_succeeded = True

                # Retained by both the slot and the concrete worker owner. A
                # timed-out Python callback is never represented as terminated.
                self._drain_thread = self.workers.thread(
                    target=drain, name="app-work-drain"
                )
                self._drain_thread.start()
            thread = self._drain_thread
        thread.join(max(0, deadline - monotonic()))
        return (
            not thread.is_alive() and self._drain_succeeded and monotonic() <= deadline
        )

    def close(self, timeout):
        with self.lock:
            self.closed = True
        done = self._drain(timeout)
        return ComponentCloseProof(
            "app-work",
            done,
            "dispatcher-local-drain" if done else "dispatcher-drain-unresolved",
        )


class OwnedAppWorkRegistration:
    """Issuer-bound startup lease; no request can manufacture slot ownership."""

    def __init__(self, slot, dispatcher):
        self._slot, self._dispatcher = slot, dispatcher
        self._committed = False

    def commit(self):
        slot = self._slot
        with slot.lock:
            _current(slot.application)
            if (
                slot.closed
                or slot.sealed
                or slot._drain_thread is not None
                or slot._lease is not self
                or slot.dispatcher is not self._dispatcher
            ):
                raise OwnerRefused("registration lease is no longer current")
            self._committed = True

    def rollback(self, timeout=2):
        slot = self._slot
        with slot.lock:
            if (
                slot.closed
                or slot.sealed
                or self._committed
                or slot._lease is not self
                or slot.dispatcher is not self._dispatcher
            ):
                raise OwnerRefused("registration rollback is unavailable")
        if not slot._drain(timeout):
            raise OwnerRefused("registration cleanup remains unresolved")
        with slot.lock:
            if slot.closed or slot.sealed or self._committed or slot._lease is not self:
                raise OwnerRefused("registration changed during cleanup")
            slot.dispatcher = slot._lease = slot._drain_thread = None
            slot._drain_succeeded = False


class OwnedAppRecoveryRegistration:
    """Exact unpublished recovery registration; unresolved cleanup retains slot."""

    def __init__(self, slot, registry):
        self._slot, self._registry = slot, registry
        self._committed = False

    def _current(self):
        slot = self._slot
        if (
            slot.closed
            or slot.sealed
            or slot._recovery_lease is not self
            or slot.recovery is not self._registry
        ):
            raise OwnerRefused("recovery registration lease is no longer current")

    def commit(self):
        slot = self._slot
        with slot.lock:
            _current(slot.application)
            self._current()
            if slot._recovery_thread is not None:
                raise OwnerRefused("recovery cleanup has already started")
            self._committed = True

    def rollback(self, timeout=2):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise ValueError("bounded recovery drain timeout required")
        slot = self._slot
        deadline = monotonic() + timeout
        with slot.lock:
            self._current()
            if self._committed:
                raise OwnerRefused("recovery registration is already committed")
            self._registry.stop_admissions()
            if slot._recovery_thread is None or (
                not slot._recovery_thread.is_alive() and not slot._recovery_done
            ):

                def drain():
                    try:
                        self._registry.close()
                    except BaseException:
                        return
                    slot._recovery_done = True

                slot._recovery_thread = slot.workers.thread(
                    target=drain, name="app-recovery-rollback"
                )
                slot._recovery_thread.start()
            thread = slot._recovery_thread
        thread.join(max(0, deadline - monotonic()))
        with slot.lock:
            self._current()
            if thread.is_alive() or not slot._recovery_done or monotonic() > deadline:
                raise OwnerRefused("recovery registration cleanup remains unresolved")
            slot.recovery = slot._recovery_lease = slot._recovery_thread = None
            slot._recovery_done = False


def install_owned_app_work_slot(application, resources, workers):
    """Private managed child composition only, before listener construction."""
    global _SLOT
    if (
        type(resources) is not ApplicationResourceOwners
        or type(workers) is not OwnedRuntimeThreads
    ):
        raise OwnerRefused("exact owned resource manifest and workers required")
    from .managed_configuration import MANIFEST_DIGEST

    if resources.manifest_digest != MANIFEST_DIGEST:
        raise OwnerRefused("fixed managed resource manifest required")
    with _LOCK:
        if _SLOT is not None:
            raise OwnerRefused("owned app work slot is already installed")
        _current(application)
        slot = resources.initialize(
            "app-work",
            lambda: _AppWorkSlot(application, workers),
            lambda resource, timeout: resource.close(timeout),
        )
        if (
            type(slot) is not _AppWorkSlot
            or slot.application is not application
            or slot.workers is not workers
        ):
            raise OwnerRefused("app work resource slot already has a different owner")
        _SLOT = slot


def _slot(application):
    with _LOCK:
        if _SLOT is None:
            raise OwnerUnsupported("runtime has no owned app work slot")
        if _SLOT.application is not application:
            raise OwnerRefused("owned app work Application differs")
        return _SLOT


def register_owned_app_work(application, dispatcher):
    return _slot(application).register(dispatcher)


def require_owned_app_work(application):
    return _slot(application).require()


def register_owned_app_recovery(application, registry):
    return _slot(application).register_recovery(registry)


def require_owned_app_recovery(application):
    return _slot(application).require_recovery()


def seal_owned_app_work(application):
    slot = _slot(application)
    with slot.lock:
        _current(application)
        if slot.closed:
            raise OwnerRefused("owned app work slot is closed")
        if slot._lease is not None and not slot._lease._committed:
            raise OwnerRefused("app work registration remains incomplete")
        if slot._recovery_lease is not None and not slot._recovery_lease._committed:
            raise OwnerRefused("recovery registration remains incomplete")
        slot.sealed = True

"""Single child-owned dispatcher slot, installed before HTTP publication."""
from threading import RLock
from time import monotonic

from ..application.ports.runtime_owner import OwnerRefused, OwnerUnsupported
from ..application.runtime_resources import ApplicationResourceOwners, ComponentCloseProof
from ..platform.runtime_threads import OwnedRuntimeThreads

_LOCK = RLock()
_SLOT = None


def _current(application):
    from .app import Application, _owned_default_application, _application_lifecycle
    if type(application) is not Application or application is not _owned_default_application:
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
            if type(dispatcher) is not AppManagedWorkDispatcher or dispatcher.application is not self.application:
                raise OwnerRefused("dispatcher Application identity differs")
            if dispatcher._closed:
                raise OwnerRefused("dispatcher admission is already closed")
            if not self.workers.owns_pool(dispatcher._executor):
                raise OwnerRefused("dispatcher executor is not owned by this runtime")
            self.dispatcher = dispatcher
            self._lease = OwnedAppWorkRegistration(self, dispatcher)
            return self._lease

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
            if self._drain_thread is None:
                def drain():
                    try:
                        AppManagedWorkDispatcher.close(dispatcher, cancel_pending=True)
                    except BaseException:
                        return
                    self._drain_succeeded = True
                # Retained by both the slot and the concrete worker owner. A
                # timed-out Python callback is never represented as terminated.
                self._drain_thread = self.workers.thread(target=drain, name="app-work-drain")
                self._drain_thread.start()
            thread = self._drain_thread
        thread.join(max(0, deadline - monotonic()))
        return not thread.is_alive() and self._drain_succeeded and monotonic() <= deadline

    def close(self, timeout):
        with self.lock:
            self.closed = True
        done = self._drain(timeout)
        return ComponentCloseProof("app-work", done,
            "dispatcher-local-drain" if done else "dispatcher-drain-unresolved")


class OwnedAppWorkRegistration:
    """Issuer-bound startup lease; no request can manufacture slot ownership."""
    def __init__(self, slot, dispatcher):
        self._slot, self._dispatcher = slot, dispatcher
        self._committed = False

    def commit(self):
        slot = self._slot
        with slot.lock:
            _current(slot.application)
            if slot.closed or slot.sealed or slot._drain_thread is not None or slot._lease is not self or slot.dispatcher is not self._dispatcher:
                raise OwnerRefused("registration lease is no longer current")
            self._committed = True

    def rollback(self, timeout=2):
        slot = self._slot
        with slot.lock:
            if slot.closed or slot.sealed or self._committed or slot._lease is not self or slot.dispatcher is not self._dispatcher:
                raise OwnerRefused("registration rollback is unavailable")
        if not slot._drain(timeout):
            raise OwnerRefused("registration cleanup remains unresolved")
        with slot.lock:
            if slot.closed or slot.sealed or self._committed or slot._lease is not self:
                raise OwnerRefused("registration changed during cleanup")
            slot.dispatcher = slot._lease = slot._drain_thread = None
            slot._drain_succeeded = False


def install_owned_app_work_slot(application, resources, workers):
    """Private managed child composition only, before listener construction."""
    global _SLOT
    if type(resources) is not ApplicationResourceOwners or type(workers) is not OwnedRuntimeThreads:
        raise OwnerRefused("exact owned resource manifest and workers required")
    from .managed_configuration import MANIFEST_DIGEST
    if resources.manifest_digest != MANIFEST_DIGEST:
        raise OwnerRefused("fixed managed resource manifest required")
    with _LOCK:
        if _SLOT is not None:
            raise OwnerRefused("owned app work slot is already installed")
        _current(application)
        slot = resources.initialize("app-work", lambda: _AppWorkSlot(application, workers),
            lambda resource, timeout: resource.close(timeout))
        if type(slot) is not _AppWorkSlot or slot.application is not application or slot.workers is not workers:
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


def seal_owned_app_work(application):
    slot = _slot(application)
    with slot.lock:
        _current(application)
        if slot.closed:
            raise OwnerRefused("owned app work slot is closed")
        if slot._lease is not None and not slot._lease._committed:
            raise OwnerRefused("app work registration remains incomplete")
        slot.sealed = True

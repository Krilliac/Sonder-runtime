"""Host-injected worker factories; application code does not own OS threads."""
from concurrent.futures import ThreadPoolExecutor as _NativePool
from dataclasses import dataclass
from threading import RLock, Thread as _NativeThread


@dataclass(frozen=True)
class RuntimeWorkerFactories:
    thread: object
    pool: object

    def __post_init__(self):
        if not callable(self.thread) or not callable(self.pool):
            raise TypeError("callable worker factories required")


_FACTORIES = None
_NATIVE_USED = False
_INSTALL_LOCK = RLock()


def install_disposable_factories(factories):
    global _FACTORIES
    with _INSTALL_LOCK:
        if type(factories) is not RuntimeWorkerFactories or _FACTORIES is not None or _NATIVE_USED:
            raise RuntimeError("worker factories require unused disposable process composition")
        _FACTORIES = factories


def Thread(*args, **kwargs):
    global _NATIVE_USED
    with _INSTALL_LOCK:
        factories = _FACTORIES
        if factories is None:
            _NATIVE_USED = True
    return _NativeThread(*args, **kwargs) if factories is None else factories.thread(*args, **kwargs)


def ThreadPoolExecutor(*args, **kwargs):
    global _NATIVE_USED
    with _INSTALL_LOCK:
        factories = _FACTORIES
        if factories is None:
            _NATIVE_USED = True
    return _NativePool(*args, **kwargs) if factories is None else factories.pool(*args, **kwargs)

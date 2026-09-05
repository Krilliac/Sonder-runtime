"""Explicit opt-in worker ownership for a required-new runtime process.

Public executor initialization/shutdown hooks supply worker identity and exit
proof. No threading monkeypatch, private executor fields or thread termination.
"""
from concurrent.futures import ThreadPoolExecutor as _NativePool
from dataclasses import dataclass
import inspect
import math
from threading import RLock, Thread as _NativeThread, current_thread
from time import monotonic


class ThreadOwnershipRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class ThreadOwnershipSnapshot:
    live_threads: int
    active_tasks: int
    incomplete_pools: int
    unresolved: bool
    admission_stopped: bool

    @property
    def clean(self):
        return self.admission_stopped and not (self.live_threads or self.active_tasks or self.incomplete_pools or self.unresolved)


class _ManagedThread(_NativeThread):
    def __init__(self, owner, *args, **kwargs):
        self._owner = owner
        self._owner_started = False
        self._owner_finished = False
        super().__init__(*args, **kwargs)

    def start(self):
        with self._owner._lock:
            self._owner._admit()
            if self._owner_started:
                raise RuntimeError("threads can only be started once")
            self._owner_started = True
        try:
            super().start()
        except BaseException:
            self._owner._failed()
            raise

    def run(self):
        try:
            with self._owner._lock:
                if self._owner._stopped:
                    return
            super().run()
        finally:
            self._owner._cleanup()
            with self._owner._lock:
                self._owner_finished = True


class _ManagedPool(_NativePool):
    def __init__(self, owner, *args, **kwargs):
        options = inspect.signature(_NativePool).bind(*args, **kwargs)
        options.apply_defaults()
        initializer = options.arguments.get("initializer")
        initargs = options.arguments.get("initargs", ())
        self._owner = owner
        self._worker_threads = set()
        self._admission_stopped = False
        self._shutdown_complete = False
        self._shutdown_thread = None
        def initialize():
            with owner._lock:
                self._worker_threads.add(current_thread())
            if initializer is not None:
                try:
                    initializer(*initargs)
                finally:
                    if not owner._cleanup():
                        raise ThreadOwnershipRefused("worker initialization cleanup unresolved")
        options.arguments["initializer"] = initialize
        options.arguments["initargs"] = ()
        super().__init__(**options.arguments)

    def submit(self, fn, /, *args, **kwargs):
        owner = self._owner
        token = object()
        with owner._lock:
            owner._admit()
            if self._admission_stopped or len(owner._tasks) >= owner._max_tasks:
                raise ThreadOwnershipRefused("worker submission stopped or at capacity")
            owner._tasks.add(token)
        def invoke():
            try:
                with owner._lock:
                    owner._admit()
                return fn(*args, **kwargs)
            finally:
                if not owner._cleanup():
                    raise ThreadOwnershipRefused("worker task cleanup unresolved")
        try:
            future = super().submit(invoke)
        except BaseException:
            with owner._lock:
                owner._tasks.discard(token)
            raise
        # Also releases cancelled tasks which never entered invoke().
        def finished(_):
            with owner._lock:
                owner._tasks.discard(token)
        future.add_done_callback(finished)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._owner._lock:
            self._admission_stopped = True
        super().shutdown(wait=wait, cancel_futures=cancel_futures)
        if wait:
            with self._owner._lock:
                self._shutdown_complete = True

    def _begin_owned_shutdown(self):
        with self._owner._lock:
            self._admission_stopped = True
            if self._shutdown_complete or self._shutdown_thread is not None:
                return
            def shutdown():
                try:
                    self.shutdown(wait=True, cancel_futures=True)
                except BaseException:
                    self._owner._failed()
            # One exact retained helper per bounded pool. A blocked public
            # shutdown keeps both helper and pool unresolved past the deadline.
            helper = _NativeThread(target=shutdown, name="sonder-owned-pool-close", daemon=True)
            self._shutdown_thread = helper
            try:
                helper.start()
            except BaseException:
                self._owner._failed()
                raise


class OwnedRuntimeThreads:
    def __init__(self, *, cleanup, max_threads=128, max_pools=16, max_tasks=256):
        for value, maximum in ((max_threads, 128), (max_pools, 16), (max_tasks, 256)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("bounded runtime worker capacity required")
        if not callable(cleanup):
            raise TypeError("host-owned current-thread cleanup required")
        self._lock = RLock()
        self._cleanup_callback = cleanup
        self._max_threads, self._max_pools, self._max_tasks = max_threads, max_pools, max_tasks
        self._threads = []
        self._pools = []
        self._tasks = set()
        self._reserved_workers = 0
        self._stopped = False
        self._unresolved = False

    def _admit(self):
        if self._stopped or self._unresolved:
            raise ThreadOwnershipRefused("runtime worker admission stopped")

    def _failed(self):
        with self._lock:
            self._unresolved = self._stopped = True

    def _cleanup(self):
        try:
            success = self._cleanup_callback() is True
        except BaseException:
            success = False
        if not success:
            self._failed()
        return success

    def thread(self, *args, **kwargs):
        with self._lock:
            self._admit()
            self._threads = [thread for thread in self._threads if not (thread._owner_finished and not thread.is_alive())]
            if len(self._threads) + self._reserved_workers >= self._max_threads:
                raise ThreadOwnershipRefused("runtime thread capacity exhausted")
            thread = _ManagedThread(self, *args, **kwargs)
            self._threads.append(thread)
            return thread

    def pool(self, *args, **kwargs):
        options = inspect.signature(_NativePool).bind(*args, **kwargs)
        workers = options.arguments.get("max_workers")
        # Managed mode chooses an explicit bounded default, independent of
        # changing native CPU-count/default policy. Unmanaged mode is native.
        reserve = 32 if workers is None else workers
        if type(reserve) is not int or not 1 <= reserve <= 32:
            raise ThreadOwnershipRefused("bounded managed pool size required")
        with self._lock:
            self._admit()
            retained = []
            for previous in self._pools:
                helper = previous._shutdown_thread
                if previous._shutdown_complete and not any(thread.is_alive() for thread in previous._worker_threads) and (helper is None or not helper.is_alive()):
                    self._reserved_workers -= previous._reserved_count
                else:
                    retained.append(previous)
            self._pools = retained
            if len(self._pools) >= self._max_pools or len(self._threads) + self._reserved_workers + reserve > self._max_threads:
                raise ThreadOwnershipRefused("runtime pool capacity exhausted")
            options.arguments["max_workers"] = reserve
            pool = _ManagedPool(self, **options.arguments)
            pool._reserved_count = reserve
            self._pools.append(pool)
            self._reserved_workers += reserve
            return pool

    def stop_admissions(self):
        with self._lock:
            self._stopped = True

    def snapshot(self):
        with self._lock:
            direct = sum(thread.is_alive() or (thread._owner_started and not thread._owner_finished) for thread in self._threads)
            workers = {thread for pool in self._pools for thread in pool._worker_threads}
            helpers = {pool._shutdown_thread for pool in self._pools if pool._shutdown_thread is not None}
            live = direct + sum(thread.is_alive() for thread in workers | helpers)
            return ThreadOwnershipSnapshot(live, len(self._tasks), sum(not pool._shutdown_complete for pool in self._pools), self._unresolved, self._stopped)

    def close(self, *, timeout=5):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError("bounded worker shutdown deadline required")
        deadline = monotonic() + timeout
        with self._lock:
            self._stopped = True
            pools, direct = tuple(self._pools), tuple(self._threads)
        for pool in pools:
            pool._begin_owned_shutdown()
        threads = direct + tuple(pool._shutdown_thread for pool in pools if pool._shutdown_thread is not None)
        for thread in threads:
            if thread is current_thread():
                continue
            if isinstance(thread, _ManagedThread) and not thread._owner_started:
                continue  # Terminal start guard proves no native thread exists.
            try:
                thread.join(max(0, deadline - monotonic()))
            except RuntimeError:
                # A reserved native start may not have returned yet. Its
                # started-but-not-finished record keeps this proof unresolved.
                if not isinstance(thread, _ManagedThread):
                    self._failed()
        return self.snapshot()


_PROCESS_OWNER = None
_NATIVE_USED = False
_INSTALL_LOCK = RLock()


def install_disposable_owner(owner):
    global _PROCESS_OWNER
    with _INSTALL_LOCK:
        if type(owner) is not OwnedRuntimeThreads or _PROCESS_OWNER is not None or _NATIVE_USED:
            raise ThreadOwnershipRefused("one exact required-new process worker owner required")
        _PROCESS_OWNER = owner


def Thread(*args, **kwargs):
    global _NATIVE_USED
    with _INSTALL_LOCK:
        owner = _PROCESS_OWNER
        if owner is None:
            _NATIVE_USED = True
    return _NativeThread(*args, **kwargs) if owner is None else owner.thread(*args, **kwargs)


def ThreadPoolExecutor(*args, **kwargs):
    global _NATIVE_USED
    with _INSTALL_LOCK:
        owner = _PROCESS_OWNER
        if owner is None:
            _NATIVE_USED = True
    return _NativePool(*args, **kwargs) if owner is None else owner.pool(*args, **kwargs)

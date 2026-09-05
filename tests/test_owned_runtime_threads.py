from threading import Event, Thread, current_thread

import pytest

from sonder_runtime.platform.runtime_threads import OwnedRuntimeThreads, ThreadOwnershipRefused


def test_direct_thread_finalizes_on_exact_worker_and_joins():
    observed = []
    owner = OwnedRuntimeThreads(cleanup=lambda: observed.append(current_thread()) or True)
    thread = owner.thread(target=lambda: None)
    thread.start()
    thread.join(2)
    assert observed == [thread]
    assert owner.close(timeout=1).clean
    with pytest.raises(ThreadOwnershipRefused):
        owner.thread(target=lambda: None)


def test_never_started_thread_cannot_start_after_owner_stop():
    owner = OwnedRuntimeThreads(cleanup=lambda: True)
    thread = owner.thread(target=lambda: pytest.fail("not admitted"))
    assert owner.close(timeout=1).clean
    with pytest.raises(ThreadOwnershipRefused):
        thread.start()


def test_stuck_thread_remains_unresolved_without_termination_claim():
    release, entered = Event(), Event()
    owner = OwnedRuntimeThreads(cleanup=lambda: True)
    thread = owner.thread(target=lambda: (entered.set(), release.wait(5)))
    thread.start()
    try:
        assert entered.wait(2)
        assert not owner.close(timeout=0.01).clean
        assert thread.is_alive()
    finally:
        release.set()
        thread.join(2)
    assert owner.close(timeout=1).clean


def test_pool_reused_worker_cleans_after_success_and_exception():
    observed = []
    owner = OwnedRuntimeThreads(cleanup=lambda: observed.append(current_thread()) or True)
    pool = owner.pool(max_workers=1)
    thread = pool.submit(current_thread).result(2)
    def failed():
        raise ValueError("fixture")
    with pytest.raises(ValueError):
        pool.submit(failed).result(2)
    assert pool.submit(current_thread).result(2) is thread
    assert observed and all(item is thread for item in observed)
    assert len(observed) >= 3
    assert owner.close(timeout=1).clean
    assert not thread.is_alive()


def test_public_shutdown_completion_required_for_blocked_initializer():
    entered, release = Event(), Event()
    owner = OwnedRuntimeThreads(cleanup=lambda: True)
    pool = owner.pool(max_workers=1, initializer=lambda: (entered.set(), release.wait(5)))
    future = pool.submit(lambda: 1)
    try:
        assert entered.wait(2)
        future.cancel()
        assert not owner.close(timeout=0.01).clean
    finally:
        release.set()
    assert owner.close(timeout=2).clean


def test_cleanup_failure_suppresses_pool_success_and_new_admission():
    owner = OwnedRuntimeThreads(cleanup=lambda: False)
    pool = owner.pool(max_workers=1)
    with pytest.raises(ThreadOwnershipRefused):
        pool.submit(lambda: "not a clean result").result(2)
    with pytest.raises(ThreadOwnershipRefused):
        pool.submit(lambda: None)
    assert not owner.close(timeout=1).clean


def test_bounded_submission_capacity_and_close_race():
    entered, release = Event(), Event()
    owner = OwnedRuntimeThreads(cleanup=lambda: True, max_tasks=1)
    pool = owner.pool(max_workers=1)
    future = pool.submit(lambda: (entered.set(), release.wait(5)))
    try:
        assert entered.wait(2)
        with pytest.raises(ThreadOwnershipRefused):
            pool.submit(lambda: None)
        assert not owner.close(timeout=0).clean
        with pytest.raises(ThreadOwnershipRefused):
            pool.submit(lambda: None)
    finally:
        release.set()
        future.result(2)
    assert owner.close(timeout=2).clean


def test_completed_pools_release_bounded_slots_only_after_public_shutdown():
    owner = OwnedRuntimeThreads(cleanup=lambda: True, max_threads=1, max_pools=1)
    for _ in range(20):
        with owner.pool(max_workers=1) as pool:
            assert pool.submit(lambda: 1).result(2) == 1
    assert owner.close(timeout=1).clean


def test_unmanaged_factories_return_native_types():
    from concurrent.futures import ThreadPoolExecutor as NativePool
    from sonder_runtime.platform.runtime_threads import Thread as create_thread, ThreadPoolExecutor as create_pool
    assert type(create_thread(target=lambda: None)) is Thread
    with create_pool(max_workers=1) as pool:
        assert type(pool) is NativePool
        assert pool.submit(lambda: 1).result(2) == 1


def test_real_worker_sqlite_caches_close_and_reopen_on_reused_thread(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = r'''
from pathlib import Path
from threading import current_thread
import sys
from sonder_runtime.adapters.persistence.owned_sqlite import OwnedSQLiteConnections, install_disposable_owner
from sonder_runtime.platform.runtime_threads import OwnedRuntimeThreads
from sonder_runtime.bootstrap.thread_resources import SQLiteThreadCleanup, install_disposable_thread_owner
from sonder_runtime.application.ports.runtime_threads import ThreadPoolExecutor
from sonder_runtime.adapters import embedding_cache
from sonder_runtime.adapters.persistence import composition_store, sqlite_factory
root=Path(sys.argv[1])
sqlite=OwnedSQLiteConnections((root,), max_connections=4)
install_disposable_owner(sqlite)
workers=OwnedRuntimeThreads(cleanup=SQLiteThreadCleanup(sqlite), max_threads=1)
install_disposable_thread_owner(workers)
pool=ThreadPoolExecutor(max_workers=1)
def task(index):
 embedding_cache._connection().execute('SELECT 1').fetchone()
 composition_store._connection().execute('SELECT 1').fetchone()
 connection=sqlite_factory.cached_connection('fixture', root/'cache.db', schema_sql='CREATE TABLE IF NOT EXISTS events(value INTEGER)')
 connection.execute('INSERT INTO events VALUES(?)',(index,)); connection.commit()
 assert connection.execute('SELECT COUNT(*) FROM events').fetchone()[0] == index+1
 assert sqlite.snapshot().open_handles == 3
 return current_thread()
first=None
for index in range(20):
 thread=pool.submit(task,index).result(3)
 assert first is None or first is thread
 first=thread
 assert sqlite.snapshot().clean, sqlite.snapshot()
assert workers.close(timeout=2).clean
assert not first.is_alive()
assert sqlite.snapshot().clean
print('closed')
'''
    environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    environment["SONDER_HOME"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "closed"


def test_explicit_package_constructor_coverage():
    import ast
    from pathlib import Path
    violations = []
    for path in (Path(__file__).resolve().parents[1] / "sonder_runtime").rglob("*.py"):
        if path.name == "runtime_threads.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        native = {"threading.Thread", "concurrent.futures.ThreadPoolExecutor"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for item in node.names:
                    if (node.module, item.name) in {("threading", "Thread"), ("concurrent.futures", "ThreadPoolExecutor")}:
                        native.add(item.asname or item.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in native:
                violations.append((str(path), node.lineno))
    assert violations == []


@pytest.mark.parametrize("kind", ["thread", "pool"])
def test_native_admission_inflight_cannot_hold_shutdown_lock_or_start_effect_after_stop(monkeypatch, kind):
    import time
    import sonder_runtime.platform.runtime_threads as module
    entered, release = Event(), Event()
    effects, errors = [], []
    owner = OwnedRuntimeThreads(cleanup=lambda: True)
    if kind == "pool":
        pool = owner.pool(max_workers=1)
        original = module._NativePool.submit
        def delayed(instance, *args, **kwargs):
            if instance is pool:
                entered.set()
                release.wait(2)
            return original(instance, *args, **kwargs)
        monkeypatch.setattr(module._NativePool, "submit", delayed)
        action = lambda: pool.submit(lambda: effects.append("ran"))
    else:
        owned = owner.thread(target=lambda: effects.append("ran"))
        original = module._NativeThread.start
        def delayed(instance):
            if instance is owned:
                entered.set()
                release.wait(2)
            return original(instance)
        monkeypatch.setattr(module._NativeThread, "start", delayed)
        action = owned.start
    def caller():
        try:
            action()
        except (RuntimeError, ThreadOwnershipRefused):
            errors.append("refused")
    caller_thread = Thread(target=caller)
    caller_thread.start()
    try:
        assert entered.wait(2)
        if kind == "thread":
            with pytest.raises(RuntimeError):
                owned.start()
        started = time.monotonic()
        assert not owner.close(timeout=0.01).clean
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        caller_thread.join(3)
    assert not caller_thread.is_alive()
    assert owner.close(timeout=2).clean
    assert effects == []


@pytest.mark.parametrize("module_name", ["sonder_runtime.platform.runtime_threads", "sonder_runtime.application.ports.runtime_threads"])
def test_late_or_duplicate_installation_cannot_adopt_native_factory_use(tmp_path, module_name):
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = r'''
import importlib, sys
from sonder_runtime.platform.runtime_threads import OwnedRuntimeThreads
from sonder_runtime.bootstrap.thread_resources import install_disposable_thread_owner
module=importlib.import_module(sys.argv[1])
module.Thread(target=lambda: None)
owner=OwnedRuntimeThreads(cleanup=lambda: True)
try:
 install_disposable_thread_owner(owner)
except RuntimeError:
 pass
else:
 raise AssertionError('late installation admitted')
try:
 owner.thread(target=lambda: None)
except RuntimeError:
 pass
else:
 raise AssertionError('failed installation did not fence owner')
print('refused')
'''
    environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    environment["SONDER_HOME"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", script, module_name], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "refused"

"""Real dispatcher/worker ownership in isolated Application processes."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = r'''
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from concurrent.futures import ThreadPoolExecutor as NativePool
import sys, time
from sonder_runtime.platform.runtime_threads import OwnedRuntimeThreads, install_disposable_owner
workers = OwnedRuntimeThreads(cleanup=lambda: True)
install_disposable_owner(workers)
from sonder_runtime.bootstrap.app import build_application, install_owned_application, stop_owned_application
from sonder_runtime.platform.config import SonderConfig
from sonder_runtime.platform import paths
config = SonderConfig()
config = replace(config, state=replace(config.state, home=sys.argv[1], workspace_roots=()))
paths.configure_home(config.state.home)
application = build_application(config=config)
install_owned_application(application)
from sonder_runtime.bootstrap.app_managed_work import AppManagedWorkDispatcher
from sonder_runtime.bootstrap.managed_app_work import install_owned_app_work_slot, register_owned_app_work, require_owned_app_work, seal_owned_app_work
from sonder_runtime.application.runtime_resources import ApplicationResourceOwners
from sonder_runtime.bootstrap.managed_configuration import COMPONENTS, CLOSE_ORDER
from sonder_runtime.application.ports.runtime_owner import OwnerRefused, OwnerUnsupported
def refused(kind, call):
    try: call()
    except kind: return
    raise AssertionError('unexpected admission')
refused(OwnerUnsupported, lambda: require_owned_app_work(application))
resources = ApplicationResourceOwners(COMPONENTS, close_order=CLOSE_ORDER)
install_owned_app_work_slot(application, resources, workers)
refused(OwnerUnsupported, lambda: require_owned_app_work(application))
dispatchers = []
def dispatcher(app):
    value = AppManagedWorkDispatcher(object(), object(), application=app,
        lifetime_factory=lambda *a: None, authorize_dispatch=lambda *a: None,
        terminal_eligibility=lambda *a: None)
    dispatchers.append(value)
    return value
try:
    mode = sys.argv[2]
    if mode == 'identity':
        for wrong in (None, object()):
            refused(OwnerRefused, lambda: register_owned_app_work(application, dispatcher(wrong)))
        unowned = dispatcher(application)
        original = unowned._executor
        native = NativePool(max_workers=1)
        unowned._executor = native
        try:
            refused(OwnerRefused, lambda: register_owned_app_work(application, unowned))
        finally:
            native.shutdown()
            unowned._executor = original
        value = dispatcher(application)
        register_owned_app_work(application, value)
        assert require_owned_app_work(application) is value
        refused(OwnerRefused, lambda: register_owned_app_work(application, value))
        refused(OwnerRefused, lambda: require_owned_app_work(object()))
        seal_owned_app_work(application)
        refused(OwnerRefused, lambda: register_owned_app_work(application, dispatcher(application)))
        assert require_owned_app_work(application) is value
        value._executor.submit(lambda: 7).result(2)
        result = resources.close(timeout=2)
        assert next(row for row in result.components if row.component == 'app-work').state == 'CLOSED'
        refused(OwnerRefused, lambda: require_owned_app_work(application))
    elif mode == 'late':
        ended = dispatcher(application)
        ended.close()
        refused(OwnerRefused, lambda: register_owned_app_work(application, ended))
        seal_owned_app_work(application)
        refused(OwnerRefused, lambda: register_owned_app_work(application, dispatcher(application)))
        stop_owned_application(application)
        refused(OwnerRefused, lambda: require_owned_app_work(application))
        assert resources.close(timeout=2).clean
    else:
        value = dispatcher(application)
        register_owned_app_work(application, value)
        entered, release = Event(), Event()
        future = value._executor.submit(lambda: (entered.set(), release.wait(5)))
        assert entered.wait(2)
        results = []
        closer = Thread(target=lambda: results.append(resources.close(timeout=.01)))
        closer.start()
        time.sleep(.1)
        assert closer.is_alive() and not results
        refused(OwnerRefused, lambda: require_owned_app_work(application))
        release.set()
        closer.join(2)
        assert not closer.is_alive()
        assert not results[0].clean
        assert resources.close(timeout=2) is results[0]
finally:
    for value in dispatchers:
        value.close()
    assert workers.close(timeout=3).clean
    stop_owned_application(application)
    application.close_providers(timeout=3)
'''


@pytest.mark.parametrize("mode", ["identity", "late", "inflight"])
def test_real_owned_dispatcher_slot(tmp_path, mode):
    environment = dict(os.environ, SONDER_HOME=str(tmp_path / "state"), SONDER_OLLAMA_WORKERS="")
    result = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp_path / "state"), mode],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True,
        text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr

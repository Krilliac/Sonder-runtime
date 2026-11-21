"""RED: serve.main must stop the owned cloud-refresh thread it starts."""
import threading

import sonder_runtime.adapters.cloud_model_refresh_schedule as schedule
from sonder_runtime.platform.runtime_threads import OwnedRuntimeThreads
from tests import test_admission_wait_status_wire as wire


def test_serve_main_sets_the_cloud_refresh_stop_event_on_shutdown(monkeypatch):
    seen = []
    monkeypatch.setattr(wire.ts.cloud_model_refresh_schedule, "start_scheduled_refresh",
                        lambda **kwargs: seen.append(kwargs) or True)
    wire._run_serve_main(monkeypatch, False, [])
    (call,) = seen
    stop = call.get("stop")
    assert isinstance(stop, threading.Event), "serve passes no stop event"
    assert stop.is_set(), "serve.main returned without stopping the refresh thread"


def test_owned_refresh_thread_joins_once_stopped(tmp_path):
    owner = OwnedRuntimeThreads(cleanup=lambda: True)
    stop = threading.Event()
    assert schedule.start_scheduled_refresh(
        origin="http://127.0.0.1:1", thread_factory=owner.thread,
        home=tmp_path, env={}, stop=stop)
    stop.set()
    assert owner.close(timeout=2).clean

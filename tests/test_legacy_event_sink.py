import sys
from types import SimpleNamespace

from sonder_runtime.adapters.legacy.services import OperationsEventSink


def test_event_store_failure_does_not_change_business_success(monkeypatch):
    class BrokenStore:
        def record_event(self, **kwargs):
            raise OSError("operations store unavailable")

    module = SimpleNamespace(OperationsStore=BrokenStore)
    monkeypatch.setitem(sys.modules, "sonder_operations_store", module)

    OperationsEventSink().emit("WORK_DONE", summary="business operation succeeded")

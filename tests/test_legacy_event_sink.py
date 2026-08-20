from sonder_runtime.adapters.operations_event_sink import OperationsEventSink
from sonder_runtime.adapters.persistence import operations_store


def test_event_store_failure_does_not_change_business_success(monkeypatch):
    class BrokenStore:
        def record_event(self, **kwargs):
            raise OSError("operations store unavailable")

    monkeypatch.setattr(operations_store, "OperationsStore", BrokenStore)

    OperationsEventSink().emit("WORK_DONE", summary="business operation succeeded")


def test_strangler_name_is_identity_compatible():
    from sonder_runtime.adapters.strangler_services import OperationsEventSink as Legacy

    assert Legacy is OperationsEventSink

"""Regression coverage for the operations-store logging platform seam."""

import sonder_logging

from sonder_runtime.adapters.persistence.operations_store import OperationsStore
from sonder_runtime.platform import logging as runtime_logging


def test_operations_store_uses_canonical_logging_exports():
    assert runtime_logging.Redactor is sonder_logging.Redactor
    assert runtime_logging.REDACTION_FAILED == sonder_logging.REDACTION_FAILED
    assert OperationsStore.__init__.__globals__["Redactor"] is runtime_logging.Redactor
    assert (
        OperationsStore.record_event.__globals__["REDACTION_FAILED"]
        == runtime_logging.REDACTION_FAILED
    )

"""Pytest-wide isolation for process-shared Sonder runtime state."""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile


_FLEET_TEST_ROOT = Path(tempfile.mkdtemp(prefix="sonder-pytest-fleet-"))

# This hook module is loaded before pytest imports test modules.  Pinning the
# environment here ensures fleet_store/master_orchestrator never open the live
# restart-safe ledger during collection, setup_function(), subprocess tests, or
# importlib.reload().  The old suite called reset_for_tests() against the live
# database and could cancel an operator's active fleet.
os.environ["SONDER_FLEET_DB"] = str(_FLEET_TEST_ROOT / "fleet.db")


def _cleanup_test_fleet_root():
    shutil.rmtree(_FLEET_TEST_ROOT, ignore_errors=True)


# Registered before test modules import master_orchestrator, so LIFO atexit
# ordering lets its owner finalizer close the isolated ledger before deletion.
# Do not restore SONDER_FLEET_DB inside this process: a later atexit callback or
# daemon thread must never fall back to the operator's live database.
atexit.register(_cleanup_test_fleet_root)

"""Install a hermetic Sonder state directory before test collection."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent
_TEST_STATE_ROOT = Path(tempfile.mkdtemp(prefix="sonder-pytest-")).resolve()
_cleanup_complete = False
os.environ.update(
    {
        "SONDER_HOME": str(_TEST_STATE_ROOT),
        "SONDER_DB": str(_TEST_STATE_ROOT / "memory.db"),
        "SONDER_FLEET_DB": str(_TEST_STATE_ROOT / "fleet.db"),
        "SONDER_FLEET_HEARTBEAT": "0",
        "SONDER_ALLOW_CLOUD": "0",
        "SONDER_WEB_TOOLS": "0",
        "SONDER_LIVE_RELOAD": "0",
        "SONDER_FALLBACK_LOCAL": "0",
        "SONDER_SERVER": "http://127.0.0.1:1",
        "SONDER_LOCAL_FALLBACK": "http://127.0.0.1:1",
        "OLLAMA_HOST": "127.0.0.1:1",
    }
)
sys.path.insert(0, str(_REPO_ROOT))


def _cleanup_test_state() -> None:
    global _cleanup_complete
    if _cleanup_complete:
        return
    _cleanup_complete = True
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        _TEST_STATE_ROOT.relative_to(temp_root)
    except ValueError:
        return
    if _TEST_STATE_ROOT.name.startswith("sonder-pytest-"):
        shutil.rmtree(_TEST_STATE_ROOT, ignore_errors=True)


atexit.register(_cleanup_test_state)


def pytest_sessionfinish(session, exitstatus) -> None:
    del session, exitstatus
    _cleanup_test_state()


def pytest_addoption(parser) -> None:
    group = parser.getgroup("sonder")
    group.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="run tests marked network",
    )
    group.addoption(
        "--run-model",
        action="store_true",
        default=False,
        help="run tests marked model",
    )


def pytest_collection_modifyitems(config, items) -> None:
    for marker, option in (("network", "--run-network"), ("model", "--run-model")):
        if config.getoption(option):
            continue
        skip = pytest.mark.skip(reason=f"requires explicit {option} opt-in")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)

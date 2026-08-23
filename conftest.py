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
        # The embed cache would couple tests through shared state: a text
        # embedded by one test would satisfy another test's embed from cache
        # and skip its mocked HTTP path. Cache tests opt back in explicitly.
        "SONDER_EMBED_CACHE": "0",
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


# Whether callers are authenticated is read from the environment at call time,
# because that is genuinely where deployment posture lives. Production
# entrypoints -- `serve`, and sonder_launcher -- export resolved config into
# os.environ as a deliberate side effect, and that behaviour is itself under
# test. The export outlives the test that triggered it, so every later test in
# the session inherited SONDER_AUTH_MODE=api-key and any code consulting it
# concluded the deployment authenticates callers. Invisible while nothing read
# those variables per call; a session-wide false posture the moment something
# did. Restore them around every test rather than per leaking test, because the
# leak is a property of the code under test, not of any one caller.
_POSTURE_VARS = ("SONDER_AUTH_MODE", "SONDER_API_KEY", "SONDER_REQUIRE_ACCOUNT")


@pytest.fixture(autouse=True)
def _isolate_deployment_posture():
    before = {name: os.environ.get(name) for name in _POSTURE_VARS}
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


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


# --- Opt-in per-test timing capture -----------------------------------------
# SONDER_TEST_TIMINGS=<path> appends one JSON line per test so slow tests can
# be ranked and compared across runs (scripts/slow_tests.py). Off by default:
# with the variable unset the hook body is two dict lookups per report.
#
# Under pytest-xdist each worker process writes to <path>.<workerid> -- a
# shared append handle across processes interleaves partial lines on Windows.
# The reader globs the suffixed files back together.
_timing_records: list[dict] = []


def _timings_path() -> str:
    base = os.environ.get("SONDER_TEST_TIMINGS", "").strip()
    if not base:
        return ""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "").strip()
    return "%s.%s" % (base, worker) if worker else base


def pytest_runtest_logreport(report) -> None:
    if not os.environ.get("SONDER_TEST_TIMINGS", "").strip():
        return
    # One record per phase; the reader sums setup+call+teardown per test and
    # keeps the worst outcome. Recording phases separately preserves the
    # difference between a slow test body and a slow fixture.
    _timing_records.append(
        {
            "nodeid": report.nodeid,
            "phase": report.when,
            "outcome": report.outcome,
            "duration": round(report.duration, 6),
        }
    )


def pytest_sessionfinish(session, exitstatus) -> None:
    del session, exitstatus
    path = _timings_path()
    if path and _timing_records:
        import json

        try:
            with open(path, "w", encoding="utf-8") as handle:
                for record in _timing_records:
                    handle.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as error:
            # Visible, not fatal: losing the timing file must not fail a run,
            # but a silent loss would read as "no slow tests".
            print("SONDER_TEST_TIMINGS write failed: %s" % error, file=sys.stderr)
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
            # get_closest_marker, not `marker in item.keywords`: keywords also
            # contain parametrize *values*, so a test parameterized with the
            # literal string "model" or "network" was silently skipped even
            # though it was never marked. A security assertion that skips
            # itself reads exactly like a passing one.
            if item.get_closest_marker(marker) is not None:
                item.add_marker(skip)

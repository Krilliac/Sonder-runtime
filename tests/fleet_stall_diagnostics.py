"""Retain bounded metadata when the delegated-stall contract fails.

This module is an optional pytest plugin solely to register the artifact path.
It does not wrap runtime calls or change their scheduling. Failure capture never
records arguments, locals, exception text, prompts, results or environment.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import uuid


TARGET_NODE = (
    "tests/test_adaptive_concurrency.py::"
    "test_run_delegated_stall_is_uncertain_and_retains_child_capacity"
)
MAX_THREADS = 32
MAX_FRAMES = 64
CHECKPOINT_KEYS = (
    "test_started_ns", "worker_entered_ns", "worker_returned_ns",
    "join_started_ns", "join_finished_ns",
)
PHASES = frozenset({"worker-entry", "coordinator-join", "stalled-result", "capacity-retained"})
REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_addoption(parser):
    parser.addoption(
        "--fleet-stall-evidence-dir", action="store", default=None,
        help="Retain bounded metadata if the delegated-stall contract fails.",
    )


def _filename(filename):
    normalized = str(filename).replace("\\", "/")
    prefix = str(REPO_ROOT).replace("\\", "/").rstrip("/") + "/"
    if normalized.casefold().startswith(prefix.casefold()):
        return ("repo/" + normalized[len(prefix):])[:256]
    return ("external/" + normalized.rsplit("/", 1)[-1])[:256]


def _runtime_counts():
    # Inspect collections directly. A stalled runtime lock, DB read or callback
    # must never become a prerequisite for observing the blocked coordinator.
    try:
        master = sys.modules.get("master_orchestrator")
        events = sys.modules.get("sonder_runtime.domain.events")
        paths = sys.modules.get("sonder_runtime.platform.paths")
        handlers = getattr(getattr(events, "_bus", None), "_handlers", {})
        return {
            "snapshot_callbacks": len(getattr(master, "_SNAPSHOT_SUBSCRIBERS", ())),
            "domain_handlers": sum(len(items) for items in handlers.values()),
            "configured_home": getattr(paths, "_HOME_OVERRIDE", None) is not None,
        }
    except Exception:
        return {"unavailable": True}


def write_stall_diagnostic(directory, *, phase, coordinator_id, checkpoints):
    """Save an assertion-time snapshot; observation errors preserve the failure."""
    try:
        frames = sys._current_frames()
        current_id = threading.get_ident()
        coordinator_id = coordinator_id if type(coordinator_id) is int else None
        identifiers = sorted(frames, key=lambda ident: (
            ident != coordinator_id, ident != current_id, ident,
        ))
        threads = []
        for ident in identifiers[:MAX_THREADS]:
            frame = frames[ident]
            stack = []
            while frame is not None and len(stack) < MAX_FRAMES:
                stack.append({"file": _filename(frame.f_code.co_filename),
                              "function": frame.f_code.co_name[:128], "line": frame.f_lineno})
                frame = frame.f_back
            threads.append({"thread_id": ident, "is_coordinator": ident == coordinator_id,
                            "frames": stack, "frames_truncated": frame is not None})
        payload = {
            "schema_version": 1, "target_node": TARGET_NODE,
            "phase": phase if phase in PHASES else "unknown",
            "coordinator_id": coordinator_id, "coordinator_present": coordinator_id in frames,
            "captured_monotonic_ns": time.monotonic_ns(),
            "checkpoints": {key: checkpoints.get(key) if type(checkpoints.get(key)) is int else None
                            for key in CHECKPOINT_KEYS},
            "thread_count": len(frames), "threads_dropped": max(0, len(frames) - MAX_THREADS),
            "limits": {"threads": MAX_THREADS, "frames_per_thread": MAX_FRAMES},
            "runtime_counts": _runtime_counts(), "threads": threads,
            "target_source_sha256": hashlib.sha256(
                (REPO_ROOT / "tests/test_adaptive_concurrency.py").read_bytes()
            ).hexdigest(),
        }
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        artifact = destination / ("stall-" + uuid.uuid4().hex + ".json")
        with artifact.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        return artifact
    except Exception:
        return None

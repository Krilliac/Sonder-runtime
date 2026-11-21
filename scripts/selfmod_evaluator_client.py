"""Submit one selfmod evaluator request to the isolated Scheduled Task.

This is the worker-side half of the opt-in evaluator broker: configure its
command line as ``SONDER_SELFMOD_EVALUATOR_BROKER`` for the nightly lane.  It
copies the request into a pre-created inbox, starts the evaluator task with
``schtasks /Run`` and prints the evaluator's response from the outbox.

The nightly worker owns the inbox write and outbox read ACLs.  The scheduled
task account owns the outbox write ACL and runs
``selfmod_evaluator_broker.py --once INBOX OUTBOX``.  The client never
evaluates a candidate and makes no identity claim itself; the worker verifies
the broker's Ed25519 receipt after this process returns.  It refuses to
create the inbox or outbox, since their ACLs are the boundary.

Environment: ``SONDER_SELFMOD_EVALUATOR_INBOX``, ``..._OUTBOX`` and
``..._TASK`` (the task name).  Windows only (``schtasks.exe``).  Exit status
0 means a response was printed (not that it passed), 1 a refusal, timeout or
launch failure (reason on stderr), 2 a usage error.

Single-threaded; holds no state between invocations.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


INBOX_ENV = "SONDER_SELFMOD_EVALUATOR_INBOX"
OUTBOX_ENV = "SONDER_SELFMOD_EVALUATOR_OUTBOX"
TASK_ENV = "SONDER_SELFMOD_EVALUATOR_TASK"
REQUEST_SCHEMA = "sonder.selfmod.evaluator-request/v1"
_MAX_WAIT_SECONDS = 900


def _refuse(reason: str) -> int:
    print("selfmod evaluator client: %s" % reason, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: selfmod_evaluator_client.py REQUEST.json", file=sys.stderr)
        return 2
    inbox_raw = os.environ.get(INBOX_ENV, "").strip()
    outbox_raw = os.environ.get(OUTBOX_ENV, "").strip()
    task = os.environ.get(TASK_ENV, "").strip()
    if not inbox_raw or not outbox_raw or not task:
        return _refuse("configure %s, %s and %s" % (INBOX_ENV, OUTBOX_ENV, TASK_ENV))
    inbox = Path(inbox_raw).resolve()
    outbox = Path(outbox_raw).resolve()
    request = Path(args[0]).resolve()
    try:
        payload = json.loads(request.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != REQUEST_SCHEMA:
            return _refuse("unsupported request schema")
        nonce = str(payload["nonce"])
        if not re.fullmatch(r"[0-9a-f]{32}", nonce):
            return _refuse("request nonce is malformed")
        if not inbox.is_dir() or not outbox.is_dir():
            return _refuse("inbox and outbox must be provisioned in advance")
        queued = inbox / (nonce + ".json")
        response = outbox / (nonce + ".json")
        if queued.exists() or response.exists():
            return _refuse("request nonce was already submitted")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=inbox, prefix=nonce + ".", suffix=".tmp", delete=False
        ) as stream:
            json.dump(payload, stream, sort_keys=True)
            temporary = Path(stream.name)
        os.replace(temporary, queued)
        launched = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", task],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            check=False, timeout=15,
        )
        if launched.returncode:
            queued.unlink(missing_ok=True)
            return _refuse("evaluator task could not be started (exit %d)" % launched.returncode)
        deadline = time.monotonic() + min(_MAX_WAIT_SECONDS, max(1, int(payload["timeout"]) + 30))
        while time.monotonic() < deadline:
            if response.is_file():
                sys.stdout.write(response.read_text(encoding="utf-8"))
                return 0
            time.sleep(0.25)
        # A request the evaluator never answered must not be evaluated later
        # for a run that has already been rejected.
        queued.unlink(missing_ok=True)
        return _refuse("evaluator did not answer before the deadline")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return _refuse("request could not be submitted (%s)" % type(exc).__name__)


if __name__ == "__main__":
    raise SystemExit(main())

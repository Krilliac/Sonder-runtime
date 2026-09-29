"""Scratch: bracket a command with protected-file snapshots of cwd. Deleted before commit."""
import json
import subprocess
import sys
import time

from sonder_runtime.application.evaluation.integrity import protected_writes, snapshot_protected

t = time.perf_counter()
before = snapshot_protected(".")
print("PROBE snapshot files=%d seconds=%.2f" % (len(before), time.perf_counter() - t), flush=True)
code = subprocess.call(sys.argv[1:])
after = snapshot_protected(".")
writes = protected_writes(before, after)
print("PROBE command_exit=%d protected_writes=%d" % (code, len(writes)))
print("PROBE writes=" + json.dumps([w.describe() for w in writes][:50]))
sys.exit(code)

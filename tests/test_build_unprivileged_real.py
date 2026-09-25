"""Real checks as an unprivileged uid (this host runs tests as root, where
file modes and namespace probes prove nothing).

Under ``setpriv`` to uid 65534:

* the private build-run and build-env directories are 0700, the tee log and
  the vcvars cache are 0600, and a cache readable by others is not trusted;
* ``unshare -rn`` enforcement: a ``cmake -P`` ``file(DOWNLOAD)`` from a
  loopback HTTP server succeeds without the prefix and fails with it, and a
  plain loopback connect is unreachable (loopback is down in the namespace).
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UID = 65534

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only"),
    pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0,
                       reason="needs root to drop to an unprivileged uid"),
    pytest.mark.skipif(shutil.which("setpriv") is None or shutil.which("unshare") is None,
                       reason="needs setpriv and unshare"),
]

CHILD = r'''
import json, os, stat, subprocess, sys
sys.path.insert(0, sys.argv[1])
base = sys.argv[2]
from sonder_runtime.platform.private_files import ensure_private_dir
from sonder_runtime.adapters.build.environment import JsonEnvCache
from sonder_runtime.adapters.build.launcher import tee_argv
from sonder_runtime.adapters.build.network import NetworkIsolation
out = {"uid": os.geteuid()}
runs = ensure_private_dir(os.path.join(base, "state", "build-runs"))
out["run_root_mode"] = oct(stat.S_IMODE(os.stat(runs).st_mode))
job = os.path.join(runs, "build-job-" + "a" * 32)
os.mkdir(job, 0o700)
log = os.path.join(job, "output.log")
tee = subprocess.run(tee_argv(sys.executable, log, ("/bin/sh", "-c", "echo hello; exit 4")),
                     capture_output=True, timeout=60)
out["tee_exit"] = tee.returncode
out["log_mode"] = oct(stat.S_IMODE(os.stat(log).st_mode))
out["log_text"] = open(log).read()
cache = JsonEnvCache(os.path.join(base, "state", "build-env", "vcvars-cache.json"))
cache.put("k", {"INCLUDE": "x"}, stored_at=1.0)
out["cache_mode"] = oct(stat.S_IMODE(os.stat(cache.path).st_mode))
out["env_dir_mode"] = oct(stat.S_IMODE(os.stat(os.path.dirname(cache.path)).st_mode))
out["cache_hit"] = cache.get("k") is not None
os.chmod(cache.path, 0o644)
out["loose_cache_trusted"] = cache.get("k") is not None
decision = NetworkIsolation(mode="enforce", executable_guard=lambda p: p).decide(allow_network=False)
out["policy"] = decision.policy
out["prefix"] = list(decision.prefix)
script = os.path.join(base, "download.cmake")
url = sys.argv[3]
def download(prefix):
    target = os.path.join(base, "dl-%d.txt" % len(prefix))
    with open(script, "w") as handle:
        handle.write('file(DOWNLOAD "%s" "%s" STATUS st TIMEOUT 5)\nlist(GET st 0 code)\n'
                     'if(NOT code EQUAL 0)\n  message(FATAL_ERROR "download failed: ${st}")\nendif()\n'
                     % (url, target))
    run = subprocess.run(list(prefix) + [sys.argv[4], "-P", script], capture_output=True, timeout=60)
    return run.returncode, (run.stdout + run.stderr).decode(errors="replace")[-300:]
out["download_plain"] = download(())
out["download_isolated"] = download(tuple(decision.prefix))
probe = ("import socket,sys\ns=socket.socket()\ns.settimeout(3)\n"
         "try:\n s.connect(('127.0.0.1', int(sys.argv[1]))); print('connected')\n"
         "except OSError as e: print('errno', e.errno)\n")
loop = subprocess.run(list(decision.prefix) + [sys.executable, "-I", "-c", probe, sys.argv[5]],
                      capture_output=True, timeout=60)
out["loopback"] = loop.stdout.decode().strip()
print(json.dumps(out))
'''


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def http_server(tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    (served / "payload.txt").write_text("downloaded")
    handler = lambda *a, **k: _Quiet(*a, directory=str(served), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def request_cleanup():
    paths = []
    yield paths.append
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def test_private_modes_and_network_enforcement_as_uid_65534(tmp_path, http_server, request_cleanup):
    cmake = shutil.which("cmake")
    if cmake is None:
        pytest.skip("cmake is required")
    # pytest's own tmp root is 0700 root-owned; the unprivileged uid needs a
    # directory whose ancestors it can traverse.
    readable = subprocess.run(
        ["setpriv", "--reuid=%d" % UID, "--regid=%d" % UID, "--clear-groups", "--",
         sys.executable, "-I", "-c", "import os,sys; os.stat(os.path.join(sys.argv[1], 'sonder_runtime', "
         "'__init__.py')); open(sys.argv[2]).close()", str(REPO), sys.executable],
        capture_output=True, timeout=60, cwd="/", env={"PATH": "/usr/bin:/bin"})
    if readable.returncode != 0:
        pytest.skip("the checkout or interpreter is not readable by uid %d here" % UID)
    base = Path(tempfile.mkdtemp(prefix="sonder-unpriv-", dir="/tmp"))
    os.chown(base, UID, UID)
    request_cleanup(base)
    port = http_server
    child = subprocess.run(
        ["setpriv", "--reuid=%d" % UID, "--regid=%d" % UID, "--clear-groups", "--",
         sys.executable, "-c", CHILD, str(REPO), str(base),
         "http://127.0.0.1:%d/payload.txt" % port, cmake, str(port)],
        capture_output=True, timeout=300, cwd="/", env={"PATH": "/usr/bin:/bin", "HOME": str(base),
                                                        "LANG": "C.UTF-8"},
    )
    assert child.returncode == 0, child.stderr.decode()[-2000:]
    out = json.loads(child.stdout.decode().strip().splitlines()[-1])
    assert out["uid"] == UID
    assert out["run_root_mode"] == "0o700" and out["env_dir_mode"] == "0o700"
    assert out["log_mode"] == "0o600" and out["cache_mode"] == "0o600"
    assert out["tee_exit"] == 4 and out["log_text"] == "hello\n"
    assert out["cache_hit"] and not out["loose_cache_trusted"]
    assert out["policy"] == "enforced_off" and out["prefix"][1:] == ["-rn", "--"]
    assert out["download_plain"][0] == 0, out["download_plain"]
    assert out["download_isolated"][0] != 0, out["download_isolated"]
    assert out["loopback"].startswith("errno")  # loopback is down in the namespace

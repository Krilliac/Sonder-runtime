"""Stdlib-only broker for the restartable NPU accelerator worker.

The broker owns the child process that holds every vendor import. It enforces
the accelerator's operational contract:

- lazy worker start (a cold call never blocks on warmup; it falls back),
- one in-flight accelerator call,
- strict per-call deadlines with kill-on-timeout,
- restart-once then a cooling-off circuit breaker,
- an available-RAM spawn gate and a worker RSS eviction cap,
- idle unload,
- bounded JSONL stdio with malformed-line poisoning.

Every unavailability is reported as a typed reason so callers fall back to the
existing local behavior. Nothing in this module can route work to cloud tiers.
"""
from __future__ import annotations

import collections
import itertools
import math
import os
import queue
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import npu_contract
import npu_manifest
import npu_providers


_FAILURE_REASONS = frozenset({
    "timeout", "crash", "malformed", "worker_error", "spawn", "rss_limit",
})
_CONSECUTIVE_FAILURE_LIMIT = 3
# Restart-once: a crashed worker may be respawned automatically one time; a
# second death without an intervening success opens the circuit.
_DEATH_LIMIT = 2
_STDERR_RING = 40
_CALLER_RESERVED_KEYS = frozenset({"id", "op", "manifest_hash"})
_STAGING_PREFIX = "sonder-npu-stage-"
_WORKER_QUEUE_LIMIT = 8

_WORKER_ENV_KEYS = frozenset({
    # Process/bootstrap inputs.
    "COMSPEC", "LANG", "LC_ALL", "LC_CTYPE", "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH", "OS", "PATH", "PATHEXT", "SYSTEMDRIVE",
    "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR",
    # Exact, non-credential vendor runtime knobs. Keep this list explicit:
    # prefix inheritance can accidentally pass API keys or auth tokens.
    "ADSP_LIBRARY_PATH", "INTEL_OPENVINO_DIR", "KMP_AFFINITY",
    "KMP_BLOCKTIME", "KMP_DUPLICATE_LIB_OK", "OMP_NUM_THREADS",
    "OMP_WAIT_POLICY", "ONEAPI_ROOT", "OPENVINO_DIR",
    "ORT_LOG_SEVERITY_LEVEL", "QNN_SDK_ROOT",
    "RYZEN_AI_INSTALLATION_PATH", "VAIP_CONFIG_HOME",
    "VITIS_AI_EP_VERBOSE", "XILINX_VART_FIRMWARE",
    "XLNX_ENABLE_CACHE", "XLNX_ONNX_EP_VERBOSE",
    "ZE_AFFINITY_MASK", "ZE_ENABLE_PCI_ID_DEVICE_ORDER",
    "NUMBER_OF_DPU_RUNNERS", "NUM_OF_DPU_RUNNERS",
    # Exact test hooks; all are inert unless SONDER_NPU_TEST_HOOKS=1.
    "SONDER_NPU_FAKE_RSS_MB", "SONDER_NPU_FORCE_NO_ORT",
    "SONDER_NPU_SIM_CRASH_ON_RUN", "SONDER_NPU_SIM_DELAY_MS",
    "SONDER_NPU_SIM_GARBAGE_ON_RUN", "SONDER_NPU_TEST_HOOKS",
})


def _worker_environment(source=None) -> dict:
    """Minimal explicit environment for Python and local vendor runtimes.

    Cloud/API credential namespaces and the parent Python import path are never
    inherited. Vendor-specific runtime variables are opt-in by exact name; the
    worker still runs as the same OS account and is not an OS sandbox.
    """
    source = os.environ if source is None else source
    environment = {}
    for name, value in source.items():
        upper = str(name).upper()
        if upper in _WORKER_ENV_KEYS:
            environment[str(name)] = str(value)
    if not any(name.upper() == "PATH" for name in environment):
        environment["PATH"] = os.defpath
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _env_float(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


class NpuUnavailable(Exception):
    """Typed accelerator unavailability; callers use existing fallbacks."""

    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class _ExchangeError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _validated_load_provenance(response, manifest) -> dict:
    """Validate worker load claims before a model is marked usable."""
    if response.get("ok") is not True:
        raise _ExchangeError("malformed", "worker load omitted literal ok=true")
    provider = response.get("provider")
    allowlist = list(manifest.get("providers") or [])
    if (
        not isinstance(provider, str)
        or provider not in npu_contract.PROVIDER_IDS
        or provider not in allowlist
    ):
        raise _ExchangeError("malformed", "worker load returned unknown provider")
    expected_ep = (
        npu_providers.SIMULATOR_EP
        if provider == "cpu-sim"
        else npu_providers.PROVIDER_EPS.get(provider, "")
    )
    ep = response.get("ep")
    chain = response.get("ep_chain")
    if (
        not expected_ep
        or ep != expected_ep
        or not isinstance(chain, list)
        or not chain
        or len(chain) > 4
        or any(not isinstance(item, str) for item in chain)
        or chain[0] != expected_ep
    ):
        raise _ExchangeError(
            "malformed", "worker load returned inconsistent execution providers",
        )
    for active_ep in chain:
        active_provider = npu_providers.provider_id_for_ep(active_ep)
        if not active_provider or active_provider not in allowlist:
            raise _ExchangeError(
                "malformed", "worker load exposed an unallowlisted provider",
            )
    if chain != [expected_ep]:
        raise _ExchangeError(
            "malformed", "worker load did not bind one exclusive provider",
        )
    provenance = {}
    for name in (
        "simulated", "cpu_fallback_disabled", "ep_fallback", "npu_attested",
    ):
        value = response.get(name)
        if not isinstance(value, bool):
            raise _ExchangeError(
                "malformed", "worker load omitted boolean %s" % name,
            )
        provenance[name] = value
    if provenance["simulated"] != (provider == "cpu-sim"):
        raise _ExchangeError("malformed", "worker load simulator claim mismatch")
    if provenance["ep_fallback"] != (provider != allowlist[0]):
        raise _ExchangeError("malformed", "worker load fallback claim mismatch")
    if provider in npu_contract.NPU_CLASS_PROVIDERS and (
        not provenance["cpu_fallback_disabled"]
    ):
        raise _ExchangeError(
            "malformed", "worker load permits ambiguous CPU fallback",
        )
    if provider in {"cpu", "cpu-sim"} and provenance["cpu_fallback_disabled"]:
        raise _ExchangeError("malformed", "worker load CPU fallback claim mismatch")
    if provenance["npu_attested"] and provider not in {"openvino", "qnn"}:
        raise _ExchangeError(
            "malformed", "worker load returned unsupported NPU attestation",
        )
    return {
        "provider": provider,
        "ep": ep,
        "ep_chain": list(chain),
        **provenance,
    }


def _canonical_detect_rows(response) -> list:
    """Validate and reduce worker detection to the exact host status schema."""
    if response.get("ok") is not True:
        raise _ExchangeError("malformed", "worker detect omitted literal ok=true")
    raw_rows = response.get("providers")
    if not isinstance(raw_rows, list) or len(raw_rows) > 16:
        raise _ExchangeError("malformed", "worker detect providers must be a list")
    rows = []
    seen = set()
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            continue
        provider_id = raw_row.get("id")
        if (
            not isinstance(provider_id, str)
            or provider_id not in npu_contract.PROVIDER_IDS
            or provider_id in seen
        ):
            continue
        seen.add(provider_id)
        rows.append({
            "id": provider_id,
            "label": npu_providers.provider_label(provider_id),
            "registered": raw_row.get("registered") is True,
            "detected": False,
            "runtime_ready": False,
            "ep": (
                npu_providers.SIMULATOR_EP
                if provider_id == "cpu-sim"
                else npu_providers.PROVIDER_EPS.get(provider_id, "")
            ),
            "reason": npu_contract.sanitize_error(
                raw_row.get("reason") or "", 160,
            ),
        })
    return rows


class _WindowsJob:
    """Best-effort kill-on-close Job Object for the worker process tree."""

    def __init__(self, proc):
        self.handle = None
        self._close_handle = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            class _IoCounters(ctypes.Structure):
                _fields_ = [
                    (name, ctypes.c_ulonglong)
                    for name in (
                        "ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount",
                        "WriteTransferCount", "OtherTransferCount",
                    )
                ]

            class _BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _ExtendedLimits(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE, wintypes.HANDLE,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ) or not kernel32.AssignProcessToJobObject(
                handle, wintypes.HANDLE(int(proc._handle))
            ):
                kernel32.CloseHandle(handle)
                return
            self.handle = handle
            self._close_handle = kernel32.CloseHandle
        except Exception:
            self.handle = None

    def close(self):
        handle, self.handle = self.handle, None
        close_handle, self._close_handle = self._close_handle, None
        if handle and close_handle is not None:
            try:
                close_handle(handle)
            except Exception:
                pass


def _is_reparse(info) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & reparse_flag
    )


def _staging_identity(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        raise OSError("staging root is not a plain directory")
    return (int(info.st_dev), int(info.st_ino))


def _discard_empty_staging_dir(path) -> None:
    """Remove a just-created empty stage without traversing its contents."""
    target = Path(path)
    try:
        if (
            target.parent.resolve() == Path(tempfile.gettempdir()).resolve()
            and target.name.startswith(_STAGING_PREFIX)
        ):
            os.rmdir(target)
    except OSError:
        pass


def _cleanup_staging_dir(path, identity) -> None:
    """Remove only the exact broker-created per-worker staging directory."""
    if path is None or identity is None:
        return
    target = Path(path)
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        if (
            target.parent.resolve() != temp_root
            or not target.name.startswith(_STAGING_PREFIX)
        ):
            return
        try:
            if _staging_identity(target) != identity:
                return
        except OSError:
            return
        safe_directories = []
        safe_files = []
        links = []
        for root, directories, files in os.walk(
            target, topdown=True, followlinks=False,
        ):
            retained = []
            for dirname in directories:
                child = Path(root) / dirname
                try:
                    info = os.stat(child, follow_symlinks=False)
                except OSError:
                    continue
                if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    links.append((child, info))
                else:
                    retained.append(dirname)
                    safe_directories.append(child)
            directories[:] = retained
            for filename in files:
                child = Path(root) / filename
                try:
                    info = os.stat(child, follow_symlinks=False)
                except OSError:
                    continue
                if _is_reparse(info):
                    links.append((child, info))
                elif stat.S_ISREG(info.st_mode):
                    safe_files.append(child)
        # Remove links/reparse points themselves without following them. If a
        # link cannot be removed, leak the bounded stage rather than traverse.
        for child, info in links:
            try:
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    os.unlink(child)
                else:
                    os.rmdir(child)
            except OSError:
                return
        for child in safe_files:
            try:
                os.chmod(child, 0o600)
            except OSError:
                pass
        for child in reversed(safe_directories):
            try:
                os.chmod(child, 0o700)
            except OSError:
                pass
        if _staging_identity(target) != identity:
            return
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        shutil.rmtree(target)
    except OSError:
        # Cleanup is best effort after the process tree is gone. Refuse any
        # broader or worker-supplied fallback path.
        return


class _Worker:
    """One child-process generation with its pump threads."""

    def __init__(
        self, proc, generation, windows_job=None, staging_dir=None,
        staging_identity=None,
    ):
        self.proc = proc
        self.generation = generation
        self.windows_job = windows_job
        self.staging_dir = staging_dir
        self.staging_identity = staging_identity
        self.queue = queue.Queue(maxsize=_WORKER_QUEUE_LIMIT)
        self.stderr_ring = collections.deque(maxlen=_STDERR_RING)
        self.dead = False
        self._destroy_lock = threading.Lock()
        self._reader = threading.Thread(
            target=self._pump_stdout, daemon=True,
            name="npu-reader-%d" % generation,
        )
        self._drainer = threading.Thread(
            target=self._pump_stderr, daemon=True,
            name="npu-stderr-%d" % generation,
        )
        self._reader.start()
        self._drainer.start()

    def _enqueue(self, payload) -> bool:
        try:
            self.queue.put_nowait(payload)
            return True
        except queue.Full:
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self.queue.put_nowait({
                    "_protocol_error": "worker response queue overflow",
                })
            except queue.Full:
                pass
            return False

    def _pump_stdout(self):
        stream = self.proc.stdout
        limit = npu_contract.MAX_LINE_BYTES
        while True:
            try:
                line = stream.readline(limit + 2)
            except (OSError, ValueError):
                line = b""
            if not line:
                self._enqueue({"_eof": True})
                return
            if len(line) > limit and not line.endswith(b"\n"):
                self._enqueue({"_protocol_error": "oversized worker line"})
                return
            try:
                payload = npu_contract.decode_line(line)
            except ValueError as exc:
                self._enqueue({"_protocol_error": str(exc)[:200]})
                return
            if not self._enqueue(payload):
                return

    def _pump_stderr(self):
        stream = self.proc.stderr
        while True:
            try:
                line = stream.readline(4096)
            except (OSError, ValueError):
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_ring.append(text[:200])

    def send(self, raw):
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def destroy(self):
        with self._destroy_lock:
            if self.dead:
                return
            self.dead = True
        if (
            self.windows_job is not None
            and self.windows_job.handle is not None
        ):
            self.windows_job.close()
        elif sys.platform == "win32" and self.proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError):
                pass
        elif sys.platform != "win32":
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        _cleanup_staging_dir(self.staging_dir, self.staging_identity)


class NpuBroker:
    def __init__(self, worker_path=None):
        self._worker_path = Path(
            worker_path or Path(__file__).resolve().with_name("npu_worker.py")
        )
        self._state_lock = threading.RLock()
        self._flight = threading.Lock()
        self._state = "cold"  # cold | warming | ready
        self._worker = None
        self._warming_worker = None
        self._warm_thread = None
        self._generation = 0
        self._counter = itertools.count(1)
        self._target_manifests = {}
        self._models = {}
        self._fingerprints = {}
        self._providers = None
        self._hello = {}
        self._circuit = {"state": "closed", "opened_ts": 0.0, "opens": 0}
        self._consecutive_failures = 0
        self._deaths_without_success = 0
        self._fallbacks = {}
        self._latency = collections.deque(maxlen=64)
        self._last_latency = 0
        self._spawns = 0
        self._idle_unloads = 0
        self._rss_evictions = 0
        self._last_rss_mb = 0
        self._last_used = 0.0
        self._ready_ts = 0.0
        self._last_error = ""
        self._ram_cache = (0.0, None)

    # -- configuration -----------------------------------------------------
    def _min_free_ram_gb(self):
        return max(0.0, _env_float("SONDER_NPU_MIN_FREE_RAM_GB", 2.0))

    def _max_rss_mb(self):
        return max(64, _env_int("SONDER_NPU_MAX_RSS_MB", 1536))

    def _idle_ttl_s(self):
        return max(1, _env_int("SONDER_NPU_IDLE_UNLOAD_S", 300))

    def _cooldown_s(self):
        return max(1, _env_int("SONDER_NPU_CIRCUIT_COOLDOWN_S", 120))

    def _available_ram_gb(self):
        override = os.environ.get("SONDER_AVAILABLE_RAM_GB", "").strip()
        if override:
            try:
                return max(0.0, float(override)), True
            except ValueError:
                pass
        now = time.monotonic()
        cached_ts, cached = self._ram_cache
        if cached is not None and now - cached_ts < 30.0:
            return cached
        try:
            import system_profile

            _total, available, live = system_profile._system_memory()
            result = (float(available or 0.0), bool(live))
        except Exception:
            result = (0.0, False)
        self._ram_cache = (now, result)
        return result

    def _declared_bundle_bytes(self, manifests) -> int:
        total_bundle = 0
        seen = set()
        for manifest in manifests or []:
            entries = [manifest.get("model")]
            entries.extend(manifest.get("extra_files") or [])
            if manifest.get("tokenizer"):
                entries.append(manifest["tokenizer"])
            if len(entries) > npu_contract.MAX_PINNED_FILES:
                raise ValueError("manifest declares too many pinned files")
            total = 0
            extra_paths = {
                str(entry.get("path") or "").casefold()
                for entry in manifest.get("extra_files") or []
                if isinstance(entry, dict)
            }
            for values in (manifest.get("provider_options") or {}).values():
                for value in (values or {}).values():
                    if isinstance(value, dict) and str(
                        value.get("path") or ""
                    ).casefold() not in extra_paths:
                        raise ValueError(
                            "provider option is not a pinned extra file"
                        )
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("manifest has an invalid pinned file entry")
                size = entry.get("bytes")
                if (
                    isinstance(size, bool)
                    or not isinstance(size, int)
                    or not 0 < size <= npu_contract.MAX_PINNED_FILE_BYTES
                ):
                    raise ValueError("manifest has an invalid pinned file size")
                key = (
                    str(manifest.get("dir") or ""),
                    str(entry.get("path") or "").casefold(),
                    str(entry.get("sha256") or ""),
                )
                if key not in seen:
                    seen.add(key)
                    total += size
            if total > npu_contract.MAX_BUNDLE_BYTES:
                raise ValueError("manifest pinned bundle exceeds size limit")
            total_bundle += total
        if total_bundle > npu_contract.MAX_BUNDLE_BYTES:
            raise ValueError("combined worker bundle exceeds size limit")
        return total_bundle

    def _ram_gate_reason(self, manifests=None):
        available, live = self._available_ram_gb()
        try:
            bundle_gb = self._declared_bundle_bytes(manifests) / 1024**3
        except ValueError:
            return "bundle_limit"
        # ORT commonly retains its own model/session allocation after Python
        # has read the pinned bytes, so reserve two bundle copies in addition
        # to the configured host headroom.
        required = self._min_free_ram_gb() + (2.0 * bundle_gb)
        if live and available < required:
            return "ram_gate"
        return ""

    # -- bookkeeping -------------------------------------------------------
    def _count(self, reason):
        with self._state_lock:
            self._fallbacks[reason] = self._fallbacks.get(reason, 0) + 1

    def _open_circuit_locked(self):
        self._circuit["state"] = "open"
        self._circuit["opened_ts"] = time.monotonic()
        self._circuit["opens"] += 1

    def _maybe_half_open_locked(self):
        if self._circuit["state"] != "open":
            return
        elapsed = time.monotonic() - self._circuit["opened_ts"]
        if elapsed >= self._cooldown_s():
            self._circuit["state"] = "half_open"

    def _record_failure(self, reason, death=False, error=""):
        with self._state_lock:
            self._count(reason)
            if error:
                self._last_error = npu_contract.sanitize_error(error, 200)
            if reason in _FAILURE_REASONS:
                self._consecutive_failures += 1
                if death:
                    self._deaths_without_success += 1
                if self._circuit["state"] == "half_open":
                    self._open_circuit_locked()
                elif (
                    self._deaths_without_success >= _DEATH_LIMIT
                    or self._consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT
                ):
                    self._open_circuit_locked()

    def _record_success(self, elapsed_ms):
        with self._state_lock:
            self._latency.append(int(elapsed_ms))
            self._last_latency = int(elapsed_ms)
            self._consecutive_failures = 0
            self._deaths_without_success = 0
            if self._circuit["state"] == "half_open":
                self._circuit["state"] = "closed"

    def _mark_providers_cold_locked(self):
        if not self._providers:
            return
        self._providers = [
            {
                **row,
                "runtime_ready": False,
                "reason": (
                    "worker cold; no compatible session loaded"
                    if row.get("runtime_ready") is True
                    else row.get("reason", "")
                ),
            }
            for row in self._providers
        ]

    def _teardown_worker_locked(self):
        worker = self._worker
        warming_worker = self._warming_worker
        self._worker = None
        self._warming_worker = None
        self._models = {}
        self._fingerprints = {}
        self._mark_providers_cold_locked()
        if worker is not None:
            worker.destroy()
        if warming_worker is not None and warming_worker is not worker:
            warming_worker.destroy()

    def _on_worker_dead(self, worker, reason, detail=""):
        with self._state_lock:
            if self._worker is worker:
                self._teardown_worker_locked()
                self._state = "cold"
            elif not worker.dead:
                worker.destroy()
            self._record_failure(reason, death=True, error=detail)

    # -- lifecycle ---------------------------------------------------------
    def _spawn_worker(self, manifests=None):
        gate = self._ram_gate_reason(manifests)
        if gate:
            detail = (
                "manifest bundle exceeds accelerator limits"
                if gate == "bundle_limit" else "available RAM below spawn gate"
            )
            raise _ExchangeError(gate, detail)
        command = [sys.executable, "-X", "utf8", str(self._worker_path)]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        staging_dir = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX)).resolve()
        try:
            staging_identity = _staging_identity(staging_dir)
        except OSError:
            _discard_empty_staging_dir(staging_dir)
            raise
        environment = _worker_environment()
        environment["SONDER_NPU_STAGE_DIR"] = str(staging_dir)
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._worker_path.parent),
                creationflags=creationflags,
                env=environment,
                start_new_session=sys.platform != "win32",
            )
        except Exception:
            _cleanup_staging_dir(staging_dir, staging_identity)
            raise
        windows_job = _WindowsJob(proc) if sys.platform == "win32" else None
        with self._state_lock:
            self._spawns += 1
            generation = self._generation
        return _Worker(
            proc, generation, windows_job=windows_job, staging_dir=staging_dir,
            staging_identity=staging_identity,
        )

    def _check_response_rss(self, worker, payload, stage):
        rss = payload.get("rss_mb") if isinstance(payload, dict) else None
        if (
            isinstance(rss, bool)
            or not isinstance(rss, (int, float))
            or not math.isfinite(float(rss))
            or rss < 0
        ):
            worker.destroy()
            raise _ExchangeError(
                "malformed", "worker %s omitted a valid rss_mb" % stage,
            )
        rss = int(rss)
        with self._state_lock:
            self._last_rss_mb = rss
        if rss <= self._max_rss_mb():
            return
        worker.destroy()
        with self._state_lock:
            if self._worker is worker:
                self._worker = None
                self._models = {}
                self._fingerprints = {}
                self._mark_providers_cold_locked()
                self._state = "cold"
            if self._warming_worker is worker:
                self._warming_worker = None
            self._rss_evictions += 1
        raise _ExchangeError(
            "rss_limit",
            "worker RSS %dMB exceeded %dMB at %s"
            % (rss, self._max_rss_mb(), stage),
        )

    def _wait_event(self, worker, event, timeout_s):
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ExchangeError("crash", "worker %s timed out" % event)
            try:
                item = worker.queue.get(timeout=remaining)
            except queue.Empty:
                raise _ExchangeError("crash", "worker %s timed out" % event)
            if item.get("_eof"):
                raise _ExchangeError("crash", "worker exited during %s" % event)
            if item.get("_protocol_error"):
                raise _ExchangeError("malformed", item["_protocol_error"])
            if item.get("event") == event:
                self._check_response_rss(worker, item, event)
                return item

    def _exchange(self, worker, payload, timeout_s):
        if "id" in payload:
            raise ValueError("protocol request id is broker-owned")
        request = {**payload, "id": next(self._counter)}
        raw = npu_contract.encode_line(request)
        try:
            worker.send(raw)
        except (OSError, ValueError) as exc:
            raise _ExchangeError("crash", "worker pipe closed: %s" % exc)
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ExchangeError("timeout", "deadline exceeded")
            try:
                item = worker.queue.get(timeout=remaining)
            except queue.Empty:
                raise _ExchangeError("timeout", "deadline exceeded")
            if item.get("_eof"):
                raise _ExchangeError("crash", "worker exited mid-call")
            if item.get("_protocol_error"):
                raise _ExchangeError("malformed", item["_protocol_error"])
            if item.get("id") == request["id"]:
                self._check_response_rss(
                    worker, item, str(payload.get("op") or "exchange"),
                )
                return item

    def _warmup(self, generation, manifests):
        # Replacement and loading are exclusive with inference. A ready worker
        # may still be serving the call that discovered a newly configured
        # manifest; wait for that call instead of killing it underneath the
        # reader thread.
        with self._flight:
            with self._state_lock:
                if self._generation != generation:
                    return
                self._teardown_worker_locked()
            self._warmup_exclusive(generation, manifests)

    def _warmup_exclusive(self, generation, manifests):
        try:
            worker = self._spawn_worker(manifests.values())
            with self._state_lock:
                if self._generation != generation:
                    worker.destroy()
                    return
                self._warming_worker = worker
        except _ExchangeError as exc:
            with self._state_lock:
                if self._generation == generation:
                    self._state = "cold"
            self._count(exc.reason)
            return
        except (OSError, ValueError) as exc:
            with self._state_lock:
                if self._generation == generation:
                    self._state = "cold"
            self._record_failure(
                "spawn", death=True, error="spawn failed: %s" % str(exc)[:160],
            )
            return
        try:
            ready = self._wait_event(
                worker, "ready", npu_contract.READY_TIMEOUT_MS / 1000.0,
            )
            if ready.get("protocol") != npu_contract.PROTOCOL_VERSION:
                raise _ExchangeError("malformed", "worker protocol version mismatch")
            hello = self._exchange(worker, {"op": "hello"}, 10.0)
            if (
                hello.get("ok") is not True
                or hello.get("protocol") != npu_contract.PROTOCOL_VERSION
            ):
                raise _ExchangeError("malformed", "worker hello version mismatch")
            detect = self._exchange(worker, {"op": "detect"}, 10.0)
            detected_provider_rows = _canonical_detect_rows(detect)
            models = {}
            fingerprints = {}
            for manifest_hash, manifest in manifests.items():
                row = {
                    "name": str(manifest.get("name") or "")[:64],
                    "operation": str(manifest.get("operation") or ""),
                    # Internal correlation key. npu_service strips this before
                    # returning status outside the broker boundary.
                    "manifest_hash": manifest_hash,
                    "manifest_hash8": manifest_hash[:8],
                    "provider": "",
                    "ep": "",
                    "ep_chain": [],
                    "ep_fallback": False,
                    "cpu_fallback_disabled": False,
                    "simulated": False,
                    "npu_attested": False,
                    "ok": False,
                    "error": "",
                }
                drift = npu_manifest.verify_files(manifest)
                if drift:
                    row["error"] = npu_contract.sanitize_error(drift, 200)
                    self._count("hash_drift")
                else:
                    response = self._exchange(
                        worker,
                        {"op": "load", "manifest": manifest},
                        npu_contract.LOAD_TIMEOUT_MS / 1000.0,
                    )
                    # Verify again after the worker has loaded the exact pinned
                    # model bytes. This catches ordinary concurrent updates;
                    # the worker's own byte hashing binds what ORT received.
                    post_load_drift = npu_manifest.verify_files(manifest)
                    if post_load_drift:
                        row["error"] = npu_contract.sanitize_error(
                            post_load_drift, 200,
                        )
                        self._count("hash_drift")
                    elif response.get("ok") is True:
                        if response.get("manifest_hash") != manifest_hash:
                            raise _ExchangeError(
                                "malformed",
                                "worker returned a mismatched manifest hash",
                            )
                        fingerprint = npu_manifest.file_fingerprints(manifest)
                        row.update(
                            ok=True,
                            **_validated_load_provenance(response, manifest),
                        )
                        fingerprints[manifest_hash] = fingerprint
                    elif response.get("ok") is False:
                        error = response.get("error")
                        if not isinstance(error, dict) or not isinstance(
                            error.get("message"), str,
                        ):
                            raise _ExchangeError(
                                "malformed", "worker load returned malformed error",
                            )
                        row["error"] = npu_contract.sanitize_error(
                            error["message"], 200,
                        )
                    else:
                        raise _ExchangeError(
                            "malformed", "worker load omitted literal boolean ok",
                        )
                models[manifest_hash] = row
        except _ExchangeError as exc:
            worker.destroy()
            with self._state_lock:
                active_generation = self._generation == generation
                if active_generation:
                    self._state = "cold"
                if self._warming_worker is worker:
                    self._warming_worker = None
            if not active_generation:
                # stop()/replacement deliberately invalidated this generation;
                # its killed worker is not a crash or circuit-breaker death.
                return
            reason = "crash" if exc.reason == "timeout" else exc.reason
            self._record_failure(reason, death=True, error=exc.detail)
            return
        except Exception as exc:
            worker.destroy()
            with self._state_lock:
                active_generation = self._generation == generation
                if active_generation:
                    self._state = "cold"
                if self._warming_worker is worker:
                    self._warming_worker = None
            if active_generation:
                self._record_failure(
                    "malformed", death=True,
                    error="unexpected worker response: %s" % str(exc)[:160],
                )
            return
        with self._state_lock:
            if self._generation != generation:
                if self._warming_worker is worker:
                    self._warming_worker = None
                worker.destroy()
                return
            self._worker = worker
            self._warming_worker = None
            self._models = models
            self._fingerprints = fingerprints
            successful = {
                row["provider"]: row
                for row in models.values() if row.get("ok")
            }
            providers = []
            for raw_row in detected_provider_rows:
                provider_id = raw_row["id"]
                loaded = successful.get(provider_id)
                reason = raw_row.get("reason")
                if loaded:
                    reason = (
                        "compatible NPU session loaded"
                        if loaded.get("npu_attested")
                        else "compatible session loaded; NPU target not attested"
                    )
                provider_row = {
                    **raw_row,
                    # Hardware detection is host-owned. The broker reports
                    # registration and compatible-session readiness only.
                    "detected": False,
                    "runtime_ready": bool(loaded),
                    "reason": npu_contract.sanitize_error(reason or "", 160),
                }
                providers.append(provider_row)
            self._providers = providers
            self._hello = {
                "python": str(hello.get("python") or "")[:20],
                "ort_version": str(hello.get("ort_version") or "")[:40],
                "ort_error": npu_contract.sanitize_error(
                    hello.get("ort_error") or "", 160,
                ),
                "platform": str(hello.get("platform") or "")[:20],
                "pid": hello.get("pid") if isinstance(hello.get("pid"), int) else 0,
            }
            self._state = "ready"
            self._ready_ts = time.monotonic()
            self._last_used = time.monotonic()
            ttl = self._idle_ttl_s()
        threading.Thread(
            target=self._reap_idle, args=(generation, ttl), daemon=True,
            name="npu-idle-%d" % generation,
        ).start()

    def _reap_idle(self, generation, ttl):
        interval = min(1.0, max(0.1, ttl / 5.0))
        while True:
            time.sleep(interval)
            with self._state_lock:
                if (
                    self._generation != generation
                    or self._state != "ready"
                    or self._worker is None
                ):
                    return
                idle = time.monotonic() - self._last_used
            if idle < ttl:
                continue
            if not self._flight.acquire(blocking=False):
                continue
            try:
                with self._state_lock:
                    if self._generation == generation and self._state == "ready":
                        self._teardown_worker_locked()
                        self._state = "cold"
                        self._idle_unloads += 1
                return
            finally:
                self._flight.release()

    def ensure_warm(self, manifests):
        """Trigger a background warmup; never blocks the caller."""
        with self._state_lock:
            self._maybe_half_open_locked()
            if self._circuit["state"] == "open":
                return False
            for manifest in manifests or []:
                manifest_hash = str(manifest.get("manifest_hash") or "")
                if manifest_hash:
                    for old_hash, old in list(self._target_manifests.items()):
                        if (
                            old_hash != manifest_hash
                            and old.get("operation") == manifest.get("operation")
                        ):
                            self._target_manifests.pop(old_hash, None)
                    self._target_manifests[manifest_hash] = manifest
            while len(self._target_manifests) > 8:
                self._target_manifests.pop(next(iter(self._target_manifests)))
            if self._state == "ready":
                missing = [
                    manifest_hash
                    for manifest_hash in self._target_manifests
                    if not self._models.get(manifest_hash, {}).get("ok")
                ]
                if not missing:
                    return True
            if self._state == "warming":
                return True
            gate = self._ram_gate_reason(self._target_manifests.values())
            if gate:
                self._count(gate)
                self._last_error = (
                    "manifest bundle exceeds accelerator limits"
                    if gate == "bundle_limit" else "available RAM below spawn gate"
                )
                return False
            self._state = "warming"
            self._generation += 1
            generation = self._generation
            targets = dict(self._target_manifests)
            thread = threading.Thread(
                target=self._warmup, args=(generation, targets), daemon=True,
                name="npu-warmup-%d" % generation,
            )
            self._warm_thread = thread
        thread.start()
        return True

    def wait_ready(self, timeout=10.0):
        """Test/diagnostic helper: wait for the pending warmup to settle."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._state_lock:
                state = self._state
                warming = (
                    self._warm_thread is not None and self._warm_thread.is_alive()
                )
            if state == "ready" and not warming:
                return True
            if state != "warming" and not warming:
                return False
            time.sleep(0.02)
        with self._state_lock:
            return (
                self._state == "ready"
                and (
                    self._warm_thread is None
                    or not self._warm_thread.is_alive()
                )
            )

    # -- calls -------------------------------------------------------------
    def call(self, manifest, payload, deadline_ms=None):
        """Run one accelerator request; raises NpuUnavailable on any miss."""
        if not isinstance(payload, dict):
            raise NpuUnavailable("invalid", "accelerator payload must be an object")
        reserved = sorted(set(payload) & _CALLER_RESERVED_KEYS)
        if reserved:
            raise NpuUnavailable(
                "invalid",
                "accelerator payload contains broker-owned key(s): %s"
                % ", ".join(reserved),
            )
        operation = str(manifest.get("operation") or "")
        limits = manifest.get("limits") or {}
        deadline = npu_contract.clamp_deadline_ms(
            deadline_ms if deadline_ms is not None else limits.get("deadline_ms"),
            operation,
        )
        with self._state_lock:
            self._maybe_half_open_locked()
            if self._circuit["state"] == "open":
                self._count("circuit_open")
                raise NpuUnavailable("circuit_open")
            state = self._state
            worker = self._worker if state == "ready" else None
            if state == "cold":
                gate = self._ram_gate_reason([manifest])
                if gate:
                    self._count(gate)
                    raise NpuUnavailable(gate)
        if state == "warming":
            self._count("warming")
            raise NpuUnavailable("warming")
        if worker is None:
            self.ensure_warm([manifest])
            reason = "warming" if self._state == "warming" else "cold"
            self._count(reason)
            raise NpuUnavailable(reason)
        if not self._flight.acquire(blocking=False):
            self._count("busy")
            raise NpuUnavailable("busy")
        try:
            with self._state_lock:
                if self._worker is not worker or self._state != "ready":
                    self._count("cold")
                    raise NpuUnavailable("cold")
                row = self._models.get(str(manifest.get("manifest_hash") or ""))
                self._last_used = time.monotonic()
            if row is None or not row.get("ok"):
                if row is None:
                    # The first capability used may have warmed only its own
                    # manifest. Schedule a serialized replacement containing
                    # every target accumulated so far, then let this call use
                    # its existing local fallback while the worker warms.
                    self.ensure_warm([manifest])
                    self._count("warming")
                    raise NpuUnavailable("warming", "manifest is warming")
                self._count("manifest_unhealthy")
                raise NpuUnavailable(
                    "manifest_unhealthy", (row or {}).get("error", ""),
                )
            # Full content hashing happened at load and bound the immutable
            # in-memory session/private staged assets. Calls use a cheap source
            # fingerprint check so tiny NPU work does not reread a 1 GiB bundle.
            drift = npu_manifest.fingerprint_drift(
                manifest,
                self._fingerprints.get(
                    str(manifest.get("manifest_hash") or ""),
                ),
            )
            if drift:
                detail = npu_contract.sanitize_error(drift, 200)
                with self._state_lock:
                    row["ok"] = False
                    row["error"] = detail
                    self._last_error = detail
                self._count("hash_drift")
                raise NpuUnavailable("manifest_unhealthy", detail)
            request = {
                **payload,
                "op": "run",
                "manifest_hash": str(manifest.get("manifest_hash") or ""),
            }
            started = time.monotonic()
            try:
                response = self._exchange(worker, request, deadline / 1000.0)
            except ValueError as exc:
                self._count("oversized")
                raise NpuUnavailable("oversized", str(exc)[:160])
            except _ExchangeError as exc:
                self._on_worker_dead(worker, exc.reason, exc.detail)
                raise NpuUnavailable(exc.reason, exc.detail)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if not isinstance(response.get("ok"), bool):
                self._on_worker_dead(
                    worker, "malformed", "worker omitted literal boolean ok",
                )
                raise NpuUnavailable("malformed", "worker response malformed")
            if response["ok"] is False:
                error = response.get("error")
                if not isinstance(error, dict) or not isinstance(
                    error.get("message"), str,
                ):
                    self._on_worker_dead(
                        worker, "malformed", "worker returned malformed error",
                    )
                    raise NpuUnavailable("malformed", "worker response malformed")
                detail = npu_contract.sanitize_error(
                    error["message"], 200,
                )
                self._record_failure("worker_error", error=detail)
                raise NpuUnavailable("worker_error", detail)
            if response.get("manifest_hash") != request["manifest_hash"]:
                self._on_worker_dead(
                    worker, "malformed", "worker returned mismatched manifest hash",
                )
                raise NpuUnavailable(
                    "malformed", "worker returned mismatched manifest hash",
                )
            provenance_fields = (
                "provider", "ep", "ep_chain", "simulated",
                "cpu_fallback_disabled", "ep_fallback", "npu_attested",
            )
            if any(
                response.get(field) != row.get(field)
                for field in provenance_fields
            ):
                self._on_worker_dead(
                    worker, "malformed",
                    "worker run provenance differs from the loaded session",
                )
                raise NpuUnavailable(
                    "malformed", "worker run provenance mismatch",
                )
            self._record_success(elapsed_ms)
            with self._state_lock:
                self._last_used = time.monotonic()
            return response
        finally:
            self._flight.release()

    # -- teardown / reporting ---------------------------------------------
    def stop(self, reason="stop"):
        with self._state_lock:
            self._generation += 1
            self._teardown_worker_locked()
            self._state = "cold"
            if reason == "idle":
                self._idle_unloads += 1

    def shutdown(self):
        self.stop("shutdown")

    def _percentile(self, samples, fraction):
        if not samples:
            return 0
        ordered = sorted(samples)
        index = min(len(ordered) - 1, int(fraction * (len(ordered) - 1) + 0.5))
        return int(ordered[index])

    def status(self):
        with self._state_lock:
            self._maybe_half_open_locked()
            now = time.monotonic()
            samples = list(self._latency)
            cooldown_remaining = 0
            if self._circuit["state"] == "open":
                cooldown_remaining = max(
                    0,
                    int(
                        self._cooldown_s()
                        - (now - self._circuit["opened_ts"])
                    ),
                )
            worker_state = self._state if self._state != "warming" else "warming"
            return {
                "protocol": npu_contract.PROTOCOL_VERSION,
                "worker": {
                    "state": worker_state,
                    "pid": self._hello.get("pid", 0) if self._worker else 0,
                    "spawns": self._spawns,
                    "idle_unloads": self._idle_unloads,
                    "rss_evictions": self._rss_evictions,
                    "rss_mb": self._last_rss_mb,
                    "uptime_s": (
                        int(now - self._ready_ts)
                        if self._worker is not None
                        else 0
                    ),
                    "idle_s": (
                        int(now - self._last_used)
                        if self._worker is not None and self._last_used
                        else 0
                    ),
                },
                "circuit": {
                    "state": self._circuit["state"],
                    "opens": self._circuit["opens"],
                    "cooldown_remaining_s": cooldown_remaining,
                    "consecutive_failures": self._consecutive_failures,
                    "deaths_without_success": self._deaths_without_success,
                },
                "hello": dict(self._hello),
                "providers": [dict(row) for row in self._providers or []],
                "models": [dict(row) for row in self._models.values()],
                "latency_ms": {
                    "count": len(samples),
                    "last": self._last_latency,
                    "p50": self._percentile(samples, 0.5),
                    "p95": self._percentile(samples, 0.95),
                },
                "fallbacks": dict(self._fallbacks),
                "last_error": self._last_error,
            }


# The live broker (and its child process) must survive helper live-reload the
# same way activity spans do: module code refreshes, process ownership does not.
if "_BROKER" not in globals():
    _BROKER = None
if "_BROKER_GUARD" not in globals():
    _BROKER_GUARD = threading.Lock()


def get_broker() -> NpuBroker:
    global _BROKER
    with _BROKER_GUARD:
        if _BROKER is None:
            import atexit

            _BROKER = NpuBroker()
            atexit.register(_BROKER.shutdown)
        return _BROKER


def reset_for_tests():
    global _BROKER
    with _BROKER_GUARD:
        if _BROKER is not None:
            _BROKER.shutdown()
        _BROKER = None

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
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import npu_contract
import npu_manifest


_FAILURE_REASONS = frozenset({
    "timeout", "crash", "malformed", "worker_error", "spawn",
})
_CONSECUTIVE_FAILURE_LIMIT = 3
# Restart-once: a crashed worker may be respawned automatically one time; a
# second death without an intervening success opens the circuit.
_DEATH_LIMIT = 2
_STDERR_RING = 40


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


class _Worker:
    """One child-process generation with its pump threads."""

    def __init__(self, proc, generation):
        self.proc = proc
        self.generation = generation
        self.queue = queue.Queue()
        self.stderr_ring = collections.deque(maxlen=_STDERR_RING)
        self.dead = False
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

    def _pump_stdout(self):
        stream = self.proc.stdout
        limit = npu_contract.MAX_LINE_BYTES
        while True:
            try:
                line = stream.readline(limit + 2)
            except (OSError, ValueError):
                line = b""
            if not line:
                self.queue.put({"_eof": True})
                return
            if len(line) > limit and not line.endswith(b"\n"):
                self.queue.put({"_protocol_error": "oversized worker line"})
                return
            try:
                payload = npu_contract.decode_line(line)
            except ValueError as exc:
                self.queue.put({"_protocol_error": str(exc)[:200]})
                return
            self.queue.put(payload)

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
        self.dead = True
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


class NpuBroker:
    def __init__(self, worker_path=None):
        self._worker_path = Path(
            worker_path or Path(__file__).resolve().with_name("npu_worker.py")
        )
        self._state_lock = threading.RLock()
        self._flight = threading.Lock()
        self._state = "cold"  # cold | warming | ready
        self._worker = None
        self._warm_thread = None
        self._generation = 0
        self._counter = itertools.count(1)
        self._target_manifests = {}
        self._models = {}
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

    def _ram_gate_reason(self):
        available, live = self._available_ram_gb()
        if live and available < self._min_free_ram_gb():
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
                self._last_error = str(error)[:200]
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

    def _teardown_worker_locked(self):
        worker = self._worker
        self._worker = None
        self._models = {}
        if worker is not None:
            worker.destroy()

    def _on_worker_dead(self, worker, reason, detail=""):
        with self._state_lock:
            if self._worker is worker:
                self._teardown_worker_locked()
                self._state = "cold"
            elif not worker.dead:
                worker.destroy()
            self._record_failure(reason, death=True, error=detail)

    # -- lifecycle ---------------------------------------------------------
    def _spawn_worker(self):
        gate = self._ram_gate_reason()
        if gate:
            raise _ExchangeError("ram_gate", "available RAM below spawn gate")
        command = [sys.executable, "-X", "utf8", str(self._worker_path)]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self._worker_path.parent),
            creationflags=creationflags,
        )
        with self._state_lock:
            self._spawns += 1
            generation = self._generation
        return _Worker(proc, generation)

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
                return item

    def _exchange(self, worker, payload, timeout_s):
        request = {"id": next(self._counter), **payload}
        raw = npu_contract.encode_line(request)
        try:
            worker.send(raw)
        except OSError as exc:
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
                return item

    def _warmup(self, generation, manifests):
        try:
            worker = self._spawn_worker()
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
            self._wait_event(
                worker, "ready", npu_contract.READY_TIMEOUT_MS / 1000.0,
            )
            hello = self._exchange(worker, {"op": "hello"}, 10.0)
            detect = self._exchange(worker, {"op": "detect"}, 10.0)
            models = {}
            for manifest_hash, manifest in manifests.items():
                row = {
                    "name": str(manifest.get("name") or "")[:64],
                    "operation": str(manifest.get("operation") or ""),
                    "manifest_hash8": manifest_hash[:8],
                    "provider": "",
                    "ep": "",
                    "ep_fallback": False,
                    "simulated": False,
                    "ok": False,
                    "error": "",
                }
                drift = npu_manifest.verify_files(manifest)
                if drift:
                    row["error"] = drift[:200]
                    self._count("hash_drift")
                else:
                    response = self._exchange(
                        worker,
                        {"op": "load", "manifest": manifest},
                        npu_contract.LOAD_TIMEOUT_MS / 1000.0,
                    )
                    if response.get("ok"):
                        row.update(
                            ok=True,
                            provider=str(response.get("provider") or "")[:24],
                            ep=str(response.get("ep") or "")[:48],
                            ep_fallback=bool(response.get("ep_fallback")),
                            simulated=bool(response.get("simulated")),
                        )
                    else:
                        error = (response.get("error") or {}).get("message", "")
                        row["error"] = str(error)[:200]
                models[manifest_hash] = row
        except _ExchangeError as exc:
            worker.destroy()
            with self._state_lock:
                if self._generation == generation:
                    self._state = "cold"
            reason = "crash" if exc.reason == "timeout" else exc.reason
            self._record_failure(reason, death=True, error=exc.detail)
            return
        with self._state_lock:
            if self._generation != generation:
                worker.destroy()
                return
            self._worker = worker
            self._models = models
            self._providers = list(detect.get("providers") or [])[:16]
            self._hello = {
                "python": str(hello.get("python") or "")[:20],
                "ort_version": str(hello.get("ort_version") or "")[:40],
                "ort_error": str(hello.get("ort_error") or "")[:160],
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
                    self._target_manifests[manifest_hash] = manifest
            while len(self._target_manifests) > 8:
                self._target_manifests.pop(next(iter(self._target_manifests)))
            if self._state == "ready":
                missing = [
                    manifest_hash
                    for manifest_hash in self._target_manifests
                    if manifest_hash not in self._models
                ]
                if not missing:
                    return True
                self._teardown_worker_locked()
                self._state = "cold"
            if self._state == "warming":
                return True
            gate = self._ram_gate_reason()
            if gate:
                self._count("ram_gate")
                self._last_error = "available RAM below spawn gate"
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
            if state == "ready":
                return True
            if state != "warming" and not warming:
                return False
            time.sleep(0.02)
        return self._state == "ready"

    # -- calls -------------------------------------------------------------
    def call(self, manifest, payload, deadline_ms=None):
        """Run one accelerator request; raises NpuUnavailable on any miss."""
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
                gate = self._ram_gate_reason()
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
                self._count("manifest_unhealthy")
                raise NpuUnavailable(
                    "manifest_unhealthy", (row or {}).get("error", ""),
                )
            request = {
                "op": "run",
                "manifest_hash": str(manifest.get("manifest_hash") or ""),
                **payload,
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
            if not response.get("ok"):
                error = response.get("error") or {}
                detail = str(error.get("message") or "")[:200]
                self._record_failure("worker_error", error=detail)
                raise NpuUnavailable("worker_error", detail)
            self._record_success(elapsed_ms)
            rss = response.get("rss_mb")
            evict = False
            with self._state_lock:
                self._last_used = time.monotonic()
                if (
                    isinstance(rss, (int, float))
                    and not isinstance(rss, bool)
                ):
                    self._last_rss_mb = int(rss)
                    evict = self._last_rss_mb > self._max_rss_mb()
                if evict and self._worker is worker:
                    self._teardown_worker_locked()
                    self._state = "cold"
                    self._rss_evictions += 1
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

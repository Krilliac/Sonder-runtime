"""Restartable accelerator worker child process.

All vendor/onnxruntime imports and native DLL loads happen here, never in the
main server process. The broker speaks bounded JSONL over stdio; the worker's
real stdout is re-pointed at stderr immediately so a chatty vendor runtime can
never corrupt the protocol stream. Any protocol violation exits the process:
the broker treats that as a crash and applies its restart/circuit policy.

Simulator provider ``cpu-sim`` is implemented with the stdlib only, so this
worker is fully exercisable in CI without any optional dependency.
"""
from __future__ import annotations

import math
import hashlib
import os
import sys
import time

import npu_contract
import npu_providers


_SESSIONS = {}
_ORT_STATE = {"module": None, "error": "", "tried": False}


def _test_hooks_enabled() -> bool:
    return os.environ.get("SONDER_NPU_TEST_HOOKS", "").strip() == "1"


def _hook(name) -> str:
    if not _test_hooks_enabled():
        return ""
    return os.environ.get(name, "").strip()


def _ort():
    """Import onnxruntime lazily; report absence instead of raising."""
    if _ORT_STATE["tried"]:
        return _ORT_STATE["module"], _ORT_STATE["error"]
    _ORT_STATE["tried"] = True
    if _hook("SONDER_NPU_FORCE_NO_ORT") == "1":
        _ORT_STATE["error"] = "onnxruntime import disabled by test hook"
        return None, _ORT_STATE["error"]
    try:
        import onnxruntime  # vendor import: allowed only in this process

        try:
            onnxruntime.set_default_logger_severity(3)
        except Exception:
            pass
        _ORT_STATE["module"] = onnxruntime
    except Exception as exc:
        _ORT_STATE["error"] = str(exc)[:200]
    return _ORT_STATE["module"], _ORT_STATE["error"]


def _rss_mb() -> int:
    fake = _hook("SONDER_NPU_FAKE_RSS_MB")
    if fake:
        try:
            return int(float(fake))
        except ValueError:
            pass
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return int(counters.WorkingSetSize / (1024 * 1024))
        except Exception:
            pass
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as stream:
            resident_pages = int(stream.read().split()[1])
        return int(resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return int(peak / divisor)
    except Exception:
        return 0


def _sim_vector(preprocess, text, dimension) -> list:
    seed = hashlib.sha256()
    seed.update(str(preprocess).encode("utf-8"))
    seed.update(b"\x00")
    seed.update(str(text).encode("utf-8", "replace"))
    base = seed.digest()
    raw = b""
    counter = 0
    while len(raw) < dimension * 2:
        raw += hashlib.sha256(base + counter.to_bytes(4, "big")).digest()
        counter += 1
    values = []
    for index in range(dimension):
        pair = (raw[2 * index] << 8) | raw[2 * index + 1]
        values.append((pair / 65535.0) * 2.0 - 1.0)
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 6) for value in values]


# Fixed simulator weights over the 16 exec-route features: length, actions,
# and sequence markers push toward autopilot; question/explain shapes pull back.
_SIM_ROUTE_WEIGHTS = (
    1.2, 0.6, 1.8, 1.6, 0.4, 0.3, 0.8, 0.7,
    0.5, 0.4, -0.8, 0.3, 0.3, 0.6, 0.5, 0.1,
)
_SIM_ROUTE_BIAS = -2.2


def _sim_route_scores(features) -> dict:
    total = _SIM_ROUTE_BIAS
    for index, value in enumerate(features):
        weight = (
            _SIM_ROUTE_WEIGHTS[index]
            if index < len(_SIM_ROUTE_WEIGHTS)
            else 0.0
        )
        total += weight * float(value)
    autopilot = 1.0 / (1.0 + math.exp(-total))
    autopilot = min(1.0, max(0.0, round(autopilot, 6)))
    return {"workbench": round(1.0 - autopilot, 6), "autopilot": autopilot}


def _load_tokenizer(manifest):
    entry = manifest.get("tokenizer")
    if not entry:
        return None, "manifest declares no tokenizer for a real provider"
    try:
        from tokenizers import Tokenizer  # vendor import: worker only
    except Exception as exc:
        return None, "tokenizer runtime (tokenizers) not installed: %s" % (
            str(exc)[:120],
        )
    path = os.path.join(manifest.get("dir") or "", entry["path"])
    try:
        return Tokenizer.from_file(path), ""
    except Exception as exc:
        return None, "tokenizer load failed: %s" % str(exc)[:160]


def _create_real_session(manifest, provider_id):
    ort, error = _ort()
    if ort is None:
        return None, "onnxruntime not installed: %s" % error
    ep_name = npu_providers.PROVIDER_EPS[provider_id]
    options = (manifest.get("provider_options") or {}).get(provider_id) or {}
    if provider_id == "openvino" and "device_type" not in options:
        options = {**options, "device_type": "NPU"}
    model_path = os.path.join(manifest.get("dir") or "", manifest["model"]["path"])
    try:
        sess_options = ort.SessionOptions()
        sess_options.log_severity_level = 3
        session = ort.InferenceSession(
            model_path,
            sess_options,
            providers=[ep_name],
            provider_options=[options] if options else None,
        )
    except Exception as exc:
        return None, "session load failed on %s: %s" % (ep_name, str(exc)[:200])
    return session, ""


def _load(request) -> dict:
    manifest = request.get("manifest")
    if not isinstance(manifest, dict):
        return _error(request, "load_invalid", "load needs a manifest object")
    manifest_hash = str(manifest.get("manifest_hash") or "")
    provider_rows = npu_providers.detect_providers(*_ort())
    provider_id, ep_fallback, error = npu_providers.resolve_provider(
        manifest, provider_rows,
    )
    if not provider_id:
        return _error(request, "load_failed", error)
    entry = {
        "manifest": manifest,
        "provider": provider_id,
        "ep": npu_providers.SIMULATOR_EP,
        "ep_chain": [npu_providers.SIMULATOR_EP],
        "ep_fallback": ep_fallback,
        "simulated": provider_id == "cpu-sim",
        "session": None,
        "tokenizer": None,
    }
    if provider_id != "cpu-sim":
        session, error = _create_real_session(manifest, provider_id)
        if session is None:
            return _error(request, "load_failed", error)
        try:
            actual = list(session.get_providers())
        except Exception:
            actual = []
        requested_ep = npu_providers.PROVIDER_EPS[provider_id]
        entry["ep_chain"] = actual[:4]
        entry["ep"] = actual[0] if actual else requested_ep
        if actual and actual[0] != requested_ep:
            # The runtime silently reassigned the session. Only accept that
            # when the manifest explicitly allowlists the CPU reference path.
            if "cpu" not in (manifest.get("providers") or []):
                return _error(
                    request,
                    "load_failed",
                    "runtime assigned %s instead of %s and the manifest does "
                    "not allowlist cpu" % (actual[0], requested_ep),
                )
            entry["provider"] = "cpu"
            entry["ep_fallback"] = True
        if manifest.get("operation") == "embedding":
            tokenizer, error = _load_tokenizer(manifest)
            if tokenizer is None:
                return _error(request, "load_failed", error)
            entry["tokenizer"] = tokenizer
        entry["session"] = session
    _SESSIONS[manifest_hash] = entry
    return {
        "id": request.get("id"),
        "ok": True,
        "manifest_hash": manifest_hash,
        "provider": entry["provider"],
        "ep": entry["ep"],
        "ep_chain": entry["ep_chain"],
        "ep_fallback": bool(entry["ep_fallback"]),
        "simulated": bool(entry["simulated"]),
        "rss_mb": _rss_mb(),
    }


def _softmax(values) -> list:
    peak = max(values)
    exps = [math.exp(value - peak) for value in values]
    total = sum(exps) or 1.0
    return [value / total for value in exps]


def _run_routing(entry, request) -> dict:
    manifest = entry["manifest"]
    features = request.get("features")
    expected = int((manifest.get("input") or {}).get("dimension") or 0)
    if (
        not isinstance(features, list)
        or len(features) != expected
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in features
        )
    ):
        return _error(
            request, "bad_input", "routing needs %d finite features" % expected,
        )
    if entry["simulated"]:
        scores = _sim_route_scores(features)
    else:
        import numpy  # vendor import: worker only

        session = entry["session"]
        feed_name = session.get_inputs()[0].name
        row = numpy.asarray([features], dtype=numpy.float32)
        outputs = session.run(None, {feed_name: row})
        flat = numpy.asarray(outputs[0], dtype=numpy.float64).reshape(-1)
        labels = manifest.get("labels") or list(npu_contract.ROUTE_MODES)
        if flat.shape[0] < len(labels):
            return _error(request, "bad_output", "model returned too few scores")
        values = [float(value) for value in flat[: len(labels)]]
        if manifest.get("postprocess") == "softmax":
            values = _softmax(values)
        scores = {}
        for label, value in zip(labels, values):
            if not math.isfinite(value):
                return _error(request, "bad_output", "non-finite score")
            scores[label] = min(1.0, max(0.0, round(value, 6)))
    margin = abs(scores["autopilot"] - scores["workbench"])
    reason_code = "score_margin" if margin >= 0.2 else "low_confidence"
    return {
        "id": request.get("id"),
        "ok": True,
        "scores": scores,
        "reason_code": reason_code,
        "provider": entry["provider"],
        "ep": entry["ep"],
        "simulated": bool(entry["simulated"]),
        "rss_mb": _rss_mb(),
    }


def _real_embed(entry, texts) -> list:
    import numpy  # vendor import: worker only

    manifest = entry["manifest"]
    session = entry["session"]
    tokenizer = entry["tokenizer"]
    encodings = [tokenizer.encode(text) for text in texts]
    width = max(len(encoding.ids) for encoding in encodings)
    ids = numpy.zeros((len(texts), width), dtype=numpy.int64)
    mask = numpy.zeros((len(texts), width), dtype=numpy.int64)
    for index, encoding in enumerate(encodings):
        ids[index, : len(encoding.ids)] = encoding.ids
        mask[index, : len(encoding.ids)] = 1
    feeds = {}
    for meta in session.get_inputs():
        if "mask" in meta.name:
            feeds[meta.name] = mask
        elif "type" in meta.name:
            feeds[meta.name] = numpy.zeros_like(ids)
        else:
            feeds[meta.name] = ids
    hidden = numpy.asarray(session.run(None, feeds)[0], dtype=numpy.float64)
    pooling = manifest.get("pooling") or "mean"
    if hidden.ndim == 3:
        if pooling == "cls":
            pooled = hidden[:, 0, :]
        else:
            weights = mask[:, :, None].astype(numpy.float64)
            pooled = (hidden * weights).sum(axis=1) / numpy.clip(
                weights.sum(axis=1), 1.0, None,
            )
    else:
        pooled = hidden
    if manifest.get("normalize", True):
        norms = numpy.clip(
            numpy.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None,
        )
        pooled = pooled / norms
    return [[float(value) for value in row] for row in pooled]


def _run_embedding(entry, request) -> dict:
    manifest = entry["manifest"]
    texts = request.get("texts")
    limits = manifest.get("limits") or {}
    max_batch = int(limits.get("max_batch") or npu_contract.MAX_TEXT_ITEMS)
    max_chars = int(limits.get("max_text_chars") or npu_contract.MAX_TEXT_CHARS)
    if (
        not isinstance(texts, list)
        or not texts
        or len(texts) > max_batch
        or any(not isinstance(text, str) for text in texts)
    ):
        return _error(
            request, "bad_input", "embedding needs 1..%d strings" % max_batch,
        )
    if any(len(text) > max_chars for text in texts):
        return _error(
            request, "bad_input",
            "embedding text exceeds the %d char manifest limit" % max_chars,
        )
    dimension = int(manifest.get("dimension") or 0)
    if entry["simulated"]:
        vectors = [
            _sim_vector(manifest.get("preprocess") or "", text, dimension)
            for text in texts
        ]
    else:
        try:
            vectors = _real_embed(entry, texts)
        except Exception as exc:
            return _error(request, "run_failed", str(exc)[:200])
        for vector in vectors:
            if len(vector) != dimension or any(
                not math.isfinite(value) for value in vector
            ):
                return _error(
                    request, "bad_output",
                    "model output does not match the declared dimension",
                )
    return {
        "id": request.get("id"),
        "ok": True,
        "vectors": vectors,
        "provider": entry["provider"],
        "ep": entry["ep"],
        "simulated": bool(entry["simulated"]),
        "rss_mb": _rss_mb(),
    }


def _error(request, code, message) -> dict:
    return {
        "id": request.get("id") if isinstance(request, dict) else None,
        "ok": False,
        "error": {"code": str(code), "message": str(message)[:300]},
        "rss_mb": _rss_mb(),
    }


def _handle(request) -> dict:
    op = str(request.get("op") or "")
    if op == "hello":
        ort, error = _ort()
        version = ""
        if ort is not None:
            version = str(getattr(ort, "__version__", "unknown"))[:40]
        return {
            "id": request.get("id"),
            "ok": True,
            "protocol": npu_contract.PROTOCOL_VERSION,
            "python": sys.version.split()[0],
            "ort_version": version,
            "ort_error": "" if ort is not None else error,
            "platform": sys.platform,
            "pid": os.getpid(),
            "rss_mb": _rss_mb(),
        }
    if op == "detect":
        return {
            "id": request.get("id"),
            "ok": True,
            "providers": npu_providers.detect_providers(*_ort()),
            "rss_mb": _rss_mb(),
        }
    if op == "load":
        return _load(request)
    if op == "unload":
        _SESSIONS.pop(str(request.get("manifest_hash") or ""), None)
        return {"id": request.get("id"), "ok": True, "rss_mb": _rss_mb()}
    if op == "ping":
        return {"id": request.get("id"), "ok": True, "rss_mb": _rss_mb()}
    if op == "run":
        delay = _hook("SONDER_NPU_SIM_DELAY_MS")
        if delay:
            time.sleep(min(60_000, int(delay)) / 1000.0)
        if _hook("SONDER_NPU_SIM_CRASH_ON_RUN") == "1":
            os._exit(3)
        entry = _SESSIONS.get(str(request.get("manifest_hash") or ""))
        if entry is None:
            return _error(request, "not_loaded", "manifest is not loaded")
        kind = str(request.get("kind") or "")
        if kind == "routing" and entry["manifest"].get("operation") == "routing":
            return _run_routing(entry, request)
        if kind == "embedding" and entry["manifest"].get("operation") == "embedding":
            return _run_embedding(entry, request)
        return _error(request, "bad_input", "kind does not match the manifest")
    if op == "shutdown":
        return {"id": request.get("id"), "ok": True, "bye": True, "rss_mb": 0}
    return _error(request, "unknown_op", "unknown op %r" % op)


def main() -> int:
    # Keep the protocol stream private: anything a vendor runtime prints to
    # "stdout" from here on actually lands on stderr.
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    stdin = sys.stdin.buffer

    def _send(payload):
        proto.write(npu_contract.encode_line(payload))

    _send({
        "event": "ready",
        "protocol": npu_contract.PROTOCOL_VERSION,
        "pid": os.getpid(),
    })
    while True:
        line = stdin.readline(npu_contract.MAX_LINE_BYTES + 2)
        if not line:
            return 0
        if len(line) > npu_contract.MAX_LINE_BYTES and not line.endswith(b"\n"):
            return 2
        try:
            request = npu_contract.decode_line(line)
        except ValueError:
            return 2
        if str(request.get("op") or "") == "run" and (
            _hook("SONDER_NPU_SIM_GARBAGE_ON_RUN") == "1"
        ):
            delay = _hook("SONDER_NPU_SIM_DELAY_MS")
            if delay:
                time.sleep(min(60_000, int(delay)) / 1000.0)
            proto.write(b"this is not a protocol line\n")
            continue
        try:
            response = _handle(request)
        except Exception as exc:  # never leak a traceback into the protocol
            response = _error(request, "internal", str(exc)[:200])
        try:
            _send(response)
        except ValueError:
            try:
                _send(_error(request, "oversized", "response exceeded line limit"))
            except ValueError:
                return 2
        if response.get("bye"):
            return 0


if __name__ == "__main__":
    sys.exit(main())

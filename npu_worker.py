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
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time

import npu_contract
import npu_manifest
import npu_providers


_SESSIONS = {}
_ORT_STATE = {"module": None, "error": "", "tried": False}
_STAGING = {"root": None, "bundles": {}}


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
        _ORT_STATE["error"] = npu_contract.sanitize_error(exc, 200)
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
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(
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


def _pinned_target(manifest, entry, label) -> Path:
    """Resolve and stat a pinned file before any whole-file allocation."""
    base = Path(manifest.get("dir") or "").resolve()
    target = (base / entry["path"]).resolve(strict=True)
    inside = os.path.normcase(os.path.commonpath([str(base), str(target)]))
    if inside != os.path.normcase(str(base)) or not target.is_file():
        raise ValueError("%s path escapes the manifest directory" % label)
    size = target.stat().st_size
    if size != entry["bytes"]:
        raise ValueError("size mismatch for %s" % label)
    if size > npu_contract.MAX_PINNED_FILE_BYTES:
        raise ValueError("%s exceeds the pinned-file size limit" % label)
    return target


def _read_pinned_bytes(manifest, entry, label) -> bytes:
    """Read one pinned snapshot through a single bounded open handle."""
    target = _pinned_target(manifest, entry, label)
    expected = int(entry["bytes"])
    chunks = []
    digest = hashlib.sha256()
    total = 0
    with target.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != expected:
            raise ValueError("size mismatch for %s" % label)
        while total <= expected:
            chunk = stream.read(min(1024 * 1024, expected + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            total += len(chunk)
    if total != expected:
        raise ValueError("size mismatch for %s" % label)
    if digest.hexdigest() != entry["sha256"]:
        raise ValueError("hash drift for %s" % label)
    return b"".join(chunks)


def _staging_root() -> Path:
    raw = os.environ.get("SONDER_NPU_STAGE_DIR", "").strip()
    if not raw:
        raise ValueError("broker did not provide a staging directory")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("broker staging path is not a directory")
    if _STAGING["root"] != root:
        _STAGING["root"] = root
        _STAGING["bundles"] = {}
    return root


def _stage_extra_files(manifest) -> Path:
    """Create a private read-only snapshot for path-valued vendor options."""
    manifest_hash = str(manifest.get("manifest_hash") or "")
    cached = _STAGING["bundles"].get(manifest_hash)
    if cached is not None:
        return cached
    root = _staging_root()
    bundle = Path(tempfile.mkdtemp(prefix=manifest_hash[:12] + "-", dir=root))
    try:
        for entry in manifest.get("extra_files") or []:
            payload = _read_pinned_bytes(
                manifest, entry, "extra file %s" % entry["path"],
            )
            target = bundle / Path(entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(target, 0o400)
        # Removing directory write permission prevents accidental mutation by
        # vendor code. The randomized worker-private path is never disclosed.
        directories = sorted(
            (item for item in bundle.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for directory in directories:
            os.chmod(directory, 0o500)
        os.chmod(bundle, 0o500)
    except Exception:
        try:
            for item in bundle.rglob("*"):
                if item.is_dir():
                    os.chmod(item, 0o700)
                else:
                    os.chmod(item, 0o600)
            os.chmod(bundle, 0o700)
        except OSError:
            pass
        shutil.rmtree(bundle, ignore_errors=True)
        raise
    _STAGING["bundles"][manifest_hash] = bundle
    return bundle


def _pinned_runtime_path(manifest, entry, label) -> str:
    """Return a vendor path into a private verified immutable snapshot."""
    bundle = _stage_extra_files(manifest)
    target = (bundle / Path(entry["path"])).resolve(strict=True)
    inside = os.path.normcase(os.path.commonpath([str(bundle), str(target)]))
    if inside != os.path.normcase(str(bundle)) or not target.is_file():
        raise ValueError("%s staging path escaped its bundle" % label)
    return str(target)


def _load_tokenizer(manifest):
    entry = manifest.get("tokenizer")
    if not entry:
        return None, "manifest declares no tokenizer for a real provider"
    try:
        from tokenizers import Tokenizer  # vendor import: worker only
    except Exception as exc:
        return None, "tokenizer runtime (tokenizers) not installed: %s" % (
            npu_contract.sanitize_error(exc, 120),
        )
    try:
        payload = _read_pinned_bytes(manifest, entry, "tokenizer")
        tokenizer = Tokenizer.from_buffer(payload)
        if getattr(tokenizer, "truncation", None) not in (None, {}):
            return None, "tokenizer-side truncation is not allowed"
        if getattr(tokenizer, "padding", None) not in (None, {}):
            return None, "tokenizer-side padding is not allowed"
        return tokenizer, ""
    except Exception as exc:
        return None, "tokenizer load failed: %s" % npu_contract.sanitize_error(
            exc, 160,
        )


def _create_real_session(manifest, provider_id):
    ort, error = _ort()
    if ort is None:
        return None, "onnxruntime not installed: %s" % error
    ep_name = npu_providers.PROVIDER_EPS[provider_id]
    raw_options = (manifest.get("provider_options") or {}).get(provider_id) or {}
    options = {}
    for key, value in raw_options.items():
        if isinstance(value, dict):
            options[key] = _pinned_runtime_path(
                manifest, value, "provider option %s.%s" % (provider_id, key),
            )
        else:
            options[key] = value
    if provider_id == "openvino" and "device_type" not in options:
        options = {**options, "device_type": "NPU"}
    try:
        model_bytes = _read_pinned_bytes(manifest, manifest["model"], "model")
        sess_options = ort.SessionOptions()
        sess_options.log_severity_level = 3
        if provider_id in npu_contract.NPU_CLASS_PROVIDERS:
            add_config = getattr(sess_options, "add_session_config_entry", None)
            if not callable(add_config):
                raise RuntimeError(
                    "onnxruntime cannot disable CPU execution-provider fallback"
                )
            add_config("session.disable_cpu_ep_fallback", "1")
        session = ort.InferenceSession(
            model_bytes,
            sess_options,
            providers=[ep_name],
            provider_options=[options] if options else None,
        )
    except Exception as exc:
        return None, "session load failed on %s: %s" % (
            ep_name, npu_contract.sanitize_error(exc, 200),
        )
    return session, ""


def _entry_for_provider(manifest, provider_id, primary_provider):
    if provider_id == "cpu-sim" and not _test_hooks_enabled():
        return None, "cpu-sim is disabled outside test hooks"
    npu_attested = provider_id in {"openvino", "qnn"}
    entry = {
        "manifest": manifest,
        "provider": provider_id,
        "ep": npu_providers.SIMULATOR_EP,
        "ep_chain": [npu_providers.SIMULATOR_EP],
        "ep_fallback": provider_id != primary_provider,
        "cpu_fallback_disabled": provider_id in npu_contract.NPU_CLASS_PROVIDERS,
        "simulated": provider_id == "cpu-sim",
        # OpenVINO is schema-constrained to device_type=NPU and QNN to a
        # pinned HTP backend. VitisAI spans Ryzen, adaptable SoCs, and Alveo,
        # so its EP name alone is not sufficient effective-device evidence.
        "npu_attested": npu_attested,
        "session": None,
        "tokenizer": None,
    }
    if provider_id == "cpu-sim":
        return entry, ""

    session, error = _create_real_session(manifest, provider_id)
    if session is None:
        return None, error
    try:
        actual = list(session.get_providers())
    except Exception as exc:
        return None, "provider introspection failed: %s" % (
            npu_contract.sanitize_error(exc, 160),
        )
    requested_ep = npu_providers.PROVIDER_EPS[provider_id]
    if not actual:
        return None, "provider introspection returned no execution providers"
    if len(actual) > 4 or any(not isinstance(ep, str) for ep in actual):
        return None, "provider introspection returned an invalid provider chain"
    allowlist = set(manifest.get("providers") or [])
    for actual_ep in actual:
        actual_provider = npu_providers.provider_id_for_ep(actual_ep)
        if not actual_provider or actual_provider not in allowlist:
            return None, (
                "runtime exposed unallowlisted execution provider %s"
                % actual_ep
            )
    if actual[0] != requested_ep or any(ep != requested_ep for ep in actual):
        return None, (
            "runtime did not bind exclusively to requested provider %s"
            % requested_ep
        )
    entry["ep_chain"] = actual
    entry["ep"] = requested_ep
    if manifest.get("operation") == "embedding":
        tokenizer, error = _load_tokenizer(manifest)
        if tokenizer is None:
            return None, error
        entry["tokenizer"] = tokenizer
    entry["session"] = session
    return entry, ""


def _load(request) -> dict:
    manifest = request.get("manifest")
    if not isinstance(manifest, dict):
        return _error(request, "load_invalid", "load needs a manifest object")
    manifest_hash = str(manifest.get("manifest_hash") or "")
    drift = npu_manifest.verify_files(manifest)
    if drift:
        return _error(request, "load_failed", drift)
    provider_rows = npu_providers.detect_providers(
        *_ort(), test_hooks=_test_hooks_enabled(),
    )
    candidates = npu_providers.provider_candidates(manifest, provider_rows)
    if not candidates:
        _provider, _fallback, error = npu_providers.resolve_provider(
            manifest, provider_rows,
        )
        return _error(request, "load_failed", error)
    primary_provider = (manifest.get("providers") or [""])[0]
    entry = None
    failures = []
    for provider_id in candidates:
        entry, error = _entry_for_provider(
            manifest, provider_id, primary_provider,
        )
        if entry is not None:
            break
        failures.append(
            "%s: %s" % (
                provider_id, npu_contract.sanitize_error(error, 160),
            )
        )
    if entry is None:
        return _error(
            request, "load_failed",
            "allowlisted providers failed: %s" % "; ".join(failures),
        )
    drift = npu_manifest.verify_files(manifest)
    if drift:
        return _error(request, "load_failed", drift)
    _SESSIONS[manifest_hash] = entry
    return {
        "id": request.get("id"),
        "ok": True,
        "manifest_hash": manifest_hash,
        "provider": entry["provider"],
        "ep": entry["ep"],
        "ep_chain": entry["ep_chain"],
        "ep_fallback": bool(entry["ep_fallback"]),
        "cpu_fallback_disabled": bool(entry["cpu_fallback_disabled"]),
        "simulated": bool(entry["simulated"]),
        "npu_attested": bool(entry["npu_attested"]),
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
        if flat.shape[0] != len(labels):
            return _error(
                request, "bad_output", "model returned the wrong score count",
            )
        values = [float(value) for value in flat]
        if any(not math.isfinite(value) for value in values):
            return _error(request, "bad_output", "non-finite score")
        if manifest.get("postprocess") == "softmax":
            values = _softmax(values)
        elif any(not 0.0 <= value <= 1.0 for value in values):
            return _error(
                request, "bad_output", "unprocessed scores must be in [0,1]",
            )
        scores = {}
        for label, value in zip(labels, values):
            scores[label] = round(value, 6)
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
        "npu_attested": bool(entry["npu_attested"]),
        "manifest_hash": str(manifest.get("manifest_hash") or ""),
        "ep_fallback": bool(entry["ep_fallback"]),
        "ep_chain": entry["ep_chain"],
        "cpu_fallback_disabled": bool(entry["cpu_fallback_disabled"]),
        "rss_mb": _rss_mb(),
    }


def _real_embed(entry, texts) -> list:
    manifest = entry["manifest"]
    session = entry["session"]
    tokenizer = entry["tokenizer"]
    encodings = [tokenizer.encode(text) for text in texts]
    if any(getattr(encoding, "overflowing", None) for encoding in encodings):
        raise ValueError("tokenizer truncated embedding input")
    if not encodings or any(not encoding.ids for encoding in encodings):
        raise ValueError("tokenizer returned empty embedding input")
    input_metas = list(session.get_inputs())
    input_names = {str(meta.name) for meta in input_metas}
    unsupported = input_names - {
        "input_ids", "attention_mask", "token_type_ids",
    }
    if unsupported:
        raise ValueError(
            "unsupported embedding model input %r" % sorted(unsupported)[0]
        )
    if "input_ids" not in input_names:
        raise ValueError("embedding model omitted required input_ids")
    if (
        len({len(encoding.ids) for encoding in encodings}) > 1
        and "attention_mask" not in input_names
    ):
        raise ValueError(
            "variable-length embedding batch requires attention_mask input"
        )

    import numpy  # vendor import: worker only

    width = max(len(encoding.ids) for encoding in encodings)
    ids = numpy.zeros((len(texts), width), dtype=numpy.int64)
    mask = numpy.zeros((len(texts), width), dtype=numpy.int64)
    type_ids = numpy.zeros((len(texts), width), dtype=numpy.int64)
    for index, encoding in enumerate(encodings):
        length = len(encoding.ids)
        attention = list(getattr(encoding, "attention_mask", []))
        token_types = list(getattr(encoding, "type_ids", []))
        if len(attention) != length or len(token_types) != length:
            raise ValueError("tokenizer returned inconsistent embedding fields")
        ids[index, :length] = encoding.ids
        mask[index, :length] = attention
        type_ids[index, :length] = token_types
    feeds = {}
    for meta in input_metas:
        name = str(meta.name)
        if name == "input_ids":
            feeds[name] = ids
        elif name == "attention_mask":
            feeds[meta.name] = mask
        elif name == "token_type_ids":
            feeds[name] = type_ids
    hidden = numpy.asarray(session.run(None, feeds)[0], dtype=numpy.float64)
    pooling = manifest.get("pooling") or "mean"
    if hidden.ndim == 3:
        if pooling == "cls":
            pooled = hidden[:, 0, :]
        elif pooling == "mean":
            weights = mask[:, :, None].astype(numpy.float64)
            pooled = (hidden * weights).sum(axis=1) / numpy.clip(
                weights.sum(axis=1), 1.0, None,
            )
        else:
            raise ValueError("unsupported embedding pooling %r" % pooling)
    elif hidden.ndim == 2:
        pooled = hidden
    else:
        raise ValueError("embedding output must be a 2-D or 3-D tensor")
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
            return _error(request, "run_failed", exc)
        if len(vectors) != len(texts):
            return _error(
                request, "bad_output", "model returned the wrong vector count",
            )
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
        "npu_attested": bool(entry["npu_attested"]),
        "manifest_hash": str(manifest.get("manifest_hash") or ""),
        "ep_fallback": bool(entry["ep_fallback"]),
        "ep_chain": entry["ep_chain"],
        "cpu_fallback_disabled": bool(entry["cpu_fallback_disabled"]),
        "rss_mb": _rss_mb(),
    }


def _error(request, code, message) -> dict:
    return {
        "id": request.get("id") if isinstance(request, dict) else None,
        "ok": False,
        "error": {
            "code": str(code),
            "message": npu_contract.sanitize_error(message, 300),
        },
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
            "providers": npu_providers.detect_providers(
                *_ort(), test_hooks=_test_hooks_enabled(),
            ),
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
        "rss_mb": _rss_mb(),
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

"""Local Ollama embeddings + vector helpers. Stdlib only. Soft-fails to None."""
import array
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
import urllib.error
import urllib.request
import urllib.parse
import embed_cache
import ollama_endpoint

BASE = ollama_endpoint.normalize()
OLLAMA_HOST = urllib.parse.urlparse(BASE).netloc
# 0.0.0.0 is a bind-all address (used so `ollama serve` is reachable from a phone
# on the LAN), not connectable on Windows — dial loopback instead.
EMBED_MODEL = os.environ.get("SONDER_EMBED_MODEL", "nomic-embed-text")

# nomic-embed-text (and the other local embedders) have a bounded context —
# 2048 tokens for nomic. Ollama does NOT truncate for us: an over-length prompt
# comes back as HTTP 500 "the input length exceeds the context length", which
# embed() then soft-fails to None. For memory recall that meant any long task
# (dense hex-dump / reverse-engineering prompts tokenize far past this at only
# ~5k chars) could never be embedded and was re-selected as "stale" on every
# backfill forever. Truncating the prompt to a conservative char budget keeps
# it under the token limit even for worst-case dense text (~2.7 chars/token
# here vs the ~4-5 of prose) and yields a usable vector from the task's opening
# — which carries its semantic gist — instead of nothing. Overridable for a
# larger-context embedder.
EMBED_MAX_CHARS = int(os.environ.get("SONDER_EMBED_MAX_CHARS", "4000"))


def canonical_model_name(model):
    value = str(model or "").strip().casefold()
    for prefix in ("registry.ollama.ai/library/", "library/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value and ":" not in value:
        value += ":latest"
    return value


EMBED_IDENTITY = canonical_model_name(EMBED_MODEL)
_REVISION_PREFIX = "ollama-manifest-sha256:"


def _bounded_manifest_digest(path, limit=8 * 1024 * 1024):
    digest = hashlib.sha256()
    total = 0
    with Path(path).open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
            return ""
        while total <= limit:
            chunk = stream.read(min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return "" if total > limit else digest.hexdigest()


def local_manifest_revision(model=None, models_root=None):
    """Hash the local Ollama manifest so retagged models cannot mix spaces."""
    identity = canonical_model_name(model or EMBED_MODEL)
    if not identity or ":" not in identity:
        return ""
    name, tag = identity.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) == 1:
        parts.insert(0, "library")
    if (
        not tag or tag in (".", "..") or "/" in tag or "\\" in tag
        or any(not part or part in (".", "..") or "\\" in part for part in parts)
    ):
        return ""
    roots = []
    if models_root:
        roots.append(Path(models_root))
    else:
        configured = os.environ.get("OLLAMA_MODELS", "").strip()
        if configured:
            roots.append(Path(configured))
        roots.append(Path.home() / ".ollama" / "models")
    for root in roots:
        manifest = root.joinpath(
            "manifests", "registry.ollama.ai", *parts, tag,
        )
        try:
            digest = _bounded_manifest_digest(manifest)
        except OSError:
            continue
        if not digest:
            continue
        return _REVISION_PREFIX + digest
    return ""


def serving_model_revision(model=None, base=None, timeout=0.5):
    """Read the digest advertised by the Ollama endpoint serving this model."""
    identity = canonical_model_name(model or EMBED_MODEL)
    try:
        selected_base = ollama_endpoint.configured_origin(base or BASE)
    except ValueError:
        return ""
    try:
        with ollama_endpoint.open_url(
            "%s/api/tags" % selected_base, timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, urllib.error.URLError):
        return ""
    for item in payload.get("models", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        if canonical_model_name(item.get("name") or item.get("model")) != identity:
            continue
        digest = str(item.get("digest") or "").strip().lower()
        if digest.startswith("sha256:"):
            digest = digest[7:]
        if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
            return _REVISION_PREFIX + digest
    return ""


def current_revision(model=None, base=None, models_root=None):
    configured = os.environ.get("SONDER_EMBED_REVISION", "").strip()
    if configured and not configured.startswith(_REVISION_PREFIX):
        return configured
    if models_root is not None:
        return local_manifest_revision(model=model, models_root=models_root) or configured
    selected_base = base or BASE
    served = serving_model_revision(model=model, base=selected_base)
    if served:
        return served
    if endpoint_is_loopback(selected_base):
        local = local_manifest_revision(model=model)
        if local:
            return local
    return configured


EMBED_REVISION = (
    os.environ.get("SONDER_EMBED_REVISION", "").strip()
    or local_manifest_revision()
)


def refresh_runtime_revision(models_root=None):
    """Refresh mutable Ollama tag provenance at request/embed boundaries."""
    global EMBED_REVISION
    EMBED_REVISION = current_revision(models_root=models_root)
    return EMBED_REVISION


_KNOWN_DIMENSIONS = {
    "nomic-embed-text:latest": 768,
}


def expected_dimension(model=None):
    """Configured/known vector size for safe dry-run compatibility checks."""
    configured = os.environ.get("SONDER_EMBED_DIM", "").strip()
    if configured:
        try:
            dimension = int(configured)
        except ValueError:
            return None
        return dimension if dimension > 0 else None
    return _KNOWN_DIMENSIONS.get(canonical_model_name(model or EMBED_MODEL))


EXPECTED_DIMENSION = expected_dimension()
_EMBED_STATE = threading.local()


def provenance(vector=None):
    """Metadata stored beside vectors so model migrations are detectable.

    ``provider``/``accelerated``/``simulated`` describe how the bound vector
    was produced (``ollama``, ``npu:<provider>``, ``cpu-reference``, or
    ``cpu-sim``). CPU reference/simulator results are never labeled as NPU
    acceleration. A vector that is not the one bound to this thread keeps the
    legacy identity fields and an unknown provider — never a fabricated one.
    """
    bound_revision = getattr(_EMBED_STATE, "revision", None)
    bound_model = getattr(_EMBED_STATE, "model", None)
    bound_provider = getattr(_EMBED_STATE, "provider", "")
    bound_accelerated = bool(getattr(_EMBED_STATE, "accelerated", False))
    bound_simulated = bool(getattr(_EMBED_STATE, "simulated", False))
    if getattr(_EMBED_STATE, "vector", None) is not vector:
        bound_revision = EMBED_REVISION
        bound_model = EMBED_IDENTITY
        bound_provider = ""
        bound_accelerated = False
        bound_simulated = False
    return {
        "model": bound_model,
        "revision": bound_revision,
        "dimension": len(vector) if vector else None,
        "provider": bound_provider,
        "accelerated": bound_accelerated,
        "simulated": bound_simulated,
    }


def valid_vector(vector):
    if not isinstance(vector, (list, tuple)) or not vector:
        return False
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in vector
    ):
        return False
    try:
        values = array.array("f", vector)
    except (OverflowError, TypeError, ValueError):
        return False
    return bool(
        values
        and all(math.isfinite(value) for value in values)
        and any(value != 0.0 for value in values)
    )


def endpoint_is_loopback(base=None):
    return ollama_endpoint.is_loopback(base or BASE)


def to_blob(vec):
    return array.array("f", vec).tobytes()


def from_blob(b):
    a = array.array("f")
    a.frombytes(b)
    return list(a)


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _npu_service():
    # Function-local so this module stays importable and hot-reload safe even
    # if the accelerator stack is absent; returns the live module object.
    import npu_service

    return npu_service


def _accelerated_embed(prompt, identity, revision, dimension):
    """Try the NPU utility accelerator for the exact current vector space.

    npu_service only answers when policy prefers embeddings AND the active
    manifest pins this identity+revision (same weights/tokenizer/pooling/
    normalization declaration). Anything else is None and the legacy path
    runs unchanged — a different embedder is never silently substituted.
    """
    try:
        return _npu_service().embed_for_space(
            prompt, identity, revision, expected_dimension=dimension,
        )
    except Exception:
        return None


def _npu_prefer_active():
    try:
        return _npu_service().embeddings_active() == "prefer"
    except Exception:
        return False


def _npu_shadow_embed(prompt, identity, revision):
    try:
        _npu_service().embed_shadow(prompt, identity, revision)
    except Exception:
        pass


def embed(text, timeout=30, base=None, model=None):
    _EMBED_STATE.vector = None
    _EMBED_STATE.revision = None
    _EMBED_STATE.model = None
    _EMBED_STATE.provider = ""
    _EMBED_STATE.accelerated = False
    _EMBED_STATE.simulated = False
    _EMBED_STATE.fallback_reason = ""
    try:
        selected_base = ollama_endpoint.configured_origin(base or BASE)
    except ValueError:
        return None
    selected_model = model or EMBED_MODEL
    explicit_runtime = base is not None or model is not None
    revision_before = (
        current_revision(model=selected_model, base=selected_base)
        if explicit_runtime else refresh_runtime_revision()
    )
    # Cap the prompt to the embedder's context budget. Without this an
    # over-length input is an HTTP 500 that soft-fails to None (see
    # EMBED_MAX_CHARS) rather than a usable vector.
    prompt = text if text is None else str(text)[:EMBED_MAX_CHARS]
    identity = canonical_model_name(selected_model)
    dimension = expected_dimension(selected_model)
    if isinstance(prompt, str) and prompt and revision_before:
        # Same (identity, revision, text) means the same deterministic vector
        # regardless of which backend produced it, so a cache hit is exact —
        # and the revision key invalidates on any model update.
        cached = embed_cache.get(prompt, identity, revision_before)
        if (
            valid_vector(cached)
            and (dimension is None or len(cached) == dimension)
        ):
            _EMBED_STATE.vector = cached
            _EMBED_STATE.revision = revision_before
            _EMBED_STATE.model = identity
            _EMBED_STATE.provider = "cache"
            return cached
    if isinstance(prompt, str) and prompt:
        accelerated = _accelerated_embed(
            prompt, identity, revision_before, dimension,
        )
        provider = (
            str(accelerated.get("provider") or "").strip().lower()
            if isinstance(accelerated, dict) else ""
        )
        acceleration_flag = (
            accelerated.get("accelerated")
            if isinstance(accelerated, dict) else None
        )
        simulated_flag = (
            accelerated.get("simulated")
            if isinstance(accelerated, dict) else None
        )
        vector = accelerated.get("vector") if isinstance(accelerated, dict) else None
        valid_accelerated = (
            isinstance(dimension, int)
            and not isinstance(dimension, bool)
            and isinstance(acceleration_flag, bool)
            and isinstance(simulated_flag, bool)
            and not simulated_flag
            and provider in {"openvino", "qnn", "cpu"}
            and accelerated.get("model") == identity
            and accelerated.get("revision") == revision_before
            and accelerated.get("dimension") == dimension
            and isinstance(vector, (list, tuple))
            and len(vector) == dimension
            and valid_vector(vector)
            and acceleration_flag == (provider in {"openvino", "qnn"})
        )
        if valid_accelerated:
            revision_after = (
                current_revision(model=selected_model, base=selected_base)
                if explicit_runtime else refresh_runtime_revision()
            )
            if revision_after != revision_before:
                _EMBED_STATE.fallback_reason = "revision_changed"
                return None
            vector = list(accelerated["vector"])
            npu_accelerated = acceleration_flag
            _EMBED_STATE.vector = vector
            _EMBED_STATE.revision = revision_before
            _EMBED_STATE.model = identity
            if npu_accelerated:
                _EMBED_STATE.provider = "npu:%s" % provider
            else:
                _EMBED_STATE.provider = "cpu-reference"
            _EMBED_STATE.accelerated = npu_accelerated
            _EMBED_STATE.simulated = simulated_flag
            embed_cache.put(
                prompt, identity, revision_before, vector,
                provider=_EMBED_STATE.provider,
            )
            return vector
        if _npu_prefer_active():
            _EMBED_STATE.fallback_reason = "npu_unavailable"
    payload = json.dumps({"model": selected_model, "prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        "%s/api/embeddings" % selected_base,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with ollama_endpoint.open_url(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            vector = data.get("embedding") if isinstance(data, dict) else None
            revision_after = (
                current_revision(model=selected_model, base=selected_base)
                if explicit_runtime else refresh_runtime_revision()
            )
            if revision_after != revision_before:
                return None
            if not valid_vector(vector):
                return None
            _EMBED_STATE.vector = vector
            _EMBED_STATE.revision = revision_before
            _EMBED_STATE.model = identity
            _EMBED_STATE.provider = "ollama"
            if isinstance(prompt, str) and prompt:
                embed_cache.put(
                    prompt, identity, revision_before, vector,
                    provider="ollama",
                )
                _npu_shadow_embed(prompt, identity, revision_before)
            return vector
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def embed_result(text, timeout=30, base=None, model=None):
    """Typed embedding outcome: vector plus full provenance.

    Returns None exactly when embed() would (preserving every fail-soft
    consumer, including lexical fallback). ``fallback_reason`` is
    "npu_unavailable" when acceleration was preferred but the legacy embedder
    served the request — the fallback is explicit, never silent.
    """
    vector = embed(text, timeout=timeout, base=base, model=model)
    if vector is None:
        return None
    return {
        "vector": vector,
        **provenance(vector),
        "fallback_reason": str(getattr(_EMBED_STATE, "fallback_reason", "") or ""),
    }

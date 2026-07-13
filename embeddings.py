"""Local Ollama embeddings + vector helpers. Stdlib only. Soft-fails to None."""
import array
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request
import urllib.parse

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").replace("http://", "")
# 0.0.0.0 is a bind-all address (used so `ollama serve` is reachable from a phone
# on the LAN), not connectable on Windows — dial loopback instead.
if OLLAMA_HOST.startswith("0.0.0.0"):
    OLLAMA_HOST = OLLAMA_HOST.replace("0.0.0.0", "127.0.0.1", 1)
BASE = "http://%s" % OLLAMA_HOST
EMBED_MODEL = os.environ.get("SONDER_EMBED_MODEL", "nomic-embed-text")


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
            if not manifest.is_file() or manifest.stat().st_size > 8 * 1024 * 1024:
                continue
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        except OSError:
            continue
        return _REVISION_PREFIX + digest
    return ""


def serving_model_revision(model=None, base=None, timeout=0.5):
    """Read the digest advertised by the Ollama endpoint serving this model."""
    identity = canonical_model_name(model or EMBED_MODEL)
    try:
        with urllib.request.urlopen(
            "%s/api/tags" % (base or BASE), timeout=timeout,
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
    """Metadata stored beside vectors so model migrations are detectable."""
    bound_revision = getattr(_EMBED_STATE, "revision", None)
    bound_model = getattr(_EMBED_STATE, "model", None)
    if getattr(_EMBED_STATE, "vector", None) is not vector:
        bound_revision = EMBED_REVISION
        bound_model = EMBED_IDENTITY
    return {
        "model": bound_model,
        "revision": bound_revision,
        "dimension": len(vector) if vector else None,
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
    try:
        hostname = urllib.parse.urlparse(base or BASE).hostname
        if not hostname:
            return False
        if hostname.casefold().rstrip(".") == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


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


def embed(text, timeout=30, base=None, model=None):
    _EMBED_STATE.vector = None
    _EMBED_STATE.revision = None
    _EMBED_STATE.model = None
    selected_base = base or BASE
    selected_model = model or EMBED_MODEL
    explicit_runtime = base is not None or model is not None
    revision_before = (
        current_revision(model=selected_model, base=selected_base)
        if explicit_runtime else refresh_runtime_revision()
    )
    payload = json.dumps({"model": selected_model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        "%s/api/embeddings" % selected_base,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
            _EMBED_STATE.model = canonical_model_name(selected_model)
            return vector
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

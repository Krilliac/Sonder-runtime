"""Local Ollama embeddings + vector helpers. Stdlib only. Soft-fails to None."""
import array
import json
import os
import urllib.error
import urllib.request

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").replace("http://", "")
# 0.0.0.0 is a bind-all address (used so `ollama serve` is reachable from a phone
# on the LAN), not connectable on Windows — dial loopback instead.
if OLLAMA_HOST.startswith("0.0.0.0"):
    OLLAMA_HOST = OLLAMA_HOST.replace("0.0.0.0", "127.0.0.1", 1)
BASE = "http://%s" % OLLAMA_HOST
EMBED_MODEL = os.environ.get("SONDER_EMBED_MODEL", "nomic-embed-text")


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


def embed(text, timeout=30):
    payload = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        "%s/api/embeddings" % BASE,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("embedding") if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

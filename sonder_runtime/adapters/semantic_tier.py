"""Bounded, local-only adapter for the opt-in semantic tier signal."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading

from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread
import time

import sonder_runtime.adapters.embeddings as embeddings
import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint
import sonder_runtime.domain.routing.semantic_tier as semantic_tier
from sonder_runtime.domain.model_routing import is_cloud_model_name

CALLER_TIMEOUT = 1.0
EMBED_TIMEOUT = 1.0
BUILD_DEADLINE = 35.0
_CACHE_LIMIT = 4
_cache_lock = threading.RLock()
_cache: OrderedDict[tuple, dict] = OrderedDict()
_flight = None
# The shipped head is trained on the bake-off bank plus synthetic prompts only.
# SONDER_SEMANTIC_TIER_HEAD points at an operator's own head (for example one
# trained on their private history), which then never has to enter the repo.
_SHIPPED_HEAD = Path(__file__).resolve().parent / "data" / "semantic_tier_head.json"
_head_cache: dict = {}


def trained_head():
    """The configured linear head, or ``None`` when absent or invalid (cached per path and mtime)."""
    path = Path(os.environ.get("SONDER_SEMANTIC_TIER_HEAD", "") or _SHIPPED_HEAD)
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
        if stamp not in _head_cache:
            _head_cache.clear()
            payload = json.loads(path.read_text(encoding="utf-8"))
            _head_cache[stamp] = semantic_tier.load_head(payload)
        return _head_cache[stamp]
    except Exception:
        return None


@dataclass
class _Job:
    key: tuple
    model: str
    base: str
    embedder: object
    query: str
    event: threading.Event
    result: dict | None = None


def _embedding_text(prompt, model):
    return "classification: " + prompt if model.startswith("nomic-embed-text") else prompt


def _call(embedder, prompt, model, base):
    if embedder is None:
        return embeddings.embed(prompt, timeout=EMBED_TIMEOUT, base=base, model=model)
    return embedder(prompt)


def _valid_centroids(value):
    return isinstance(value, dict) and bool(value)


def _put_cache(key, centroids):
    if not _valid_centroids(centroids):
        return
    with _cache_lock:
        _cache[key] = centroids
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)


def _classify(vector, centroids, model):
    if not isinstance(vector, (list, tuple)) or not _valid_centroids(centroids):
        return None
    try:
        decision = semantic_tier.classify_vector(vector, centroids)
        if decision.tier == "vision" or decision.margin < semantic_tier.MIN_MARGIN:
            return None
        return {"tier": decision.tier, "margin": float(decision.margin), "model": model}
    except Exception:
        return None


def _classify_head(vector, head, model):
    try:
        decision = semantic_tier.classify_with_head(vector, head)
        if decision.tier == "vision" or decision.margin < semantic_tier.HEAD_MIN_MARGIN:
            return None
        return {"tier": decision.tier, "margin": float(decision.margin), "model": model,
                "method": "trained-head"}
    except Exception:
        return None


def _run_job(job):
    global _flight
    deadline = time.monotonic() + BUILD_DEADLINE
    centroids = None
    try:
        head = trained_head()
        if head is not None and embeddings.canonical_model_name(head.embedding_model) == job.model:
            # One embedding, no bank to build: the head is the measured better path.
            query = _call(job.embedder, head.input_prefix + job.query, job.model, job.base)
            job.result = None if query is None else _classify_head(query, head, job.model)
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("semantic query deadline")
        query = _call(job.embedder, _embedding_text(job.query, job.model), job.model, job.base)
        if query is None:
            raise ValueError("query embedding unavailable")
        query_provenance = (
            embeddings.provenance(query) if job.embedder is None else None
        )
        cache_key = job.key
        if query_provenance is not None:
            cache_key += (
                query_provenance.get("revision"),
                query_provenance.get("dimension"),
            )
        with _cache_lock:
            centroids = _cache.get(cache_key)
        if centroids is None:
            vectors_by_tier = {}
            for tier, prompts in semantic_tier.EXAMPLE_BANK.items():
                vectors = []
                for prompt in prompts:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("semantic centroid build deadline")
                    vector = _call(job.embedder, _embedding_text(prompt, job.model), job.model, job.base)
                    if vector is None:
                        raise ValueError("embedding unavailable")
                    if job.embedder is None:
                        provenance = embeddings.provenance(vector)
                        if any(
                            provenance.get(field) != query_provenance.get(field)
                            for field in ("model", "revision", "dimension")
                        ):
                            raise ValueError("embedding provenance changed")
                    vectors.append(vector)
                vectors_by_tier[tier] = vectors
            centroids = semantic_tier.build_centroids(vectors_by_tier)
            _put_cache(cache_key, centroids)
        if time.monotonic() >= deadline:
            raise TimeoutError("semantic query deadline")
        job.result = _classify(query, centroids, job.model)
    except Exception:
        job.result = None
    finally:
        with _cache_lock:
            job.event.set()
            if _flight is job:
                _flight = None


def semantic_signal(prompt, *, embedder=None, model=None):
    """Return a semantic tier result, or ``None`` on any soft failure.

    Production accepts only the configured loopback Ollama origin. One global
    background flight prevents worker multiplication during cold builds or
    timeouts. Injected embedders use a separate cache namespace keyed by
    callable identity and are never retained as production state.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    model_name = embeddings.canonical_model_name(model or embeddings.EMBED_MODEL)
    if not model_name or is_cloud_model_name(model_name) or model_name.startswith("cloud"):
        return None
    if embedder is None:
        try:
            base = embeddings.BASE
            if not embeddings.endpoint_is_loopback(base):
                return None
            with ollama_endpoint._default_embedding_operation() as allowed:
                if not allowed:
                    return None
        except Exception:
            return None
    else:
        base = "injected"
    key = ("injected" if embedder is not None else "production", embedder,
           model_name, base, semantic_tier.BANK_DIGEST)
    job = _Job(key, model_name, base, embedder, prompt, threading.Event())
    global _flight
    with _cache_lock:
        if _flight is not None:
            return None
        _flight = job
        try:
            worker = owned_runtime_thread(target=_run_job, args=(job,),
                                      name="sonder-semantic-flight", daemon=True)
            worker.start()
        except Exception:
            _flight = None
            return None
    job.event.wait(CALLER_TIMEOUT)
    return job.result

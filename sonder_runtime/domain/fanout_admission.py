"""Pure fanout receipt limits and the immutable admission record.

A durable fanout run persists its limits and its selected targets as JSON;
this module decodes the limits with their clamps and describes the request
envelope (targets, ceilings, upper bounds, privacy disclosure) without
inventing a price. It is explicit-input and side-effect free: the model
classifiers and the local thinking floor are injected by the caller. Moved
from ``server.py`` in the WP1 Three-Hundred-Third Slice with its behaviour
byte-for-byte intact.
"""
from __future__ import annotations

import json
import math


def fanout_limits(run):
    try:
        raw = json.loads(run.get("limits_json") or "{}")
    except (TypeError, ValueError):
        raw = {}
    return {
        "num_predict": max(32, min(int(raw.get("num_predict", 512)), 4096)),
        "timeout": max(5, min(int(raw.get("timeout", 45)), 300)),
        "cloud_workers": max(1, min(int(raw.get("cloud_workers", 2)), 2)),
        "resident_before": [str(name) for name in raw.get("resident_before", []) if str(name)],
        # Legacy receipts have no trustworthy snapshot provenance.  Conserving
        # a model is safe; evicting one based on an unknown empty snapshot is
        # not, so the backwards-compatible default is deliberately false.
        "resident_snapshot_known": raw.get("resident_snapshot_known") is True,
        "plan_skipped": list(raw.get("plan_skipped", [])),
        "selection_profile": str(raw.get("selection_profile") or "").strip().lower(),
    }


def fanout_admission(
    run, rows, limits, *, is_cloud_model_name, known_thinking_model,
    local_thinking_min_num_predict,
):
    """Describe the immutable request envelope without inventing a price.

    This is an admission record, not a latency or billing promise.  Model
    catalogs do not publish a trustworthy, stable provider pricing schedule,
    so the receipt gives callers concrete request and scheduling ceilings
    instead of a misleading currency estimate.

    ``is_cloud_model_name`` and ``known_thinking_model`` classify a selected
    target and ``local_thinking_min_num_predict`` is the local thinking floor;
    the caller injects them so this record stays free of the routing cache.
    """
    # The persisted snapshot, not mutable result rows, is the admission
    # authority.  A fenced worker must never make an inconsistent row look
    # like a selected target in the caller-visible privacy/budget record.
    try:
        raw_snapshot = json.loads(run.get("models_json") or "[]")
    except (TypeError, ValueError):
        raw_snapshot = []
    selected = sorted(
        {str(name).strip() for name in raw_snapshot if str(name).strip()},
        key=str.casefold,
    )
    cloud_targets = [name for name in selected if is_cloud_model_name(name)]
    # Durable fanout dispatches exactly the immutable selected targets.  In
    # particular, a K3 availability failure remains a failed K3 row rather
    # than silently sending the sealed prompt to K2.7 and misattributing the
    # answer or model health.
    disclosed_cloud_targets = sorted(set(cloud_targets), key=str.casefold)
    effective_num_predict = max([
        int(limits["num_predict"]),
        *[
            max(int(limits["num_predict"]), 4096)
            for name in cloud_targets
            if str(name).casefold().startswith(("kimi-k3:", "glm-5.2:", "kimi-k2.7-code:"))
        ],
        *[
            max(int(limits["num_predict"]), local_thinking_min_num_predict)
            for name in selected
            if not is_cloud_model_name(name) and known_thinking_model(name)
        ],
    ])
    local_count = len(selected) - len(cloud_targets)
    cloud_workers = limits["cloud_workers"]
    # Locals execute serially to protect shared VRAM/RAM. Cloud rows use the
    # bounded worker pool. This deliberately excludes setup and queue costs.
    request_phase_seconds = limits["timeout"] * (
        local_count + math.ceil(len(cloud_targets) / cloud_workers)
    )
    return {
        "selected_models": selected,
        "targets": {
            "total": len(selected), "local": local_count, "cloud": len(cloud_targets),
        },
        "execution": {
            "num_predict": effective_num_predict,
            "requested_num_predict": limits["num_predict"],
            "request_timeout_s": limits["timeout"],
            "local_concurrency": 1,
            "cloud_concurrency": cloud_workers,
        },
        "upper_bounds": {
            "initial_request_attempts_total": len(selected),
            "initial_cloud_request_attempts": len(cloud_targets),
            "scheduled_request_phase_wall_ms": int(request_phase_seconds * 1000),
            "excludes": [
                "catalog discovery", "queue or lease wait", "model load or unload",
                "provider retry or throttle beyond a request timeout", "explicit later resume attempts",
            ],
        },
        "cost": {
            "provider_pricing": "not_estimated",
            "reason": "the runtime has no trustworthy provider price schedule",
        },
        "privacy": {
            "cloud_opt_in": bool(run.get("cloud_opt_in")),
            "cloud_targets": disclosed_cloud_targets,
            "prompt_leaves_machine": bool(disclosed_cloud_targets),
            "notice": (
                "selected cloud targets receive the prompt; cloud calls require explicit operator opt-in"
                if disclosed_cloud_targets else "no selected cloud target receives the prompt"
            ),
        },
    }

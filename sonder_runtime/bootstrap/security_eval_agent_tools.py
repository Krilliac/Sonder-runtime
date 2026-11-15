"""``security_eval``: measure a local model's security-hypothesis precision and recall.

Judges (item mode) or finds (chunk mode) hypotheses against an external labelled JSONL set, with and
without retrieved repo context, through the normal Sonder model path. Nothing here reaches the network,
runs a subprocess or executes model output: replies are parsed and scored as text. Writes go only to
the ``out_dir`` the caller names. The dataset is never committed; its path is an argument or
``SONDER_SECURITY_EVAL_DATASET``. See ``docs/architecture`` and the SONDER_SECURITY_EVAL_* keys in
``sonder_runtime/platform/security_eval_config.py``.

server.py is size-capped, so the tool registers itself from here (discovered by
``agent_help_tools.discover_agent_tool_registrars``).

Why the gateway is the text-only wrapper: the default checked ``ToolLaneGateway`` refuses models denied
from tool lanes, and the security adviser models are exactly the ones to deny there. This tool is TEXT
only (verdicts are parsed and scored, never run), the same class as ChatService, so it gets the same
``ServedTierGateway`` wrapper over the application's model gateway.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Mapping

from ..adapters.provider_bindings import ProviderBindings  # noqa: F401 - documents the bindings type
from ..adapters.security_eval.dataset import load_dataset
from ..adapters.security_eval.model_client import GatewayVerdictModel
from ..adapters.security_eval.output_store import OutputStore
from ..adapters.security_eval.repo_reader import ConfinedRepo, RepoLimits, RetrievalSettings
from ..adapters.tool_lane_gateway import ServedTierGateway
from ..application.security_eval.runner import ARMS, MODES, RunPlan, SecurityEvalRunner
from ..domain.common.errors import DependencyUnavailable, InvalidInput, SonderError
from ..domain.security_eval.context_select import HELPER_VERSION
from ..domain.security_eval.errors import TemplateError
from ..domain.security_eval.prompts import DEFAULT_CHUNK_TEMPLATE, DEFAULT_ITEM_TEMPLATE, DEFAULT_SYSTEM_PROMPT
from ..platform.security_eval_config import (
    CONTEXT_MODES, MODEL_RE, TEMPLATE_MAX_BYTES, SecurityEvalConfig, security_eval_config_from_env)

_LIST_SPLIT = re.compile(r"[,\s;]+")


class _MonotonicClock:
    def now(self) -> float:
        return time.monotonic()


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _names(text: str) -> frozenset:
    return frozenset(part for part in _LIST_SPLIT.split(str(text or "")) if part)


def _arms(context: str) -> tuple[str, ...]:
    if context == "none":
        return ("none",)
    if context == "retrieved":
        return ("retrieved",)
    if context == "both":
        return ARMS
    raise InvalidInput("context must be one of %s" % ", ".join(CONTEXT_MODES))


def _read_template(path: str) -> str | None:
    if not path:
        return None
    try:
        size = os.path.getsize(path)
        if size > TEMPLATE_MAX_BYTES:
            raise TemplateError("the template file is over %d bytes" % TEMPLATE_MAX_BYTES)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise TemplateError("the template file could not be read (%s)" % type(exc).__name__) from None


def _section(application: Any) -> SecurityEvalConfig:
    config = getattr(application, "config", None)
    section = getattr(config, "security_eval", None)
    if isinstance(section, SecurityEvalConfig):
        return section
    return security_eval_config_from_env(os.environ, [])


def _identity(cfg: SecurityEvalConfig, model: str, mode: str, template: str | None) -> dict:
    """Everything that changes what a row means; hashed into the resume digest."""
    identity: dict[str, Any] = {
        "model": model, "helper_version": HELPER_VERSION, "num_ctx": cfg.num_ctx,
        "num_predict": cfg.num_predict, "temperature": cfg.temperature, "think": cfg.think,
        "system_sha": hashlib.sha256(DEFAULT_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12],
        "retrieval": {"max_context_chars": cfg.max_context_chars, "max_identifiers": cfg.max_identifiers,
                      "max_hits_per_kind": cfg.max_hits_per_kind, "snippet_lines": cfg.snippet_lines,
                      "common_hit_limit": cfg.common_hit_limit, "index_max_files": cfg.index_max_files,
                      "index_max_bytes": cfg.index_max_bytes, "max_file_bytes": cfg.max_file_bytes},
    }
    if mode == "item":
        identity["pad_lines"] = cfg.source_pad_lines
        identity["template_sha"] = hashlib.sha256(
            (template or DEFAULT_ITEM_TEMPLATE).encode("utf-8")).hexdigest()[:12]
    else:
        identity["chunk_chars"] = cfg.chunk_chars
        identity["match_line_tolerance"] = cfg.match_line_tolerance
        identity["template_sha"] = hashlib.sha256(DEFAULT_CHUNK_TEMPLATE.encode("utf-8")).hexdigest()[:12]
    return identity


def _summary_of(result) -> dict:
    report = result.report
    overall = (report.get("scores") or {}).get("overall") or {}
    arms = {arm: {"precision": conf["precision"], "recall": conf["recall"], "n": conf["n"],
                  "tp": conf["tp"], "fp": conf["fp"], "fn": conf["fn"], "tn": conf["tn"]}
            for arm, conf in overall.items()}
    return {
        "ok": True, "complete": result.complete, "stopped_reason": result.stopped_reason,
        "remaining": result.remaining, "processed": result.processed, "skipped_done": result.skipped_done,
        "scored": int((report.get("outcomes") or {}).get("SCORED", 0)), "arms": arms,
        "paired": report.get("paired"), "chunk_scores": report.get("chunk_scores"),
        "served_models": report.get("served_models"), "served_model_source": report.get("served_model_source"),
        "tokens": report.get("tokens"), "invalid_dataset_lines": report.get("invalid_dataset_lines", 0),
    }


def evaluate(application: Any, *, model: str = "", out_dir: str = "", dataset: str = "", mode: str = "item",
             context: str = "", limit: int = 0, categories: str = "", stages: str = "", hids: str = "",
             score_only: bool = False) -> dict:
    """Run one slice of the eval; raises typed errors (the tool wrapper turns them into a result)."""
    cfg = _section(application)
    model = (model or cfg.model).strip()
    if not model:
        raise InvalidInput("model is required (argument or SONDER_SECURITY_EVAL_MODEL)")
    if not MODEL_RE.fullmatch(model):
        raise InvalidInput("model is not a valid model name")
    if not isinstance(out_dir, str) or not out_dir.strip():
        raise InvalidInput("out_dir is required: the directory the results are written to")
    if mode not in MODES:
        raise InvalidInput("mode must be one of %s" % ", ".join(MODES))
    arms = _arms((context or cfg.context).strip().lower())
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise InvalidInput("limit must be a non-negative integer")
    dataset_path = (dataset or cfg.dataset).strip()
    loaded = load_dataset(dataset_path, max_bytes=cfg.max_dataset_bytes, max_records=cfg.max_records)
    template = _read_template(cfg.template_file)
    gateway = ServedTierGateway(application.model_gateway, bindings=application.provider_bindings)
    options: dict[str, Any] = {"temperature": cfg.temperature, "num_predict": cfg.num_predict}
    if cfg.num_ctx is not None:
        options["num_ctx"] = cfg.num_ctx
    if cfg.think is not None:
        options["think"] = cfg.think
    roots = tuple(dict.fromkeys(record.repo_root for record in loaded.records))
    store = OutputStore(out_dir, protected_roots=roots, dataset_path=loaded.path)
    limits = RepoLimits(cfg.index_max_files, cfg.index_max_bytes, cfg.max_file_bytes, float(cfg.index_timeout_seconds))
    settings = RetrievalSettings(cfg.max_identifiers, cfg.max_hits_per_kind, cfg.snippet_lines,
                                 cfg.common_hit_limit, cfg.max_context_chars)
    opened: dict[str, ConfinedRepo] = {}

    def repos(root: str) -> ConfinedRepo:
        if root not in opened:
            opened[root] = ConfinedRepo(root, cfg.repo_roots, limits, settings)
        return opened[root]

    verdicts = GatewayVerdictModel(gateway, model, options=options, allow_remote_ollama=cfg.allow_remote_ollama,
                                   item_timeout_seconds=float(cfg.item_timeout_seconds))
    plan = RunPlan(
        requested_model=model, identity=_identity(cfg, model, mode, template), mode=mode, arms=arms,
        limit=limit, time_budget_seconds=float(cfg.call_budget_seconds), pad_lines=cfg.source_pad_lines,
        template=template, keep_raw_chars=cfg.keep_raw_chars, chunk_chars=cfg.chunk_chars,
        match_line_tolerance=cfg.match_line_tolerance, categories=_names(categories), stages=_names(stages),
        hids=_names(hids), dataset_sha256=loaded.sha256, invalid_lines=len(loaded.invalid))
    runner = SecurityEvalRunner(repos=repos, model=verdicts, store=store, clock=_MonotonicClock())
    result = runner.score_only(loaded.records, plan) if score_only else runner.run(loaded.records, plan)
    payload = _summary_of(result)
    payload["report"] = os.path.join(store.out_dir, "report.json")
    payload["summary"] = os.path.join(store.out_dir, "summary.md")
    return payload


def register(mcp, record, runtime=None) -> None:
    """Register ``security_eval``; ``runtime`` is the server composition's zero-argument provider."""
    if runtime is not None and not callable(runtime):
        raise TypeError("runtime must be a callable returning the runtime object, got %s" % type(runtime).__name__)

    def run(args: dict) -> str:
        started = time.time()
        try:
            owner = runtime() if callable(runtime) else None
            provider = getattr(owner, "_application", None)
            if not callable(provider):
                raise DependencyUnavailable("the runtime application is not available")
            payload = evaluate(provider(), **args)
        except SonderError as exc:
            payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}
        except Exception as exc:  # noqa: BLE001 - a tool reports, it never crashes the server
            payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}
        output = _json(payload)
        record("security_eval", args, ok=bool(payload.get("ok")), started=started,
               summary=str(payload.get("error") or "ok")[:200], output=output)
        return output

    @mcp.tool()
    def security_eval(model: str = "", out_dir: str = "", dataset: str = "", mode: str = "item",
                      context: str = "", limit: int = 0, categories: str = "", stages: str = "",
                      hids: str = "", score_only: bool = False) -> str:
        """Measure a local model's precision and recall on labelled security hypotheses, with and without retrieved repo context. Reads an external JSONL dataset (argument or SONDER_SECURITY_EVAL_DATASET), calls the named local model only (cloud and unconsented remote routes are refused), and writes items.jsonl, report.json and summary.md under out_dir. A call is a bounded slice; call again with the same out_dir to resume. score_only rescoring makes no model call."""
        return run({"model": model, "out_dir": out_dir, "dataset": dataset, "mode": mode, "context": context,
                    "limit": limit, "categories": categories, "stages": stages, "hids": hids,
                    "score_only": score_only})


__all__ = ["evaluate", "register"]

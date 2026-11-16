"""Operator settings for the security-hypothesis eval (SONDER_SECURITY_EVAL_*).

A bad value keeps the narrow default for that key and is reported, so a typo never widens what the
eval may read, send or run. Hard ceilings on the index and dataset sizes cannot be raised by config.
"""
from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from sonder_runtime.platform.config_environment import env_bool

CONTEXT_MODES = ("none", "retrieved", "both")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
PREFIX = "SONDER_SECURITY_EVAL_"
TEMPLATE_MAX_BYTES = 32768


@dataclass(frozen=True)
class SecurityEvalConfig:
    dataset: str = ""
    repo_roots: tuple[str, ...] = ()
    model: str = ""
    context: str = "both"
    template_file: str = ""
    num_ctx: int | None = None
    num_predict: int = 768
    temperature: float = 0.0
    think: bool | None = None
    item_timeout_seconds: int = 300
    call_budget_seconds: int = 1500
    allow_remote_ollama: bool = False
    source_pad_lines: int = 20
    max_context_chars: int = 6000
    max_identifiers: int = 12
    max_hits_per_kind: int = 3
    snippet_lines: int = 3
    common_hit_limit: int = 200
    index_max_files: int = 20000
    index_max_bytes: int = 64 << 20
    max_file_bytes: int = 1 << 20
    index_timeout_seconds: int = 60
    max_dataset_bytes: int = 64 << 20
    max_records: int = 20000
    keep_raw_chars: int = 0
    chunk_chars: int = 9500
    match_line_tolerance: int = 5


_MIB = 1 << 20
# key, field, low, high (inclusive)
_INT_KEYS = (
    ("NUM_CTX", "num_ctx", 512, 1 << 20),
    ("NUM_PREDICT", "num_predict", 64, 8192),
    ("ITEM_TIMEOUT_SECONDS", "item_timeout_seconds", 10, 1800),
    ("CALL_BUDGET_SECONDS", "call_budget_seconds", 30, 21600),
    ("SOURCE_PAD_LINES", "source_pad_lines", 0, 200),
    ("MAX_CONTEXT_CHARS", "max_context_chars", 500, 32000),
    ("MAX_IDENTIFIERS", "max_identifiers", 1, 64),
    ("MAX_HITS_PER_KIND", "max_hits_per_kind", 1, 20),
    ("SNIPPET_LINES", "snippet_lines", 0, 20),
    ("COMMON_HIT_LIMIT", "common_hit_limit", 10, 5000),
    ("INDEX_MAX_FILES", "index_max_files", 1, 100_000),
    ("INDEX_MAX_BYTES", "index_max_bytes", _MIB, 512 * _MIB),
    ("MAX_FILE_BYTES", "max_file_bytes", 1024, 8 * _MIB),
    ("INDEX_TIMEOUT_SECONDS", "index_timeout_seconds", 1, 600),
    ("MAX_DATASET_BYTES", "max_dataset_bytes", 1024, 512 * _MIB),
    ("MAX_RECORDS", "max_records", 1, 200_000),
    ("KEEP_RAW_CHARS", "keep_raw_chars", 0, 4000),
    ("CHUNK_CHARS", "chunk_chars", 1000, 32000),
    ("MATCH_LINE_TOLERANCE", "match_line_tolerance", 0, 100),
)
_ON = ("1", "true", "yes", "on")
_OFF = ("0", "false", "no", "off")


def _path_ok(value: str) -> bool:
    return chr(0) not in value and len(value) <= 4096


def _with(config: SecurityEvalConfig, name: str, value: str | int | float) -> SecurityEvalConfig:
    """``config`` with the field called ``name`` set; ``name`` comes from this module's key tables."""
    changes: dict[str, Any] = {name: value}
    return replace(config, **changes)


def security_eval_config_from_env(env: Mapping[str, object] | None, errors: list[str] | None = None) -> SecurityEvalConfig:
    """Parse the SONDER_SECURITY_EVAL_* keys of ``env``; problems are appended to ``errors``."""
    problems = errors if errors is not None else []
    text = {key: str(value).strip() for key, value in dict(env or {}).items()
            if isinstance(key, str) and key.startswith(PREFIX)}
    config = SecurityEvalConfig()

    def bad(key: str, why: str) -> None:
        problems.append("%s%s %s" % (PREFIX, key, why))

    for key in ("DATASET", "TEMPLATE_FILE"):
        value = text.get(PREFIX + key, "")
        if value:
            if _path_ok(value):
                config = _with(config, key.lower(), value)
            else:
                bad(key, "is not a usable path")
    roots: list[str] = []
    for part in text.get(PREFIX + "REPO_ROOTS", "").split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        if _path_ok(part) and os.path.isabs(part):
            if part not in roots:
                roots.append(part)
        else:
            bad("REPO_ROOTS", "entry %r is not an absolute path" % part[:60])
    if roots:
        config = replace(config, repo_roots=tuple(roots))
    model = text.get(PREFIX + "MODEL", "")
    if model:
        if MODEL_RE.fullmatch(model):
            config = replace(config, model=model)
        else:
            bad("MODEL", "is not a valid model name")
    context = text.get(PREFIX + "CONTEXT", "").lower()
    if context:
        if context in CONTEXT_MODES:
            config = replace(config, context=context)
        else:
            bad("CONTEXT", "must be one of %s" % ", ".join(CONTEXT_MODES))
    for key, name, low, high in _INT_KEYS:
        raw = text.get(PREFIX + key, "")
        if not raw:
            continue
        try:
            number = int(raw)
        except ValueError:
            bad(key, "is not an integer")
            continue
        if low <= number <= high:
            config = _with(config, name, number)
        else:
            bad(key, "must be within %d..%d" % (low, high))
    raw = text.get(PREFIX + "TEMPERATURE", "")
    if raw:
        try:
            temperature = float(raw)
        except ValueError:
            temperature = math.nan
        if 0.0 <= temperature <= 1.0:
            config = replace(config, temperature=temperature)
        else:
            bad("TEMPERATURE", "must be a number within 0..1")
    raw = text.get(PREFIX + "THINK", "").lower()
    if raw:
        if raw in _ON or raw in _OFF:
            config = replace(config, think=env_bool(raw))
        else:
            bad("THINK", "must be on or off")
    raw = text.get(PREFIX + "ALLOW_REMOTE_OLLAMA", "").lower()
    if raw:
        if raw in _ON or raw in _OFF:
            config = replace(config, allow_remote_ollama=env_bool(raw))
        else:
            bad("ALLOW_REMOTE_OLLAMA", "must be on or off")
    return config

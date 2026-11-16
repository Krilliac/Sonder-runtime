"""The ``[gpu_admission]`` section: one GPU, one queue, one window per model.

Every value that shapes admission lives here and nowhere else, with the same
bounds in TOML, in the environment overrides and in the typed binding. Defaults
are conservative: ``enforce`` mode, one GPU slot, 4 GiB floors on available RAM
and on commit headroom, and a request for a different ``num_ctx`` is served at
the window the model is already loaded with.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

MODES = ("enforce", "observe", "off")
ON_HOST_FLOOR = ("queue", "refuse")
CTX_CONFLICT = ("serve_pinned", "wait_unload")

# name -> (minimum, maximum), inclusive.
INTEGER_BOUNDS = {
    "gpu_slots": (1, 8),
    "embed_slots": (1, 8),
    "probe_slots": (1, 16),
    "probe_wait_ms": (1, 30_000),
    "max_queue_depth": (1, 4096),
    "max_queue_wait_seconds": (1, 3600),
    "poll_interval_ms": (20, 1000),
    "min_available_ram_mb": (0, 1_048_576),
    "min_commit_headroom_mb": (0, 1_048_576),
    "host_recheck_interval_ms": (100, 10_000),
    "unmeasured_host_factor_pct": (50, 400),
    "host_overhead_mb": (0, 16_384),
    "max_repins_per_model": (0, 20),
    "repin_window_seconds": (60, 86_400),
    "ps_cache_ms": (100, 10_000),
    "ctx_check_interval_seconds": (1, 3600),
    "lease_ttl_margin_seconds": (5, 600),
}

# Environment name for each key that has one (the rest are TOML-only).
ENVIRONMENT_NAMES = {
    "mode": "SONDER_GPU_ADMISSION",
    "store_path": "SONDER_GPU_ADMISSION_STORE",
    "gpu_slots": "SONDER_GPU_SLOTS",
    "embed_slots": "SONDER_GPU_EMBED_SLOTS",
    "probe_slots": "SONDER_GPU_PROBE_SLOTS",
    "probe_wait_ms": "SONDER_GPU_PROBE_WAIT_MS",
    "max_queue_depth": "SONDER_GPU_QUEUE_DEPTH",
    "max_queue_wait_seconds": "SONDER_GPU_QUEUE_WAIT_SECONDS",
    "poll_interval_ms": "SONDER_GPU_POLL_MS",
    "min_available_ram_mb": "SONDER_MIN_AVAILABLE_RAM_MB",
    "min_commit_headroom_mb": "SONDER_MIN_COMMIT_HEADROOM_MB",
    "on_host_floor": "SONDER_GPU_ON_HOST_FLOOR",
    "ctx_conflict": "SONDER_GPU_CTX_CONFLICT",
}


@dataclass(frozen=True)
class GpuAdmissionConfig:
    mode: str = "enforce"
    # "" selects the machine-wide default store (every Sonder process on the
    # box shares one GPU, so every one of them must share one queue).
    store_path: str = ""
    gpu_slots: int = 1
    embed_slots: int = 1
    probe_slots: int = 2
    probe_wait_ms: int = 2000
    max_queue_depth: int = 64
    max_queue_wait_seconds: int = 300
    poll_interval_ms: int = 100
    min_available_ram_mb: int = 4096
    min_commit_headroom_mb: int = 4096
    on_host_floor: str = "queue"
    host_recheck_interval_ms: int = 1000
    unmeasured_host_factor_pct: int = 125
    host_overhead_mb: int = 1024
    ctx_conflict: str = "serve_pinned"
    max_repins_per_model: int = 2
    repin_window_seconds: int = 600
    ps_cache_ms: int = 1000
    ctx_check_interval_seconds: int = 30
    lease_ttl_margin_seconds: int = 30
    runner_process_names: tuple[str, ...] = (
        "llama-server.exe", "llama-server", "ollama.exe", "ollama",
    )


def gpu_admission_errors(config) -> list[str]:
    """Every bound violation in ``config.gpu_admission``, as readable messages."""
    section = config.gpu_admission if hasattr(config, "gpu_admission") else config
    errors: list[str] = []
    for name, allowed in (("mode", MODES), ("on_host_floor", ON_HOST_FLOOR),
                          ("ctx_conflict", CTX_CONFLICT)):
        if getattr(section, name) not in allowed:
            errors.append("[gpu_admission].%s must be one of %s" % (name, ", ".join(allowed)))
    for name, (low, high) in INTEGER_BOUNDS.items():
        value = getattr(section, name)
        if type(value) is not int or not low <= value <= high:
            errors.append("[gpu_admission].%s must be an integer in %d..%d" % (name, low, high))
    path = section.store_path
    if not isinstance(path, str):
        errors.append("[gpu_admission].store_path must be a string")
    elif path:
        candidate = Path(path)
        if "\x00" in path or not candidate.is_absolute():
            errors.append("[gpu_admission].store_path must be an absolute path")
        elif candidate.is_symlink():
            errors.append("[gpu_admission].store_path must not be a symlink")
    names = section.runner_process_names
    if (not isinstance(names, tuple) or not 1 <= len(names) <= 16
            or any(not isinstance(name, str) or not 1 <= len(name) <= 128 or "/" in name
                   or "\\" in name for name in names)):
        errors.append("[gpu_admission].runner_process_names must list 1..16 bare process names")
    return errors


def apply_gpu_admission_environment(section: GpuAdmissionConfig, env, errors: list[str]) -> GpuAdmissionConfig:
    """Fold the documented ``SONDER_GPU_*`` overrides over ``section``.

    An unparseable value is reported and ignored (it never silently relaxes a
    limit); a parseable out-of-range value is caught by ``gpu_admission_errors``.
    """
    updates: dict[str, object] = {}
    for key, variable in ENVIRONMENT_NAMES.items():
        raw = str(env.get(variable, "") or "").strip()
        if not raw:
            continue
        if key in INTEGER_BOUNDS:
            try:
                updates[key] = int(raw)
            except ValueError:
                errors.append("%s is not an integer" % variable)
        elif key == "store_path":
            updates[key] = raw
        else:
            updates[key] = raw.lower()
    return replace(section, **updates) if updates else section


__all__ = [
    "CTX_CONFLICT", "ENVIRONMENT_NAMES", "GpuAdmissionConfig", "INTEGER_BOUNDS",
    "MODES", "ON_HOST_FLOOR", "apply_gpu_admission_environment", "gpu_admission_errors",
]

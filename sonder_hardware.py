"""Best-effort hardware detection and a local-model sizing recommender.

Sonder runs whatever model the host can actually serve, so the first useful
thing to know about a new box is how big a model it can hold and how the
orchestration loop should be tuned for it. This module answers that offline,
from cheap host probes, and turns the answer into concrete Ollama-shaped advice
(model size band, ``num_ctx``, ``keep_alive``) plus a read on whether the
adaptive speculation cost model is likely to earn its keep here.

Two design rules make this safe to import anywhere and easy to test:

- **Nothing runs at import.** No probe fires, no subprocess spawns, no mutable
  environment is read until you call :func:`detect_hardware`.
- **Every probe is injectable.** :func:`detect_hardware` takes a ``probes``
  mapping of small callables; tests pass fakes so they never touch real CPU,
  RAM, or a GPU, and never spawn ``nvidia-smi``.

The recommender is a pure function of a hardware dict, so you can feed it a
synthetic profile — a tiny laptop, a 24 GB desktop GPU, a 200 GB server — and
get deterministic advice back with no host access at all.
"""
from __future__ import annotations

import os
import platform
import subprocess


# --- model sizing thresholds --------------------------------------------------
#
# A model must fit in the fastest large memory the host has: on a GPU that is
# VRAM (the weights live on the card), on a CPU-only box that is system RAM.
# The two ladders differ because CPU inference wants OS headroom and is slow
# enough that we stay one notch more conservative than the raw fit would allow.
# Bands are quoted the way people shop for local models: parameter counts at a
# roughly 4-bit quantization, which is what actually fits these envelopes.
#
# Each ladder is read as "capacity strictly below this many GB -> this band".
_VRAM_BANDS = (
    (6.0, "3-4B"),      # 4-6 GB cards: small assistants only
    (12.0, "7B"),       # 8 GB cards comfortably hold a Q4 7-8B
    (40.0, "13-34B"),   # 16-24 GB desktop/workstation cards
    (float("inf"), "70B+"),  # 48 GB+ (A6000, dual-GPU, data-center)
)
_RAM_BANDS = (
    (9.0, "3-4B"),      # 8 GB laptops
    (24.0, "7B"),       # 16 GB mainstream machines
    (96.0, "13-34B"),   # 32-64 GB workstations
    (float("inf"), "70B+"),  # 128 GB+ servers
)

# Default context window per band. Bigger models are the ones people point at
# long-context work, and they are also the ones running on memory that can spare
# the KV cache, so the two scale together.
_BAND_CTX = {
    "3-4B": 4096,
    "7B": 8192,
    "13-34B": 16384,
    "70B+": 32768,
}
_MAX_CTX = 32768

# Workloads whose read-only tools are genuinely slow (big scans, remote reads),
# which is the *other* half the speculation overlap needs to be non-trivial.
_SLOW_TOOL_WORKLOADS = frozenset({"coding", "agentic", "research"})
# Workloads that use tools at all. Pure chat speculates nothing.
_KNOWN_WORKLOADS = frozenset({"general", "chat", "coding", "agentic", "research"})

# Capacity at or above which the box is dedicated enough to pin the model in
# memory instead of paying reload latency.
_RESIDENT_GB = 48.0


# --- default host probes ------------------------------------------------------
#
# These are the only functions that actually look at the machine. They are never
# called at import, and every one is overridable through ``detect_hardware``'s
# ``probes`` argument so tests can inject fakes. Each swallows its own failures
# and returns a conservative "unknown" rather than raising.

def _probe_cpu_count() -> int | None:
    try:
        return os.cpu_count()
    except Exception:
        return None


def _probe_total_ram_gb() -> float | None:
    """Total physical RAM in GB, via stdlib only, or ``None`` if unknown."""
    # POSIX (Linux, and most BSD/macOS): pages * page size.
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return round(pages * page_size / 1e9, 1)
    except (ValueError, AttributeError, OSError):
        pass
    # Windows: GlobalMemoryStatusEx via ctypes, guarded hard.
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemStatus()
        status.dwLength = ctypes.sizeof(_MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return round(status.ullTotalPhys / 1e9, 1)
    except Exception:
        pass
    return None


def _probe_gpu() -> tuple[bool, float | None]:
    """Return ``(gpu_present, vram_gb)`` by shelling out to ``nvidia-smi``.

    Kept behind a short timeout and a blanket ``except`` so a missing binary,
    a hung driver, or a machine without an NVIDIA card all resolve to
    ``(False, None)`` instead of raising. Non-NVIDIA accelerators are simply
    not detected here; callers can inject a probe that knows about them.
    """
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception:
        return (False, None)
    if out.returncode != 0:
        return (False, None)
    best_mib = 0.0
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            best_mib = max(best_mib, float(line))
        except ValueError:
            continue
    if best_mib <= 0:
        return (False, None)
    return (True, round(best_mib / 1024.0, 1))


def _probe_platform() -> str:
    try:
        return platform.system() or "unknown"
    except Exception:
        return "unknown"


_DEFAULT_PROBES = {
    "cpu_count": _probe_cpu_count,
    "total_ram_gb": _probe_total_ram_gb,
    "gpu": _probe_gpu,
    "platform": _probe_platform,
}


def detect_hardware(probes=None) -> dict:
    """Return a best-effort hardware profile as a plain dict.

    Keys: ``cpu_count`` (int|None), ``total_ram_gb`` (float|None),
    ``gpu_present`` (bool), ``vram_gb`` (float|None), ``platform`` (str).

    ``probes`` maps any of ``cpu_count``, ``total_ram_gb``, ``gpu``,
    ``platform`` to a zero-argument callable, overriding the corresponding
    default host probe. The ``gpu`` probe returns ``(present, vram_gb)``.
    Every probe is invoked inside a guard, so a raising fake still yields a
    well-formed profile with the offending field marked unknown.
    """
    merged = dict(_DEFAULT_PROBES)
    if probes:
        merged.update(probes)

    def _call(name, default):
        fn = merged.get(name)
        if fn is None:
            return default
        try:
            return fn()
        except Exception:
            return default

    cpu_count = _call("cpu_count", None)
    total_ram_gb = _call("total_ram_gb", None)
    gpu_result = _call("gpu", (False, None))
    plat = _call("platform", "unknown")

    try:
        gpu_present, vram_gb = gpu_result
        gpu_present = bool(gpu_present)
    except (TypeError, ValueError):
        gpu_present, vram_gb = False, None

    return {
        "cpu_count": cpu_count,
        "total_ram_gb": total_ram_gb,
        "gpu_present": gpu_present,
        "vram_gb": vram_gb if gpu_present else None,
        "platform": plat if isinstance(plat, str) else "unknown",
    }


def _band_for(capacity_gb: float, ladder) -> str:
    for ceiling, band in ladder:
        if capacity_gb < ceiling:
            return band
    return ladder[-1][1]


def _capacity(hw: dict) -> tuple[float, str]:
    """Return ``(usable_gb, basis)`` where basis is ``'vram'`` or ``'ram'``.

    A GPU with known VRAM decides the band (weights live on the card); on a
    CPU-only box we fall back to system RAM. Unknowns collapse to zero, which
    lands on the smallest band — the safe direction to be wrong in.
    """
    if hw.get("gpu_present") and hw.get("vram_gb"):
        return float(hw["vram_gb"]), "vram"
    ram = hw.get("total_ram_gb")
    return (float(ram) if ram else 0.0), "ram"


def recommend(hw: dict, *, workload: str = "general") -> dict:
    """Advise how to run a local model on ``hw`` for a given ``workload``.

    Returns a dict with ``model_band``, ``num_ctx``, ``keep_alive``,
    ``speculation_likely`` (bool), the memory ``basis`` and ``capacity_gb`` the
    call reasoned from, and a ``rationale`` list of short human-readable lines.

    ``workload`` is one of ``general`` (default), ``chat``, ``coding``,
    ``agentic``, ``research``; anything else is treated as ``general``.
    """
    if workload not in _KNOWN_WORKLOADS:
        workload = "general"

    capacity_gb, basis = _capacity(hw)
    ladder = _VRAM_BANDS if basis == "vram" else _RAM_BANDS
    band = _band_for(capacity_gb, ladder)

    rationale: list[str] = []
    if basis == "vram":
        rationale.append(
            f"GPU with ~{capacity_gb:g} GB VRAM sizes the model band to {band}."
        )
    else:
        ram_note = f"~{capacity_gb:g} GB" if capacity_gb else "unknown"
        rationale.append(
            f"No usable GPU detected; {ram_note} system RAM sizes the band "
            f"to {band} on CPU inference."
        )

    # Context window: start from the band default, widen for tool-heavy work
    # that carries large histories, narrow for pure chat.
    num_ctx = _BAND_CTX[band]
    if workload in ("coding", "agentic"):
        num_ctx = min(num_ctx * 2, _MAX_CTX)
        rationale.append(
            f"{workload} workload keeps large histories; widening num_ctx to "
            f"{num_ctx}."
        )
    elif workload == "chat":
        num_ctx = max(num_ctx // 2, 2048)
        rationale.append(f"chat workload narrows num_ctx to {num_ctx}.")
    else:
        rationale.append(f"num_ctx {num_ctx} is the default for the {band} band.")

    # keep_alive: pin the model on dedicated memory, hold it a while on
    # roomy/GPU boxes, drop it quickly on a memory-tight laptop.
    if capacity_gb >= _RESIDENT_GB:
        keep_alive = "-1"
        rationale.append(
            f"~{capacity_gb:g} GB is dedicated-class memory; keep_alive=-1 "
            "pins the model resident."
        )
    elif band in ("13-34B", "70B+") or hw.get("gpu_present"):
        keep_alive = "30m"
        rationale.append(
            "Roomy/GPU box; keep_alive=30m avoids reloading a big model "
            "between turns."
        )
    else:
        keep_alive = "5m"
        rationale.append(
            "Memory-tight box; keep_alive=5m frees RAM promptly after a turn."
        )

    # Adaptive speculation cost model. The overlap it can hide is the shorter of
    # the model-decision latency and the speculated tool's latency. A big model
    # makes decisions multi-second (worth hiding behind); slow-tool workloads
    # make the tools substantial (worth overlapping). Need both: a fast laptop
    # running a small model has sub-second decisions and the overlap is ~0, so
    # the model stays dormant; pure chat speculates nothing at all.
    big_model = band in ("13-34B", "70B+")
    tool_using = workload != "chat"
    slow_tools = workload in _SLOW_TOOL_WORKLOADS
    speculation_likely = big_model and slow_tools
    if not tool_using:
        rationale.append(
            "Speculation dormant: chat issues no tool calls to speculate on."
        )
    elif speculation_likely:
        rationale.append(
            f"Speculation likely engages: a {band} model's multi-second "
            f"decisions can hide this {workload} workload's slow read-only "
            "tools."
        )
    elif big_model:
        rationale.append(
            f"Speculation borderline: the {band} model's decisions are slow "
            "enough, but a general workload's tools are too fast to overlap "
            "much."
        )
    else:
        rationale.append(
            f"Speculation dormant: a {band} model decides in well under a "
            "second, so there is almost no latency to hide."
        )

    return {
        "workload": workload,
        "model_band": band,
        "num_ctx": num_ctx,
        "keep_alive": keep_alive,
        "speculation_likely": speculation_likely,
        "basis": basis,
        "capacity_gb": round(capacity_gb, 1),
        "rationale": rationale,
    }


def render(hw: dict, rec: dict) -> str:
    """Return a compact terminal summary of a profile and its recommendation."""
    cpu = hw.get("cpu_count")
    ram = hw.get("total_ram_gb")
    lines = ["Sonder hardware profile", "-----------------------"]
    lines.append(f"  platform   : {hw.get('platform', 'unknown')}")
    lines.append(f"  cpu cores  : {cpu if cpu is not None else 'unknown'}")
    lines.append(
        f"  system ram : {f'{ram:g} GB' if ram else 'unknown'}"
    )
    if hw.get("gpu_present"):
        vram = hw.get("vram_gb")
        lines.append(
            f"  gpu        : present ({f'{vram:g} GB VRAM' if vram else 'VRAM unknown'})"
        )
    else:
        lines.append("  gpu        : none detected")

    lines.append("")
    lines.append("Recommendation")
    lines.append("--------------")
    lines.append(f"  workload    : {rec.get('workload', 'general')}")
    lines.append(f"  model band  : {rec.get('model_band', '?')}")
    lines.append(f"  num_ctx     : {rec.get('num_ctx', '?')}")
    lines.append(f"  keep_alive  : {rec.get('keep_alive', '?')}")
    lines.append(
        f"  speculation : {'likely engages' if rec.get('speculation_likely') else 'dormant'}"
    )
    lines.append("")
    lines.append("Why")
    lines.append("---")
    for note in rec.get("rationale", []):
        lines.append(f"  - {note}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    import sys

    _workload = sys.argv[1] if len(sys.argv) > 1 else "general"
    _hw = detect_hardware()
    _rec = recommend(_hw, workload=_workload)
    print(render(_hw, _rec))

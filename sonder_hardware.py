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
- **Every probe is injectable.** :func:`detect_profile` takes a ``probes``
  mapping of small callables; tests pass fakes so they never touch real CPU,
  RAM, or a GPU, and never spawn ``nvidia-smi``.

The recommender is a pure function of a hardware dict, so you can feed it a
synthetic profile — a tiny laptop, a 24 GB desktop GPU, a 200 GB server — and
get deterministic advice back with no host access at all.
"""
from __future__ import annotations

import os
import platform
import json
import re
import subprocess
import threading
from pathlib import Path


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
    (7.0, "3-4B"),      # 4-6 GB cards: small assistants only
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
_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_CACHE: dict | None = None

# Approximate Q4 weight + modest working-context envelopes. These are planning
# classes, not promises: exact architectures, quantizers, and context lengths
# vary. The explicit ladder is more useful than the broad legacy bands when a
# user is deciding whether CPU/unified-memory spill can make a model runnable.
_MODEL_FOOTPRINTS = (
    (3.0, "3-4B"),
    (6.0, "7-8B"),
    (10.0, "14B"),
    (20.0, "32B"),
    (40.0, "70B"),
)


# --- mixture-of-experts: two parameter counts, two different questions --------
#
# Every ladder above reads one parameter count as the answer to both "how much
# memory does this need" and "how slow will it be". A Mixture-of-Experts model
# breaks that: all experts must be resident, so *memory* still follows the
# **total** count, but a single token only routes through a fraction of them, so
# *decode latency* follows the **active** count. Qwen3-Coder-30B-A3B is 31B
# total / 3.3B active -- it occupies a 32B-class memory envelope and decodes
# roughly like a 3B. Sizing it from either number alone is wrong in one
# direction or the other: from the total, we would predict multi-second
# decisions it does not have; from the active count, we would claim it fits on
# a card that cannot hold it.
#
# So the two are kept separate below. The hardware ladders keep deciding what
# *fits* (unchanged), and this ladder decides how fast the bound model actually
# *decides*, which is the only input the speculation read needs. Bands reuse the
# hardware vocabulary so the two are directly comparable.
_DECODE_BANDS = (
    (5.0, "3-4B"),          # <5B active: sub-second decisions
    (13.0, "7B"),           # 7-8B active
    (40.0, "13-34B"),       # 13-34B active: multi-second decisions
    (float("inf"), "70B+"),
)

# Ollama/Qwen spell an MoE tag as "<total>b-a<active>b" -- qwen3-coder:30b-a3b,
# qwen3:235b-a22b, qwen3-coder:480b-a35b. A dense tag carries a single "<n>b".
# Quantization suffixes (-q4_K_M, -fp16, -instruct) trail the size and are
# ignored. Anything unrecognised returns None rather than a guess, because a
# wrong parameter count is worse here than an absent one: absent falls back to
# today's hardware-derived behavior, wrong silently changes the advice.
_MOE_TAG_RE = re.compile(r"(?<![0-9a-z])([0-9]+(?:\.[0-9]+)?)b-a([0-9]+(?:\.[0-9]+)?)b(?![0-9a-z])")
_DENSE_TAG_RE = re.compile(r"(?<![0-9a-z])([0-9]+(?:\.[0-9]+)?)b(?![0-9a-z])")


def params_from_model_tag(tag) -> tuple[float, float] | None:
    """Parse ``(total_params_b, active_params_b)`` out of an Ollama model tag.

    Returns ``None`` when the tag carries no usable size, which is the common
    case for an alias like ``sonder:latest``. For a dense tag both numbers are
    the same; for an MoE tag they differ, and that difference is the whole point
    of this function.
    """
    text = str(tag or "").strip().lower()
    if not text:
        return None
    moe = _MOE_TAG_RE.search(text)
    if moe:
        total = float(moe.group(1))
        active = float(moe.group(2))
        # An "active" count above the total is a malformed tag, not an MoE;
        # refuse it rather than reporting a model faster than it can be.
        # active == total is degenerate but harmless -- it simply reads dense.
        if total > 0 and 0 < active <= total:
            return total, active
        return None
    dense = _DENSE_TAG_RE.search(text)
    if dense:
        total = float(dense.group(1))
        if total > 0:
            return total, total
    return None


def decode_band(active_params_b) -> str | None:
    """Return the band whose *decode speed* an ``active_params_b`` model matches.

    ``None`` when the active count is unknown or nonsensical, so callers keep
    their existing hardware-derived band instead of acting on a bad number.
    """
    try:
        active = float(active_params_b)
    except (TypeError, ValueError):
        return None
    if active <= 0:
        return None
    return _band_for(active, _DECODE_BANDS)


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

    Kept behind a timeout and a blanket ``except`` so a missing binary, a hung
    driver, or a machine without an NVIDIA card all resolve to ``(False, None)``
    instead of raising. Non-NVIDIA accelerators are simply not detected here;
    callers can inject a probe that knows about them.

    The timeout is generous and one retry follows a timeout. On a laptop with
    switchable graphics the discrete GPU idles powered down, and the first
    ``nvidia-smi`` after an idle stretch blocks while the driver wakes it --
    can take several seconds, with the next call returning quickly. A tight
    timeout therefore fails exactly when
    the GPU is cold, which is precisely when nothing else has warmed it: the
    host then reports "no GPU", sizes the band for CPU inference, and advises
    a short keep_alive with speculation dormant -- all on a machine with a
    working CUDA device. Retrying once converts that intermittent miss into a
    hit, because the timed-out probe is itself the wake-up call.
    """
    out = None
    for timeout_s in (8.0, 8.0):
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            break
        except subprocess.TimeoutExpired:
            continue  # cold GPU; the probe that just timed out did the waking
        except Exception:
            return (False, None)
    if out is None:
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


_PCI_VENDORS = {
    "0x1002": "AMD",
    "0x10de": "NVIDIA",
    "0x8086": "Intel",
    "0x106b": "Apple",
    "0x1acc": "Hailo",
}


def _vendor_from_text(*values: object) -> str:
    """Normalize a display-adapter vendor without claiming backend support."""
    text = " ".join(str(value or "") for value in values).lower()
    if "nvidia" in text or "ven_10de" in text:
        return "NVIDIA"
    if "advanced micro devices" in text or "amd" in text or "ati " in text or "ven_1002" in text:
        return "AMD"
    if "intel" in text or "ven_8086" in text:
        return "Intel"
    if "apple" in text or "ven_106b" in text:
        return "Apple"
    return "unknown"


def _looks_integrated(name: str, vendor: str) -> bool | None:
    lowered = (name or "").lower()
    if vendor == "Apple":
        return True
    if vendor == "NVIDIA":
        return False
    if vendor == "Intel":
        if any(marker in lowered for marker in (" arc a", " arc b", "arc pro", "data center gpu flex")):
            return False
        if any(marker in lowered for marker in ("uhd graphics", "iris", "hd graphics")):
            return True
        return None
    if vendor == "AMD":
        if any(marker in lowered for marker in ("radeon graphics", "780m", "760m", "740m", "680m", "660m", "890m")):
            return True
        if any(marker in lowered for marker in ("radeon rx", "radeon pro", "instinct")):
            return False
    return None


def _accelerator(
    *, name: str, vendor: str = "unknown", memory_gb: float | None = None,
    memory_kind: str = "unknown", integrated: bool | None = None, probe: str,
    device_id: str = "",
    presence_verified: bool | None = True,
) -> dict:
    if integrated is None:
        integrated = _looks_integrated(name, vendor)
    return {
        "name": str(name or "display adapter"),
        "vendor": vendor,
        "memory_gb": round(float(memory_gb), 1) if memory_gb else None,
        "memory_kind": memory_kind,
        "integrated": integrated if isinstance(integrated, bool) else None,
        "probe": probe,
        "device_id": str(device_id or ""),
        "presence_verified": (
            presence_verified if isinstance(presence_verified, bool) else None
        ),
        # Detection proves only that the OS enumerates a device. Ollama/backend
        # readiness requires a separate runtime probe and is intentionally not
        # inferred from a vendor name or installed display driver.
        "runtime_ready": None,
    }


def _probe_windows_accelerators(registry=None) -> list[dict]:
    """Enumerate Windows display-class devices using the stdlib registry API."""
    if registry is None:
        try:
            import winreg as registry
        except Exception:
            return []
    class_path = (
        "SYSTEM\\CurrentControlSet\\Control\\Class\\"
        "{4d36e968-e325-11ce-bfc1-08002be10318}"
    )
    records: list[dict] = []
    access = getattr(registry, "KEY_READ", 0) | getattr(
        registry, "KEY_WOW64_64KEY", 0
    )

    def _open(parent, child):
        try:
            return registry.OpenKey(parent, child, 0, access)
        except TypeError:
            return registry.OpenKey(parent, child)

    try:
        root = _open(registry.HKEY_LOCAL_MACHINE, class_path)
    except OSError:
        return []
    try:
        count = registry.QueryInfoKey(root)[0]
        for index in range(count):
            try:
                child_name = registry.EnumKey(root, index)
                child = _open(root, child_name)
            except OSError:
                continue
            try:
                values = {}
                for value_index in range(registry.QueryInfoKey(child)[1]):
                    try:
                        key, value, _kind = registry.EnumValue(child, value_index)
                        values[key] = value
                    except OSError:
                        continue
                name = str(values.get("DriverDesc") or "").strip()
                if not name:
                    continue
                provider = values.get("ProviderName")
                matching_id = values.get("MatchingDeviceId")
                vendor = _vendor_from_text(provider, matching_id, name)
                raw_bytes = values.get("HardwareInformation.qwMemorySize")
                if not isinstance(raw_bytes, int) or raw_bytes <= 0:
                    raw_bytes = values.get("HardwareInformation.MemorySize")
                memory_gb = raw_bytes / (1024 ** 3) if isinstance(raw_bytes, int) and raw_bytes > 0 else None
                integrated = _looks_integrated(name, vendor)
                records.append(_accelerator(
                    name=name,
                    vendor=vendor,
                    memory_gb=memory_gb,
                    memory_kind="reported adapter memory" if memory_gb else "unknown",
                    integrated=integrated,
                    probe="windows-display-registry",
                    device_id=child_name,
                    presence_verified=None,
                ))
            except OSError:
                continue
            finally:
                registry.CloseKey(child)
    finally:
        registry.CloseKey(root)
    return _dedupe_accelerators(records)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return ""


def _probe_linux_accelerators(
    root: Path = Path("/sys/class/drm"), nvidia_probe=None,
) -> list[dict]:
    """Enumerate bounded DRM sysfs paths; supplement NVIDIA via nvidia-smi."""
    records: list[dict] = []
    try:
        cards = sorted(root.glob("card[0-9]*"))
    except OSError:
        return records
    seen_devices = set()
    for card in cards:
        device = card / "device"
        try:
            identity = str(device.resolve(strict=True))
        except OSError:
            identity = str(device)
        if identity in seen_devices:
            continue
        seen_devices.add(identity)
        vendor_id = _read_text(device / "vendor").lower()
        if not vendor_id:
            continue
        vendor = _PCI_VENDORS.get(vendor_id, "unknown")
        uevent = _read_text(device / "uevent")
        driver = ""
        try:
            driver = (device / "driver").resolve(strict=True).name
        except OSError:
            pass
        raw_vram = _read_text(device / "mem_info_vram_total")
        try:
            memory_gb = int(raw_vram) / (1024 ** 3) if raw_vram else None
        except ValueError:
            memory_gb = None
        name = "%s display adapter" % (vendor if vendor != "unknown" else card.name)
        if driver:
            name += " (%s)" % driver
        records.append(_accelerator(
            name=name,
            vendor=vendor,
            memory_gb=memory_gb,
            memory_kind="dedicated VRAM" if memory_gb else "unknown",
            integrated=_looks_integrated(name + " " + uevent, vendor),
            probe="linux-drm-sysfs",
            device_id=identity,
        ))
    nvidia = (nvidia_probe or _probe_nvidia_accelerators)()
    if nvidia:
        records = [item for item in records if item.get("vendor") != "NVIDIA"] + nvidia
    return _dedupe_accelerators(records)


def _parse_memory_gb(value: object) -> float | None:
    text = str(value or "").strip().lower().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(gb|mb)", text)
    if not match:
        return None
    amount = float(match.group(1))
    return amount if match.group(2) == "gb" else amount / 1024.0


def _probe_nvidia_accelerators(runner=None) -> list[dict]:
    runner = runner or subprocess.run
    result = None
    for _attempt in range(2):
        try:
            result = runner(
                [
                    "nvidia-smi",
                    "--query-gpu=index,uuid,name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=8.0,
            )
            break
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            return []
    if result is None:
        return []
    if result.returncode != 0:
        return []
    records = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        index, uuid = parts[0], parts[1]
        name = ",".join(parts[2:-1]).strip()
        raw_memory = parts[-1]
        try:
            memory_gb = float(raw_memory) / 1024.0
        except ValueError:
            continue
        records.append(_accelerator(
            name=name.strip() or "NVIDIA GPU", vendor="NVIDIA",
            memory_gb=memory_gb, memory_kind="dedicated VRAM",
            integrated=False, probe="nvidia-smi",
            device_id=uuid or ("nvidia-index:%s" % index),
        ))
    return records


def _dedupe_accelerators(records: list[dict]) -> list[dict]:
    """Drop exact/stale duplicates while retaining distinct physical adapters."""
    result = []
    seen = set()
    for item in records:
        device_id = str(item.get("device_id") or "").lower()
        key = (str(item.get("probe") or ""), device_id) if device_id else (
            str(item.get("vendor") or "unknown").lower(),
            str(item.get("name") or "display adapter").lower(),
            item.get("memory_gb"),
            item.get("integrated"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _probe_macos_accelerators(runner=None) -> list[dict]:
    """Parse bounded system_profiler JSON; Apple memory remains unified/unknown."""
    runner = runner or subprocess.run
    try:
        result = runner(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True, text=True, timeout=8.0,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    try:
        rows = json.loads(result.stdout).get("SPDisplaysDataType", [])
    except (TypeError, ValueError, AttributeError):
        return []
    records = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("sppci_model") or row.get("_name") or "display adapter")
        vendor = _vendor_from_text(row.get("spdisplays_vendor"), name)
        unified = vendor == "Apple" or bool(row.get("spdisplays_vram_shared"))
        memory_gb = None if unified else _parse_memory_gb(row.get("spdisplays_vram"))
        records.append(_accelerator(
            name=name, vendor=vendor, memory_gb=memory_gb,
            memory_kind="unified system memory" if unified else ("dedicated VRAM" if memory_gb else "unknown"),
            integrated=unified or _looks_integrated(name, vendor),
            probe="macos-system-profiler",
            device_id=str(row.get("_spdisplays_device-id") or ""),
        ))
    return _dedupe_accelerators(records)


def _probe_accelerators() -> list[dict]:
    system = _probe_platform().lower()
    if system == "windows":
        records = _probe_windows_accelerators()
        nvidia = _probe_nvidia_accelerators()
        if nvidia:
            records = [
                item for item in records if item.get("vendor") != "NVIDIA"
            ] + nvidia
        return _dedupe_accelerators(records)
    if system == "linux":
        return _probe_linux_accelerators()
    if system == "darwin":
        return _probe_macos_accelerators()
    return []


def _probe_platform() -> str:
    try:
        return platform.system() or "unknown"
    except Exception:
        return "unknown"


_DEFAULT_PROBES = {
    "cpu_count": _probe_cpu_count,
    "total_ram_gb": _probe_total_ram_gb,
    "gpu": _probe_gpu,
    "accelerators": _probe_accelerators,
    "platform": _probe_platform,
}


def detect_profile(probes=None) -> dict:
    """Return an enriched best-effort hardware profile as a plain dict.

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
    # Explicit legacy ``gpu`` injection must keep its old isolation guarantee:
    # it replaces, rather than supplements, the new platform probe.
    use_legacy_gpu = bool(probes and "gpu" in probes and "accelerators" not in probes)
    accelerators = [] if use_legacy_gpu else _call("accelerators", [])
    if not isinstance(accelerators, list):
        accelerators = []
    accelerators = [item for item in accelerators if isinstance(item, dict)]
    gpu_result = _call("gpu", (False, None)) if use_legacy_gpu else (False, None)
    plat = _call("platform", "unknown")

    try:
        gpu_present, vram_gb = gpu_result
        gpu_present = bool(gpu_present)
    except (TypeError, ValueError):
        gpu_present, vram_gb = False, None

    if gpu_present:
        accelerators.append(_accelerator(
            name="GPU", vendor="unknown", memory_gb=vram_gb,
            memory_kind="dedicated VRAM" if vram_gb else "unknown",
            integrated=False, probe="legacy-gpu",
        ))
    known_memory = [
        float(item["memory_gb"]) for item in accelerators
        if item.get("memory_gb")
        and item.get("integrated") is False
        and item.get("presence_verified") is True
    ]
    detected_vram = max(known_memory) if known_memory else None
    return {
        "cpu_count": cpu_count,
        "total_ram_gb": total_ram_gb,
        "gpu_present": bool(accelerators),
        "vram_gb": round(detected_vram, 1) if detected_vram else None,
        "platform": plat if isinstance(plat, str) else "unknown",
        "accelerators": accelerators,
        "accelerator_count": len(accelerators),
        "runtime_readiness": "not-probed",
    }


def detect_hardware(probes=None) -> dict:
    """Return the stable legacy five-field profile.

    New callers that need per-device inventory should use :func:`detect_profile`.
    Keeping this exact shape avoids breaking scripts that compare or serialize
    the original public result.
    """
    profile = detect_profile(probes=probes)
    return {
        "cpu_count": profile["cpu_count"],
        "total_ram_gb": profile["total_ram_gb"],
        "gpu_present": profile["gpu_present"],
        "vram_gb": profile["vram_gb"],
        "platform": profile["platform"],
    }


def get_profile(*, workload: str = "general", refresh: bool = False,
                model: str = "") -> dict:
    """Return cached host inventory plus a fresh workload recommendation.

    ``model`` is the optional tag bound to the tier being advised; it only
    sharpens the recommendation for MoE models (see :func:`recommend`).
    """
    global _PROFILE_CACHE
    with _PROFILE_CACHE_LOCK:
        if refresh or _PROFILE_CACHE is None:
            _PROFILE_CACHE = detect_profile()
        hardware = dict(_PROFILE_CACHE)
        hardware["accelerators"] = [
            dict(item) for item in _PROFILE_CACHE.get("accelerators", [])
        ]
    return {
        "hardware": hardware,
        "recommendation": recommend(hardware, workload=workload, model=model),
    }


def profile_text(*, workload: str = "general", refresh: bool = False,
                 model: str = "") -> str:
    """Render the cached enriched profile for humans and tool-using agents."""
    profile = get_profile(workload=workload, refresh=refresh, model=model)
    return render(profile["hardware"], profile["recommendation"])


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
    if "accelerators" in hw:
        discrete_memory = [
            float(item["memory_gb"])
            for item in (hw.get("accelerators") or [])
            if isinstance(item, dict)
            and item.get("memory_gb")
            and item.get("integrated") is False
            and item.get("presence_verified") is True
        ]
        if discrete_memory:
            return max(discrete_memory), "vram"
    elif hw.get("gpu_present") and hw.get("vram_gb"):
        # Preserve the legacy five-field API, whose GPU probe represented a
        # discrete NVIDIA device and had no per-adapter topology metadata.
        return float(hw["vram_gb"]), "vram"
    ram = hw.get("total_ram_gb")
    return (float(ram) if ram else 0.0), "ram"


def _largest_model_class(usable_gb: float) -> str:
    chosen = "below 3B"
    for footprint, label in _MODEL_FOOTPRINTS:
        if usable_gb + 1e-9 < footprint:
            break
        chosen = label
    return chosen


def _execution_plan(hw: dict) -> dict:
    """Return conservative resident and CPU/unified-spill planning numbers."""
    accelerators = hw.get("accelerators") or []
    discrete = [
        item for item in accelerators
        if isinstance(item, dict)
        and item.get("integrated") is False
        and item.get("presence_verified") is True
        and item.get("memory_gb")
    ]
    unified = [
        item for item in accelerators
        if isinstance(item, dict) and item.get("memory_kind") == "unified system memory"
    ]
    primary = max(discrete, key=lambda item: float(item.get("memory_gb") or 0), default=None)
    ram = float(hw.get("total_ram_gb") or 0.0)
    if primary:
        fast_gb = float(primary.get("memory_gb") or 0.0)
        resident_usable = fast_gb * 0.85
        # A split model can combine most VRAM with RAM after reserving the
        # larger of 4 GB or 25% for the OS, runtime, KV cache, and tools.
        ram_for_weights = max(0.0, ram - max(4.0, ram * 0.25))
        hybrid_usable = resident_usable + ram_for_weights
        mode = "gpu-resident" if _largest_model_class(hybrid_usable) == _largest_model_class(resident_usable) else "gpu+ram-hybrid"
    elif unified:
        primary = unified[0]
        resident_usable = ram * 0.70
        hybrid_usable = resident_usable
        mode = "unified-memory"
    else:
        resident_usable = ram * 0.70
        hybrid_usable = resident_usable
        mode = "cpu" if ram else "unknown"

    auxiliary = [
        item for item in accelerators
        if isinstance(item, dict) and item is not primary
    ]
    cpu_count = int(hw.get("cpu_count") or 4)
    notes = [
        "Leave GPU-layer placement on auto unless a measured backend requires an override.",
        "Start from detected logical CPUs, then benchmark lower thread counts; physical/performance-core counts and cache topology are not inferred here.",
    ]
    if auxiliary:
        notes.append(
            "Keep auxiliary/integrated adapters available for displays or a separate small routing, embedding, or draft service; verify backend support before assigning work."
        )
    if mode == "gpu+ram-hybrid":
        notes.append(
            "The larger class requires system-memory spill and is capacity-oriented; expect materially lower token throughput than a fully resident model."
        )
    return {
        "execution_mode": mode,
        "primary_accelerator": primary,
        "auxiliary_accelerators": auxiliary,
        "resident_usable_gb": round(resident_usable, 1),
        "hybrid_usable_gb": round(hybrid_usable, 1),
        "resident_model_class": _largest_model_class(resident_usable),
        "hybrid_model_class": _largest_model_class(hybrid_usable),
        "runtime_options": {
            "num_thread": max(1, cpu_count),
            "num_gpu": "auto",
            "num_batch": 512,
        },
        "optimization_notes": notes,
    }


def recommend(hw: dict, *, workload: str = "general", model: str = "") -> dict:
    """Advise how to run a local model on ``hw`` for a given ``workload``.

    Returns a dict with ``model_band``, ``num_ctx``, ``keep_alive``,
    ``speculation_likely`` (bool), the memory ``basis`` and ``capacity_gb`` the
    call reasoned from, and a ``rationale`` list of short human-readable lines.

    ``workload`` is one of ``general`` (default), ``chat``, ``coding``,
    ``agentic``, ``research``; anything else is treated as ``general``.

    ``model`` is the optional tag actually bound to the tier being advised. It
    is used for one thing: reading the model's *active* parameter count so the
    speculation call is made on how fast the model decides rather than on how
    much memory the host could hold. Omit it and every number below is exactly
    what it was before -- the hardware band stands in for the model, which is
    correct for a dense model and wrong only for MoE.
    """
    if not isinstance(workload, str) or workload not in _KNOWN_WORKLOADS:
        workload = "general"

    capacity_gb, basis = _capacity(hw)
    params = params_from_model_tag(model)
    total_params_b, active_params_b = params if params else (None, None)
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
            f"No accelerator memory was detected; {ram_note} system RAM "
            f"conservatively sizes the band to {band}. Ollama may still use "
            "a Metal, AMD, Intel, or other backend it detects."
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
    # Which band governs decode speed. With no bound model the hardware band is
    # the only estimate available and is used exactly as before; with an MoE tag
    # the active count takes over, because that -- not the resident footprint --
    # is what sets how long a decision takes.
    speed_band = decode_band(active_params_b) or band
    if params and active_params_b < total_params_b:
        rationale.append(
            f"{model} is MoE: ~{total_params_b:g}B total sets the {band} memory "
            f"envelope, but only ~{active_params_b:g}B is active per token, so it "
            f"decides like a {speed_band} model."
        )

    big_model = speed_band in ("13-34B", "70B+")
    tool_using = workload != "chat"
    slow_tools = workload in _SLOW_TOOL_WORKLOADS
    speculation_likely = big_model and slow_tools
    if not tool_using:
        rationale.append(
            "Speculation dormant: chat issues no tool calls to speculate on."
        )
    elif speculation_likely:
        rationale.append(
            f"Speculation likely engages: a {speed_band} model's multi-second "
            f"decisions can hide this {workload} workload's slow read-only "
            "tools."
        )
    elif big_model:
        rationale.append(
            f"Speculation borderline: the {speed_band} model's decisions are slow "
            "enough, but a general workload's tools are too fast to overlap "
            "much."
        )
    else:
        rationale.append(
            f"Speculation dormant: a {speed_band} model decides in well under a "
            "second, so there is almost no latency to hide."
        )

    result = {
        "workload": workload,
        "model_band": band,
        "decode_band": speed_band,
        "model": str(model or ""),
        "total_params_b": total_params_b,
        "active_params_b": active_params_b,
        "num_ctx": num_ctx,
        "keep_alive": keep_alive,
        "speculation_likely": speculation_likely,
        "basis": basis,
        "capacity_gb": round(capacity_gb, 1),
        "rationale": rationale,
    }
    result.update(_execution_plan(hw))
    return result


def render(hw: dict, rec: dict) -> str:
    """Return a compact terminal summary of a profile and its recommendation."""
    cpu = hw.get("cpu_count")
    ram = hw.get("total_ram_gb")
    lines = ["Sonder hardware profile", "-----------------------"]
    lines.append(f"  platform   : {hw.get('platform', 'unknown')}")
    lines.append(f"  logical CPUs: {cpu if cpu is not None else 'unknown'}")
    lines.append(
        f"  system ram : {f'{ram:g} GB' if ram else 'unknown'}"
    )
    if hw.get("gpu_present"):
        vram = hw.get("vram_gb")
        lines.append(
            f"  gpu        : present ({f'{vram:g} GB VRAM' if vram else 'VRAM unknown'})"
        )
    else:
        lines.append("  gpu memory : not detected (Ollama may still accelerate)")
    for index, item in enumerate(hw.get("accelerators") or [], 1):
        memory = item.get("memory_gb")
        memory_text = f", {memory:g} GB {item.get('memory_kind', 'memory')}" if memory else f", {item.get('memory_kind', 'memory unknown')}"
        integrated = item.get("integrated")
        role = "integrated" if integrated is True else ("discrete" if integrated is False else "topology unknown")
        presence = (
            "present now" if item.get("presence_verified") is True
            else "OS configuration entry; presence unverified"
        )
        lines.append(
            f"  accelerator {index}: {item.get('vendor', 'unknown')} {item.get('name', 'device')} ({role}{memory_text}; {presence}, runtime not probed)"
        )

    lines.append("")
    lines.append("Recommendation")
    lines.append("--------------")
    lines.append(f"  workload    : {rec.get('workload', 'general')}")
    lines.append(f"  model band  : {rec.get('model_band', '?')}")
    if rec.get("decode_band") and rec.get("decode_band") != rec.get("model_band"):
        lines.append(
            f"  decode band : {rec.get('decode_band')} "
            f"({rec.get('active_params_b'):g}B active of "
            f"{rec.get('total_params_b'):g}B)"
        )
    lines.append(f"  num_ctx     : {rec.get('num_ctx', '?')}")
    lines.append(f"  keep_alive  : {rec.get('keep_alive', '?')}")
    lines.append(f"  execution   : {rec.get('execution_mode', '?')}")
    lines.append(f"  resident fit: {rec.get('resident_model_class', '?')}")
    lines.append(f"  hybrid fit  : {rec.get('hybrid_model_class', '?')} (capacity estimate)")
    runtime = rec.get("runtime_options") or {}
    lines.append(
        "  runtime hint: threads=%s, gpu_layers=%s, batch=%s (benchmark before pinning)"
        % (runtime.get("num_thread", "?"), runtime.get("num_gpu", "auto"), runtime.get("num_batch", "?"))
    )
    lines.append(
        f"  speculation : {'likely engages' if rec.get('speculation_likely') else 'dormant'}"
    )
    lines.append("")
    lines.append("Why")
    lines.append("---")
    for note in rec.get("rationale", []):
        lines.append(f"  - {note}")
    for note in rec.get("optimization_notes", []):
        lines.append(f"  - {note}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    import sys

    _workload = sys.argv[1] if len(sys.argv) > 1 else "general"
    _model = sys.argv[2] if len(sys.argv) > 2 else ""
    _hw = detect_profile()
    _rec = recommend(_hw, workload=_workload, model=_model)
    print(render(_hw, _rec))

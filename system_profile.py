"""File-backed standing instructions and shared host hardware profile.

The profile is intentionally plain Markdown so a user can edit it while the
server/proxy/REPL is running. server._build_system reads it on each request, so
changes become active without a restart.
"""
import json
import os
import platform
import re
import subprocess
from dataclasses import asdict, dataclass


DEFAULT_TEXT = """# Trilobite standing instructions

- Be direct, concrete, and honest about local-model limits.
- Prefer working code and verifiable steps.
- Use local privacy as a strength: keep sensitive context on this machine.
- Act as a local implementer whose work is audited: make useful drafts and
  changes, but never invent repository evidence or claim unrun validation.
- For concrete workspace tasks, use guarded tools instead of prose-only shell
  instructions. Start unfamiliar repositories with `workspace_inventory`,
  narrow searches and reads, keep a visible checklist, and respect every scan
  budget and truncation reason.
- Validate persistent changes against their exact on-disk paths. Finish with
  changed paths, checks, honest failures, exact actions, and checklist state.
- Resolve ordinary greenfield design choices yourself when the user delegates
  them; do not turn normal implementation decisions into a questionnaire.
- Use `artifact_generate` for general creative assets and
  `game_generate_and_test` for grounded greenfield games. Verify generated
  packs/projects before calling them ready. Ground other writing, data, docs,
  UI, image, audio, model, and bundle paths with `artifact_ground`; matching
  hashes do not replace format-specific validity checks.
- Use bounded hardware-aware fan-out. Large fleets are explicit opt-in; queue
  diversity separately from RAM/CPU-limited worker slots, honor cooperative
  cancellation, persist cross-process state, never auto-replay interrupted work,
  and serialize compile-heavy jobs under memory pressure.
- Use `/autopilot run` for an explicitly requested persistent goal. Decompose,
  execute, review, and replan within the host's local-tier, tool, root, task,
  failure, and cycle limits. Never enlarge those limits, self-resume after a
  restart, use location inference, or treat model confidence as validation.
- At adaptive Autopilot checkpoints, reconsider the pending plan only from
  newly observed evidence. Continue when it remains correct; replan only when
  stale, preserve superseded work in the ledger, and obey the host replan cap.
- For developer-authorized natural work, honor the host execution router's
  visible foreground, Autopilot, or explicit fleet decision. Ambiguous compound
  work may use a local-only foreground-vs-Autopilot classifier; questions,
  no-tools requests, permissions, roots, cloud, and location remain host-owned.
- Treat the shared local runtime policy as host-owned. Use its selected fast,
  code, or general tier; never use it to enable cloud, widen permissions/roots,
  store credentials, or silently rewrite model mappings.
- Respect atomic MCP refresh state. Newly published tools may appear after a
  request; on a failed refresh, disclose the error and use only the host's last
  known-good registry without attempting a bypass.
- Ground self-improvement claims in learning-health metrics. Keep interaction-
  grounded and seeded lessons distinct, and do not substitute raw totals for
  outcome coverage, positive-signal rate, or memory-hygiene evidence.
- Negative repository claims require exact-anchor evidence. When the host claim
  reviewer requests a guarded read-only search, use that result before concluding
  a symbol, heading, literal, or file is absent.
- Show only redacted memory privacy findings. Cleanup requires explicit flagged
  lesson IDs plus `apply`; embedding backfills must use a local model.
"""


def workspace_root():
    return os.path.abspath(os.path.dirname(__file__))


def default_path():
    return os.environ.get(
        "TRILOBITE_SYSTEM_PROFILE",
        os.path.join(workspace_root(), "system_profile.md"),
    )


def _resolve_path(path=None):
    path = path or default_path()
    if not os.path.isabs(path):
        path = os.path.join(workspace_root(), path)
    path = os.path.abspath(path)
    root = workspace_root()
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("profile path must stay inside workspace: %r" % path)
    return path


def read_profile(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def ensure_profile(path=None):
    path = _resolve_path(path)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_TEXT)
    return read_profile(path), path


def write_profile(text, path=None):
    path = _resolve_path(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write((text or "").rstrip() + "\n")
    return path


def append_profile(text, path=None):
    current = read_profile(path)
    addition = (text or "").strip()
    if not addition:
        raise ValueError("profile text is empty")
    combined = "%s\n\n%s" % (current, addition) if current else addition
    return write_profile(combined, path)


def system_prompt():
    path = default_path()
    try:
        text = read_profile(path)
        if not os.path.exists(_resolve_path(path)):
            text, _ = ensure_profile(path)
    except (OSError, ValueError):
        # A read-only install should still be usable; diagnostics reports the
        # path problem and the built-in server prompt remains in effect.
        return ""
    if not text:
        return ""
    return "Standing instructions from system_profile.md:\n%s" % text


def _env_float(name, default=None):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        return default


def _env_bool(name, default=False):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class HardwareProfile:
    """Live host capacity. RAM and VRAM intentionally remain independent."""

    os_name: str
    architecture: str
    system_ram_total_gb: float
    system_ram_available_gb: float
    gpu_vendor: str = "none"
    gpu_name: str = ""
    cuda_available: bool = False
    rocm_available: bool = False
    vram_total_gb: float = 0.0
    vram_free_gb: float = 0.0
    compute_capability: str = ""
    cpu_offload_supported: bool = False
    availability_live: bool = True

    def to_dict(self):
        return asdict(self)


def _system_memory():
    total = available = 0.0
    try:
        import psutil  # optional; already present in many Trilobite installs
        vm = psutil.virtual_memory()
        return vm.total / 1024**3, vm.available / 1024**3, True
    except ImportError:
        pass
    if os.path.exists("/proc/meminfo"):
        values = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as stream:
                for line in stream:
                    key, _, value = line.partition(":")
                    if key in {"MemTotal", "MemAvailable", "MemFree"}:
                        values[key] = int(value.split()[0]) / 1024**2
            total = values.get("MemTotal", 0.0)
            available = values.get("MemAvailable", values.get("MemFree", 0.0))
            return total, available, bool(total and available)
        except (OSError, ValueError):
            pass
    if os.name == "nt":
        script = (
            "$o=Get-CimInstance Win32_OperatingSystem;"
            "@{total=[double]$o.TotalVisibleMemorySize;free=[double]$o.FreePhysicalMemory}"
            "|ConvertTo-Json -Compress"
        )
        try:
            raw = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", script], text=True, timeout=10
            )
            data = json.loads(raw)
            return data["total"] / 1024**2, data["free"] / 1024**2, True
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            pass
    if platform.system() == "Darwin":
        try:
            total = int(subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, timeout=10
            ).strip()) / 1024**3
            # vm_stat free+inactive+speculative pages are safely reclaimable.
            raw = subprocess.check_output(["vm_stat"], text=True, timeout=10)
            page = int(re.search(r"page size of (\d+)", raw).group(1))
            counts = [int(x.replace(".", "")) for x in re.findall(
                r"Pages (?:free|inactive|speculative):\s+(\d+\.)", raw
            )]
            return total, sum(counts) * page / 1024**3, bool(counts)
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            pass
    return total, available, False


def _nvidia_profile():
    query = "name,memory.total,memory.free,compute_cap"
    try:
        raw = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # Older nvidia-smi builds do not expose compute_cap in query mode.
        try:
            raw = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                text=True,
                timeout=10,
            )
            raw = "\n".join(f"{row}, " for row in raw.splitlines())
        except (OSError, subprocess.SubprocessError):
            return None
    rows = [row for row in raw.splitlines() if row.strip()]
    if not rows:
        return None
    parsed = []
    for row in rows:
        fields = [part.strip() for part in row.split(",")]
        if len(fields) < 4:
            continue
        try:
            parsed.append((fields[0], float(fields[1]), float(fields[2]), fields[3]))
        except ValueError:
            continue
    if not parsed:
        return None
    # Training is single-GPU. Recommend against the GPU with the most free VRAM.
    name, total_mib, free_mib, capability = max(parsed, key=lambda item: item[2])
    return name, total_mib / 1024, free_mib / 1024, capability


def _rocm_profile():
    try:
        raw = subprocess.check_output(
            ["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"],
            text=True,
            timeout=10,
        )
        data = json.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    cards = []
    for values in data.values():
        if not isinstance(values, dict):
            continue
        name = str(values.get("Card series") or values.get("Card model") or "AMD GPU")
        numbers = {}
        for key, value in values.items():
            match = re.search(r"(\d+)", str(value).replace(",", ""))
            if match and "VRAM" in key.upper():
                numbers[key.lower()] = int(match.group(1)) / 1024**3
        total = next((v for k, v in numbers.items() if "total" in k), 0.0)
        used = next((v for k, v in numbers.items() if "used" in k), 0.0)
        if total:
            cards.append((name, total, max(0.0, total - used)))
    return max(cards, key=lambda item: item[2]) if cards else None


def detect_hardware() -> HardwareProfile:
    """Detect live capacity with environment overrides for testing/admin use."""
    total, available, live = _system_memory()
    total = _env_float("TRILOBITE_RAM_GB", total) or 0.0
    available_override = _env_float("TRILOBITE_AVAILABLE_RAM_GB")
    if available_override is not None:
        available, live = min(total or available_override, available_override), True
    elif not available and total:
        available, live = total * 0.75, False

    nvidia = _nvidia_profile()
    rocm = None if nvidia else _rocm_profile()
    vendor = "nvidia" if nvidia else "amd" if rocm else "none"
    name, vram_total, vram_free, capability = (
        (*nvidia, ) if nvidia else (*rocm, "") if rocm else ("", 0.0, 0.0, "")
    )
    vendor = os.environ.get("TRILOBITE_GPU_VENDOR", vendor).strip().lower() or "none"
    vram_total = _env_float("TRILOBITE_VRAM_GB", vram_total) or 0.0
    free_override = _env_float("TRILOBITE_FREE_VRAM_GB")
    vram_free = min(vram_total, free_override) if free_override is not None else vram_free
    if vram_total and not vram_free:
        vram_free, live = vram_total * 0.75, False
    cuda = _env_bool("TRILOBITE_CUDA_AVAILABLE", bool(nvidia))
    rocm_available = _env_bool("TRILOBITE_ROCM_AVAILABLE", bool(rocm))
    os_name = platform.system() or os.name
    offload = bool((cuda or rocm_available) and os_name in {"Linux", "Windows"})
    return HardwareProfile(
        os_name=os_name,
        architecture=platform.machine(),
        system_ram_total_gb=round(total, 2),
        system_ram_available_gb=round(min(total or available, available), 2),
        gpu_vendor=vendor,
        gpu_name=os.environ.get("TRILOBITE_GPU_NAME", name).strip(),
        cuda_available=cuda,
        rocm_available=rocm_available,
        vram_total_gb=round(vram_total, 2),
        vram_free_gb=round(min(vram_total or vram_free, vram_free), 2),
        compute_capability=os.environ.get("TRILOBITE_COMPUTE_CAPABILITY", capability).strip(),
        cpu_offload_supported=offload,
        availability_live=live,
    )

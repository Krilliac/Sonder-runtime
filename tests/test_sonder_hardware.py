"""Offline tests for hardware detection and the model-sizing recommender.

Every case feeds synthetic probes or synthetic hardware dicts, so no test
touches real CPU/RAM/GPU state and none spawns ``nvidia-smi``.
"""
import sonder_hardware


# --- probe injection ----------------------------------------------------------

def test_detect_hardware_uses_injected_probes():
    hw = sonder_hardware.detect_hardware(
        probes={
            "cpu_count": lambda: 8,
            "total_ram_gb": lambda: 16.0,
            "gpu": lambda: (True, 8.0),
            "platform": lambda: "Linux",
        }
    )
    assert hw == {
        "cpu_count": 8,
        "total_ram_gb": 16.0,
        "gpu_present": True,
        "vram_gb": 8.0,
        "platform": "Linux",
    }


def test_detect_hardware_never_spawns_subprocess_when_probes_injected(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("nvidia-smi must not be spawned in tests")

    monkeypatch.setattr(sonder_hardware.subprocess, "run", _boom)
    # os.sysconf does not exist on Windows; raising=False keeps the
    # never-spawn guard meaningful on POSIX without inverting the test
    # into an AttributeError on hosts that lack the attribute.
    monkeypatch.setattr(sonder_hardware.os, "sysconf", _boom, raising=False)

    hw = sonder_hardware.detect_hardware(
        probes={
            "cpu_count": lambda: 4,
            "total_ram_gb": lambda: 8.0,
            "gpu": lambda: (False, None),
            "platform": lambda: "Darwin",
        }
    )
    assert hw["gpu_present"] is False
    assert hw["vram_gb"] is None
    assert hw["total_ram_gb"] == 8.0


def test_detect_hardware_guards_raising_probe():
    def _raise():
        raise RuntimeError("driver hung")

    hw = sonder_hardware.detect_hardware(
        probes={
            "cpu_count": lambda: 2,
            "total_ram_gb": lambda: 4.0,
            "gpu": _raise,
            "platform": lambda: "Linux",
        }
    )
    # A raising GPU probe degrades to "no GPU", not an exception.
    assert hw["gpu_present"] is False
    assert hw["vram_gb"] is None
    assert hw["cpu_count"] == 2


def test_detect_hardware_partial_injection_and_bad_gpu_shape():
    # Only override the two probes that would touch hardware; a GPU probe that
    # returns a malformed value degrades to "no GPU" instead of raising.
    hw = sonder_hardware.detect_hardware(
        probes={
            "total_ram_gb": lambda: 32.0,
            "gpu": lambda: "not-a-tuple",
            "cpu_count": lambda: 12,
            "platform": lambda: "Linux",
        }
    )
    assert set(hw) == {
        "cpu_count",
        "total_ram_gb",
        "gpu_present",
        "vram_gb",
        "platform",
    }
    assert hw["gpu_present"] is False
    assert hw["vram_gb"] is None
    assert hw["total_ram_gb"] == 32.0


# --- recommend: across the hardware spectrum ----------------------------------

def _hw(cpu=4, ram=None, gpu=False, vram=None, plat="Linux"):
    return {
        "cpu_count": cpu,
        "total_ram_gb": ram,
        "gpu_present": gpu,
        "vram_gb": vram if gpu else None,
        "platform": plat,
    }


def test_recommend_tiny_laptop():
    rec = sonder_hardware.recommend(_hw(cpu=4, ram=8.0))
    assert rec["model_band"] == "3-4B"
    assert rec["num_ctx"] == 4096
    assert rec["keep_alive"] == "5m"
    assert rec["speculation_likely"] is False
    assert rec["basis"] == "ram"


def test_recommend_7b_capable_desktop():
    rec = sonder_hardware.recommend(_hw(cpu=8, ram=16.0))
    assert rec["model_band"] == "7B"
    assert rec["num_ctx"] == 8192
    assert rec["keep_alive"] == "5m"
    assert rec["speculation_likely"] is False


def test_recommend_24gb_gpu():
    rec = sonder_hardware.recommend(_hw(cpu=16, ram=64.0, gpu=True, vram=24.0))
    assert rec["basis"] == "vram"
    assert rec["model_band"] == "13-34B"
    assert rec["num_ctx"] == 16384
    # 24 GB VRAM is below the resident threshold but it's a GPU box.
    assert rec["keep_alive"] == "30m"


def test_recommend_big_server():
    rec = sonder_hardware.recommend(_hw(cpu=64, ram=256.0))
    assert rec["basis"] == "ram"
    assert rec["model_band"] == "70B+"
    assert rec["num_ctx"] == 32768
    # 256 GB is dedicated-class memory -> pin the model resident.
    assert rec["keep_alive"] == "-1"


def test_recommend_gpu_vram_beats_ram_for_band():
    # Modest RAM but a big card: the card decides the band.
    rec = sonder_hardware.recommend(_hw(cpu=8, ram=16.0, gpu=True, vram=48.0))
    assert rec["basis"] == "vram"
    assert rec["model_band"] == "70B+"
    assert rec["keep_alive"] == "-1"  # 48 GB hits the resident threshold


# --- recommend: speculation engagement logic ----------------------------------

def test_speculation_engages_big_model_slow_tools():
    rec = sonder_hardware.recommend(
        _hw(cpu=64, ram=256.0), workload="coding"
    )
    assert rec["model_band"] == "70B+"
    assert rec["speculation_likely"] is True
    assert any("Speculation likely engages" in line for line in rec["rationale"])


def test_speculation_dormant_on_fast_laptop_even_when_tools_slow():
    rec = sonder_hardware.recommend(_hw(cpu=8, ram=16.0), workload="agentic")
    # Small model -> sub-second decisions -> nothing to hide.
    assert rec["model_band"] == "7B"
    assert rec["speculation_likely"] is False


def test_speculation_dormant_for_big_model_but_general_workload():
    rec = sonder_hardware.recommend(
        _hw(cpu=16, ram=64.0, gpu=True, vram=24.0), workload="general"
    )
    assert rec["model_band"] == "13-34B"
    # Big model, but general-workload tools are too fast to overlap much.
    assert rec["speculation_likely"] is False
    assert any("borderline" in line for line in rec["rationale"])


def test_chat_workload_narrows_context_and_stays_dormant():
    rec = sonder_hardware.recommend(
        _hw(cpu=64, ram=256.0), workload="chat"
    )
    assert rec["num_ctx"] == 16384  # 32768 // 2
    assert rec["speculation_likely"] is False
    assert any("issues no tool calls" in line for line in rec["rationale"])


def test_unknown_workload_falls_back_to_general():
    rec = sonder_hardware.recommend(_hw(cpu=4, ram=8.0), workload="nonsense")
    assert rec["workload"] == "general"


def test_unknown_hardware_lands_on_smallest_band():
    rec = sonder_hardware.recommend(_hw(cpu=None, ram=None))
    assert rec["capacity_gb"] == 0.0
    assert rec["model_band"] == "3-4B"
    assert rec["speculation_likely"] is False


# --- render -------------------------------------------------------------------

def test_render_contains_key_fields():
    hw = _hw(cpu=16, ram=64.0, gpu=True, vram=24.0)
    rec = sonder_hardware.recommend(hw, workload="coding")
    text = sonder_hardware.render(hw, rec)
    assert "Sonder hardware profile" in text
    assert "13-34B" in text
    assert "24 GB VRAM" in text
    assert "coding" in text
    # Every rationale line is surfaced.
    for note in rec["rationale"]:
        assert note in text


def test_render_handles_unknown_fields():
    hw = _hw(cpu=None, ram=None)
    rec = sonder_hardware.recommend(hw)
    text = sonder_hardware.render(hw, rec)
    assert "cpu cores  : unknown" in text
    assert "system ram : unknown" in text
    assert "gpu        : none detected" in text


# --- cold discrete GPU ---------------------------------------------------------

def test_probe_gpu_retries_a_cold_switchable_gpu(monkeypatch):
    """A laptop's discrete GPU idles powered down, so the first nvidia-smi after
    an idle stretch blocks while the driver wakes it and the next one returns at
    once. The old 2s single-shot timed out exactly then and reported "no GPU",
    which sized the band for CPU inference on a working CUDA box."""
    calls = []

    class _Result:
        returncode = 0
        stdout = "6141\n"

    def _fake_run(cmd, **kwargs):
        calls.append(kwargs.get("timeout"))
        if len(calls) == 1:
            raise sonder_hardware.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return _Result()

    monkeypatch.setattr(sonder_hardware.subprocess, "run", _fake_run)
    assert sonder_hardware._probe_gpu() == (True, 6.0)
    assert len(calls) == 2, "a timed-out cold probe must be retried once"
    assert all(t and t > 2.0 for t in calls), "2s is shorter than a cold wake"


def test_probe_gpu_gives_up_when_every_attempt_times_out(monkeypatch):
    """A genuinely hung driver must still resolve to "no GPU" rather than hang
    the caller or raise through it."""
    calls = []

    def _always_timeout(cmd, **kwargs):
        calls.append(1)
        raise sonder_hardware.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(sonder_hardware.subprocess, "run", _always_timeout)
    assert sonder_hardware._probe_gpu() == (False, None)
    assert len(calls) == 2, "bounded retries: must not loop forever"


def test_probe_gpu_missing_binary_is_not_retried(monkeypatch):
    """No nvidia-smi at all is a settled answer, not a cold GPU - one look."""
    calls = []

    def _missing(cmd, **kwargs):
        calls.append(1)
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(sonder_hardware.subprocess, "run", _missing)
    assert sonder_hardware._probe_gpu() == (False, None)
    assert len(calls) == 1, "a missing binary must not be retried"

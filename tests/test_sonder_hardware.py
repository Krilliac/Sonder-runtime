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


def test_detect_profile_adds_inventory_without_changing_legacy_shape(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("host subprocess must not run with injected accelerators")

    monkeypatch.setattr(sonder_hardware.subprocess, "run", _boom)
    probes = {
        "cpu_count": lambda: 24,
        "total_ram_gb": lambda: 32.0,
        "platform": lambda: "Windows",
        "accelerators": lambda: [sonder_hardware._accelerator(
            name="Radeon 780M", vendor="AMD", memory_gb=0.5,
            memory_kind="reported adapter memory", integrated=True,
            probe="fixture",
        ), sonder_hardware._accelerator(
            name="GeForce RTX", vendor="NVIDIA", memory_gb=16.0,
            memory_kind="dedicated VRAM", probe="fixture",
        )],
    }
    profile = sonder_hardware.detect_profile(probes)
    legacy = sonder_hardware.detect_hardware(probes)
    assert profile["accelerator_count"] == 2
    assert profile["vram_gb"] == 16.0
    assert profile["runtime_readiness"] == "not-probed"
    assert set(legacy) == {
        "cpu_count", "total_ram_gb", "gpu_present", "vram_gb", "platform",
    }


def test_windows_registry_probe_reads_qword_and_skips_denied_entry():
    class FakeRegistry:
        HKEY_LOCAL_MACHINE = object()
        rows = {
            "0000": [
                ("DriverDesc", "AMD Radeon 780M", 1),
                ("ProviderName", "Advanced Micro Devices, Inc.", 1),
                ("HardwareInformation.qwMemorySize", 512 * 1024 ** 2, 11),
            ],
            "0001": [
                ("DriverDesc", "Intel Arc B580", 1),
                ("MatchingDeviceId", "PCI\\VEN_8086&DEV_E20B", 1),
                ("HardwareInformation.qwMemorySize", 12 * 1024 ** 3, 11),
            ],
        }

        @staticmethod
        def OpenKey(parent, child):
            if parent is FakeRegistry.HKEY_LOCAL_MACHINE:
                return "root"
            if child == "0002":
                raise OSError("access denied")
            return child

        @staticmethod
        def QueryInfoKey(handle):
            if handle == "root":
                return (4, 0, 0)
            if handle == "0003":
                raise OSError("corrupt registry child")
            return (0, len(FakeRegistry.rows[handle]), 0)

        @staticmethod
        def EnumKey(_handle, index):
            return ("0000", "0001", "0002", "0003")[index]

        @staticmethod
        def EnumValue(handle, index):
            return FakeRegistry.rows[handle][index]

        @staticmethod
        def CloseKey(_handle):
            pass

    rows = sonder_hardware._probe_windows_accelerators(FakeRegistry)
    assert [(row["vendor"], row["memory_gb"], row["integrated"]) for row in rows] == [
        ("AMD", 0.5, True),
        ("Intel", 12.0, False),
    ]
    assert all(row["runtime_ready"] is None for row in rows)
    assert all(row["presence_verified"] is None for row in rows)


def test_linux_sysfs_probe_is_bounded_and_handles_missing_vram(tmp_path):
    drm = tmp_path / "drm"
    amd = drm / "card0" / "device"
    intel = drm / "card1" / "device"
    amd.mkdir(parents=True)
    intel.mkdir(parents=True)
    (amd / "vendor").write_text("0x1002\n", encoding="utf-8")
    (amd / "mem_info_vram_total").write_text(str(8 * 1024 ** 3), encoding="utf-8")
    (intel / "vendor").write_text("0x8086\n", encoding="utf-8")
    rows = sonder_hardware._probe_linux_accelerators(
        drm, nvidia_probe=lambda: [],
    )
    assert [(row["vendor"], row["memory_gb"]) for row in rows] == [
        ("AMD", 8.0), ("Intel", None),
    ]
    assert [row["integrated"] for row in rows] == [None, None]


def test_linux_nvidia_smi_supplements_generic_sysfs(tmp_path):
    drm = tmp_path / "drm"
    device = drm / "card0" / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text("0x10de", encoding="utf-8")
    nvidia = sonder_hardware._accelerator(
        name="NVIDIA RTX 5070 Ti", vendor="NVIDIA", memory_gb=16.0,
        memory_kind="dedicated VRAM", probe="fixture",
    )
    rows = sonder_hardware._probe_linux_accelerators(
        drm, nvidia_probe=lambda: [nvidia],
    )
    assert rows == [nvidia]


def test_macos_apple_gpu_uses_unified_memory_without_fake_vram():
    class Result:
        returncode = 0
        stdout = '{"SPDisplaysDataType":[{"sppci_model":"Apple M4 GPU","spdisplays_vendor":"Apple","spdisplays_vram_shared":"Apple M4"}]}'

    rows = sonder_hardware._probe_macos_accelerators(runner=lambda *a, **k: Result())
    assert rows[0]["memory_kind"] == "unified system memory"
    assert rows[0]["memory_gb"] is None
    profile = {
        "cpu_count": 12, "total_ram_gb": 32.0, "gpu_present": True,
        "vram_gb": None, "platform": "Darwin", "accelerators": rows,
    }
    rec = sonder_hardware.recommend(profile)
    assert rec["execution_mode"] == "unified-memory"
    assert rec["resident_usable_gb"] == 22.4
    assert rec["hybrid_usable_gb"] == 22.4


def test_hybrid_plan_uses_largest_gpu_not_mixed_vendor_sum():
    accelerators = [
        sonder_hardware._accelerator(
            name="GeForce", vendor="NVIDIA", memory_gb=16.0,
            memory_kind="dedicated VRAM", probe="fixture",
        ),
        sonder_hardware._accelerator(
            name="Arc", vendor="Intel", memory_gb=12.0,
            memory_kind="dedicated VRAM", probe="fixture",
        ),
    ]
    hw = _hw(cpu=24, ram=32.0, gpu=True, vram=16.0)
    hw["accelerators"] = accelerators
    rec = sonder_hardware.recommend(hw, workload="coding")
    assert rec["resident_usable_gb"] == 13.6
    assert rec["hybrid_usable_gb"] == 37.6
    assert rec["resident_model_class"] == "14B"
    assert rec["hybrid_model_class"] == "32B"
    assert rec["primary_accelerator"]["vendor"] == "NVIDIA"
    assert rec["auxiliary_accelerators"][0]["vendor"] == "Intel"


def test_integrated_only_adapter_sizes_from_system_ram():
    integrated = sonder_hardware._accelerator(
        name="AMD Radeon 780M", vendor="AMD", memory_gb=0.5,
        memory_kind="reported adapter memory", integrated=True, probe="fixture",
    )
    hw = _hw(cpu=16, ram=32.0, gpu=True, vram=0.5)
    hw["accelerators"] = [integrated]
    rec = sonder_hardware.recommend(hw)
    assert rec["basis"] == "ram"
    assert rec["model_band"] == "13-34B"
    assert rec["execution_mode"] == "cpu"


def test_topology_unknown_adapter_does_not_override_system_ram():
    unknown = sonder_hardware._accelerator(
        name="AMD display adapter", vendor="AMD", memory_gb=0.5,
        memory_kind="dedicated VRAM", integrated=None, probe="linux-drm-sysfs",
    )
    hw = _hw(cpu=16, ram=32.0, gpu=True, vram=0.5)
    hw["accelerators"] = [unknown]
    rec = sonder_hardware.recommend(hw)
    assert rec["basis"] == "ram"
    assert rec["execution_mode"] == "cpu"


def test_six_gb_gpu_does_not_overclaim_resident_7b_with_context():
    gpu = sonder_hardware._accelerator(
        name="GeForce RTX", vendor="NVIDIA", memory_gb=6.0,
        memory_kind="dedicated VRAM", integrated=False, probe="fixture",
    )
    hw = _hw(cpu=16, ram=16.0, gpu=True, vram=6.0)
    hw["accelerators"] = [gpu]
    rec = sonder_hardware.recommend(hw, workload="coding")
    assert rec["model_band"] == "3-4B"
    assert rec["resident_model_class"] == "3-4B"
    assert rec["hybrid_model_class"] == "14B"


def test_non_string_workload_falls_back_to_general():
    rec = sonder_hardware.recommend(_hw(cpu=8, ram=16.0), workload=["coding"])
    assert rec["workload"] == "general"


def test_nvidia_inventory_parser_handles_name_and_memory():
    class Result:
        returncode = 0
        stdout = "0, GPU-abc, NVIDIA GeForce RTX 5070 Ti, 16303\nmalformed\n"

    rows = sonder_hardware._probe_nvidia_accelerators(
        runner=lambda *args, **kwargs: Result(),
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "NVIDIA GeForce RTX 5070 Ti"
    assert rows[0]["memory_gb"] == 15.9
    assert rows[0]["device_id"] == "GPU-abc"


def test_nvidia_inventory_retries_a_cold_gpu():
    calls = []

    class Result:
        returncode = 0
        stdout = "0, GPU-cold, NVIDIA RTX, 6141\n"

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise sonder_hardware.subprocess.TimeoutExpired(
                command, kwargs.get("timeout"),
            )
        return Result()

    rows = sonder_hardware._probe_nvidia_accelerators(runner=runner)
    assert len(calls) == 2
    assert rows[0]["device_id"] == "GPU-cold"


def test_nvidia_inventory_keeps_identical_physical_gpus():
    class Result:
        returncode = 0
        stdout = (
            "0, GPU-one, NVIDIA RTX 5070 Ti, 16303\n"
            "1, GPU-two, NVIDIA RTX 5070 Ti, 16303\n"
        )

    rows = sonder_hardware._probe_nvidia_accelerators(
        runner=lambda *args, **kwargs: Result(),
    )
    assert [row["device_id"] for row in rows] == ["GPU-one", "GPU-two"]
    assert len(sonder_hardware._dedupe_accelerators(rows)) == 2


def test_profile_cache_refreshes_only_when_requested(monkeypatch):
    calls = []
    fixture = {
        "cpu_count": 8, "total_ram_gb": 16.0, "gpu_present": False,
        "vram_gb": None, "platform": "Linux", "accelerators": [],
        "accelerator_count": 0, "runtime_readiness": "not-probed",
    }

    def fake_detect():
        calls.append(1)
        return dict(fixture)

    monkeypatch.setattr(sonder_hardware, "_PROFILE_CACHE", None)
    monkeypatch.setattr(sonder_hardware, "detect_profile", fake_detect)
    assert sonder_hardware.get_profile(workload="chat")["recommendation"]["workload"] == "chat"
    sonder_hardware.get_profile(workload="coding")
    sonder_hardware.get_profile(refresh=True)
    assert len(calls) == 2


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
    assert "runtime hint:" in text
    # Every rationale line is surfaced.
    for note in rec["rationale"]:
        assert note in text


def test_render_handles_unknown_fields():
    hw = _hw(cpu=None, ram=None)
    rec = sonder_hardware.recommend(hw)
    text = sonder_hardware.render(hw, rec)
    assert "logical CPUs: unknown" in text
    assert "system ram : unknown" in text
    assert "gpu memory : not detected (Ollama may still accelerate)" in text


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


# --- mixture-of-experts: total params size memory, active params size latency --

def test_params_from_model_tag_reads_moe_and_dense_tags():
    assert sonder_hardware.params_from_model_tag("qwen3-coder:30b-a3b-q4_K_M") == (30.0, 3.0)
    assert sonder_hardware.params_from_model_tag("qwen3-coder:480b-a35b-q8_0") == (480.0, 35.0)
    assert sonder_hardware.params_from_model_tag("qwen3:235b-a22b") == (235.0, 22.0)
    # A dense tag reports the same count twice, which keeps every caller's
    # active-vs-total comparison meaningful without a special case.
    assert sonder_hardware.params_from_model_tag("qwen2.5-coder:14b") == (14.0, 14.0)
    assert sonder_hardware.params_from_model_tag("qwen2.5-coder:1.5b") == (1.5, 1.5)


def test_params_from_model_tag_returns_none_rather_than_guessing():
    # An alias carries no size; guessing one would silently change the advice.
    assert sonder_hardware.params_from_model_tag("sonder:latest") is None
    assert sonder_hardware.params_from_model_tag("nomic-embed-text") is None
    assert sonder_hardware.params_from_model_tag("") is None
    assert sonder_hardware.params_from_model_tag(None) is None
    # Active >= total is a malformed tag, not a very fast model.
    assert sonder_hardware.params_from_model_tag("bogus:8b-a9b") is None


def test_decode_band_reads_active_parameters():
    assert sonder_hardware.decode_band(3.3) == "3-4B"
    assert sonder_hardware.decode_band(7.0) == "7B"
    assert sonder_hardware.decode_band(22.0) == "13-34B"
    assert sonder_hardware.decode_band(70.0) == "70B+"
    # Unknown/nonsense leaves the caller on its hardware-derived band.
    assert sonder_hardware.decode_band(None) is None
    assert sonder_hardware.decode_band(0) is None
    assert sonder_hardware.decode_band("huge") is None


def test_moe_model_keeps_memory_band_but_decodes_like_its_active_count():
    hw = _hw(cpu=24, ram=32.0, gpu=True, vram=16.0)
    rec = sonder_hardware.recommend(
        hw, workload="coding", model="qwen3-coder:30b-a3b-q4_K_M"
    )
    # All experts stay resident, so the memory envelope is unchanged...
    assert rec["model_band"] == "13-34B"
    # ...but only ~3B is active per token, so decisions are sub-second.
    assert rec["decode_band"] == "3-4B"
    assert rec["total_params_b"] == 30.0
    assert rec["active_params_b"] == 3.0
    assert rec["speculation_likely"] is False
    assert any("is MoE" in line for line in rec["rationale"])


def test_dense_model_of_the_same_footprint_still_engages_speculation():
    # The control for the case above: same host, same memory band, dense model.
    hw = _hw(cpu=24, ram=32.0, gpu=True, vram=16.0)
    rec = sonder_hardware.recommend(hw, workload="coding", model="qwen2.5-coder:14b")
    assert rec["model_band"] == "13-34B"
    assert rec["decode_band"] == "13-34B"
    assert rec["speculation_likely"] is True
    assert not any("is MoE" in line for line in rec["rationale"])


def test_recommend_without_a_model_is_unchanged():
    hw = _hw(cpu=24, ram=32.0, gpu=True, vram=16.0)
    bare = sonder_hardware.recommend(hw, workload="coding")
    named = sonder_hardware.recommend(hw, workload="coding", model="sonder:latest")
    # An unparseable tag must not perturb any pre-existing field.
    for key in ("model_band", "num_ctx", "keep_alive", "speculation_likely", "rationale"):
        assert bare[key] == named[key]
    # With no size to read, decode band falls back to the hardware band.
    assert bare["decode_band"] == bare["model_band"]
    assert bare["active_params_b"] is None


def test_huge_moe_still_decodes_slowly_enough_to_speculate():
    # Active count, not total, is the test: 35B active is genuinely slow.
    rec = sonder_hardware.recommend(
        _hw(cpu=64, ram=256.0), workload="coding", model="qwen3-coder:480b-a35b-q8_0"
    )
    assert rec["decode_band"] == "13-34B"
    assert rec["speculation_likely"] is True


def test_render_shows_decode_band_only_when_it_differs():
    hw = _hw(cpu=24, ram=32.0, gpu=True, vram=16.0)
    moe = sonder_hardware.render(
        hw, sonder_hardware.recommend(hw, model="qwen3-coder:30b-a3b-q4_K_M")
    )
    assert "decode band" in moe
    assert "3B active of 30B" in moe
    dense = sonder_hardware.render(
        hw, sonder_hardware.recommend(hw, model="qwen2.5-coder:14b")
    )
    assert "decode band" not in dense


# --- review follow-up: total params must be computed with, not just printed ---

def test_size_is_read_from_the_tag_not_the_repository_name():
    """A name that merely contains a size token is not size metadata.

    ``custom-70b-model:latest`` used to parse as 70B and change the decode and
    speculation advice, even though ``:latest`` says nothing about size. No tag
    means no size, which falls back to the hardware band.
    """
    assert sonder_hardware.params_from_model_tag("custom-70b-model:latest") is None
    assert sonder_hardware.params_from_model_tag("my-30b-a3b-fork:latest") is None
    # A size in the tag is still read normally.
    assert sonder_hardware.params_from_model_tag("custom-70b-model:14b") == (14.0, 14.0)
    assert sonder_hardware.params_from_model_tag("qwen3-coder:30b-a3b-q4_K_M") == (30.0, 3.0)
    # A bare name carries no tag at all.
    assert sonder_hardware.params_from_model_tag("nomic-embed-text") is None
    # A registry path keeps its last colon as the tag separator.
    assert sonder_hardware.params_from_model_tag(
        "registry.ollama.ai/library/qwen3-coder:30b-a3b") == (30.0, 3.0)
    # Size in the repo name but an unrelated tag: absent beats wrong.
    assert sonder_hardware.params_from_model_tag(
        "hf.co/org/Qwen3-27B-GGUF:q4_k_m") is None


def test_memory_band_and_fit_are_host_independent():
    assert sonder_hardware.memory_band(30.0) == "13-34B"
    assert sonder_hardware.memory_band(3.3) == "3-4B"
    assert sonder_hardware.memory_band(70.0) == "70B+"
    assert sonder_hardware.memory_band(None) is None
    assert sonder_hardware.memory_band(0) is None
    assert sonder_hardware.band_fits("13-34B", "70B+") is True
    assert sonder_hardware.band_fits("13-34B", "13-34B") is True
    assert sonder_hardware.band_fits("13-34B", "7B") is False
    assert sonder_hardware.band_fits("13-34B", "nonsense") is None


def test_moe_memory_class_does_not_follow_the_host():
    """The model's footprint is a property of the model, not of the card.

    The hardware band reports the largest model that would *fit*, so read alone
    it agrees with whatever it is pointed at -- the same 30B model would be
    described as a 7B envelope on a small card and a 70B+ one on a large card,
    which is exactly what hides a model that cannot fit.
    """
    tag = "qwen3-coder:30b-a3b-q4_K_M"
    small = sonder_hardware.recommend(
        _hw(cpu=16, ram=32.0, gpu=True, vram=8.0), workload="coding", model=tag)
    exact = sonder_hardware.recommend(
        _hw(cpu=16, ram=32.0, gpu=True, vram=16.0), workload="coding", model=tag)
    large = sonder_hardware.recommend(
        _hw(cpu=16, ram=32.0, gpu=True, vram=48.0), workload="coding", model=tag)

    # Model class is constant; only the host band and the fit verdict move.
    assert small["model_memory_band"] == "13-34B"
    assert exact["model_memory_band"] == "13-34B"
    assert large["model_memory_band"] == "13-34B"
    assert (small["model_band"], exact["model_band"], large["model_band"]) == (
        "7B", "13-34B", "70B+")
    assert small["fits_capacity"] is False
    assert exact["fits_capacity"] is True
    assert large["fits_capacity"] is True
    assert any("will spill past accelerator memory" in line for line in small["rationale"])
    assert any("so it fits" in line for line in exact["rationale"])
    # Decode speed is set by the active count, so it never moves with the host.
    for rec in (small, exact, large):
        assert rec["decode_band"] == "3-4B"
        assert rec["speculation_likely"] is False


def test_dense_model_too_large_for_the_host_is_called_out():
    rec = sonder_hardware.recommend(
        _hw(cpu=8, ram=16.0, gpu=True, vram=8.0), workload="coding",
        model="qwen2.5-coder:32b")
    assert rec["model_memory_band"] == "13-34B"
    assert rec["fits_capacity"] is False
    rendered = sonder_hardware.render(_hw(cpu=8, ram=16.0, gpu=True, vram=8.0), rec)
    assert "FIT WARNING" in rendered


def test_unsized_model_reports_no_fit_verdict_rather_than_a_guess():
    rec = sonder_hardware.recommend(
        _hw(cpu=16, ram=32.0, gpu=True, vram=16.0), workload="coding",
        model="sonder:latest")
    assert rec["model_memory_band"] is None
    assert rec["fits_capacity"] is None
    assert "FIT WARNING" not in sonder_hardware.render(
        _hw(cpu=16, ram=32.0, gpu=True, vram=16.0), rec)

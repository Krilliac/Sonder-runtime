from sonder_runtime.adapters.accelerators import backend_probe


def test_cuda_probe_is_conservative_when_runtime_is_missing(monkeypatch):
    monkeypatch.setattr(
        backend_probe.importlib.util,
        "find_spec",
        lambda name: None if name == "torch" else None,
    )
    backend_probe.probe_cuda_runtime.cache_clear()

    assert backend_probe.probe_cuda_runtime() is False


def test_hardware_profile_does_not_infer_cuda_from_nvidia_vendor(monkeypatch):
    import sonder_hardware

    probes = {
        "cpu_count": lambda: 8,
        "total_ram_gb": lambda: 32.0,
        "platform": lambda: "Windows",
        "accelerators": lambda: [sonder_hardware._accelerator(
            name="NVIDIA GPU", vendor="NVIDIA", memory_gb=16.0,
            memory_kind="dedicated VRAM", probe="fixture",
        )],
        "backends": lambda: {},
        "cuda": lambda: False,
    }

    profile = sonder_hardware.detect_profile(probes)

    assert profile["gpu_vendor"] == "NVIDIA"
    assert profile["cuda_available"] is False

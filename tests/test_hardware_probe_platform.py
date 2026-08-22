import sonder_hardware
from sonder_runtime.platform.hardware_probe import (
    parse_memory_gb,
    probe_cpu_count,
    probe_platform,
    probe_total_ram_gb,
    read_text,
)


def test_hardware_memory_parser_has_canonical_platform_owner():
    assert sonder_hardware._parse_memory_gb is parse_memory_gb


def test_hardware_probe_text_reader_has_canonical_platform_owner(tmp_path):
    target = tmp_path / "uevent"
    target.write_text(" DRIVER=amdgpu \n", encoding="utf-8")
    assert sonder_hardware._read_text is read_text
    assert read_text(target) == "DRIVER=amdgpu"


def test_hardware_probe_text_reader_swallows_unreadable_paths(tmp_path):
    missing = tmp_path / "missing"
    assert read_text(missing) == ""


def test_hardware_memory_parser_accepts_decimal_units_and_commas():
    assert parse_memory_gb("16 GB") == 16.0
    assert parse_memory_gb("512 MB") == 0.5
    assert parse_memory_gb("1,5 gb") == 1.5


def test_hardware_memory_parser_rejects_missing_or_unknown_units():
    assert parse_memory_gb(None) is None
    assert parse_memory_gb("16") is None
    assert parse_memory_gb("unknown") is None


def test_hardware_platform_probe_has_canonical_platform_owner(monkeypatch):
    import sonder_runtime.platform.hardware_probe as hardware_probe

    monkeypatch.setattr(hardware_probe.platform, "system", lambda: "Windows")
    assert sonder_hardware._probe_platform is probe_platform
    assert probe_platform() == "Windows"


def test_hardware_platform_probe_degrades_when_platform_lookup_fails(monkeypatch):
    import sonder_runtime.platform.hardware_probe as hardware_probe

    def fail():
        raise RuntimeError("platform lookup failed")

    monkeypatch.setattr(hardware_probe.platform, "system", fail)
    assert probe_platform() == "unknown"


def test_hardware_cpu_probe_has_canonical_platform_owner():
    assert sonder_hardware._probe_cpu_count is probe_cpu_count


def test_hardware_cpu_probe_returns_os_value(monkeypatch):
    import sonder_runtime.platform.hardware_probe as hardware_probe

    monkeypatch.setattr(hardware_probe.os, "cpu_count", lambda: 12)
    assert probe_cpu_count() == 12


def test_hardware_cpu_probe_degrades_when_os_lookup_fails(monkeypatch):
    import sonder_runtime.platform.hardware_probe as hardware_probe

    def fail():
        raise RuntimeError("probe unavailable")

    monkeypatch.setattr(hardware_probe.os, "cpu_count", fail)
    assert probe_cpu_count() is None


def test_hardware_ram_probe_has_canonical_platform_owner():
    assert sonder_hardware._probe_total_ram_gb is probe_total_ram_gb


def test_hardware_ram_probe_uses_posix_page_totals(monkeypatch):
    import sonder_runtime.platform.hardware_probe as hardware_probe

    values = {"SC_PHYS_PAGES": 2_000_000, "SC_PAGE_SIZE": 4096}
    monkeypatch.setattr(hardware_probe.os, "sysconf", values.__getitem__, raising=False)
    assert probe_total_ram_gb() == 8.2


def test_hardware_ram_probe_falls_back_when_posix_lookup_fails(monkeypatch):
    import sonder_runtime.platform.hardware_probe as hardware_probe

    def fail(_name):
        raise OSError("probe unavailable")

    monkeypatch.setattr(hardware_probe.os, "sysconf", fail, raising=False)
    result = probe_total_ram_gb()
    assert result is None or result > 0


def test_hardware_gpu_probe_has_canonical_platform_owner():
    from sonder_runtime.adapters.accelerators.gpu_probe import probe_nvidia_gpu

    assert sonder_hardware._probe_gpu is probe_nvidia_gpu


def test_hardware_gpu_probe_uses_largest_reported_vram(monkeypatch):
    import sonder_runtime.adapters.accelerators.gpu_probe as hardware_probe

    class Result:
        returncode = 0
        stdout = "8192\n24576\n"

    monkeypatch.setattr(hardware_probe.subprocess, "run", lambda *args, **kwargs: Result())
    assert hardware_probe.probe_nvidia_gpu() == (True, 24.0)


def test_hardware_gpu_probe_retries_a_cold_driver_timeout(monkeypatch):
    import sonder_runtime.adapters.accelerators.gpu_probe as hardware_probe

    class Result:
        returncode = 0
        stdout = "16384\n"

    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise hardware_probe.subprocess.TimeoutExpired("nvidia-smi", kwargs["timeout"])
        return Result()

    monkeypatch.setattr(hardware_probe.subprocess, "run", run)
    assert hardware_probe.probe_nvidia_gpu() == (True, 16.0)
    assert calls == [8.0, 8.0]


def test_hardware_gpu_probe_degrades_on_missing_driver(monkeypatch):
    import sonder_runtime.adapters.accelerators.gpu_probe as hardware_probe

    def fail(*args, **kwargs):
        raise OSError("missing")

    monkeypatch.setattr(hardware_probe.subprocess, "run", fail)
    assert hardware_probe.probe_nvidia_gpu() == (False, None)

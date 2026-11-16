"""``[gpu_admission]``: TOML, bounds, unknown keys, environment overrides, binding."""
from __future__ import annotations

import pytest

from sonder_runtime.platform import config as sonder_config
from sonder_runtime.platform import gpu_admission_config as section


def _load(tmp_path=None, body=None, env=None, **overrides):
    path = None
    if body is not None:
        path = tmp_path / "sonder.toml"
        path.write_text(body, encoding="utf-8")
    return sonder_config.load_config(path, env=env if env is not None else {}, overrides=overrides)


def test_defaults_match_the_design():
    config = _load().gpu_admission
    assert (config.mode, config.gpu_slots, config.embed_slots, config.probe_slots) == ("enforce", 1, 1, 2)
    assert (config.min_available_ram_mb, config.min_commit_headroom_mb) == (4096, 4096)
    assert (config.on_host_floor, config.ctx_conflict) == ("queue", "serve_pinned")
    assert config.store_path == ""
    assert config.runner_process_names == ("llama-server.exe", "llama-server", "ollama.exe", "ollama")


def test_toml_values_are_parsed(tmp_path):
    body = (
        '[gpu_admission]\nmode = "observe"\ngpu_slots = 2\nprobe_wait_ms = 500\n'
        'on_host_floor = "refuse"\nctx_conflict = "wait_unload"\n'
        'runner_process_names = ["llama-server.exe"]\nmin_available_ram_mb = 8192\n'
    )
    config = _load(tmp_path, body).gpu_admission
    assert config.mode == "observe" and config.gpu_slots == 2 and config.probe_wait_ms == 500
    assert config.on_host_floor == "refuse" and config.ctx_conflict == "wait_unload"
    assert config.runner_process_names == ("llama-server.exe",)
    assert config.min_available_ram_mb == 8192


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(sonder_config.ConfigError) as caught:
        _load(tmp_path, "[gpu_admission]\nvram_floor_mb = 1\n")
    assert "unknown key [gpu_admission].vram_floor_mb" in str(caught.value)


@pytest.mark.parametrize("name,low,high", [
    (name, low, high) for name, (low, high) in section.INTEGER_BOUNDS.items()
])
def test_every_bound_accepts_the_edges_and_rejects_just_outside(tmp_path, name, low, high):
    for value in (low, high):
        loaded = _load(tmp_path, "[gpu_admission]\n%s = %d\n" % (name, value)).gpu_admission
        assert getattr(loaded, name) == value
    for value in (low - 1, high + 1):
        with pytest.raises(sonder_config.ConfigError) as caught:
            _load(tmp_path, "[gpu_admission]\n%s = %d\n" % (name, value))
        assert "[gpu_admission].%s must be an integer in" % name in str(caught.value)


@pytest.mark.parametrize("key,value", [
    ("mode", "strict"), ("on_host_floor", "wait"), ("ctx_conflict", "exact"),
])
def test_enumerated_values_are_validated(tmp_path, key, value):
    with pytest.raises(sonder_config.ConfigError) as caught:
        _load(tmp_path, '[gpu_admission]\n%s = "%s"\n' % (key, value))
    assert "[gpu_admission].%s must be one of" % key in str(caught.value)


def test_wrong_toml_type_is_rejected(tmp_path):
    with pytest.raises(sonder_config.ConfigError) as caught:
        _load(tmp_path, '[gpu_admission]\ngpu_slots = "two"\n')
    assert "[gpu_admission].gpu_slots must be an integer" in str(caught.value)


def test_store_path_must_be_absolute_and_not_a_symlink(tmp_path):
    with pytest.raises(sonder_config.ConfigError) as caught:
        _load(env={"SONDER_GPU_ADMISSION_STORE": "relative/gpu.sqlite3"})
    assert "store_path must be an absolute path" in str(caught.value)
    good = tmp_path / "ok.sqlite3"
    assert _load(env={"SONDER_GPU_ADMISSION_STORE": str(good)}).gpu_admission.store_path == str(good)
    target = tmp_path / "real.sqlite3"
    target.write_bytes(b"")
    link = tmp_path / "link.sqlite3"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(sonder_config.ConfigError) as caught:
        _load(env={"SONDER_GPU_ADMISSION_STORE": str(link)})
    assert "must not be a symlink" in str(caught.value)


def test_environment_overrides_apply(tmp_path):
    store = tmp_path / "gpu.sqlite3"
    env = {
        "SONDER_GPU_ADMISSION": "OBSERVE", "SONDER_GPU_ADMISSION_STORE": str(store),
        "SONDER_GPU_SLOTS": "2", "SONDER_GPU_EMBED_SLOTS": "3", "SONDER_GPU_PROBE_SLOTS": "4",
        "SONDER_GPU_PROBE_WAIT_MS": "750", "SONDER_GPU_QUEUE_DEPTH": "10",
        "SONDER_GPU_QUEUE_WAIT_SECONDS": "60", "SONDER_GPU_POLL_MS": "50",
        "SONDER_MIN_AVAILABLE_RAM_MB": "2048", "SONDER_MIN_COMMIT_HEADROOM_MB": "1024",
        "SONDER_GPU_ON_HOST_FLOOR": "refuse", "SONDER_GPU_CTX_CONFLICT": "wait_unload",
    }
    config = _load(env=env).gpu_admission
    assert (config.mode, config.store_path) == ("observe", str(store))
    assert (config.gpu_slots, config.embed_slots, config.probe_slots) == (2, 3, 4)
    assert (config.probe_wait_ms, config.max_queue_depth, config.max_queue_wait_seconds) == (750, 10, 60)
    assert (config.poll_interval_ms, config.min_available_ram_mb,
            config.min_commit_headroom_mb) == (50, 2048, 1024)
    assert (config.on_host_floor, config.ctx_conflict) == ("refuse", "wait_unload")


def test_environment_beats_toml(tmp_path):
    config = _load(tmp_path, "[gpu_admission]\ngpu_slots = 2\n", env={"SONDER_GPU_SLOTS": "3"})
    assert config.gpu_admission.gpu_slots == 3


def test_malformed_environment_integer_is_reported_not_ignored():
    with pytest.raises(sonder_config.ConfigError) as caught:
        _load(env={"SONDER_GPU_SLOTS": "many"})
    assert "SONDER_GPU_SLOTS is not an integer" in str(caught.value)


def test_environment_value_out_of_bounds_is_rejected():
    with pytest.raises(sonder_config.ConfigError) as caught:
        _load(env={"SONDER_GPU_SLOTS": "9"})
    assert "[gpu_admission].gpu_slots must be an integer in 1..8" in str(caught.value)


def test_dotted_override_applies():
    assert _load(**{"gpu_admission.max_queue_depth": "7"}).gpu_admission.max_queue_depth == 7


def test_redacted_dump_lists_the_section():
    dump = _load().as_redacted_dict()
    assert dump["gpu_admission"]["gpu_slots"] == 1
    assert dump["gpu_admission"]["runner_process_names"][0] == "llama-server.exe"

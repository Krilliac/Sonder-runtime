"""The host-environment probe has one canonical platform owner."""
import importlib


def test_root_compatibility_import_is_the_platform_implementation():
    legacy = importlib.import_module("environment_probe")
    packaged = importlib.import_module("sonder_runtime.platform.environment_probe")

    assert legacy is packaged
    assert legacy.probe is packaged.probe
    assert legacy.agent_brief is packaged.agent_brief
    assert legacy.format_profile is packaged.format_profile


def test_platform_probe_keeps_one_shared_cache(monkeypatch):
    module = importlib.import_module("sonder_runtime.platform.environment_probe")
    module.probe(refresh=True)
    cached = module.probe()

    monkeypatch.setattr(module, "_which_map", lambda names: {})
    assert module.probe() is cached

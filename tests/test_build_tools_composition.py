"""Composition of the build tools: lazy, fail-closed, wired in both app facades.

``compose_build_tools`` performs no probes, reads or launches; a runtime that
lacks the build packages (or fails to compose them) still starts and every
build tool answers ``BUILD_TOOLS_UNAVAILABLE``. The operator configuration
(SONDER_BUILD_*) parses narrowly: a bad value keeps the narrower default and
is reported.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

import permission_modes as pm
from sonder_runtime.bootstrap import build_tools
from sonder_runtime.bootstrap.build_tools import (
    clangd_navigator_factory,
    compose_build_tools,
    load_build_profiles,
)
from sonder_runtime.platform.config import BuildToolsConfig, build_tools_config_from_env

pytestmark = pytest.mark.unit


def _make_absent(monkeypatch, *names):
    """Make the named modules fail to import, as in a runtime shipped without them.

    A ``None`` entry in ``sys.modules`` makes ``import`` raise ImportError;
    monkeypatch restores the real modules afterwards.
    """
    for name in names:
        monkeypatch.setitem(sys.modules, name, None)


class Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(("call", args))
        raise AssertionError("composition must not reach the process or job layers")

    def lookup(self, name):
        self.calls.append(("lookup", name))
        return None

    def capability_summary(self, **kwargs):
        return ""


class Digest:
    def digest_text(self, text, *, label=""):
        raise AssertionError("composition must not digest anything")


def _compose(tmp_path, monkeypatch, **kwargs):
    from sonder_runtime.platform import paths as runtime_paths

    monkeypatch.setattr(runtime_paths, "state_path",
                        lambda name, *args, **kw: str(tmp_path / "state" / name))
    provider, registry, inventory = Spy(), Spy(), Spy()
    services = compose_build_tools(config=SimpleNamespace(build_tools=BuildToolsConfig()),
                                   inventory=inventory, digest=Digest(),
                                   process_job_provider=provider, job_registry=registry,
                                   redactor=None, **kwargs)
    return services, provider, registry, inventory


@pytest.mark.parametrize("missing", [
    "sonder_runtime.application.build.run_service",
    "sonder_runtime.adapters.build.planner",
])
def test_without_the_build_packages_composition_returns_none(tmp_path, monkeypatch, missing):
    _make_absent(monkeypatch, missing)
    services, provider, registry, inventory = _compose(tmp_path, monkeypatch)
    assert services is None
    assert provider.calls == registry.calls == inventory.calls == []


def test_composition_performs_no_probes_reads_or_launches(tmp_path, monkeypatch):
    services, provider, registry, inventory = _compose(tmp_path, monkeypatch)
    assert services is not None and services.model is not None and services.jobs is not None
    assert provider.calls == registry.calls == inventory.calls == []
    assert not (tmp_path / "state").exists() or not any((tmp_path / "state").rglob("*"))


def test_the_runtime_composes_the_build_tools_into_its_typed_gateway(tmp_path, monkeypatch):
    from sonder_runtime.adapters.filesystem import file_ops
    from sonder_runtime.application.tools.gateway_contract import (
        ToolGatewayRequest, ToolPermission, ToolScope,
    )
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.platform import paths as runtime_paths

    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    previous = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    monkeypatch.setattr(pm, "current_mode", lambda: pm.PLAN)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    try:
        application = bootstrap_app.build_application()
        names = {item.name for item in application.tools.graph.registry.list_all()}
        assert set(build_tools.BUILD_TYPED_TOOLS) <= names
        request = ToolGatewayRequest(
            "r-1", "build_model", {"project": str(tmp_path)},
            ToolScope(principal_id="owner", source="repl", allowed_effects=frozenset({"read_files"})),
            ToolPermission(frozenset({"read_files"})))
        receipt = application.tools.execute(request)
        body = json.loads(receipt.output)
        assert "error_code" not in body or body["error_code"] in (
            "BUILD_TREE_MISSING", "BUILD_MODEL_UNAVAILABLE", "PROJECT_OUTSIDE_ROOTS")
        # a build is refused in plan mode before anything is planned
        from sonder_runtime.domain.common.errors import Forbidden

        with pytest.raises(Forbidden):
            application.tools.execute(ToolGatewayRequest(
                "r-2", "build_job", {"target": "game"},
                ToolScope(principal_id="owner", source="mcp",
                          allowed_effects=frozenset({"read_files", "write_files", "execute"})),
                ToolPermission(frozenset({"read_files", "write_files", "execute"}))))
        application.close_providers(timeout=5)
    finally:
        if previous is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous)


def test_app_wires_build_tools_in_both_facades():
    import inspect

    from sonder_runtime.bootstrap import app as bootstrap_app

    source = inspect.getsource(bootstrap_app.build_application)
    assert source.count("build_tool_executor(") == 2, "main facade and lane-tests files executor"
    assert source.count("build_permission_resolvers(build_tools, grants=build_grants)") == 2
    assert source.count("grant_authorities=(build_grants,)") == 2


def test_a_failing_composition_leaves_the_services_none(monkeypatch):
    from sonder_runtime.bootstrap import app as bootstrap_app

    def boom(**kwargs):
        raise RuntimeError("broken adapter")

    monkeypatch.setattr(build_tools, "compose_build_tools", boom)
    developer = SimpleNamespace(inventory=object(), digest=object())
    assert bootstrap_app._compose_build_tools(
        None, None, developer, lambda: None, lambda: None, None,
        tools_getter=lambda: None, model_gateway_getter=lambda: None) is None
    assert bootstrap_app._compose_build_tools(
        None, None, None, lambda: None, lambda: None, None,
        tools_getter=lambda: None, model_gateway_getter=lambda: None) is None


# --- configuration ------------------------------------------------------------------------


def test_defaults_are_the_narrow_choice():
    config = build_tools_config_from_env({})
    assert config == BuildToolsConfig()
    assert (config.network, config.fix_world, config.max_timeout_seconds) == ("default", "host", 7200)
    assert config.user_presets is True and config.clangd_config is False
    assert config.fix_propose_only_ok is False and config.utility_targets == ()


def test_every_key_parses():
    errors = []
    config = build_tools_config_from_env({
        "SONDER_BUILD_PROFILES": "/etc/sonder/profiles.json",
        "SONDER_BUILD_ENV_PASSTHROUGH": "CUDA_PATH, VULKAN_SDK;SCE_ROOT_DIR",
        "SONDER_BUILD_NETWORK": "Enforce",
        "SONDER_BUILD_MAX_TIMEOUT_SECONDS": "86400",
        "SONDER_BUILD_FIX_WORLD": "container",
        "SONDER_BUILD_UTILITY_TARGETS": "deploy,upload_symbols",
        "SONDER_BUILD_USER_PRESETS": "0",
        "SONDER_BUILD_CLANGD_CONFIG": "1",
        "SONDER_BUILD_FIX_MODEL_ROUTE": "local/codegen",
        "SONDER_BUILD_FIX_PROPOSE_ONLY_OK": "true",
    }, errors)
    assert errors == []
    assert config.env_passthrough == ("CUDA_PATH", "VULKAN_SDK", "SCE_ROOT_DIR")
    assert (config.network, config.fix_world, config.max_timeout_seconds) == ("enforce", "container", 86400)
    assert config.utility_targets == ("deploy", "upload_symbols")
    assert config.user_presets is False and config.clangd_config is True
    assert config.fix_model_route == "local/codegen" and config.fix_propose_only_ok is True


@pytest.mark.parametrize("key, value", [
    ("SONDER_BUILD_NETWORK", "off"), ("SONDER_BUILD_MAX_TIMEOUT_SECONDS", "999999"),
    ("SONDER_BUILD_MAX_TIMEOUT_SECONDS", "ten"), ("SONDER_BUILD_FIX_WORLD", "vm"),
    ("SONDER_BUILD_UTILITY_TARGETS", "ok,bad;name,a:b"), ("SONDER_BUILD_ENV_PASSTHROUGH", "A=B"),
    ("SONDER_BUILD_FIX_MODEL_ROUTE", "cloud route!"),
])
def test_bad_values_are_reported_and_never_widen(key, value):
    errors = []
    config = build_tools_config_from_env({key: value}, errors)
    assert errors and key in errors[0]
    default = BuildToolsConfig()
    assert config.network == default.network and config.fix_world == default.fix_world
    assert config.max_timeout_seconds == default.max_timeout_seconds
    assert "a:b" not in config.utility_targets and "A=B" not in config.env_passthrough
    assert config.fix_model_route == default.fix_model_route


def test_load_config_carries_the_build_section(tmp_path):
    from sonder_runtime.platform.config import ConfigError, load_config

    env = {"SONDER_HOME": str(tmp_path / "home"), "SONDER_BUILD_NETWORK": "advisory"}
    assert load_config(env=env).build_tools.network == "advisory"
    with pytest.raises(ConfigError, match="SONDER_BUILD_NETWORK"):
        load_config(env={**env, "SONDER_BUILD_NETWORK": "wide-open"})


# --- operator profiles --------------------------------------------------------------------


PROFILE = json.dumps({"profiles": [{"name": "fastbuild", "executable": "fbuild",
                                    "actions": {"build": ["-config", "{build_dir}/fbuild.bff"]}}]})


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_profiles_load_only_from_a_private_regular_file(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(PROFILE)
    os.chmod(path, 0o644)
    assert load_build_profiles(str(path)) == ()
    os.chmod(path, 0o600)
    loaded = load_build_profiles(str(path))
    assert [profile.name for profile in loaded] == ["fastbuild"]
    link = tmp_path / "link.json"
    os.symlink(path, link)
    assert load_build_profiles(str(link)) == ()
    assert load_build_profiles(str(tmp_path / "missing.json")) == ()
    assert load_build_profiles("") == ()
    assert load_build_profiles(str(path), platform_name="nt") == ()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_profiles_are_ignored_without_the_build_domain(tmp_path, monkeypatch):
    path = tmp_path / "profiles.json"
    path.write_text(PROFILE)
    os.chmod(path, 0o600)
    _make_absent(monkeypatch, "sonder_runtime.domain.build.templates")
    assert load_build_profiles(str(path)) == ()


# --- clangd (lane D) wiring ---------------------------------------------------------------


def test_clangd_is_composed_only_when_the_inventory_has_it(tmp_path):
    factory = clangd_navigator_factory(BuildToolsConfig())
    assert factory is not None
    assert factory(inventory=Spy(), project_root=str(tmp_path), build_dir=str(tmp_path)) is None
    record = SimpleNamespace(path="/usr/bin/clangd")
    inventory = SimpleNamespace(lookup=lambda name: record if name == "clangd" else None)
    navigator = factory(inventory=inventory, project_root=str(tmp_path), build_dir=str(tmp_path))
    assert navigator is not None and navigator.session.pid is None, "nothing is launched"
    navigator.close()

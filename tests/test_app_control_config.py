from sonder_runtime.platform.app_control_config import (
    AppControlConfig,
    app_control_transport,
)
from sonder_runtime.platform.config import SonderConfig


def test_disabled_and_untrusted_transport_refuse():
    assert not app_control_transport(SonderConfig(), raw_peer="127.0.0.1", origin=None)


def test_section_exists_disabled():
    assert SonderConfig().app_control == AppControlConfig()


from dataclasses import replace
import json
import os
from pathlib import Path
import pytest
from sonder_runtime.platform.config import load_config, FeaturesConfig
from sonder_runtime.bootstrap.app_control import AppProjectGrantCatalog
from sonder_runtime.application.compute_fabric.artifact_spool import (
    PrivateDirectoryAnchor,
)
from sonder_runtime.adapters.security.control_plane_paths import (
    ControlPlanePaths,
    live_control_plane_inventory,
)


def enabled(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    private = tmp_path / "private"
    with PrivateDirectoryAnchor.open_base(private):
        pass
    catalog = private / "grants.json"
    entry = dict(
        grant_id="grant-1",
        revision=1,
        project="project-a",
        accounts=["alice"],
        role="admin",
        roots=[str(root.resolve())],
        tools=["read_file"],
        allow_cloud=False,
        allow_remote=False,
        expires_at=2000,
    )
    catalog.write_text(json.dumps({"version": 1, "grants": [entry]}), encoding="utf8")
    catalog.chmod(0o600)
    config = replace(
        SonderConfig(),
        features=FeaturesConfig(host_control=True),
        app_control=AppControlConfig(
            enabled=True,
            runtime_id="runtime-unique-1",
            catalog_file=str(catalog),
            allow_numeric_loopback_native=True,
        ),
    )
    state = {"config": config, "roots": (str(root),), "inventory": ControlPlanePaths()}
    loader = AppProjectGrantCatalog(
        config_provider=lambda: state["config"],
        workspace_roots=lambda: state["roots"],
        private_inventory=lambda: live_control_plane_inventory(
            additional=lambda: state["inventory"]
        ),
        clock=lambda: 1000,
    )
    return loader, state, catalog, entry


def test_actual_config_provenance_redaction_no_env_grants(tmp_path):
    path = tmp_path / "app.toml"
    catalog = tmp_path / "private" / "catalog.json"
    path.write_text(
        '[features]\nhost_control=true\n[app_control]\nenabled=true\nruntime_id="runtime-install-1"\ncatalog_file='
        + json.dumps(str(catalog))
        + "\nallow_numeric_loopback_native=true\n",
        encoding="utf8",
    )
    config = load_config(
        path, env={"SONDER_APP_CONTROL_RUNTIME_ID": "attacker-runtime"}
    )
    assert config.app_control.runtime_id == "runtime-install-1"
    assert str(catalog.resolve()) in config.private_source_paths
    assert str(catalog) not in json.dumps(config.as_redacted_dict())
    assert not load_config(
        env={"SONDER_APP_CONTROL_ENABLED": "true"}
    ).app_control.enabled


@pytest.mark.parametrize(
    "listener,peer,origin,expected",
    [
        ("127.0.0.1", "127.0.0.1", None, True),
        ("::1", "::1", None, True),
        ("localhost", "127.0.0.1", None, False),
        ("0.0.0.0", "127.0.0.1", None, False),
        ("127.0.0.1", "192.0.2.1", None, False),
        ("127.0.0.1", "127.0.0.1", "null", False),
        ("127.0.0.1", "127.0.0.1", "https://app.test", False),
    ],
)
def test_native_transport_raw_only(tmp_path, listener, peer, origin, expected):
    _, state, _, _ = enabled(tmp_path)
    config = replace(
        state["config"], server=replace(state["config"].server, host=listener)
    )
    assert app_control_transport(config, raw_peer=peer, origin=origin) is expected


def test_browser_transport_requires_every_deployment_prerequisite(tmp_path):
    _, state, _, _ = enabled(tmp_path)
    cfg = state["config"]
    cfg = replace(
        cfg,
        server=replace(
            cfg.server, cors_origins=("https://app.test",), tls_terminated_by_proxy=True
        ),
        app_control=replace(
            cfg.app_control,
            app_origins=("https://app.test",),
            proxy_cidrs=("192.0.2.0/24",),
            proxy_only_backend=True,
            allow_numeric_loopback_native=False,
        ),
    )
    assert app_control_transport(cfg, raw_peer="192.0.2.3", origin="https://app.test")
    for peer, origin in [
        ("192.0.3.3", "https://app.test"),
        ("192.0.2.3", None),
        ("192.0.2.3", "http://app.test"),
        ("192.0.2.3", "https://app.test/"),
        ("localhost", "https://app.test"),
    ]:
        assert not app_control_transport(cfg, raw_peer=peer, origin=origin)
    assert not app_control_transport(
        replace(cfg, app_control=replace(cfg.app_control, proxy_only_backend=False)),
        raw_peer="192.0.2.3",
        origin="https://app.test",
    )


def test_catalog_live_grant_and_replacement_fence(tmp_path):
    loader, state, path, entry = enabled(tmp_path)
    grant = loader.resolve("project-a", "alice", "admin")
    loader.require_current(grant)
    for account, role in [("bob", "admin"), ("alice", "user")]:
        with pytest.raises(PermissionError):
            loader.resolve("project-a", account, role)
    entry["revision"] = 2
    path.write_text(json.dumps({"version": 1, "grants": [entry]}))
    with pytest.raises(PermissionError):
        loader.require_current(grant)
    newer = loader.resolve("project-a", "alice", "admin")
    assert newer.revision == 2
    replacement = path.with_suffix(".tmp")
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(path)
    with pytest.raises(PermissionError):
        loader.require_current(newer)


def test_catalog_all_model_roots_private_inventory_and_expiry(tmp_path):
    loader, state, path, entry = enabled(tmp_path)
    grant = loader.resolve("project-a", "alice", "admin")
    original = state["roots"]
    state["roots"] = (*original, str(path.parent))
    with pytest.raises(PermissionError):
        loader.require_current(grant)
    state["roots"] = original
    state["inventory"] = ControlPlanePaths(owned_directories=(Path(original[0]),))
    with pytest.raises(PermissionError):
        loader.require_current(grant)
    state["inventory"] = ControlPlanePaths()
    entry["expires_at"] = 999
    path.write_text(json.dumps({"version": 1, "grants": [entry]}))
    with pytest.raises(PermissionError):
        loader.snapshot()


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e.update(accounts=["Alice"]),
        lambda e: e.update(role="user"),
        lambda e: e.update(revision=True),
        lambda e: e.update(allow_cloud=True),
        lambda e: e.update(expires_at=float("inf")),
        lambda e: e.update(tools=["*"]),
        lambda e: e.update(unexpected="value"),
    ],
)
def test_catalog_strict_shapes(tmp_path, change):
    loader, _, path, entry = enabled(tmp_path)
    change(entry)
    path.write_text(json.dumps({"version": 1, "grants": [entry]}))
    with pytest.raises(PermissionError):
        loader.snapshot()


def test_catalog_hardlink_and_duplicate_key_refusal(tmp_path):
    loader, _, path, _ = enabled(tmp_path)
    link = path.with_suffix(".link")
    os.link(path, link)
    with pytest.raises(PermissionError):
        loader.snapshot()
    link.unlink()
    path.write_text('{"version":1,"version":1,"grants":[]}')
    with pytest.raises(PermissionError):
        loader.snapshot()


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.test/",
        "http://app.test",
        "null",
        "https://user@app.test",
        "https://app.test?q=x",
        "https://app.test#x",
        "https://*.test",
        "https://APP.test",
        "https://app.test:443",
    ],
)
def test_browser_origin_canonical_validation(tmp_path, origin):
    _, state, _, _ = enabled(tmp_path)
    cfg = state["config"]
    cfg = replace(
        cfg,
        server=replace(
            cfg.server, cors_origins=(origin,), tls_terminated_by_proxy=True
        ),
        app_control=replace(
            cfg.app_control,
            app_origins=(origin,),
            proxy_cidrs=("192.0.2.0/24",),
            proxy_only_backend=True,
        ),
    )
    assert not app_control_transport(cfg, raw_peer="192.0.2.1", origin=origin)


def test_catalog_bounds_and_postread_replacement(tmp_path):
    loader, state, path, entry = enabled(tmp_path)
    path.write_bytes(b"x" * 262145)
    with pytest.raises(PermissionError):
        loader.snapshot()
    path.write_text(json.dumps({"version": 1, "grants": [entry]}))
    calls = 0
    old_inventory = loader._inventory

    def replace_during_live_recheck():
        nonlocal calls
        calls += 1
        if calls == 2:
            replacement = path.with_suffix(".new")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(path)
        return old_inventory()

    loader._inventory = replace_during_live_recheck
    with pytest.raises(PermissionError):
        loader.snapshot()


def test_config_source_closure_cannot_be_model_writable(tmp_path):
    loader, state, path, _ = enabled(tmp_path)
    state["config"] = replace(
        state["config"],
        private_source_paths=(str(Path(state["roots"][0]) / "config.toml"),),
    )
    with pytest.raises(PermissionError):
        loader.snapshot()

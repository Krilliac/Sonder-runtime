"""Model defaults are data; corrupt configuration must never invent defaults."""

import json
from pathlib import Path

import pytest

from sonder_runtime.adapters import model_lists as ml


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    path = tmp_path / "model_lists.json"
    monkeypatch.setenv("SONDER_MODEL_LISTS", str(path))
    ml.clear_cache()
    yield path
    ml.clear_cache()


def test_shipped_defaults_preserve_every_extracted_value(overlay):
    expected = json.loads(
        (Path(__file__).parent / "fixtures/model_lists_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert ml.load().document() == expected


def test_overlay_replaces_lists_and_merges_dictionary_subkeys(overlay):
    overlay.write_text(
        json.dumps(
            {
                "version": 1,
                "retired_cloud_models": ["old:cloud"],
                "default_local_models": {"code": "custom:7b"},
                "capability_router": {"preferred": {"simple": "code"}},
            }
        ),
        encoding="utf-8",
    )
    config = ml.load()
    assert config.values["retired_cloud_models"] == ("old:cloud",)
    assert config.values["default_local_models"]["code"] == "custom:7b"
    assert config.values["default_local_models"]["general"] == "sonder:latest"
    assert config.values["capability_router"]["preferred"]["simple"] == "code"
    assert config.source_of("default_local_models.code") == "overlay"
    assert config.source_of("default_local_models.general") == "shipped"


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        '{"version": true}',
        '{"version": 2}',
        '{"version": 1, "typo": []}',
        '{"version": 1, "stable_alias": "bad name"}',
        '{"version": 1, "default_local_models": {"code": "remote:cloud"}}',
        '{"version": 1, "default_routing": {"chat": "vision"}}',
        '{"version": 1, "tier_strength": {"code": true}}',
        '{"version": 1, "ensemble_skip_tiers": ["unknown"]}',
        '{"version": 1, "training_model_specs": {"7b": {"hf_revision": "main"}}}',
    ],
)
def test_invalid_overlay_uses_shipped_values_reports_issue_and_preserves_bytes(
    overlay, raw
):
    baseline = ml.load().document()
    overlay.write_text(raw, encoding="utf-8")
    config = ml.load()
    assert config.document() == baseline
    assert config.issues and config.issues[0].source == ml.ConfigSource.OVERLAY
    assert all(source == "shipped" for source in config.sources.values())
    assert overlay.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("contents", [None, "{}", '{"version": 9}', "not json"])
def test_unavailable_shipped_data_is_a_typed_error(
    overlay, tmp_path, monkeypatch, contents
):
    path = tmp_path / "missing-shipped.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(ml, "SHIPPED_PATH", path)
    with pytest.raises(ml.ModelListsConfigurationError, match="missing-shipped.json"):
        ml.load()
    assert "missing-shipped.json" in "\n".join(ml.describe())


def test_cache_notices_atomic_replacement_and_data_is_immutable(overlay):
    first = ml.load()
    assert ml.load() is first
    with pytest.raises(TypeError):
        first.values["stable_alias"] = "other:latest"
    replacement = overlay.with_suffix(".new")
    replacement.write_text(
        '{"version": 1, "stable_alias": "other:latest"}', encoding="utf-8"
    )
    replacement.replace(overlay)
    assert ml.load().values["stable_alias"] == "other:latest"
    detached = ml.load().document()
    detached["default_local_models"]["code"] = "changed"
    assert ml.load().values["default_local_models"]["code"] != "changed"


def test_runtime_environment_still_wins(overlay):
    from sonder_runtime.domain.runtime_model_configuration import (
        RuntimeModelConfiguration,
    )
    from sonder_runtime.domain.runtime_policy import rules
    from tests.support.tool_lane import FLOOR

    overlay.write_text(
        '{"version": 1, "default_local_models": {"code": "custom:7b"}}',
        encoding="utf-8",
    )
    values = ml.load().values
    env = {
        "SONDER_CODE": "operator:7b",
        "SONDER_EMBED_MODEL": "operator-embed:latest",
        "SONDER_CODE_LOCAL": "fallback:7b",
        "SONDER_CLOUD_GENERAL": "operator:cloud",
    }
    config = RuntimeModelConfiguration.from_environment(env, floor=FLOOR, lists=values)
    assert config.tier_map()["code"] == "operator:7b"
    assert config.local_code_model == "fallback:7b"
    assert config.tier_map()["cloud-general"] == "operator:cloud"
    assert (
        rules.default_policy(env, floor=FLOOR, lists=values)["embedding_model"]
        == "operator-embed:latest"
    )


def test_console_writes_require_exact_attendance_and_reset_overlay_only(overlay):
    from sonder_runtime.adapters import model_lists_editor as editor

    for attendance in (False, 1, "yes"):
        assert "refused" in editor.command(
            'set stable_alias "custom:latest"', operator_attended=attendance
        )
        assert not overlay.exists()
    assert "set stable_alias" in editor.command(
        'set stable_alias "custom:latest"', operator_attended=True
    )
    assert ml.load().values["stable_alias"] == "custom:latest"
    assert "overlay" in editor.command("", operator_attended=False)
    assert "reset stable_alias" in editor.command(
        "reset stable_alias", operator_attended=True
    )
    assert ml.load().values["stable_alias"] == "sonder:latest"
    assert json.loads(overlay.read_text(encoding="utf-8")) == {"version": 1}


def test_console_refuses_bad_overlay_and_invalid_edits(overlay):
    from sonder_runtime.adapters import model_lists_editor as editor

    assert "refused" in editor.command(
        'set default_local_models {"code": "bad name"}', operator_attended=True
    )
    assert not overlay.exists()
    overlay.write_text("{broken", encoding="utf-8")
    assert "refused" in editor.command("reset stable_alias", operator_attended=True)
    assert overlay.read_text(encoding="utf-8") == "{broken"


def test_packaging_and_control_plane_protection(overlay):
    from scripts.package_local_system import PACKAGE_DATA_FILES, REQUIRED_FILES
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    name = "sonder_runtime/adapters/data/model_lists.json"
    assert name in PACKAGE_DATA_FILES and name in REQUIRED_FILES
    protected = live_control_plane_inventory()
    assert protected.protects(overlay)
    assert protected.protects(str(overlay) + ".lock")
    assert protected.protects(str(overlay) + ".tmp-test")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("retired_cloud_models", ["retired:cloud"]),
        ("legacy_cloud_general_model", "older:cloud"),
        ("default_cloud_models", {"general": "live:cloud"}),
        ("stable_alias", "custom:latest"),
        ("default_local_models", {"reasoning": "custom:14b"}),
        ("default_embedding_model", "other-embed:latest"),
        ("reserved_personal_model", "private-personal:latest"),
        ("default_routing", {"chat": "code"}),
        ("vision_only_model_families", ["custom-vision"]),
        ("tier_strength", {"reasoning": 3}),
        ("working_strength", 2),
        ("capability_router", {"fallback": ["code", "general", "fast"]}),
        ("ensemble_skip_tiers", ["vision", "reasoning"]),
        ("bootstrap_models", {"small": "custom:1.5b"}),
        ("training_model_specs", {"7b": {"ollama": "custom:7b"}}),
    ],
)
def test_each_key_can_be_overridden_and_reset_at_console(overlay, key, value):
    from sonder_runtime.adapters.model_lists_editor import command

    original = ml.load().document()
    assert "refused" not in command(
        "set %s %s" % (key, json.dumps(value)), operator_attended=True
    )
    effective = ml.load().document()[key]
    if isinstance(value, dict):
        for subkey, item in value.items():
            if isinstance(item, dict):
                assert all(effective[subkey][k] == v for k, v in item.items())
            else:
                assert effective[subkey] == item
    else:
        assert effective == value
    assert "refused" not in command("reset " + key, operator_attended=True)
    assert ml.load().document() == original


def test_status_applies_environment_and_labels_sources(overlay, monkeypatch):
    monkeypatch.setenv("SONDER_CODE", "operator:7b")
    monkeypatch.setenv("SONDER_CLOUD_GENERAL", "live:cloud")
    monkeypatch.setenv("SONDER_EMBED_MODEL", "embed:latest")
    text = "\n".join(ml.describe())
    assert 'default_local_models.code = "operator:7b" [env]' in text
    assert 'default_cloud_models.general = "live:cloud" [env]' in text
    assert 'default_embedding_model = "embed:latest" [env]' in text


def test_status_reports_invalid_environment_without_hiding_lists(overlay, monkeypatch):
    monkeypatch.setenv("SONDER_CODE", "bad model name")
    assert "SONDER_CODE" in "\n".join(ml.describe())
    assert "ERROR" in "\n".join(ml.describe())
    assert "default_local_models.code" in "\n".join(ml.describe())


def test_reserved_alias_normalization_uses_configured_name(overlay):
    from sonder_runtime.domain.runtime_policy import rules

    assert rules.is_reserved_personal_alias("custom-personal", "custom-personal")
    assert rules.is_reserved_personal_alias("custom-personal:latest", "custom-personal")


def test_escalation_adapter_preserves_explicit_inputs(overlay):
    from sonder_runtime.adapters import tier_escalation

    values = ml.load().document()
    values["tier_strength"]["fast"] = 4
    assert tier_escalation.reachable_tiers("fast", ["general"], lists=values) == (
        "fast",
    )


def test_server_console_reuses_operator_attendance(overlay):
    import server

    assert "refused" in server._runtime_command(
        'models set stable_alias "custom:latest"'
    )
    assert not overlay.exists()
    assert "refused" not in server._runtime_command(
        'models set stable_alias "custom:latest"', operator_approved=True
    )
    assert ml.load().values["stable_alias"] == "custom:latest"


@pytest.mark.parametrize("contents", [None, "not json", "{}", '{"version": 9}', "oversized"], ids=["missing", "malformed", "empty", "version", "oversized"])
def test_unavailable_shipped_data_does_not_crash_mcp_import(overlay, tmp_path, contents):
    import os
    import subprocess
    import sys

    missing = tmp_path / "missing-shipped.json"
    if contents == "oversized":
        document = ml.load().document()
        document["training_model_specs"]["7b"]["params"] = 10 ** 400
        contents = json.dumps(document)
    if contents is not None:
        missing.write_text(contents, encoding="utf-8")
    code = """
import sys
from pathlib import Path
from sonder_runtime.adapters import model_lists
model_lists.SHIPPED_PATH = Path(sys.argv[1])
import server
assert 'missing-shipped.json' in server._runtime_command('models')
assert 'missing-shipped.json' in server._runtime_command('status')
from sonder_runtime.adapters import runtime_policy
try:
    runtime_policy.default_policy({})
except model_lists.ModelListsConfigurationError:
    pass
else:
    raise AssertionError('missing defaults were accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(missing)],
        env={**os.environ, "SONDER_PREWARM": "0"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr



def test_model_list_overlay_cannot_remove_the_tool_policy_floor(overlay):
    from sonder_runtime.adapters import runtime_policy
    from sonder_runtime.domain.tool_lane_models import decide_listed
    from tests.support.tool_lane import DENIED, FLOOR

    overlay.write_text(
        json.dumps({
            "version": 1,
            "retired_cloud_models": [],
            "default_local_models": {"code": DENIED},
        }),
        encoding="utf-8",
    )
    policy = runtime_policy.default_policy({})
    deny = runtime_policy.tool_lane_deny_list(policy)
    assert FLOOR.names <= deny.names
    assert set(FLOOR.prefixes) <= set(deny.prefixes)
    assert decide_listed(DENIED, deny, free_form=False).denied



def test_oversized_numeric_overlay_is_reported_and_preserved(overlay):
    raw = json.dumps({"version": 1, "training_model_specs": {"7b": {"params": 10 ** 400}}})
    overlay.write_text(raw, encoding="utf-8")
    config = ml.load()
    assert config.issues and config.issues[0].source is ml.ConfigSource.OVERLAY
    assert config.values["training_model_specs"]["7b"]["params"] < 10 ** 400
    assert overlay.read_text(encoding="utf-8") == raw


def test_oversized_shipped_numeric_data_is_a_typed_error(overlay, tmp_path, monkeypatch):
    document = ml.load().document()
    document["training_model_specs"]["7b"]["params"] = 10 ** 400
    shipped = tmp_path / "oversized-shipped.json"
    shipped.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(ml, "SHIPPED_PATH", shipped)
    with pytest.raises(ml.ModelListsConfigurationError, match="oversized-shipped"):
        ml.load()
    assert "unavailable" in "\n".join(ml.describe())


def test_oversized_numeric_console_edit_is_refused_without_a_write(overlay):
    from sonder_runtime.adapters.model_lists_editor import command
    result = command(
        'set training_model_specs ' + json.dumps({"7b": {"params": 10 ** 400}}),
        operator_attended=True,
    )
    assert result.startswith("refused")
    assert not overlay.exists()

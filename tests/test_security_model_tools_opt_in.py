"""Tests for explicit host-only relaxation of the shipped ``security-advisers`` tool floor.

The shipped ``security-advisers`` floor blocks security-tuned models (such as
``astra-cyber-*``) from tool-using lanes by default.  Operators may opt in on
the host environment via:

    SONDER_ALLOW_SECURITY_MODEL_TOOLS=I_ACCEPT_SECURITY_MODEL_TOOL_RISK

This relaxes ONLY the shipped ``security-advisers`` model-name floor while
keeping tamper pinning, operator overlays, undetermined-served-model guards,
other shipped floor profiles, permission modes, approvals, workspace
containment, and secret redaction intact.
"""
from __future__ import annotations

import json
import logging

import pytest

from sonder_runtime.adapters import model_tool_grants as grants_adapter
from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters import tool_lane_guard as guard
from sonder_runtime.domain import tool_lane_models as rule
from sonder_runtime.domain.tool_lane_models import ModelLaneStatus
from tests.support.tool_lane import ALLOWED, DENIED, HF_ORNITH, clear_environment

ASTRA_35B = "astra-cyber-35b-a3b:latest"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(tmp_path / "overlay" / "model_tool_policy.json"))
    (tmp_path / "overlay").mkdir()
    monkeypatch.setattr(guard, "_GENERATION_CACHE", None, raising=False)
    monkeypatch.setattr(mtp, "_OPT_IN_LOGGED", set())
    mtp.clear_cache()
    yield
    mtp.clear_cache()


def _write_overlay(tmp_path, profiles=None, **extra):
    path = tmp_path / "overlay" / "model_tool_policy.json"
    path.write_text(
        json.dumps({"version": 1, "model_profiles": profiles or [], **extra}),
        encoding="utf-8",
    )
    return path


def _decide(model: str) -> rule.ModelLaneDecision:
    deny = runtime_policy.tool_lane_deny_list(runtime_policy.load(create=False))
    return rule.decide(model, deny, free_form=False)


def test_default_denies_security_models():
    assert not mtp.security_model_tools_allowed()
    config = mtp.load()
    assert config.security_floor_relaxed is False
    assert _decide(ASTRA_35B).denied
    assert _decide(DENIED).denied
    assert _decide(HF_ORNITH).denied
    assert not _decide(ALLOWED).denied
    assert all("WARNING" not in line for line in mtp.describe(config))
    assert runtime_policy._seed_model({"SONDER_SECURITY": ASTRA_35B}, "security") == ""


@pytest.mark.parametrize(
    "bad_value",
    [
        "1",
        "true",
        "TRUE",
        "yes",
        "on",
        "i_accept_security_model_tool_risk",
        "I_ACCEPT_RISK",
        "accept",
    ],
)
def test_shorthand_or_wrong_ack_fails_closed(monkeypatch, caplog, bad_value):
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, bad_value)
    assert not mtp.security_model_tools_allowed()
    with caplog.at_level(logging.WARNING, logger=mtp.__name__):
        config = mtp.load()
    assert config.security_floor_relaxed is False
    assert _decide(ASTRA_35B).denied
    assert _decide(DENIED).denied
    assert any("ignored" in rec.message for rec in caplog.records)
    assert runtime_policy._seed_model({"SONDER_SECURITY": ASTRA_35B}, "security") == ""


def test_exact_ack_relaxes_security_advisers_floor_with_audit_warning(monkeypatch, caplog):
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)
    assert mtp.security_model_tools_allowed()
    with caplog.at_level(logging.WARNING, logger=mtp.__name__):
        config = mtp.load()

    assert config.security_floor_relaxed is True
    assert config.floor.available is True
    assert not _decide(ASTRA_35B).denied
    assert not _decide(DENIED).denied
    assert not _decide(HF_ORNITH).denied

    # Security tier binding and model_tool_grants accept the security model now.
    assert runtime_policy._seed_model({"SONDER_SECURITY": ASTRA_35B}, "security") == ASTRA_35B
    grants = grants_adapter.grant_set(config)
    assert "astra-cyber-" not in grants.deny_prefixes

    # Other shipped invariants remain intact.
    assert "" in config.floor.undetermined_served
    assert "default" in config.floor.undetermined_served
    assert config.tool_ceiling
    assert config.secret_globs

    # Audit visibility: warning logged and shown in describe().
    assert any("intentionally relaxed" in rec.message for rec in caplog.records)
    described = "\n".join(mtp.describe(config))
    assert "WARNING:" in described
    assert mtp.SECURITY_ADVISERS_PROFILE_ID in described
    assert mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV in described


def test_cache_invalidates_immediately_on_env_toggle(monkeypatch):
    # First load with default (denied) populates both caches.
    assert rule.decide(ASTRA_35B, guard.generation_deny_list(), free_form=False).denied

    # Toggling env ON invalidates identity_key and both caches automatically.
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)
    assert not rule.decide(ASTRA_35B, guard.generation_deny_list(), free_form=False).denied

    # Toggling env OFF restores denial immediately.
    monkeypatch.delenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV)
    assert rule.decide(ASTRA_35B, guard.generation_deny_list(), free_form=False).denied


def test_operator_overlay_denials_stay_enforced_when_opt_in_active(monkeypatch, tmp_path):
    _write_overlay(
        tmp_path,
        profiles=[
            {
                "id": "custom-blocked",
                "match": {"names": ["custom-risky:latest"], "prefixes": ["blocked-prefix-"]},
            },
            {
                "id": "security-advisers",
                "match": {"names": [DENIED], "prefixes": []},
            },
        ],
    )
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)

    # Shipped security-advisers entries are relaxed...
    assert not _decide(ASTRA_35B).denied
    # ...but explicit operator denials (custom OR re-added under security-advisers) stay enforced.
    assert _decide("custom-risky:latest").denied
    assert _decide("blocked-prefix-7b").denied
    assert _decide(DENIED).denied


def test_existing_read_only_overlay_extending_security_advisers_stays_valid(monkeypatch, tmp_path):
    _write_overlay(
        tmp_path,
        profiles=[
            {
                "id": "security-advisers",
                "match": {"names": [], "prefixes": []},
                "tools": ["text_search"],
                "lanes": ["agent"],
            }
        ],
    )
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)

    config = mtp.load()
    assert config.readable_overlay is True
    assert not config.issues
    assert config.security_floor_relaxed is True
    assert not _decide(ASTRA_35B).denied


def test_broken_overlay_still_fails_closed_when_opt_in_active(monkeypatch, tmp_path):
    path = tmp_path / "overlay" / "model_tool_policy.json"
    path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)

    verdict = _decide(ASTRA_35B)
    assert verdict.denied
    assert verdict.status is ModelLaneStatus.OVERLAY_UNUSABLE


def test_tampered_or_missing_shipped_file_still_fails_closed(monkeypatch, tmp_path):
    shipped_copy = tmp_path / "shipped" / "model_tool_policy.json"
    shipped_copy.parent.mkdir()
    shipped_copy.write_text(mtp.SHIPPED_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mtp, "SHIPPED_PATH", shipped_copy)
    mtp.clear_cache()

    # Initial valid load pins shipped_copy.
    assert mtp.load().floor.available is True

    # Tamper with shipped file while opt-in is active: must STILL fail closed.
    tampered = json.loads(shipped_copy.read_text(encoding="utf-8"))
    tampered["undetermined_served_models"].append("tampered")
    shipped_copy.write_text(json.dumps(tampered), encoding="utf-8")
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)

    config = mtp.load()
    assert config.floor.available is False
    assert config.security_floor_relaxed is False
    assert _decide(ASTRA_35B).status is ModelLaneStatus.CONFIG_UNAVAILABLE
    assert _decide(ALLOWED).status is ModelLaneStatus.CONFIG_UNAVAILABLE


def test_other_shipped_floor_profiles_remain_enforced(monkeypatch, tmp_path):
    shipped_copy = tmp_path / "shipped_multi" / "model_tool_policy.json"
    shipped_copy.parent.mkdir()
    doc = json.loads(mtp.SHIPPED_PATH.read_text(encoding="utf-8"))
    doc["model_profiles"].append(
        {
            "id": "other-hard-floor",
            "floor": True,
            "match": {"names": ["never-tools:latest"], "prefixes": ["hard-blocked-"]},
            "tools": [],
            "lanes": [],
        }
    )
    shipped_copy.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(mtp, "SHIPPED_PATH", shipped_copy)
    monkeypatch.setenv(mtp.ALLOW_SECURITY_MODEL_TOOLS_ENV, mtp.ALLOW_SECURITY_MODEL_TOOLS_ACK)
    mtp.clear_cache()

    assert not _decide(ASTRA_35B).denied
    assert _decide("never-tools:latest").denied
    assert _decide("hard-blocked-9b").denied

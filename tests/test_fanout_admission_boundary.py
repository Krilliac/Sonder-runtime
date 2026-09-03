"""Fanout limits and the admission record live in the domain; root names stay compatible."""
import json

import server
from sonder_runtime.domain import fanout_admission as admission


def _record(run, limits, *, thinking=lambda _name: False):
    return admission.fanout_admission(
        run, [{"model": "injected:cloud"}], limits,
        is_cloud_model_name=lambda name: name.endswith(":cloud"),
        known_thinking_model=thinking,
        local_thinking_min_num_predict=4096,
    )


def test_root_limits_decoder_is_an_identity_preserving_alias():
    assert server._fanout_limits is admission.fanout_limits


def test_limits_decode_with_clamps_and_conservative_defaults():
    assert admission.fanout_limits({}) == {
        "num_predict": 512, "timeout": 45, "cloud_workers": 2, "resident_before": [],
        "resident_snapshot_known": False, "plan_skipped": [], "selection_profile": "",
    }
    assert admission.fanout_limits({"limits_json": "not json"})["num_predict"] == 512
    limits = admission.fanout_limits({"limits_json": json.dumps({
        "num_predict": 99999, "timeout": 1, "cloud_workers": 9,
        "resident_before": ["a", "", 7], "resident_snapshot_known": "yes",
        "plan_skipped": ["b"], "selection_profile": " Healthy-Chat ",
    })})
    assert limits == {
        "num_predict": 4096, "timeout": 5, "cloud_workers": 2, "resident_before": ["a", "7"],
        "resident_snapshot_known": False, "plan_skipped": ["b"], "selection_profile": "healthy-chat",
    }


def test_the_record_uses_the_immutable_snapshot_and_the_injected_classifiers():
    run = {
        "models_json": json.dumps(["kimi-k3:cloud", "local-thinking", " local-plain ", ""]),
        "cloud_opt_in": True,
    }
    limits = {"num_predict": 512, "timeout": 10, "cloud_workers": 2}
    record = _record(run, limits, thinking=lambda name: name == "local-thinking")
    assert record["selected_models"] == ["kimi-k3:cloud", "local-plain", "local-thinking"]
    assert record["targets"] == {"total": 3, "local": 2, "cloud": 1}
    assert record["execution"] == {
        "num_predict": 4096, "requested_num_predict": 512, "request_timeout_s": 10,
        "local_concurrency": 1, "cloud_concurrency": 2,
    }
    assert record["upper_bounds"]["initial_request_attempts_total"] == 3
    assert record["upper_bounds"]["initial_cloud_request_attempts"] == 1
    assert record["upper_bounds"]["scheduled_request_phase_wall_ms"] == 30_000
    assert record["cost"] == {
        "provider_pricing": "not_estimated",
        "reason": "the runtime has no trustworthy provider price schedule",
    }
    assert record["privacy"] == {
        "cloud_opt_in": True, "cloud_targets": ["kimi-k3:cloud"], "prompt_leaves_machine": True,
        "notice": "selected cloud targets receive the prompt; cloud calls require explicit operator opt-in",
    }


def test_a_local_only_run_discloses_no_cloud_target_and_keeps_the_requested_budget():
    limits = {"num_predict": 256, "timeout": 20, "cloud_workers": 2}
    record = _record({"models_json": json.dumps(["gemma3:12b"]), "cloud_opt_in": False}, limits)
    assert record["execution"]["num_predict"] == 256
    assert record["targets"] == {"total": 1, "local": 1, "cloud": 0}
    assert record["upper_bounds"]["scheduled_request_phase_wall_ms"] == 20_000
    assert record["privacy"]["prompt_leaves_machine"] is False
    assert record["privacy"]["notice"] == "no selected cloud target receives the prompt"
    assert _record({"models_json": "broken"}, limits)["targets"]["total"] == 0


def test_root_wrapper_injects_the_live_classifiers(monkeypatch):
    monkeypatch.setattr(server, "_is_cloud_model_name", lambda name: name == "far:away")
    monkeypatch.setattr(server, "_known_thinking_model", lambda name: name == "deep")
    run = {"models_json": json.dumps(["far:away", "deep"]), "cloud_opt_in": True}
    record = server._fanout_admission(run, [], {"num_predict": 64, "timeout": 5, "cloud_workers": 1})
    assert record["privacy"]["cloud_targets"] == ["far:away"]
    assert record["execution"]["num_predict"] == server.LOCAL_THINKING_MIN_NUM_PREDICT

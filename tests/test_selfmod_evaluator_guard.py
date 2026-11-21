from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import selfmod
import scripts.nightly_selfmod as nightly_selfmod
import scripts.selfmod_evaluator_broker as evaluator_broker
import scripts.selfmod_evaluator_client as evaluator_client
import sonder_runtime.adapters.inference.external_membership as membership
from sonder_runtime.adapters.inference.external_membership import ed25519_payload_bytes
from tests.test_nightly_selfmod_model_selection import _DirectStages, _drive_to_gates

_EVALUATOR = "EXAMPLE\\sonder-evaluator"
_WORKER = "EXAMPLE\\nightly-worker"
_ENV_NAMES = (
    nightly_selfmod._EVALUATOR_BROKER_ENV,
    nightly_selfmod._EVALUATOR_IDENTITY_ENV,
    nightly_selfmod._EVALUATOR_ISOLATION_ENV,
    nightly_selfmod._EVALUATOR_PUBLIC_KEY_ENV,
    nightly_selfmod._EVALUATOR_PRIVATE_KEY_ENV,
    nightly_selfmod._EVALUATOR_STRICT_ENV,
    evaluator_broker.CANDIDATE_IDENTITY_ENV,
    evaluator_broker.CANDIDATE_TASK_ENV,
    evaluator_client.INBOX_ENV,
    evaluator_client.OUTBOX_ENV,
    evaluator_client.TASK_ENV,
)


@pytest.fixture(autouse=True)
def _no_operator_broker(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _receipt_key(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = tmp_path / "evaluator-public.pem"
    public.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return key, public


def _configure_broker(monkeypatch, public, command=("broker.exe",)):
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_BROKER_ENV, json.dumps(list(command)))
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_IDENTITY_ENV, _EVALUATOR)
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_ISOLATION_ENV, "windows-scheduled-task")
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_PUBLIC_KEY_ENV, str(public))


def _plain_key_reads(monkeypatch):
    # _capture_authority also requires a private, link-free parent directory;
    # pytest's tmp_path is not provisioned that way.
    monkeypatch.setattr(membership, "_capture_authority",
                        lambda path, _maximum: open(path, "rb").read())


def _signed(key, payload):
    return {"schema": "sonder.selfmod.evaluator-receipt/v1", "payload": payload,
            "signature": base64.b64encode(key.sign(ed25519_payload_bytes(payload))).decode("ascii")}


def _held_out_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly_selfmod, "REPO", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_reflection.py").write_text(
        "from reflection import answer\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "reflection.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    return candidate


def test_evaluator_and_verifier_truth_sources_are_protected():
    protected = (
        "verifiers.py",
        "promotion_eval.py",
        "eval_retrieval.py",
        "ruff_verifier.py",
        "node_verifier.py",
        "json_schema_verifier.py",
        "sql_verifier.py",
        "scripts/nightly_selfmod.py",
        "scripts/nightly_self_improve.py",
        "scripts/check_architecture.py",
        "scripts/generate_documentation_catalogs.py",
        "tests/test_verifiers.py",
        "scripts/selfmod_evaluator_broker.py",
        "scripts/selfmod_evaluator_client.py",
        "sonder_runtime/adapters/inference/external_membership.py",
    )
    assert all(selfmod.is_protected_path(path) for path in protected)
    assert not set(protected) & set(nightly_selfmod._eligible_candidate_files())


# --- configuration: absent, complete, or refused before any run ----------------


def test_no_broker_configuration_leaves_the_lane_unchanged(tmp_path, monkeypatch):
    assert nightly_selfmod._broker_command() is None
    assert nightly_selfmod._evaluator_broker_refusal() is None
    # The supervised held-out runner keeps its candidate-root import canary.
    assert "target resolved outside candidate" in nightly_selfmod._HELD_OUT_RUNNER
    candidate = _held_out_repo(tmp_path, monkeypatch)
    prepared = nightly_selfmod._prepare_held_out("reflection.py", candidate, 30)
    try:
        assert "SELFMOD HELD-OUT CANARY" in prepared["command"][2]
        assert json.loads(prepared["command"][-1])["target_module"] == "reflection"
    finally:
        prepared["cleanup"].cleanup()


def test_strict_mode_without_a_broker_refuses_before_any_run(monkeypatch):
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_STRICT_ENV, "1")
    reason = nightly_selfmod._evaluator_broker_refusal()
    assert reason and nightly_selfmod._EVALUATOR_BROKER_ENV in reason

    created = []
    monkeypatch.setattr(nightly_selfmod.selfmod, "settings", lambda: {"enabled": True, "mode": "propose"})
    monkeypatch.setattr(nightly_selfmod, "_model_refusal", lambda *_a, **_k: "")
    monkeypatch.setattr(nightly_selfmod, "_isolation_refusal", lambda: None)
    monkeypatch.setattr(nightly_selfmod.selfmod, "create_plan", lambda *a, **k: created.append(a))
    result = nightly_selfmod.run(object(), lambda _m: None, stages=_DirectStages())
    assert result.startswith("isolated evaluator broker unavailable, no run started")
    assert created == []


def test_partial_broker_configuration_refuses_instead_of_dropping_the_gate(monkeypatch, tmp_path):
    _, public = _receipt_key(tmp_path)
    _configure_broker(monkeypatch, public)
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_ISOLATION_ENV, "same-user")
    assert nightly_selfmod._broker_command() is None
    assert nightly_selfmod._evaluator_broker_refusal()
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_ISOLATION_ENV, "windows-scheduled-task")
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_BROKER_ENV, "broker.exe")  # not a JSON argv
    assert nightly_selfmod._broker_command() is None
    assert nightly_selfmod._evaluator_broker_refusal()


def test_broker_configuration_requires_scheduled_task_identity_and_key(monkeypatch, tmp_path):
    _, public = _receipt_key(tmp_path)
    _configure_broker(monkeypatch, public)
    monkeypatch.setattr(nightly_selfmod, "_worker_identity", lambda: _WORKER)
    assert nightly_selfmod._broker_command() == ("broker.exe",)
    assert nightly_selfmod._evaluator_broker_refusal() is None

    monkeypatch.setattr(nightly_selfmod, "_worker_identity", lambda: _EVALUATOR)
    assert "own identity" in nightly_selfmod._evaluator_broker_refusal()
    monkeypatch.setattr(nightly_selfmod, "_worker_identity", lambda: _WORKER)

    monkeypatch.setenv(nightly_selfmod._EVALUATOR_PRIVATE_KEY_ENV, str(tmp_path / "private.pem"))
    assert "signing key" in nightly_selfmod._evaluator_broker_refusal()
    monkeypatch.delenv(nightly_selfmod._EVALUATOR_PRIVATE_KEY_ENV)

    monkeypatch.setenv(nightly_selfmod._EVALUATOR_PUBLIC_KEY_ENV, str(tmp_path / "missing.pem"))
    assert "public key" in nightly_selfmod._evaluator_broker_refusal()


# --- receipt validation ---------------------------------------------------------


def test_evaluator_receipt_must_attest_distinct_configured_identity(monkeypatch):
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_IDENTITY_ENV, _EVALUATOR)
    monkeypatch.setattr(nightly_selfmod, "_worker_identity", lambda: _WORKER)
    outcome = {
        "passed": True,
        "output": json.dumps({
            "schema": "sonder.selfmod.evaluator-receipt/v1",
            "payload": {
                "schema": "sonder.selfmod.evaluator-receipt/v1",
                "nonce": "n-1", "request_digest": "r-1", "manifest_digest": "m-1",
                "identity": _WORKER,
                "isolation": "windows-scheduled-task",
                "passed": True,
            },
            "signature": "invalid",
        }),
    }
    accepted, reason = nightly_selfmod._validate_evaluator_receipt(
        outcome, {"receipt_nonce": "n-1", "request_digest": "r-1", "manifest_digest": "m-1"}
    )
    assert not accepted
    assert "identity mismatch" in reason


def test_evaluator_receipt_accepts_matching_nonce_and_separate_identity(monkeypatch, tmp_path):
    key, public = _receipt_key(tmp_path)
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_IDENTITY_ENV, _EVALUATOR)
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_PUBLIC_KEY_ENV, str(public))
    monkeypatch.setattr(nightly_selfmod, "_worker_identity", lambda: _WORKER)
    _plain_key_reads(monkeypatch)
    payload = {
        "schema": "sonder.selfmod.evaluator-receipt/v1",
        "nonce": "n-1", "request_digest": "r-1", "manifest_digest": "m-1",
        "identity": _EVALUATOR, "isolation": "windows-scheduled-task", "passed": True,
    }
    request = {"receipt_nonce": "n-1", "request_digest": "r-1", "manifest_digest": "m-1"}
    accepted, reason = nightly_selfmod._validate_evaluator_receipt(
        {"passed": True, "output": json.dumps(_signed(key, payload))}, request)
    assert accepted
    assert reason == "broker evaluator receipt accepted"

    # A correctly signed receipt for another request is still refused.
    for field, value in (("nonce", "n-2"), ("request_digest", "r-2"), ("manifest_digest", "m-2")):
        other = dict(payload, **{field: value})
        accepted, _ = nightly_selfmod._validate_evaluator_receipt(
            {"passed": True, "output": json.dumps(_signed(key, other))}, request)
        assert not accepted, field
    # A non-zero broker exit is refused whatever it printed.
    accepted, _ = nightly_selfmod._validate_evaluator_receipt(
        {"passed": False, "output": json.dumps(_signed(key, payload))}, request)
    assert not accepted


def test_evaluator_receipt_rejects_forged_stdout(monkeypatch, tmp_path):
    _, public = _receipt_key(tmp_path)
    foreign = Ed25519PrivateKey.generate()
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_IDENTITY_ENV, _EVALUATOR)
    monkeypatch.setenv(nightly_selfmod._EVALUATOR_PUBLIC_KEY_ENV, str(public))
    monkeypatch.setattr(nightly_selfmod, "_worker_identity", lambda: _WORKER)
    _plain_key_reads(monkeypatch)
    payload = {"schema": "sonder.selfmod.evaluator-receipt/v1", "nonce": "n-1",
               "request_digest": "r-1", "manifest_digest": "m-1",
               "identity": _EVALUATOR, "isolation": "windows-scheduled-task", "passed": True}
    accepted, reason = nightly_selfmod._validate_evaluator_receipt(
        {"passed": True, "output": json.dumps(_signed(foreign, payload))},
        {"receipt_nonce": "n-1", "request_digest": "r-1", "manifest_digest": "m-1"},
    )
    assert not accepted
    assert "signature invalid" in reason


# --- the gate end to end against a stand-in broker command ---------------------


_SIGNING_BROKER = r'''
import base64, json, sys
from cryptography.hazmat.primitives import serialization
key = serialization.load_pem_private_key(open(sys.argv[1], "rb").read(), password=None)
request = json.loads(open(sys.argv[3], encoding="utf-8").read())
payload = {"schema": "sonder.selfmod.evaluator-receipt/v1", "nonce": request["nonce"],
           "request_digest": request["request_digest"],
           "manifest_digest": request["manifest_digest"],
           "identity": sys.argv[2], "isolation": "windows-scheduled-task", "passed": True}
raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
print("evaluator noise")
print(json.dumps({"schema": payload["schema"], "payload": payload,
                  "signature": base64.b64encode(key.sign(raw)).decode("ascii")}))
'''


def _signing_broker(tmp_path, key):
    private = tmp_path / "evaluator-private.pem"
    private.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    script = tmp_path / "signing_broker.py"
    script.write_text(_SIGNING_BROKER, encoding="utf-8")
    return [sys.executable, str(script), str(private)]


def test_broker_gate_accepts_a_signed_receipt_for_this_request(monkeypatch, tmp_path):
    candidate = _held_out_repo(tmp_path, monkeypatch)
    key, public = _receipt_key(tmp_path)
    command = (*_signing_broker(tmp_path, key), _EVALUATOR)
    _configure_broker(monkeypatch, public, command)
    monkeypatch.setattr(nightly_selfmod, "_worker_identity", lambda: _WORKER)
    _plain_key_reads(monkeypatch)
    held_out = nightly_selfmod._prepare_held_out("reflection.py", candidate, 30)
    try:
        passed, detail = nightly_selfmod._evaluator_broker_gate(
            "reflection.py", candidate, held_out, 30, nightly_selfmod._broker_command())
        assert (passed, detail) == (True, "broker evaluator receipt accepted")

        # The same broker signing as the wrong identity is refused.
        wrong = (*_signing_broker(tmp_path, key), _WORKER)
        passed, detail = nightly_selfmod._evaluator_broker_gate(
            "reflection.py", candidate, held_out, 30, wrong)
        assert not passed and "identity" in detail
    finally:
        held_out["cleanup"].cleanup()


def test_broker_gate_refuses_a_failed_or_missing_broker(monkeypatch, tmp_path):
    candidate = _held_out_repo(tmp_path, monkeypatch)
    _, public = _receipt_key(tmp_path)
    _configure_broker(monkeypatch, public)
    held_out = nightly_selfmod._prepare_held_out("reflection.py", candidate, 30)
    try:
        passed, detail = nightly_selfmod._evaluator_broker_gate(
            "reflection.py", candidate, held_out, 30,
            (sys.executable, "-c", "raise SystemExit(1)"))
        assert not passed and detail == "broker command failed"
        passed, detail = nightly_selfmod._evaluator_broker_gate(
            "reflection.py", candidate, held_out, 30,
            (str(tmp_path / "no-such-broker.exe"),))
        assert not passed and "could not start" in detail
    finally:
        held_out["cleanup"].cleanup()
    # No snapshot (an unavailable held-out suite) is a refusal, not a pass.
    passed, detail = nightly_selfmod._evaluator_broker_gate(
        "reflection.py", candidate, {"command": [], "cleanup": None}, 30, ("broker.exe",))
    assert not passed and "snapshot unavailable" in detail


def test_nightly_rejects_a_candidate_the_broker_refuses(tmp_path, monkeypatch):
    calls = _drive_to_gates(tmp_path, monkeypatch)
    _, public = _receipt_key(tmp_path)
    _configure_broker(monkeypatch, public)
    order = []
    # Give the run a parent-scored host grade and an independent oracle so the
    # broker's position relative to every supervised candidate gate is pinned.
    monkeypatch.setattr(nightly_selfmod, "_prepare_held_out", lambda *_a: {
        "source_paths": (), "command": ["python", "-c", "pass"], "cleanup": None,
        "protected_paths": (), "host_cases": ({"case": 1},)})
    oracle_path = tmp_path / "oracle.json"
    oracle_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(nightly_selfmod, "_load_oracle", lambda *_a: (
        SimpleNamespace(path=oracle_path, sha256="0" * 64), ""))

    def host_grade(*_a, **_k):
        order.append("host_grade")
        return {"passed": True}

    def oracle_grade(*_a, **_k):
        order.append("oracle_grade")
        return {"passed": True, "independent": True}
    monkeypatch.setattr(nightly_selfmod, "_parent_scored_gate", host_grade)
    monkeypatch.setattr(nightly_selfmod, "_independent_oracle_gate", oracle_grade)

    def gate(target, workspace, held_out, timeout, broker):
        order.append(("broker", [kind for kind, _ in calls["gates"]]))
        return False, "broker returned no signed evaluator receipt"
    monkeypatch.setattr(nightly_selfmod, "_evaluator_broker_gate", gate)

    lines = []
    result = nightly_selfmod.run(object(), lines.append, test_timeout=60,
                                 branch=True, stages=_DirectStages())
    assert result == ("candidate rejected: isolated evaluator broker refused "
                      "(broker returned no signed evaluator receipt)")
    # Every supervised gate ran first: the syntax/regression/held_out checks,
    # then the parent-scored host grade and the independent oracle.
    assert order[:2] == ["host_grade", "oracle_grade"] and len(order) == 3
    assert order[2][0] == "broker"
    assert "held_out" in order[2][1] and "regression" in order[2][1]
    logged = [line.strip().split(":")[0] for line in lines
              if line.strip().startswith(("host_grade: ", "oracle_grade: ",
                                          "evaluator_broker: "))]
    assert logged[-3:] == ["host_grade", "oracle_grade", "evaluator_broker"]
    assert calls["review"] == [] and calls["git"] == []
    assert calls["reject"] and "isolated evaluator broker refused" in calls["reject"][0]


def test_broker_acceptance_adds_a_gate_and_unlocks_nothing(tmp_path, monkeypatch):
    calls = _drive_to_gates(tmp_path, monkeypatch, mode="auto-low-risk")
    _, public = _receipt_key(tmp_path)
    _configure_broker(monkeypatch, public)
    monkeypatch.setattr(nightly_selfmod, "_evaluator_broker_gate",
                        lambda *_a: (True, "broker evaluator receipt accepted"))
    lines = []
    result = nightly_selfmod.run(object(), lines.append, test_timeout=60,
                                 branch=False, stages=_DirectStages())
    assert "READY for review" in result
    assert calls["approve"] == [] and calls["deploy"] == []
    assert any("evaluator_broker: pass -- broker evaluator receipt accepted" in line
               for line in lines)
    assert "held_out" in [kind for kind, _ in calls["gates"]]


def test_nightly_without_a_broker_never_calls_the_broker_gate(tmp_path, monkeypatch):
    calls = _drive_to_gates(tmp_path, monkeypatch)

    def gate(*_args):
        raise AssertionError("broker gate must not run without configuration")
    monkeypatch.setattr(nightly_selfmod, "_evaluator_broker_gate", gate)
    result = nightly_selfmod.run(object(), lambda _m: None, test_timeout=60,
                                 branch=True, stages=_DirectStages())
    assert "evaluator broker" not in result
    assert len(calls["review"]) == 1


# --- reference broker endpoint --------------------------------------------------


def test_reference_broker_refuses_to_run_as_the_worker_identity(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(evaluator_broker, "_identity", lambda: "worker")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema": "sonder.selfmod.evaluator-request/v1",
        "nonce": "n-1",
        "expected_evaluator_identity": "worker",
        "worker_identity": "worker",
        "isolation": "windows-scheduled-task",
    }), encoding="utf-8")
    assert evaluator_broker.main([str(request)]) == 1
    receipt = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert receipt["payload"]["passed"] is False and "signature" not in receipt
    assert "worker identity" in receipt["error"]


def test_reference_broker_accepts_a_nightly_request_then_fails_closed(monkeypatch, tmp_path, capsys):
    candidate = _held_out_repo(tmp_path, monkeypatch)
    _, public = _receipt_key(tmp_path)
    _configure_broker(monkeypatch, public)
    monkeypatch.setattr(nightly_selfmod, "_worker_identity", lambda: _WORKER)
    monkeypatch.setattr(evaluator_broker, "_identity", lambda: _EVALUATOR)
    held_out = nightly_selfmod._prepare_held_out("reflection.py", candidate, 30)
    request = nightly_selfmod._prepare_broker_request("reflection.py", candidate, held_out, 30)
    try:
        payload = json.loads(request["path"].read_text(encoding="utf-8"))
        # The broker recomputes the same manifest and request digests.
        evaluator_broker._static_request_checks(payload)
        assert payload["nonce"] == request["receipt_nonce"]

        monkeypatch.setenv(evaluator_broker.CANDIDATE_IDENTITY_ENV, "EXAMPLE\\sonder-candidate")
        monkeypatch.setenv(evaluator_broker.CANDIDATE_TASK_ENV, "SonderSelfmodCandidate")
        assert evaluator_broker.main([str(request["path"])]) == 1
        receipt = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert receipt["payload"]["passed"] is False and "signature" not in receipt
        assert "handoff is not provisioned" in receipt["error"]

        # A candidate edit after the request was written is refused.
        (candidate / "reflection.py").write_text("def answer():\n    return 0\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="target digest changed"):
            evaluator_broker._static_request_checks(payload)
    finally:
        request["cleanup"].cleanup()
        held_out["cleanup"].cleanup()


def test_reference_broker_rejects_truth_symlink_canary(tmp_path):
    root = tmp_path / "candidate"
    truth = tmp_path / "truth"
    root.mkdir()
    truth.mkdir()
    target = root / "reflection.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    real = truth / "real.py"
    real.write_text("VALUE = 1\n", encoding="utf-8")
    link = truth / "linked.py"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    payload = {
        "root": str(root), "suite_root": str(truth),
        "target_path": "reflection.py", "target_sha256": evaluator_broker._sha(target),
        "files": [{"path": str(link), "sha256": evaluator_broker._sha(real)}],
        "suites": [{"path": str(truth / "test_reflection.py")}],
    }
    payload["manifest_digest"] = hashlib.sha256(
        evaluator_broker._canonical(evaluator_broker._manifest(payload))
    ).hexdigest()
    payload["request_digest"] = hashlib.sha256(evaluator_broker._canonical(payload)).hexdigest()
    with pytest.raises(RuntimeError, match="truth snapshot changed"):
        evaluator_broker._static_request_checks(payload)


def test_reference_broker_once_answers_one_request_atomically(monkeypatch, tmp_path):
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    (inbox / ("a" * 32 + ".json")).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(evaluator_broker, "_identity", lambda: _EVALUATOR)
    assert evaluator_broker.main(["--once", str(inbox), str(outbox)]) == 1
    answered = json.loads((outbox / ("a" * 32 + ".json")).read_text(encoding="utf-8"))
    assert answered["payload"]["passed"] is False
    assert not list(outbox.glob("*.tmp"))
    # Already answered: nothing left to do.
    assert evaluator_broker.main(["--once", str(inbox), str(outbox)]) == 0


# --- worker-side client ---------------------------------------------------------


def _client_request(tmp_path, nonce="b" * 32):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema": "sonder.selfmod.evaluator-request/v1", "nonce": nonce, "timeout": 1,
    }), encoding="utf-8")
    return request


def test_client_refuses_without_provisioned_inbox_and_outbox(monkeypatch, tmp_path):
    request = _client_request(tmp_path)
    assert evaluator_client.main([str(request)]) == 1
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    outbox.mkdir()
    monkeypatch.setenv(evaluator_client.INBOX_ENV, str(inbox))
    monkeypatch.setenv(evaluator_client.OUTBOX_ENV, str(outbox))
    monkeypatch.setenv(evaluator_client.TASK_ENV, "SonderSelfmodEvaluator")
    monkeypatch.setattr(evaluator_client.subprocess, "run", lambda *_a, **_k: pytest.fail("launched"))
    assert evaluator_client.main([str(request)]) == 1
    assert not inbox.exists()


def test_client_rejects_a_malformed_nonce(monkeypatch, tmp_path):
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    monkeypatch.setenv(evaluator_client.INBOX_ENV, str(inbox))
    monkeypatch.setenv(evaluator_client.OUTBOX_ENV, str(outbox))
    monkeypatch.setenv(evaluator_client.TASK_ENV, "SonderSelfmodEvaluator")
    monkeypatch.setattr(evaluator_client.subprocess, "run", lambda *_a, **_k: pytest.fail("launched"))
    assert evaluator_client.main([str(_client_request(tmp_path, nonce="../escape"))]) == 1
    assert list(inbox.iterdir()) == []


def test_client_submits_to_the_task_and_prints_its_response(monkeypatch, tmp_path, capsys):
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    monkeypatch.setenv(evaluator_client.INBOX_ENV, str(inbox))
    monkeypatch.setenv(evaluator_client.OUTBOX_ENV, str(outbox))
    monkeypatch.setenv(evaluator_client.TASK_ENV, "SonderSelfmodEvaluator")
    nonce = "c" * 32
    launched = []

    def fake_run(argv, **_kwargs):
        launched.append(argv)
        assert (inbox / (nonce + ".json")).is_file()
        (outbox / (nonce + ".json")).write_text('{"schema": "receipt"}\n', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(evaluator_client.subprocess, "run", fake_run)
    assert evaluator_client.main([str(_client_request(tmp_path, nonce))]) == 0
    assert launched == [["schtasks.exe", "/Run", "/TN", "SonderSelfmodEvaluator"]]
    assert capsys.readouterr().out == '{"schema": "receipt"}\n'


def test_client_withdraws_the_request_when_the_task_cannot_start(monkeypatch, tmp_path):
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    monkeypatch.setenv(evaluator_client.INBOX_ENV, str(inbox))
    monkeypatch.setenv(evaluator_client.OUTBOX_ENV, str(outbox))
    monkeypatch.setenv(evaluator_client.TASK_ENV, "SonderSelfmodEvaluator")
    monkeypatch.setattr(evaluator_client.subprocess, "run",
                        lambda argv, **_k: subprocess.CompletedProcess(argv, 1, "", "denied"))
    assert evaluator_client.main([str(_client_request(tmp_path, "d" * 32))]) == 1
    assert list(inbox.iterdir()) == []

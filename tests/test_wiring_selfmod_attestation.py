"""Typed candidate isolation attestation and the nightly gate that consumes it.

Host-independent contract tests.  The real-supervisor path is covered end to
end by tests/test_wiring_selfmod_linux_nightly.py (Linux, root).
"""

from __future__ import annotations

import sys

import pytest

from sonder_runtime.application.selfmod.candidate_isolation import (
    IsolationAttestation,
    IsolationAttestationError,
    accepted_probe_attestation,
)


_NETWORK = {"isolation": "netns", "netns_inode": 4026532262,
            "supervisor_netns_inode": 4026531833, "interfaces": ["lo"],
            "loopback_up": False}
_SOCKET_FILTER = {"mechanism": "seccomp", "socket_families": [1, 2, 10, 16],
                  "io_uring": "denied"}
# The boundary fields a hand-built linux-uid attestation must carry.
_BOUNDARY = {"network_isolated": True, "no_new_privs": True,
             "socket_families_filtered": True}


def _linux_result(**job):
    return {"exit_code": 0, "passed": True, "output": "ok",
            "job": {"integrity": "linux-uid", "uid": 210_000, "gid": 210_000,
                    "network": dict(_NETWORK), "no_new_privs": True,
                    "socket_filter": dict(_SOCKET_FILTER), **job}}


def test_linux_uid_attestation_requires_distinct_unprivileged_uid():
    typed = IsolationAttestation.from_supervisor_result(
        _linux_result(supervisor_uid=0), expected_kind="linux-uid", supervisor_uid=0,
    )
    assert typed.kind == "linux-uid" and typed.candidate_uid == 210_000 and typed.passed
    assert typed.network_isolated is True and typed.no_new_privs is True
    assert typed.socket_families_filtered is True
    assert typed.as_record()["socket_families_filtered"] is True
    for bad in ({"uid": 0}, {"uid": None}, {"uid": "210000"}, {"supervisor_uid": 5},
                {"gid": 0}):
        with pytest.raises(IsolationAttestationError):
            IsolationAttestation.from_supervisor_result(
                _linux_result(**bad), expected_kind="linux-uid", supervisor_uid=0,
            )
    with pytest.raises(IsolationAttestationError):
        # The candidate uid may never be the supervisor's own uid.
        IsolationAttestation.from_supervisor_result(
            _linux_result(), expected_kind="linux-uid", supervisor_uid=210_000,
        )


@pytest.mark.parametrize("job", [
    {"network": None},
    {"network": {**_NETWORK, "isolation": "host"}},
    {"network": {**_NETWORK, "netns_inode": _NETWORK["supervisor_netns_inode"]}},
    {"network": {**_NETWORK, "netns_inode": "4026532262"}},
    {"network": {**_NETWORK, "supervisor_netns_inode": 0}},
    {"network": {**_NETWORK, "interfaces": ["eth0", "lo"]}},
    {"network": {**_NETWORK, "loopback_up": True}},
    {"network": {**_NETWORK, "loopback_up": None}},
    {"no_new_privs": False},
    {"no_new_privs": 1},
    {"no_new_privs": None},
    # The namespace does not scope AF_VSOCK and friends; only the exact
    # allow-list of namespace-scoped families counts.
    {"socket_filter": None},
    {"socket_filter": {**_SOCKET_FILTER, "mechanism": "none"}},
    {"socket_filter": {**_SOCKET_FILTER, "socket_families": [1, 2, 10, 16, 40]}},
    {"socket_filter": {**_SOCKET_FILTER, "socket_families": [2, 1, 10, 16]}},
    {"socket_filter": {**_SOCKET_FILTER, "io_uring": "allowed"}},
])
def test_linux_uid_attestation_requires_network_namespace_and_no_new_privs(job):
    with pytest.raises(IsolationAttestationError):
        IsolationAttestation.from_supervisor_result(
            _linux_result(**job), expected_kind="linux-uid", supervisor_uid=0,
        )


def test_hand_built_linux_uid_attestation_requires_both_boundaries():
    identity = {"supervisor_uid": 0, "candidate_uid": 210_000}
    assert IsolationAttestation("linux-uid", 0, True, **identity, **_BOUNDARY).passed
    for missing in ({**_BOUNDARY, "network_isolated": False},
                    {**_BOUNDARY, "no_new_privs": False},
                    {**_BOUNDARY, "network_isolated": 1},
                    {**_BOUNDARY, "socket_families_filtered": False},
                    {**_BOUNDARY, "socket_families_filtered": 1}):
        with pytest.raises(IsolationAttestationError):
            IsolationAttestation("linux-uid", 0, True, **identity, **missing)


def test_attested_socket_families_match_the_filter_the_supervisor_installs():
    from scripts import selfmod_linux_isolation as linux
    from sonder_runtime.application.selfmod import candidate_isolation

    assert linux._socket_filter_report() == _SOCKET_FILTER
    assert tuple(_SOCKET_FILTER["socket_families"]) == candidate_isolation.ATTESTED_SOCKET_FAMILIES
    assert linux.SOCKET_FILTER == candidate_isolation.SOCKET_FILTER


def test_neither_supervisor_can_vouch_for_the_other():
    with pytest.raises(IsolationAttestationError):
        IsolationAttestation.from_supervisor_result(
            _linux_result(), expected_kind="low", supervisor_uid=None,
        )
    with pytest.raises(IsolationAttestationError):
        IsolationAttestation.from_supervisor_result(
            {"exit_code": 0, "passed": True, "job": {"integrity": "low"}},
            expected_kind="linux-uid", supervisor_uid=0,
        )
    with pytest.raises(IsolationAttestationError):
        IsolationAttestation("linux-uid", 0, True)  # hand-built without identity


def test_pass_flag_and_integrity_failure_must_agree_with_exit_status():
    with pytest.raises(IsolationAttestationError):
        IsolationAttestation.from_supervisor_result(
            {"exit_code": 1, "passed": True, "job": {"integrity": "low"}},
            expected_kind="low", supervisor_uid=None,
        )
    failed = IsolationAttestation.from_supervisor_result(
        {"exit_code": 2, "passed": False, "integrity_failed": True,
         "job": {"integrity": "low"}},
        expected_kind="low", supervisor_uid=None,
    )
    assert failed.integrity_failed and not failed.passed


@pytest.mark.parametrize("selected,recorded,kind,accepted", [
    ("linux-uid", "linux-uid", "linux-uid", True),
    ("low", "low", "low", True),
    ("low", "linux-uid", "linux-uid", False),
    ("linux-uid", "low", "low", False),
    ("linux-uid", "linux-uid", None, False),
])
def test_gate_accepts_only_the_selected_supervisors_typed_attestation(
    selected, recorded, kind, accepted,
):
    attestation = None
    if kind == "linux-uid":
        attestation = IsolationAttestation("linux-uid", 0, True, supervisor_uid=0,
                                           candidate_uid=210_000, **_BOUNDARY)
    elif kind == "low":
        attestation = IsolationAttestation("low", 0, True)
    probe = {"passed": True, "isolation": recorded, "attestation": attestation,
             "test_id": 1}
    assert (accepted_probe_attestation(probe, selected_kind=selected) is not None) is accepted


def test_nightly_parent_gate_accepts_linux_uid_probe_on_configured_linux(monkeypatch, tmp_path):
    from scripts import nightly_selfmod, selfmod_host_grader
    from scripts import selfmod_linux_isolation as linux

    monkeypatch.setattr(linux.sys, "platform", "linux")
    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, "210000")
    monkeypatch.setattr(nightly_selfmod, "_test_python", lambda: sys.executable)
    graded = []

    def probe(run_id, kind, command, **kwargs):
        return {"passed": True, "output": "", "isolation": "linux-uid", "test_id": 3,
                "attestation": IsolationAttestation(
                    "linux-uid", 0, True, supervisor_uid=0, candidate_uid=210_000,
                    **_BOUNDARY)}

    monkeypatch.setattr(nightly_selfmod, "_record_candidate_test", probe)
    monkeypatch.setattr(selfmod_host_grader, "grade", lambda *_a: (False, "graded"))
    monkeypatch.setattr(nightly_selfmod.selfmod, "record_host_grade",
                        lambda run_id, probe_id, **kw: graded.append(probe_id) or kw)
    held_out = {"host_cases": ({"args": [], "kwargs": {}, "expected": 1},),
                "protected_paths": ()}
    nightly_selfmod._parent_scored_gate("r", tmp_path, "reflection.py", "f", held_out, 5)
    # The linux-uid probe reached the grader and its row was graded.
    assert graded == [3]

    graded.clear()
    monkeypatch.delenv(linux.CANDIDATE_UID_ENV)
    result = nightly_selfmod._parent_scored_gate("r", tmp_path, "reflection.py", "f", held_out, 5)
    # Without a configured uid the Windows supervisor is selected; a
    # linux-uid probe cannot satisfy it.
    assert result == {"passed": False, "detail": "parent challenge lacked an attested low probe"}
    assert graded == []


def test_journaled_stage_fails_closed_without_a_composed_binding():
    from sonder_runtime.application.selfmod.selfmod_service import GuardedLegacySelfmodService
    from sonder_runtime.domain.common.errors import Forbidden, InvalidInput

    service = GuardedLegacySelfmodService(object())
    with pytest.raises(Forbidden):
        service.journaled_stage("r", "record_test", {}, lambda: pytest.fail("ran unjournaled"))
    composed = GuardedLegacySelfmodService(object(), effect_binding_factory=lambda _r: None)
    with pytest.raises(InvalidInput):
        composed.journaled_stage("r", "not-a-stage", {}, lambda: {})


def test_preflight_names_the_setting_and_the_docs(monkeypatch):
    from scripts import selfmod_linux_isolation as linux

    monkeypatch.setattr(linux.sys, "platform", "linux")
    monkeypatch.setattr(linux.os, "name", "posix")
    monkeypatch.delenv(linux.CANDIDATE_UID_ENV, raising=False)
    message = linux.candidate_isolation_preflight()
    assert linux.CANDIDATE_UID_ENV in message
    assert linux.ISOLATION_DOC in message
    monkeypatch.setattr(linux.sys, "platform", "darwin")
    assert "unsupported platform" in linux.candidate_isolation_preflight()

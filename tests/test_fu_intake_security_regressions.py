"""Regression tests for intake security/privacy follow-up findings (PRs #824, #831, #840, #842 F2, #844, #845)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import artifact_fetch
from sonder_runtime.adapters.inference import anthropic_spend
from sonder_runtime.adapters.inference.anthropic_spend import SpendLedger
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
    _bypasses_proxy,
    _host_is_loopback,
)
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.adapters.web_provider import LegacyWebProvider
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.web import EgressPolicy, WebRequest
from sonder_runtime.application.security.recovery_artifacts import (
    RecoveryArtifactIntegrityError,
    RecoveryArtifactService,
    _canonical,
    _digest,
)
from sonder_runtime.application.session.continuity import SessionContinuityService
from sonder_runtime.application.session.query_export import SessionQueryEngine
from sonder_runtime.bootstrap import mesh_agent_tools
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.openai_spend_policy import ChatPrice
import local_service_probe as probe
import web_tools


# --- PR #824: OpenAI-compatible config validation, loopback unification, cross-ledger reservations ---


@pytest.mark.parametrize(
    "kwargs",
    [
        {"daily_usd": Decimal("Infinity")},
        {"daily_usd": Decimal("-Infinity")},
        {"daily_usd": Decimal("NaN")},
        {"daily_usd": Decimal("-0.01")},
        {"daily_usd": Decimal("1000001")},
        {"daily_usd": True},
        {"max_tokens_ceiling": -5},
        {"max_tokens_ceiling": 0},
        {"max_tokens_ceiling": True},
        {"max_tokens_ceiling": 1_000_001},
        {"price": ChatPrice(Decimal("-1"), Decimal("1"))},
        {"price": ChatPrice(Decimal("1"), Decimal("-0.5"))},
        {"price": ChatPrice(Decimal("NaN"), Decimal("1"))},
        {"price": ChatPrice(Decimal("Infinity"), Decimal("0"))},
        {"price": ChatPrice(Decimal("10001"), Decimal("0"))},
        {"embed_price": Decimal("-0.01")},
        {"embed_price": Decimal("NaN")},
        {"embed_price": Decimal("Infinity")},
        {"embed_price": Decimal("10001")},
    ],
)
def test_openai_config_post_init_rejects_invalid_spend_fields(kwargs):
    with pytest.raises(InvalidInput):
        OpenAICompatibleConfig(base_url="https://api.example.com", **kwargs)


@pytest.mark.parametrize(
    ("url", "expected_loopback", "expected_bypass"),
    [
        ("https://127.0.0.1:8443/v1", True, True),
        ("https://127.0.0.2:8443/v1", True, True),
        ("https://localhost.:8443/v1", True, True),
        ("https://[::1]:8443/v1", True, True),
        ("https://0.0.0.0:8443/v1", False, False),
        ("https://localhost..:8443/v1", False, False),
        ("ftp://localhost:21/v1", False, False),
        ("https://user:pass@127.0.0.1:8443/v1", False, False),
        ("https://api.example.com/v1", False, False),
        ("http://api.example.com/v1", False, True),
    ],
)
def test_loopback_and_proxy_bypass_agree(url, expected_loopback, expected_bypass):
    assert OpenAICompatibleGateway._is_loopback(url) is expected_loopback
    assert _bypasses_proxy(url) is expected_bypass


def test_host_is_loopback_helper():
    assert _host_is_loopback("localhost.") is True
    assert _host_is_loopback("127.0.0.2") is True
    assert _host_is_loopback("::1") is True
    assert _host_is_loopback("0.0.0.0") is False
    assert _host_is_loopback("::") is False
    assert _host_is_loopback("localhost..") is False


def test_spend_ledger_persists_reservations_across_instances_and_prunes_stale(tmp_path, monkeypatch):
    now = [1_700_000_000.0]
    path = tmp_path / "openai-spend.json"
    ledgers = [
        SpendLedger(
            path, clock=lambda: now[0], filename="openai-spend.json",
            schema="sonder.openai_spend.v1", label="OpenAI-compatible",
            reservation_ttl_seconds=60.0,
        )
        for _ in range(4)
    ]
    cap = Decimal("0.005")
    amt = Decimal("0.002")

    tokens = []
    for ledger in ledgers:
        tok, _spent, _inflight = ledger.try_reserve(amt, cap)
        if tok is not None:
            tokens.append((ledger, tok))
    # Cap is $0.005 and each reservation is $0.002 -> at most 2 across separate instances.
    assert len(tokens) == 2

    # Release one -> third instance can now reserve.
    tokens[0][0].release(tokens[0][1])
    tok3, _spent, inflight3 = ledgers[2].try_reserve(amt, cap)
    assert tok3 is not None and inflight3 == amt
    ledgers[2].release(tok3)

    # Advance past TTL -> remaining un-released reservation is pruned automatically.
    now[0] += 61.0
    tok4, _spent, inflight4 = ledgers[3].try_reserve(amt, cap)
    assert tok4 is not None and inflight4 == Decimal(0)
    ledgers[3].release(tok4)

    # Dead foreign PID reservation is also pruned even before TTL expiry.
    doc = json.loads(path.read_text(encoding="utf-8"))
    day = next(iter(doc["days"]))
    doc["days"][day]["reserved"] = {
        "999999:foreign:1": {"usd": "0.004", "pid": 999999, "expires_at": now[0] + 600.0},
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(anthropic_spend, "pid_alive", lambda pid: False)
    tok5, _spent, inflight5 = ledgers[0].try_reserve(amt, cap)
    assert tok5 is not None and inflight5 == Decimal(0)
    ledgers[0].release(tok5)


# --- PR #831: artifact_fetch provenance non-dict JSON & empty prior_digest on non-regular dest ---


@pytest.mark.parametrize("raw_sidecar", ['["x"]', '"text"', "123", "true"])
def test_artifact_fetch_non_object_provenance_rejects_cleanly(tmp_path, monkeypatch, raw_sidecar):
    dest = tmp_path / "pkg.zip"
    dest.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    sidecar = artifact_fetch._provenance_path(dest)
    sidecar.write_text(raw_sidecar, encoding="utf-8")

    assert artifact_fetch.read_provenance(dest) is None
    monkeypatch.setattr(artifact_fetch, "_web_tools", lambda: SimpleNamespace(enabled=lambda: True))
    monkeypatch.setattr(artifact_fetch.file_ops, "_require_mutation_access", lambda *_a: None)
    monkeypatch.setattr(artifact_fetch.file_ops, "require_read_access", lambda p, **_k: p)
    result = artifact_fetch.fetch_artifact(
        "https://example.com/pkg.zip",
        dest,
        extra_roots=str(tmp_path),
        bypass=True,
    )
    assert result["ok"] is False
    assert result["verdict"] == "rejected"
    assert any(c["check"] == "provenance" and not c["ok"] for c in result["checks"])
    assert result.get("reused") is not True


def test_artifact_fetch_directory_with_empty_prior_digest_not_marked_reused(tmp_path, monkeypatch):
    dest = tmp_path / "pkg_dir"
    dest.mkdir()
    url = "https://example.com/pkg.zip"
    sidecar = artifact_fetch._provenance_path(dest)
    sidecar.write_text(json.dumps({"url": url, "sha256": ""}), encoding="utf-8")

    monkeypatch.setattr(artifact_fetch, "_web_tools", lambda: SimpleNamespace(enabled=lambda: True))
    monkeypatch.setattr(artifact_fetch.file_ops, "_require_mutation_access", lambda *_a: None)
    monkeypatch.setattr(artifact_fetch.file_ops, "require_read_access", lambda p, **_k: p)
    result = artifact_fetch.fetch_artifact(
        url,
        dest,
        extra_roots=str(tmp_path),
        bypass=True,
    )
    assert result["ok"] is False
    assert result["verdict"] == "rejected"
    assert result.get("reused") is not True
    assert any(c["check"] == "provenance" and not c["ok"] for c in result["checks"])


# --- PR #840: web_provider overall deadline & local_service_probe ';' query separator ---


def test_web_provider_attaches_shared_deadline_and_bounds_slow_drip_peer():
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]
    stop = threading.Event()

    def serve_drip():
        try:
            conn, _ = server_sock.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(4096)
                payload = b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\n" + b"x" * 40
                for byte in payload:
                    if stop.is_set():
                        return
                    conn.sendall(bytes([byte]))
                    time.sleep(0.08)
            except OSError:
                return

    worker = threading.Thread(target=serve_drip, daemon=True)
    worker.start()

    class _LocalTransport:
        USER_AGENT = web_tools.USER_AGENT
        _urlopen = staticmethod(web_tools._urlopen)
        _decode_content_encoding = staticmethod(web_tools._decode_content_encoding)

        @staticmethod
        def enabled():
            return True

        @staticmethod
        def _validated_public_target(url):
            return SimpleNamespace(), ("127.0.0.1",)

    provider = LegacyWebProvider(_LocalTransport())
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            provider.request(
                WebRequest(f"http://127.0.0.1:{port}/drip"),
                EgressPolicy(allowed_hosts=("127.0.0.1",), schemes=("http",)),
                local_owner_context(correlation_id="web-drip", cloud_allowed=True, timeout_seconds=0.5),
            )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"slow-drip request bypassed overall deadline ({elapsed:.2f}s)"
    finally:
        stop.set()
        server_sock.close()
        worker.join(timeout=2)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/x?q=1;token=secret",
        "http://127.0.0.1:8080/x?q=1;api_key=abc",
        "http://127.0.0.1:8080/x?a=1;authorization=xyz",
    ],
)
def test_local_service_probe_rejects_semicolon_credential_query_params(url):
    with pytest.raises(ValueError, match="credential query parameters"):
        probe._validate_target(url)


def test_local_service_probe_encodes_raw_semicolon_on_wire(monkeypatch):
    class _FakeSocket:
        def __init__(self):
            self.sent = bytearray()
            self._reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"

        def settimeout(self, _t):
            pass

        def connect(self, _addr):
            pass

        def sendall(self, data):
            self.sent.extend(data)

        def recv(self, size):
            chunk, self._reply = self._reply[:size], self._reply[size:]
            return chunk

        def close(self):
            pass

    fake = _FakeSocket()
    monkeypatch.setattr(probe.socket, "socket", lambda *a, **k: fake)
    parsed, addresses = probe._validate_target("http://127.0.0.1:8080/x?q=1;safe=2")
    probe._request_once(parsed, addresses, "GET", 2.0)
    request_line = bytes(fake.sent).split(b"\r\n", 1)[0]
    assert b"%3B" in request_line
    assert b";" not in request_line


# --- PR #842 F2: mesh_agent_tools._output_root confines to default_asset_root + lexical UNC check ---


def test_mesh_output_root_confines_to_asset_root_and_rejects_unc_before_realpath(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    asset_root = ws / "artifacts" / "generated"
    asset_root.mkdir(parents=True)
    rt = SimpleNamespace(
        assetgen=SimpleNamespace(
            workspace_root=lambda: str(ws),
            default_asset_root=lambda: str(asset_root),
        )
    )

    # Inside workspace but outside asset_root -> refused.
    for bad_rel in ("app", "sonder_runtime", "../outside"):
        with pytest.raises(mesh_agent_tools.SonderError, match="inside the asset workspace"):
            mesh_agent_tools._output_root(rt, bad_rel, "hero")

    # Default and explicit subdirectory inside asset_root -> accepted.
    default_target = mesh_agent_tools._output_root(rt, "", "hero")
    assert asset_root in default_target.parents
    custom_target = mesh_agent_tools._output_root(rt, "artifacts/generated/custom", "hero")
    assert asset_root in custom_target.parents

    # UNC / device / NT spellings are refused lexically BEFORE os.path.realpath is called.
    realpath_calls: list[str] = []
    real_realpath = os.path.realpath

    def spy_realpath(p):
        realpath_calls.append(str(p))
        return real_realpath(p)

    monkeypatch.setattr(mesh_agent_tools.os.path, "realpath", spy_realpath)
    for hostile in (r"\\attacker\share\x", "//attacker/share/x", r"\\?\UNC\attacker\share", r"\\.\pipe\x", r"\??\C:\x"):
        realpath_calls.clear()
        with pytest.raises(mesh_agent_tools.SonderError, match="refused"):
            mesh_agent_tools._output_root(rt, hostile, "hero")
        assert realpath_calls == [], f"realpath was called for {hostile!r}: {realpath_calls}"


# --- PR #844: recovery_artifacts all 4 integrity findings ---


def test_recovery_artifacts_write_refuses_malformed_tail_and_missing_newline(tmp_path):
    svc = RecoveryArtifactService(tmp_path / "rec", owner="alice")
    svc.write("run-1", actor="alice", kind="backup", payload={"step": 1})

    # Finding 3: missing trailing newline on non-empty audit.jsonl -> RecoveryArtifactIntegrityError.
    raw = svc.audit_path.read_bytes()
    assert raw.endswith(b"\n")
    svc.audit_path.write_bytes(raw.rstrip(b"\n"))
    with pytest.raises(RecoveryArtifactIntegrityError, match="newline"):
        svc.write("run-2", actor="alice", kind="backup", payload={"step": 2})
    assert not (tmp_path / "rec" / "run-2.json").exists()

    # Finding 1a: missing audit_digest key on tail entry -> RecoveryArtifactIntegrityError.
    entry = json.loads(raw.decode("utf-8").strip())
    entry_missing = {k: v for k, v in entry.items() if k != "audit_digest"}
    svc.audit_path.write_bytes((json.dumps(entry_missing) + "\n").encode("utf-8"))
    with pytest.raises(RecoveryArtifactIntegrityError, match="malformed"):
        svc.write("run-2", actor="alice", kind="backup", payload={"step": 2})

    # Finding 1b: empty audit_digest string on tail entry -> RecoveryArtifactIntegrityError.
    entry_empty = {**entry, "audit_digest": ""}
    svc.audit_path.write_bytes((json.dumps(entry_empty) + "\n").encode("utf-8"))
    with pytest.raises(RecoveryArtifactIntegrityError, match="malformed"):
        svc.write("run-2", actor="alice", kind="backup", payload={"step": 2})


def test_recovery_artifacts_inspect_checks_audit_owner_and_raises_integrity_error_on_blank_fields(tmp_path):
    svc = RecoveryArtifactService(tmp_path / "rec", owner="alice")
    art = svc.write("run-1", actor="alice", kind="backup", payload={"step": 1})

    # Finding 2: self-consistent audit entry with foreign owner must fail verification.
    forged_entry = {
        "artifact_id": "run-1",
        "owner": "mallory",
        "artifact_digest": art.digest,
        "previous": "",
    }
    forged_digest = _digest(forged_entry)
    forged_entry["audit_digest"] = forged_digest
    svc.audit_path.write_bytes((json.dumps(forged_entry, sort_keys=True) + "\n").encode("utf-8"))
    meta_path = svc._metadata_path("run-1")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["audit_digest"] = forged_digest
    meta_path.write_bytes(_canonical(meta))

    inspection = svc.inspect("run-1", actor="alice")
    assert inspection.audit_chain_matches is False
    assert inspection.verified is False

    # Finding 4: tampered body with empty/whitespace/NUL owner or kind raises RecoveryArtifactIntegrityError.
    body_path = svc._path("run-1")
    for bad_body in (
        {"artifact_id": "run-1", "owner": "", "kind": "backup", "payload": {}},
        {"artifact_id": "run-1", "owner": "   ", "kind": "backup", "payload": {}},
        {"artifact_id": "run-1", "owner": "alice\x00", "kind": "backup", "payload": {}},
        {"artifact_id": "run-1", "owner": "alice", "kind": "", "payload": {}},
        {"artifact_id": "run-1", "owner": "alice", "kind": "bad\x00kind", "payload": {}},
    ):
        body_path.write_bytes(_canonical(bad_body))
        with pytest.raises(RecoveryArtifactIntegrityError):
            svc.inspect("run-1", actor="alice")


# --- PR #845: query_export & continuity pagination past marker scan limit ---


def test_query_export_and_continuity_paginate_retention_markers_past_limit(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=5)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    # Create 6 earlier retention markers (> limit=5) plus a secret event withheld by marker #7.
    secret_event = repo.append("s1", "user.message", {"content": "WITHHELD_PAST_LIMIT", "turn_id": "t-secret"})
    for idx in range(6):
        dummy = repo.append("s1", "user.message", {"content": f"old-{idx}", "turn_id": f"t-{idx}"})
        repo.append(
            "s1",
            SessionContinuityService.RETENTION_MARKER_EVENT,
            {
                "privacy_class": "public_metadata",
                "action": "redact_or_expire",
                "targets": [
                    {
                        "event_id": dummy.event_id,
                        "sequence": dummy.sequence,
                        "privacy_class": "sensitive_content",
                        "expires_at_utc": old_ts,
                    }
                ],
            },
        )
    repo.append(
        "s1",
        SessionContinuityService.RETENTION_MARKER_EVENT,
        {
            "privacy_class": "public_metadata",
            "action": "redact_or_expire",
            "targets": [
                {
                    "event_id": secret_event.event_id,
                    "sequence": secret_event.sequence,
                    "privacy_class": "sensitive_content",
                    "expires_at_utc": old_ts,
                }
            ],
        },
    )

    engine = SessionQueryEngine(repo, max_page_size=5, max_scan=5)
    # Plain query, text search, and export must all redact the 7th-marker withheld event.
    assert engine.query_events("s1", text="WITHHELD_PAST_LIMIT", page_size=5).records == ()
    first_page = engine.query_events("s1", page_size=1).records
    assert len(first_page) == 1 and first_page[0].redacted is True
    assert "WITHHELD_PAST_LIMIT" not in json.dumps(first_page[0].payload)
    exported = engine.export_events("s1", max_events=5)
    assert exported.events[0].redacted is True
    assert "WITHHELD_PAST_LIMIT" not in json.dumps(exported.events[0].payload)

    # Continuity execute_retention must also see all 7 markers past limit=5 and not re-mark applied candidates.
    continuity = SessionContinuityService(repo, max_events=100)
    assert len(continuity._retention_markers("s1")) == 7
    fake_candidate = SimpleNamespace(
        event_id=secret_event.event_id,
        sequence=secret_event.sequence,
        privacy_class=SimpleNamespace(value="sensitive_content"),
        expires_at_utc=old_ts,
    )
    continuity.retention_candidates = lambda *_a, **_k: (fake_candidate,)  # type: ignore[method-assign]
    result = continuity.execute_retention(
        "s1", context=local_owner_context(correlation_id="ret-past-limit"),
    )
    assert result.applied == ()
    assert result.marker is None

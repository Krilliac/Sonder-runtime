"""Process-boundary coverage for the explicit control-state rehearsal command.

The loopback fixture is deliberately only a disposable transport double.  It
does not represent an independent witness, an elected coordinator, or proof of
automatic failover/failback.
"""
from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
import os
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from sonder_runtime.__main__ import main as runtime_main
from sonder_runtime.domain.cluster_availability import (
    ControlStateEvent,
    FenceReceipt,
    PartitionState,
    ReplicationAcknowledgement,
)


_TEST_KEY = "control-state-rehearsal-test-key"
_DIGEST = "a" * 64


def _write_rehearsal_inputs(
    root: Path,
    *,
    origin: str,
    local_id: str = "node-a",
    peer_id: str = "node-b",
    witness_id: str = "witness-a",
    cluster_id: str = "rehearsal-command",
) -> tuple[Path, Path, Path]:
    """Create disposable, explicitly loopback-only command inputs."""
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state"
    config_path = root / "sonder.toml"
    secrets_path = root / "sonder.env"
    config_path.write_text(
        "\n".join(
            (
                'profile = "server-private"',
                "",
                "[state]",
                f"home = {json.dumps(str(state_path))}",
                "workspace_roots = []",
                "",
                "[compute]",
                "allow_remote = true",
                f'node_id = "{local_id}"',
                "nodes = [{ "
                f'id = "{peer_id}", '
                'origin = "https://peer.example.test:8443", '
                'workloads = ["build"], '
                'capabilities = ["cpu"], '
                'workspace_mappings = ["default"] '
                "}]",
                "",
                "[deployment]",
                'profile = "pooled-pair"',
                f'preferred_primary = "{local_id}"',
                "",
                "[control_state_rehearsal]",
                "enabled = true",
                f'cluster_id = "{cluster_id}"',
                f'node_id = "{local_id}"',
                f'witness_id = "{witness_id}"',
                'provider_id = "provider-test"',
                f"origin = {json.dumps(origin)}",
                "timeout_seconds = 5",
                "allow_insecure_loopback = true",
                "",
            )
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(
        "\n".join(
            (
                "SONDER_API_KEY=" + "a" * 24,
                "SONDER_CONTROL_STATE_REHEARSAL_API_KEY=" + _TEST_KEY,
                "",
            )
        ),
        encoding="utf-8",
    )
    return config_path, secrets_path, state_path


def _command_args(
    config_path: Path,
    secrets_path: Path,
    *,
    event_id: str = "rehearsal-event-a",
    resource_kind: str = "job",
    resource_id: str = "rehearsal-job-a",
    owner_epoch: int = 3,
    sequence: int = 7,
    payload_digest: str = _DIGEST,
    confirm_fence: str | None = None,
    new_owner_id: str | None = None,
) -> list[str]:
    args = [
        "control-state-rehearsal",
        "--config",
        str(config_path),
        "--secrets",
        str(secrets_path),
        "--json",
        "--event-id",
        event_id,
        "--resource-kind",
        resource_kind,
        "--resource-id",
        resource_id,
        "--owner-epoch",
        str(owner_epoch),
        "--sequence",
        str(sequence),
        "--payload-digest",
        payload_digest,
    ]
    if confirm_fence is not None:
        args.extend(("--confirm-fence", confirm_fence))
    if new_owner_id is not None:
        args.extend(("--new-owner-id", new_owner_id))
    return args


def _report(capsys) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def _assert_safe_report(
    report: dict[str, object], *, forbidden: tuple[str, ...] = ()
) -> None:
    rendered = json.dumps(report, sort_keys=True)
    assert report["promotion_attempted"] is False
    assert report["automatic_takeover_available"] is False
    assert report["automatic_failback_available"] is False
    assert report["evidence_scope"] == "process-boundary-transport-rehearsal"
    for value in forbidden:
        assert value not in rendered


@dataclass
class _RecordingCoordinator:
    local_id: str
    peer_id: str
    witness_id: str

    def __post_init__(self) -> None:
        self.appended: list[ControlStateEvent] = []
        self.reads: list[tuple[str, int, int]] = []
        self.fences: list[tuple[object, ControlStateEvent, str, object]] = []
        self.capabilities = SimpleNamespace(
            data_replica_ids=(self.local_id, self.peer_id),
            witness_ids=(self.witness_id,),
            provider_id="provider-test",
        )

    def append(self, event: ControlStateEvent) -> ReplicationAcknowledgement:
        self.appended.append(event)
        return ReplicationAcknowledgement(
            event_id=event.event_id,
            cluster_id=event.cluster_id,
            owner_epoch=event.owner_epoch,
            sequence=event.sequence,
            provider_id="provider-test",
            protocol_version=event.protocol_version,
            data_replica_ids=(self.local_id, self.peer_id),
            witness_ids=(self.witness_id,),
            durable=True,
        )

    def read(
        self, cluster_id: str, *, after_sequence: int, limit: int
    ) -> tuple[ControlStateEvent, ...]:
        self.reads.append((cluster_id, after_sequence, limit))
        return tuple(
            event
            for event in self.appended
            if event.cluster_id == cluster_id and event.sequence > after_sequence
        )[:limit]

    def prepare_takeover(
        self,
        scope,
        event: ControlStateEvent,
        *,
        new_owner_id: str,
        acknowledgement: ReplicationAcknowledgement,
    ):
        self.fences.append((scope, event, new_owner_id, acknowledgement))
        return SimpleNamespace(
            acknowledgement=acknowledgement,
            fence_receipt=FenceReceipt(
                receipt_id="rehearsal-fence-receipt",
                cluster_id=event.cluster_id,
                resource_kind=event.resource_kind,
                resource_id=event.resource_id,
                previous_owner_id=event.owner_id,
                previous_owner_epoch=event.owner_epoch,
                provider_id="provider-test",
                protocol_version=event.protocol_version,
                partition_state=PartitionState.SAFE,
                external=True,
                accepted=True,
            ),
            decision=SimpleNamespace(
                allowed=True,
                reason="evidence-collected-only",
                next_epoch=event.owner_epoch + 1,
                data_replica_count=2,
            ),
        )


def _replace_factory(monkeypatch, coordinator: _RecordingCoordinator) -> None:
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal

    monkeypatch.setattr(
        rehearsal,
        "build_control_state_rehearsal",
        lambda _config: coordinator,
    )


def test_command_collects_exact_rehearsal_evidence_without_promotion(
    tmp_path, monkeypatch, capsys
) -> None:
    """Catches an unconfirmed command that skips/read-widens evidence or promotes."""
    config_path, secrets_path, _ = _write_rehearsal_inputs(
        tmp_path, origin="https://control.example.test"
    )
    coordinator = _RecordingCoordinator("node-a", "node-b", "witness-a")
    _replace_factory(monkeypatch, coordinator)

    assert runtime_main(_command_args(config_path, secrets_path)) == 0

    report = _report(capsys)
    _assert_safe_report(
        report,
        forbidden=(
            _TEST_KEY,
            "https://control.example.test",
            str(config_path),
            _DIGEST,
        ),
    )
    assert report["status"] == "collected"
    assert coordinator.reads == [("rehearsal-command", 6, 1)]
    assert len(coordinator.appended) == 1
    assert coordinator.fences == []


@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    [
        ({"confirm_fence": "wrong"}, "invalid_confirmation"),
        ({"confirm_fence": "external-fence"}, "new_owner_required"),
        (
            {
                "confirm_fence": "external-fence",
                "new_owner_id": "node-a",
            },
            "new_owner_not_configured_peer",
        ),
        (
            {
                "confirm_fence": "external-fence",
                "new_owner_id": "witness-a",
            },
            "new_owner_not_configured_peer",
        ),
        ({"new_owner_id": "node-b"}, "new_owner_without_confirmation"),
        ({"event_id": "live-event"}, "rehearsal_event_required"),
        ({"resource_id": "live-job"}, "rehearsal_resource_required"),
        ({"owner_epoch": 0}, "owner_epoch_invalid"),
        ({"sequence": 0}, "sequence_invalid"),
        ({"payload_digest": "A" * 64}, "payload_digest_invalid"),
    ],
)
def test_invalid_request_never_constructs_provider_or_mutates_control_state(
    tmp_path, monkeypatch, capsys, changes, expected_reason
) -> None:
    """Catches a malformed rehearsal request reaching factory/provider I/O."""
    config_path, secrets_path, _ = _write_rehearsal_inputs(
        tmp_path, origin="https://control.example.test"
    )
    factory_calls: list[object] = []
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal

    monkeypatch.setattr(
        rehearsal,
        "build_control_state_rehearsal",
        lambda config: factory_calls.append(config),
    )

    assert runtime_main(_command_args(config_path, secrets_path, **changes)) == 2

    report = _report(capsys)
    _assert_safe_report(report, forbidden=(_TEST_KEY, str(config_path), _DIGEST))
    assert report["status"] == "rejected"
    assert report["reason"] == expected_reason
    assert factory_calls == []


def test_non_rehearsal_config_scope_never_constructs_provider(
    tmp_path, monkeypatch, capsys
) -> None:
    """Catches a live-looking configured cluster reaching the rehearsal factory."""
    config_path, secrets_path, _ = _write_rehearsal_inputs(
        tmp_path,
        origin="https://control.example.test",
        cluster_id="production-cluster",
    )
    factory_calls: list[object] = []
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal

    monkeypatch.setattr(
        rehearsal,
        "build_control_state_rehearsal",
        lambda config: factory_calls.append(config),
    )

    assert runtime_main(_command_args(config_path, secrets_path)) == 2

    report = _report(capsys)
    _assert_safe_report(report, forbidden=(_TEST_KEY, str(config_path), _DIGEST))
    assert report["reason"] == "rehearsal_cluster_required"
    assert factory_calls == []


@pytest.mark.parametrize(
    ("before_command", "arguments", "as_json"),
    [
        (
            (),
            (
                "--set",
                "control_state_rehearsal.origin=https://origin.invalid/parser-secret-value",
            ),
            True,
        ),
        (
            ("--origin", "https://origin.invalid/parser-secret-value"),
            (),
            True,
        ),
        ((), ("--api-key", "parser-secret-value"), True),
        ((), ("--cluster-id", "production-cluster"), True),
        ((), ("--witness-id", "witness-live"), True),
        ((), ("--provider-id", "provider-live"), True),
        ((), ("--local-owner-id", "node-live"), True),
        ((), ("--resource-kind", "parser-secret-value"), False),
        ((), ("--owner-epoch", "parser-secret-value"), False),
    ],
)
def test_rehearsal_parser_redacts_unsafe_values_before_config_or_provider_io(
    tmp_path, monkeypatch, capsys, before_command, arguments, as_json
) -> None:
    """Catches a parser rejection echoing inputs or reaching command I/O."""
    config_path, secrets_path, _ = _write_rehearsal_inputs(
        tmp_path, origin="https://control.example.test"
    )
    config_calls: list[object] = []
    factory_calls: list[object] = []
    import sonder_runtime.__main__ as entrypoint
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal

    monkeypatch.setattr(
        entrypoint, "_load_config", lambda args: config_calls.append(args)
    )
    monkeypatch.setattr(
        rehearsal,
        "build_control_state_rehearsal",
        lambda config: factory_calls.append(config),
    )
    args = _command_args(config_path, secrets_path)
    if not as_json:
        args.remove("--json")
    args.extend(arguments)

    assert runtime_main([*before_command, *args]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    rendered = captured.out + captured.err
    for unsafe_value in (*before_command, *arguments):
        assert unsafe_value not in rendered
    assert "parser-secret-value" not in rendered
    if as_json:
        report = json.loads(captured.out)
        _assert_safe_report(report, forbidden=("parser-secret-value",))
        assert report["status"] == "rejected"
        assert report["reason"] == "invalid_arguments"
    else:
        assert "reason: invalid_arguments\n" in captured.out
    assert config_calls == []
    assert factory_calls == []


@pytest.mark.parametrize("as_json", [False, True])
def test_rehearsal_parser_emits_a_stable_invalid_arguments_report(
    tmp_path, capsys, as_json
) -> None:
    """Catches parse failures bypassing the command's stable refusal report."""
    config_path, secrets_path, _ = _write_rehearsal_inputs(
        tmp_path, origin="https://control.example.test"
    )
    args = _command_args(config_path, secrets_path)
    if not as_json:
        args.remove("--json")
    args.extend(("--resource-kind", "parser-secret-value"))

    assert runtime_main(args) == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    if as_json:
        report = json.loads(captured.out)
        _assert_safe_report(report, forbidden=("parser-secret-value",))
        assert report["status"] == "rejected"
        assert report["reason"] == "invalid_arguments"
    else:
        assert captured.out == (
            "schema: sonder.control-state-rehearsal.v1\n"
            "status: rejected\n"
            "evidence_scope: process-boundary-transport-rehearsal\n"
            "promotion_attempted: False\n"
            "automatic_takeover_available: False\n"
            "automatic_failback_available: False\n"
            "reason: invalid_arguments\n"
        )


def test_parser_requires_an_explicit_rehearsal_config(tmp_path, capsys) -> None:
    """Catches the command falling back to a normal runtime configuration."""
    _config_path, secrets_path, _state_path = _write_rehearsal_inputs(
        tmp_path, origin="https://control.example.test"
    )
    args = _command_args(tmp_path / "not-used.toml", secrets_path)
    del args[1:3]

    assert runtime_main(args) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    _assert_safe_report(report)
    assert report["reason"] == "invalid_arguments"


def test_non_rehearsal_parser_keeps_standard_argument_errors(capsys) -> None:
    """Catches content-free rehearsal errors accidentally applying globally."""
    with pytest.raises(SystemExit) as exited:
        runtime_main(["status", "--ordinary-unknown-option"])

    assert exited.value.code == 2
    assert "unrecognized arguments: --ordinary-unknown-option" in capsys.readouterr().err


def test_rehearsal_literal_collision_redacts_before_config_or_provider_io(
    monkeypatch, capsys
) -> None:
    """Catches a leading option value selecting another command before the literal."""
    config_calls: list[object] = []
    factory_calls: list[object] = []
    import sonder_runtime.__main__ as entrypoint
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal

    monkeypatch.setattr(
        entrypoint, "_load_config", lambda args: config_calls.append(args)
    )
    monkeypatch.setattr(
        rehearsal,
        "build_control_state_rehearsal",
        lambda config: factory_calls.append(config),
    )

    assert runtime_main(
        [
            "--origin",
            "status",
            "control-state-rehearsal",
            "--json",
            "--api-key",
            "collision-secret",
        ]
    ) == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "collision-secret" not in captured.out
    assert "--api-key" not in captured.out
    report = json.loads(captured.out)
    _assert_safe_report(report, forbidden=("collision-secret",))
    assert report["status"] == "rejected"
    assert report["reason"] == "invalid_arguments"
    assert config_calls == []
    assert factory_calls == []


def test_rehearsal_parser_preserves_help_exit(capsys) -> None:
    """Catches the redaction boundary swallowing the normal help path."""
    with pytest.raises(SystemExit) as exited:
        runtime_main(["control-state-rehearsal", "--help"])

    assert exited.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "--config CONFIG" in captured.out


def test_rehearsal_parser_preserves_global_version_exit(capsys) -> None:
    """Catches the redaction boundary swallowing a global successful exit."""
    with pytest.raises(SystemExit) as exited:
        runtime_main(["--version", "control-state-rehearsal"])

    assert exited.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "sonder-runtime" in captured.out


def test_config_io_failure_is_redacted_before_provider_construction(
    tmp_path, monkeypatch, capsys
) -> None:
    """Catches a config filesystem failure leaking its path or exception detail."""
    config_path, secrets_path, _ = _write_rehearsal_inputs(
        tmp_path, origin="https://control.example.test"
    )
    secret_like_value = "config-io-secret-value"
    factory_calls: list[object] = []
    import sonder_runtime.__main__ as entrypoint
    import sonder_runtime.bootstrap.control_state_rehearsal as rehearsal

    def fail_config_load(_args):
        raise OSError(f"{config_path}: {secret_like_value}")

    monkeypatch.setattr(entrypoint, "_load_config", fail_config_load)
    monkeypatch.setattr(
        rehearsal,
        "build_control_state_rehearsal",
        lambda config: factory_calls.append(config),
    )

    assert runtime_main(_command_args(config_path, secrets_path)) == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    report = json.loads(captured.out)
    _assert_safe_report(
        report,
        forbidden=(
            secret_like_value,
            str(config_path),
            str(secrets_path),
            "OSError",
        ),
    )
    assert report["status"] == "rejected"
    assert report["reason"] == "configuration_invalid"
    assert factory_calls == []


def test_confirmed_fence_uses_collected_acknowledgement_without_duplicate_append(
    tmp_path, monkeypatch, capsys
) -> None:
    """Catches confirmed fencing that appends twice or requests a local takeover."""
    config_path, secrets_path, _ = _write_rehearsal_inputs(
        tmp_path, origin="https://control.example.test"
    )
    coordinator = _RecordingCoordinator("node-a", "node-b", "witness-a")
    _replace_factory(monkeypatch, coordinator)

    assert runtime_main(
        _command_args(
            config_path,
            secrets_path,
            confirm_fence="external-fence",
            new_owner_id="node-b",
        )
    ) == 0

    report = _report(capsys)
    _assert_safe_report(report, forbidden=(_TEST_KEY, str(config_path), _DIGEST))
    assert report["status"] == "fence_evidence_collected"
    assert len(coordinator.appended) == 1
    assert coordinator.reads == [("rehearsal-command", 6, 1)]
    assert len(coordinator.fences) == 1
    scope, event, new_owner_id, acknowledgement = coordinator.fences[0]
    assert scope == event.scope
    assert new_owner_id == "node-b"
    assert acknowledgement.event_id == event.event_id


class _LoopbackHandler(BaseHTTPRequestHandler):
    """Parent-owned provider fixture that stores no authorization header."""

    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def _json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        authorized = self.headers.get("Authorization") == f"Bearer {self.server.expected_key}"
        self.server.authorization_results.append(authorized)
        return authorized

    def do_POST(self) -> None:  # noqa: N802 - HTTP handler contract
        if not self._authorized():
            self._json(401, {"object": "denied"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        path = urlsplit(self.path).path
        if path == "/v1/control-state/events":
            event = payload["event"]
            self.server.events[(event["cluster_id"], event["sequence"])] = event
            self.server.calls.append(
                {
                    "kind": "append",
                    "event_id": event["event_id"],
                    "cluster_id": event["cluster_id"],
                    "owner_id": event["owner_id"],
                    "owner_epoch": event["owner_epoch"],
                    "sequence": event["sequence"],
                }
            )
            if event["event_id"].endswith("-malformed"):
                self._json(200, {"object": "malformed"})
                return
            replicas, witnesses = self.server.members[event["owner_id"]]
            self._json(
                200,
                {
                    "object": "replication_acknowledgement",
                    "acknowledgement": {
                        "event_id": event["event_id"],
                        "cluster_id": event["cluster_id"],
                        "owner_epoch": event["owner_epoch"],
                        "sequence": event["sequence"],
                        "provider_id": "provider-test",
                        "protocol_version": 1,
                        "data_replica_ids": list(replicas),
                        "witness_ids": list(witnesses),
                        "durable": True,
                    },
                },
            )
            return
        if path == "/v1/control-state/fence":
            scope = payload["ownership"]
            self.server.calls.append(
                {
                    "kind": "fence",
                    "cluster_id": scope["cluster_id"],
                    "resource_kind": scope["resource_kind"],
                    "resource_id": scope["resource_id"],
                    "owner_id": scope["owner_id"],
                    "epoch": scope["epoch"],
                }
            )
            self._json(
                200,
                {
                    "object": "fence_receipt",
                    "receipt": {
                        "receipt_id": "rehearsal-fence-receipt",
                        "cluster_id": scope["cluster_id"],
                        "resource_kind": scope["resource_kind"],
                        "resource_id": scope["resource_id"],
                        "previous_owner_id": scope["owner_id"],
                        "previous_owner_epoch": scope["epoch"],
                        "provider_id": "provider-test",
                        "protocol_version": 1,
                        "partition_state": "safe",
                        "external": True,
                        "accepted": True,
                    },
                },
            )
            return
        self._json(404, {"object": "missing"})

    def do_GET(self) -> None:  # noqa: N802 - HTTP handler contract
        if not self._authorized():
            self._json(401, {"object": "denied"})
            return
        parsed = urlsplit(self.path)
        if parsed.path != "/v1/control-state/events":
            self._json(404, {"object": "missing"})
            return
        query = parse_qs(parsed.query, strict_parsing=True)
        cluster_id = query["cluster_id"][0]
        after_sequence = int(query["after_sequence"][0])
        limit = int(query["limit"][0])
        self.server.calls.append(
            {
                "kind": "read",
                "cluster_id": cluster_id,
                "after_sequence": after_sequence,
                "limit": limit,
            }
        )
        page = [
            event
            for (event_cluster, sequence), event in sorted(self.server.events.items())
            if event_cluster == cluster_id and sequence > after_sequence
        ][:limit]
        self._json(200, {"object": "control_state_events", "events": page})


@pytest.fixture
def loopback_provider():
    """Run a bounded parent-owned transport fixture for spawned children."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    server.expected_key = _TEST_KEY
    server.authorization_results = []
    server.calls = []
    server.events = {}
    server.members = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            origin=f"http://127.0.0.1:{server.server_port}", server=server
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _child_rehearsal(
    config_path: str,
    secrets_path: str,
    state_path: str,
    readiness_path: str,
    result_path: str,
    command: list[str],
) -> None:
    """Spawn-safe child entry point; test secrets must not inherit from parent."""
    for key in tuple(os.environ):
        if key.startswith("SONDER_"):
            del os.environ[key]
    Path(readiness_path).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "state_path": state_path,
                "config_path_seen": bool(config_path),
                "secrets_path_seen": bool(secrets_path),
                "stage": "ready",
            }
        ),
        encoding="utf-8",
    )
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        try:
            code = runtime_main(command)
        except SystemExit as exc:
            code = int(exc.code)
    result = {
        "pid": os.getpid(),
        "state_path": state_path,
        "config_path_seen": bool(config_path),
        "secrets_path_seen": bool(secrets_path),
        "exit_code": code,
        "stdout": stdout.getvalue(),
    }
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")


def _run_child(
    context,
    *,
    config_path: Path,
    secrets_path: Path,
    state_path: Path,
    readiness_path: Path,
    result_path: Path,
    command: list[str],
) -> dict[str, object]:
    """Run one child with a hard join bound and no leaked process."""
    child = context.Process(
        target=_child_rehearsal,
        args=(
            str(config_path),
            str(secrets_path),
            str(state_path),
            str(readiness_path),
            str(result_path),
            command,
        ),
    )
    child.start()
    try:
        child.join(timeout=20)
        assert not child.is_alive(), "rehearsal child exceeded bounded completion time"
        assert child.exitcode == 0
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["readiness"] = json.loads(readiness_path.read_text(encoding="utf-8"))
        return result
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)


def _child_inputs(
    root: Path,
    provider,
    *,
    name: str,
    local_id: str,
    peer_id: str,
    witness_id: str,
    sequence: int,
    event_id: str | None = None,
) -> tuple[Path, Path, Path, Path, Path, list[str]]:
    config_path, secrets_path, state_path = _write_rehearsal_inputs(
        root,
        origin=provider.origin,
        local_id=local_id,
        peer_id=peer_id,
        witness_id=witness_id,
    )
    provider.server.members[local_id] = ((local_id, peer_id), (witness_id,))
    return (
        config_path,
        secrets_path,
        state_path,
        root / f"{name}-result.json",
        root / f"{name}-ready.json",
        _command_args(
            config_path,
            secrets_path,
            event_id=event_id or f"rehearsal-{name}",
            resource_id=f"rehearsal-job-{name}",
            owner_epoch=3,
            sequence=sequence,
        ),
    )


def test_spawned_children_collect_exact_process_boundary_transport_evidence(
    tmp_path, monkeypatch, loopback_provider
) -> None:
    """Catches a child that crosses a state boundary without exact evidence checks."""
    monkeypatch.setenv("SONDER_CONTROL_STATE_REHEARSAL_API_KEY", "inherited-secret")
    context = multiprocessing.get_context("spawn")
    first = _child_inputs(
        tmp_path / "first",
        loopback_provider,
        name="first",
        local_id="node-first",
        peer_id="peer-first",
        witness_id="witness-first",
        sequence=11,
    )
    second = _child_inputs(
        tmp_path / "second",
        loopback_provider,
        name="second",
        local_id="node-second",
        peer_id="peer-second",
        witness_id="witness-second",
        sequence=12,
    )
    first_result = _run_child(
        context,
        config_path=first[0],
        secrets_path=first[1],
        state_path=first[2],
        readiness_path=first[4],
        result_path=first[3],
        command=first[5],
    )
    second_result = _run_child(
        context,
        config_path=second[0],
        secrets_path=second[1],
        state_path=second[2],
        readiness_path=second[4],
        result_path=second[3],
        command=second[5],
    )

    assert first_result["exit_code"] == 0
    assert second_result["exit_code"] == 0
    assert first_result["pid"] != second_result["pid"]
    assert first_result["state_path"] != second_result["state_path"]
    assert first[0] != second[0]
    assert first[1] != second[1]
    assert first[3] != second[3]
    assert first[4] != second[4]
    for result in (first_result, second_result):
        readiness = result["readiness"]
        assert readiness["stage"] == "ready"
        assert readiness["pid"] == result["pid"]
        assert readiness["state_path"] == result["state_path"]
        assert readiness["config_path_seen"] is True
        assert readiness["secrets_path_seen"] is True
    for result in (first_result, second_result):
        report = json.loads(result["stdout"])
        _assert_safe_report(
            report,
            forbidden=(
                _TEST_KEY,
                loopback_provider.origin,
                str(first[0]),
                str(second[0]),
                _DIGEST,
            ),
        )
        assert report["status"] == "collected"
    assert loopback_provider.server.calls == [
        {
            "kind": "append",
            "event_id": "rehearsal-first",
            "cluster_id": "rehearsal-command",
            "owner_id": "node-first",
            "owner_epoch": 3,
            "sequence": 11,
        },
        {
            "kind": "read",
            "cluster_id": "rehearsal-command",
            "after_sequence": 10,
            "limit": 1,
        },
        {
            "kind": "append",
            "event_id": "rehearsal-second",
            "cluster_id": "rehearsal-command",
            "owner_id": "node-second",
            "owner_epoch": 3,
            "sequence": 12,
        },
        {
            "kind": "read",
            "cluster_id": "rehearsal-command",
            "after_sequence": 11,
            "limit": 1,
        },
    ]
    assert loopback_provider.server.authorization_results == [True] * 4
    assert _TEST_KEY not in json.dumps(loopback_provider.server.calls)


def test_malformed_provider_child_appends_once_then_stops_without_read_or_fence(
    tmp_path, loopback_provider
) -> None:
    """Catches malformed append evidence that reaches read, fence, or promotion."""
    inputs = _child_inputs(
        tmp_path / "malformed",
        loopback_provider,
        name="malformed",
        local_id="node-malformed",
        peer_id="peer-malformed",
        witness_id="witness-malformed",
        sequence=21,
        event_id="rehearsal-malformed",
    )
    result = _run_child(
        multiprocessing.get_context("spawn"),
        config_path=inputs[0],
        secrets_path=inputs[1],
        state_path=inputs[2],
        readiness_path=inputs[4],
        result_path=inputs[3],
        command=inputs[5],
    )

    assert result["exit_code"] == 1
    report = json.loads(result["stdout"])
    _assert_safe_report(report, forbidden=(_TEST_KEY, loopback_provider.origin, _DIGEST))
    assert report["status"] == "blocked"
    assert report["reason"] == "dependency_unavailable"
    assert loopback_provider.server.calls == [
        {
            "kind": "append",
            "event_id": "rehearsal-malformed",
            "cluster_id": "rehearsal-command",
            "owner_id": "node-malformed",
            "owner_epoch": 3,
            "sequence": 21,
        }
    ]
    assert loopback_provider.server.authorization_results == [True]


def test_confirmed_child_collects_one_fence_receipt_without_promoting(
    tmp_path, loopback_provider
) -> None:
    """Catches confirmed fencing that retries append or claims a role transition."""
    inputs = _child_inputs(
        tmp_path / "confirmed",
        loopback_provider,
        name="confirmed",
        local_id="node-confirmed",
        peer_id="peer-confirmed",
        witness_id="witness-confirmed",
        sequence=31,
    )
    command = [
        *inputs[5],
        "--confirm-fence",
        "external-fence",
        "--new-owner-id",
        "peer-confirmed",
    ]
    result = _run_child(
        multiprocessing.get_context("spawn"),
        config_path=inputs[0],
        secrets_path=inputs[1],
        state_path=inputs[2],
        readiness_path=inputs[4],
        result_path=inputs[3],
        command=command,
    )

    assert result["exit_code"] == 0
    report = json.loads(result["stdout"])
    _assert_safe_report(report, forbidden=(_TEST_KEY, loopback_provider.origin, _DIGEST))
    assert report["status"] == "fence_evidence_collected"
    assert loopback_provider.server.calls == [
        {
            "kind": "append",
            "event_id": "rehearsal-confirmed",
            "cluster_id": "rehearsal-command",
            "owner_id": "node-confirmed",
            "owner_epoch": 3,
            "sequence": 31,
        },
        {
            "kind": "read",
            "cluster_id": "rehearsal-command",
            "after_sequence": 30,
            "limit": 1,
        },
        {
            "kind": "fence",
            "cluster_id": "rehearsal-command",
            "resource_kind": "job",
            "resource_id": "rehearsal-job-confirmed",
            "owner_id": "node-confirmed",
            "epoch": 3,
        },
    ]
    assert loopback_provider.server.authorization_results == [True] * 3

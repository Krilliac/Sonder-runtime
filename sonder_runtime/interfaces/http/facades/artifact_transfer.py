"""Pure artifact wire adapter; production routing/authentication remain host-owned."""

from dataclasses import dataclass
from ....application.artifacts.transfer import ArtifactRange, TransferError

_MOBILITY_ENVELOPE_FIELDS = frozenset(
    {
        "protocol_version",
        "recipient_attestation",
        "command_id",
        "spec",
        "receipt",
    }
)
_MOBILITY_RECEIPT_FIELDS = frozenset(
    {"transfer_id", "state", "offset", "chunk_bytes", "expires_at", "revision"}
)
_MOBILITY_SEALED_RECEIPT_FIELDS = _MOBILITY_RECEIPT_FIELDS | {"artifact"}
_MOBILITY_RECEIPT_STATES = frozenset(
    {"open", "verifying", "sealed", "aborted", "failed"}
)


@dataclass(frozen=True)
class ArtifactTransferHttpResult:
    status_code: int
    body: dict | ArtifactRange


def _response_state(result):
    """Read only the fixed receipt state shape emitted by transfer services."""
    if not isinstance(result, dict):
        return None
    if result.get("state") == "verifying":
        # Preserve the legacy receipt result exactly.
        return "verifying"
    if (
        set(result) != _MOBILITY_ENVELOPE_FIELDS
        or result.get("protocol_version") != "mobility-v1"
        or not isinstance(result.get("recipient_attestation"), dict)
        or not isinstance(result.get("command_id"), str)
        or not isinstance(result.get("spec"), dict)
    ):
        return None
    receipt = result.get("receipt")
    if not isinstance(receipt, dict) or set(receipt) not in {
        _MOBILITY_RECEIPT_FIELDS,
        _MOBILITY_SEALED_RECEIPT_FIELDS,
    }:
        return None
    state = receipt["state"]
    if not isinstance(state, str) or state not in _MOBILITY_RECEIPT_STATES:
        return None
    if state == "sealed" and not isinstance(receipt.get("artifact"), dict):
        return None
    if state != "sealed" and "artifact" in receipt:
        return None
    return state


def dispatch_artifact_transfer(service, action, payload, context, *, body=b"", mobility=None):
    if not isinstance(payload, dict):
        raise TransferError("INVALID_REQUEST")
    schemas = {
        "begin": {"spec", "command_id"},
        "inspect": {"transfer_id"},
        "append": {"transfer_id", "offset", "chunk_sha256"},
        "seal": {"transfer_id", "command_id"},
        "abort": {"transfer_id", "command_id"},
        "mobility_receipt": {"transfer_id", "command_id"},
        "artifact": {"artifact_id"},
        "range": {"artifact_id", "offset", "length"},
    }
    if action not in schemas or set(payload) != schemas[action]:
        raise TransferError("INVALID_REQUEST")
    if action == "begin":
        if mobility is None:
            result = service.begin_upload(payload["spec"], payload["command_id"], context)
        else:
            result = service.begin_mobility_upload(
                payload["spec"], payload["command_id"], mobility, context
            )
    elif action == "inspect":
        result = service.inspect_upload(payload["transfer_id"], context)
    elif action == "append":
        if mobility is None:
            result = service.append_chunk(
                payload["transfer_id"],
                payload["offset"],
                payload["chunk_sha256"],
                body,
                context,
            )
        else:
            result = service.append_mobility_chunk(
                payload["transfer_id"],
                payload["offset"],
                payload["chunk_sha256"],
                body,
                mobility,
                context,
            )
    elif action == "seal":
        if mobility is None:
            result = service.seal_upload(
                payload["transfer_id"], payload["command_id"], context
            )
        else:
            result = service.seal_mobility_upload(
                payload["transfer_id"], payload["command_id"], mobility, context
            )
    elif action == "abort":
        if mobility is None:
            result = service.abort_upload(
                payload["transfer_id"], payload["command_id"], context
            )
        else:
            result = service.abort_mobility_upload(
                payload["transfer_id"], payload["command_id"], mobility, context
            )
    elif action == "mobility_receipt":
        if mobility is None:
            raise TransferError("MOBILITY_PROTOCOL")
        result = service.inspect_mobility_upload(
            payload["transfer_id"], payload["command_id"], mobility, context
        )
    elif action == "artifact":
        result = service.inspect_artifact(payload["artifact_id"], context)
    else:
        result = service.read_range(
            payload["artifact_id"], payload["offset"], payload["length"], context
        )
    status = 202 if _response_state(result) == "verifying" else 200
    return ArtifactTransferHttpResult(status, result)


def transfer_error_status(error):
    code = str(error)
    if code == "NOT_FOUND":
        return 404
    if code in {"FORBIDDEN", "UNSAFE_STORE"}:
        return 403
    if code in {"BUSY", "CAPACITY", "QUOTA"}:
        return 429
    if code == "UNAVAILABLE":
        return 503
    if "CONFLICT" in code or code == "INCOMPLETE":
        return 409
    return 400

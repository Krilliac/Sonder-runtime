"""Pure artifact wire adapter; production routing/authentication remain host-owned."""

from dataclasses import dataclass
from ....application.artifacts.transfer import ArtifactRange, TransferError


@dataclass(frozen=True)
class ArtifactTransferHttpResult:
    status_code: int
    body: dict | ArtifactRange


def dispatch_artifact_transfer(service, action, payload, context, *, body=b""):
    if not isinstance(payload, dict):
        raise TransferError("INVALID_REQUEST")
    schemas = {
        "begin": {"spec", "command_id"},
        "inspect": {"transfer_id"},
        "append": {"transfer_id", "offset", "chunk_sha256"},
        "seal": {"transfer_id", "command_id"},
        "abort": {"transfer_id", "command_id"},
        "artifact": {"artifact_id"},
        "range": {"artifact_id", "offset", "length"},
    }
    if action not in schemas or set(payload) != schemas[action]:
        raise TransferError("INVALID_REQUEST")
    if action == "begin":
        result = service.begin_upload(payload["spec"], payload["command_id"], context)
    elif action == "inspect":
        result = service.inspect_upload(payload["transfer_id"], context)
    elif action == "append":
        result = service.append_chunk(
            payload["transfer_id"],
            payload["offset"],
            payload["chunk_sha256"],
            body,
            context,
        )
    elif action == "seal":
        result = service.seal_upload(
            payload["transfer_id"], payload["command_id"], context
        )
    elif action == "abort":
        result = service.abort_upload(
            payload["transfer_id"], payload["command_id"], context
        )
    elif action == "artifact":
        result = service.inspect_artifact(payload["artifact_id"], context)
    else:
        result = service.read_range(
            payload["artifact_id"], payload["offset"], payload["length"], context
        )
    status = (
        202 if isinstance(result, dict) and result.get("state") == "verifying" else 200
    )
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

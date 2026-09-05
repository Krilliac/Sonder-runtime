"""Host lifecycle commands, distinct from the canonical child/process ledgers."""

from dataclasses import dataclass
import hashlib
import json
import re


class OwnerRefused(RuntimeError):
    pass


class OwnerUnsupported(OwnerRefused):
    pass


class OwnerCommitAmbiguous(OwnerRefused):
    def __init__(self, prepared):
        self.prepared = prepared
        super().__init__(
            "owner storage outcome is unresolved; reconcile the exact operation ID"
        )


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


@dataclass(frozen=True)
class PreparedOwnerOperation:
    operation_id: str
    action: str
    expected_revision: int
    payload: bytes

    def __post_init__(self):
        if not isinstance(self.operation_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", self.operation_id
        ):
            raise OwnerRefused("invalid owner operation ID")
        if self.action not in {"select", "launch", "stop"}:
            raise OwnerRefused("unsupported owner action")
        if (
            type(self.expected_revision) is not int
            or not 0 <= self.expected_revision < 2**62
        ):
            raise OwnerRefused("invalid owner revision")
        if type(self.payload) is not bytes or len(self.payload) > 32768:
            raise OwnerRefused("owner payload exceeds bounds")
        try:
            value = json.loads(self.payload)
            if type(value) is not dict or canonical(value) != self.payload:
                raise ValueError()
            expected = {"config"} if self.action == "select" else set()
            if set(value) != expected or (
                self.action == "select" and type(value["config"]) is not dict
            ):
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise OwnerRefused("invalid immutable owner payload") from None

    @property
    def digest(self):
        return hashlib.sha256(
            canonical(
                [
                    self.operation_id,
                    self.action,
                    self.expected_revision,
                    self.payload.decode(),
                ]
            )
        ).hexdigest()


def prepare_owner_operation(operation_id, action, expected_revision, arguments):
    return PreparedOwnerOperation(
        operation_id, action, expected_revision, canonical(arguments)
    )

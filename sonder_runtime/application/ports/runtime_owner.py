"""Host lifecycle commands, distinct from the canonical child/process ledgers."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any


# Windows CPython minor versions the managed runtime profile may be sealed on.
# 3.12 stays for one release so a deployed venv-managed keeps verifying while
# the side-by-side 3.14 venv is built and switched in.
MANAGED_PYTHON_MINORS = ((3, 12), (3, 14))
MANAGED_PYTHON_LABEL = "3.12 or 3.14"


class OwnerRefused(RuntimeError):
    pass


class OwnerUnsupported(OwnerRefused):
    pass


class OwnerCommitAmbiguous(OwnerRefused):
    def __init__(self, prepared: PreparedOwnerOperation) -> None:
        self.prepared = prepared
        super().__init__(
            "owner storage outcome is unresolved; reconcile the exact operation ID"
        )


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


@dataclass(frozen=True)
class PreparedOwnerOperation:
    operation_id: str
    action: str
    expected_revision: int
    payload: bytes

    def __post_init__(self) -> None:
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
    def digest(self) -> str:
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


def prepare_owner_operation(
    operation_id: str,
    action: str,
    expected_revision: int,
    arguments: Mapping[str, Any],
) -> PreparedOwnerOperation:
    return PreparedOwnerOperation(
        operation_id, action, expected_revision, canonical(arguments)
    )

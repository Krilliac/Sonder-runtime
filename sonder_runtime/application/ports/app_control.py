"""Immutable private control records. Data alone is never authentication."""

from typing import Protocol
from dataclasses import dataclass, field
import math
from pathlib import Path
import re


class AppControlError(Exception):
    pass


class CommandConflict(AppControlError):
    pass


class NotFound(AppControlError):
    pass


class CapacityExceeded(AppControlError):
    pass


class StoreUnavailable(AppControlError):
    pass


class OutcomeUnknown(StoreUnavailable):
    pass


def text(value, *, maximum=128):
    if (
        type(value) is not str
        or not 1 <= len(value.encode("utf8")) <= maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("invalid bounded text")
    return value


def identifier(value):
    text(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid identifier")
    return value


def digest(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid digest")
    return value


def principal(value):
    if type(value) is not str or not re.fullmatch(r"account:[0-9a-f]{64}", value):
        raise ValueError("invalid account principal")
    return value


def account_reference(value):
    if type(value) is not str or not re.fullmatch(
        r"account-session-v1:[0-9a-f]{64}\.[0-9a-f]{64}", value
    ):
        raise ValueError("invalid private account reference")
    return value


def positive(value, maximum=2**63 - 1):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid positive integer")


def timestamp(value):
    if (
        type(value) not in (float, int)
        or not math.isfinite(value)
        or not 0 < value <= 253402300799
    ):
        raise ValueError("invalid timestamp")


@dataclass(frozen=True)
class AppControlLimits:
    account_session_cap: int = 4
    global_session_cap: int = 64
    account_binding_cap: int = 16
    global_binding_cap: int = 256
    command_cap: int = 4096
    page_cap: int = 100
    session_ttl_seconds: int = 900
    binding_ttl_seconds: int = 3600

    def __post_init__(self):
        for name, maximum in [
            ("account_session_cap", 64),
            ("global_session_cap", 1024),
            ("account_binding_cap", 256),
            ("global_binding_cap", 4096),
            ("command_cap", 65536),
            ("page_cap", 100),
            ("session_ttl_seconds", 3600),
            ("binding_ttl_seconds", 86400),
        ]:
            positive(getattr(self, name), maximum)


@dataclass(frozen=True)
class GrantSnapshot:
    grant_id: str
    revision: int
    project_handle: str
    roots: tuple[str, ...] = field(repr=False)
    tools: tuple[str, ...]
    allow_cloud: bool
    allow_remote: bool
    expires_at: float
    grant_digest: str
    catalog_digest: str
    catalog_file_identity: tuple[int, int, int, int]

    def __post_init__(self):
        identifier(self.grant_id)
        identifier(self.project_handle)
        positive(self.revision)
        digest(self.grant_digest)
        digest(self.catalog_digest)
        timestamp(self.expires_at)
        if (
            type(self.roots) is not tuple
            or not 1 <= len(self.roots) <= 16
            or self.roots != tuple(sorted(set(self.roots)))
        ):
            raise ValueError("canonical root tuple required")
        for root in self.roots:
            text(root, maximum=4096)
            if not Path(root).is_absolute() or str(Path(root)) != str(
                Path(root).resolve()
            ):
                raise ValueError("canonical absolute root required")
        if (
            type(self.tools) is not tuple
            or not 1 <= len(self.tools) <= 64
            or self.tools != tuple(sorted(set(self.tools)))
        ):
            raise ValueError("canonical tool tuple required")
        for tool in self.tools:
            identifier(tool)
        if type(self.allow_cloud) is not bool or type(self.allow_remote) is not bool:
            raise ValueError("boolean ceiling required")
        if (
            type(self.catalog_file_identity) is not tuple
            or len(self.catalog_file_identity) != 4
            or any(type(x) is not int or x < 0 for x in self.catalog_file_identity)
        ):
            raise ValueError("invalid catalog identity")


@dataclass(frozen=True)
class ControlSessionRecord:
    control_session_id: str
    principal_id: str
    runtime_id: str
    account_session_ref: str = field(repr=False)
    grant: GrantSnapshot = field(repr=False)
    salt: str = field(repr=False)
    verifier: str = field(repr=False)
    account_expires_at: float
    issued_at: float
    expires_at: float
    revoked_at: float | None = None

    def __post_init__(self):
        identifier(self.control_session_id)
        principal(self.principal_id)
        identifier(self.runtime_id)
        account_reference(self.account_session_ref)
        digest(self.salt)
        digest(self.verifier)
        if type(self.grant) is not GrantSnapshot:
            raise ValueError("typed grant required")
        timestamp(self.account_expires_at)
        timestamp(self.issued_at)
        timestamp(self.expires_at)
        if (
            not self.issued_at
            < self.expires_at
            <= min(self.grant.expires_at, self.account_expires_at)
        ):
            raise ValueError("session expiry exceeds grant")
        if self.revoked_at is not None:
            timestamp(self.revoked_at)


@dataclass(frozen=True)
class BindingRecord:
    binding_id: str
    canonical_host_id: str
    principal_id: str
    runtime_id: str
    grant: GrantSnapshot = field(repr=False)
    created_at: float
    expires_at: float
    revision: int = 1
    revoked_at: float | None = None
    local_history_alias: str = ""
    display_title: str = ""

    def __post_init__(self):
        identifier(self.binding_id)
        principal(self.principal_id)
        identifier(self.runtime_id)
        positive(self.revision)
        if self.canonical_host_id != "app-session:" + self.binding_id:
            raise ValueError("fresh canonical host identity required")
        if type(self.grant) is not GrantSnapshot:
            raise ValueError("typed grant required")
        timestamp(self.created_at)
        timestamp(self.expires_at)
        if not self.created_at < self.expires_at <= self.grant.expires_at:
            raise ValueError("binding expiry exceeds grant")
        if self.revoked_at is not None:
            timestamp(self.revoked_at)
        for value in (self.local_history_alias, self.display_title):
            if value != "":
                text(value, maximum=256)


@dataclass(frozen=True)
class SelectionRecord:
    principal_id: str
    control_session_id: str
    selection_id: str
    epoch: int
    binding_id: str | None
    binding_revision: int | None

    def __post_init__(self):
        principal(self.principal_id)
        identifier(self.control_session_id)
        identifier(self.selection_id)
        positive(self.epoch)
        if self.binding_id is None:
            if self.binding_revision is not None:
                raise ValueError("cleared selection has no revision")
        else:
            identifier(self.binding_id)
            positive(self.binding_revision)


@dataclass(frozen=True)
class CommandKey:
    principal_id: str
    session_scope: str = field(repr=False)
    command_id: str

    def __post_init__(self):
        principal(self.principal_id)
        identifier(self.command_id)
        if type(self.session_scope) is not str:
            raise ValueError("invalid command scope")
        if self.session_scope.startswith("account:"):
            account_reference(self.session_scope[8:])
        elif self.session_scope.startswith("control:"):
            identifier(self.session_scope[8:])
        else:
            raise ValueError("invalid command scope")


ACTIONS = frozenset(
    ("enroll", "create_binding", "select_binding", "clear_selection", "revoke_binding")
)


@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    action: str
    result_code: str
    entity_id: str
    entity_revision: int | None = None
    selection_epoch: int | None = None

    def __post_init__(self):
        identifier(self.command_id)
        identifier(self.entity_id)
        if self.action not in ACTIONS or self.result_code != "COMMITTED":
            raise ValueError("invalid receipt projection")
        if self.entity_revision is not None:
            positive(self.entity_revision)
        if self.selection_epoch is not None:
            positive(self.selection_epoch)


@dataclass(frozen=True)
class CommandRecord:
    key: CommandKey
    action: str
    argument_digest: str
    state: str
    public_receipt: CommandReceipt

    def __post_init__(self):
        if (
            type(self.key) is not CommandKey
            or type(self.public_receipt) is not CommandReceipt
        ):
            raise ValueError("typed command required")
        digest(self.argument_digest)
        if (
            self.state != "committed"
            or self.action != self.public_receipt.action
            or self.key.command_id != self.public_receipt.command_id
        ):
            raise ValueError("invalid command state")


@dataclass(frozen=True)
class BindingPage:
    items: tuple[BindingRecord, ...]
    next_position: int | None

    def __post_init__(self):
        if (
            type(self.items) is not tuple
            or len(self.items) > 100
            or any(type(item) is not BindingRecord for item in self.items)
        ):
            raise ValueError("bounded immutable binding page required")
        if self.next_position is not None:
            positive(self.next_position)


class AppControlSessionReader(Protocol):
    """Private observation and current durable grant admission are distinct."""

    def read_session(
        self, *, principal_id: str, control_session_id: str
    ) -> ControlSessionRecord | None: ...
    def require_session(
        self, *, principal_id: str, control_session_id: str
    ) -> ControlSessionRecord: ...

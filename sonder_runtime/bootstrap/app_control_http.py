"""Private HTTP app-control composition, with no lane execution authority."""

from dataclasses import asdict
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import ipaddress
import stat
import time

from ..adapters.persistence.app_control import SQLiteAppControlStore
from ..adapters.persistence.fleet_store import _ensure_schema, database_path
from ..adapters.security.account_admission import account_admission, password_admission
from ..adapters.security.account_auth import account_auth as admin_auth
from ..adapters.security.control_plane_paths import (
    ControlPlanePaths,
    live_control_plane_inventory,
)
from ..application.ports.app_control import (
    AppControlLimits,
    BindingRecord,
    ControlSessionRecord,
    GrantSnapshot,
    CommandKey,
    CommandConflict,
    CapacityExceeded,
    NotFound,
    OutcomeUnknown,
    StoreUnavailable,
    identifier,
    text,
)
from ..platform.app_control_config import app_control_errors, app_control_transport
from .app_control import AppProjectGrantCatalog

from ..application.ports.app_control_http import ControlError


def canonical_digest(payload):
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def grant_snapshot(grant):
    return GrantSnapshot(
        grant.grant_id,
        grant.revision,
        grant.project,
        tuple(sorted(grant.roots)),
        tuple(sorted(grant.tools)),
        grant.allow_cloud,
        grant.allow_remote,
        grant.expires_at,
        grant.digest,
        grant.catalog_digest,
        grant.file_identity,
    )


def _principal(account):
    return "account:" + hashlib.sha256(account.username.encode()).hexdigest()


def _verifier(salt, secret):
    return hashlib.sha256(
        ("sonder-app-control-v1\0" + salt + "\0" + secret).encode()
    ).hexdigest()


def _binding(value):
    return dict(
        binding_id=value.binding_id,
        host_conversation_id=value.canonical_host_id,
        project=value.grant.project_handle,
        title=value.display_title,
        local_history_alias=value.local_history_alias,
        revision=value.revision,
        expires_at=value.expires_at,
        revoked=value.revoked_at is not None,
    )


class AppControlBinding:
    def __init__(
        self,
        config_provider,
        *,
        account_open,
        account_path,
        fleet_path=database_path,
        private_inventory=live_control_plane_inventory,
        lanes_provider=None,
        clock=time.time
    ):
        self._config_provider, self._open = config_provider, account_open
        self._account_path, self._account_identity = account_path, None
        self._fleet_path, self._inventory, self._lanes = (
            fleet_path,
            private_inventory,
            lanes_provider,
        )
        self._clock, self.store, self._initial = clock, None, None
        self.catalog = AppProjectGrantCatalog(
            config_provider=config_provider,
            workspace_roots=lambda: self._config_provider().state.workspace_roots,
            private_inventory=self._private,
            clock=clock,
        )

    def _private(self):
        config = self._config_provider()
        roots = tuple(Path(p).resolve() for p in config.state.workspace_roots)
        if not 1 <= len(roots) <= 256 or any(not p.is_dir() for p in roots):
            raise PermissionError("complete model roots unavailable")
        inventory = self._inventory()
        inventory.require_disjoint(roots)
        live_control_plane_inventory(
            additional=lambda: ControlPlanePaths(
                databases=(Path(self._fleet_path()), Path(self._account_path())),
                files=tuple(Path(p) for p in config.private_source_paths),
            )
        ).require_disjoint(roots)
        return inventory

    def _source(self, conn=None):
        raw = Path(self._account_path())
        if not raw.is_absolute() or raw != raw.resolve() or raw.is_symlink():
            raise PermissionError("canonical private account database required")
        meta = raw.lstat()
        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_nlink != 1
            or getattr(meta, "st_file_attributes", 0) & 0x400
        ):
            raise PermissionError("private account database changed")
        for suffix in ("-wal", "-shm", "-journal"):
            side = Path(str(raw) + suffix)
            if side.exists() or side.is_symlink():
                value = side.lstat()
                if (
                    not stat.S_ISREG(value.st_mode)
                    or value.st_nlink != 1
                    or side.is_symlink()
                    or getattr(value, "st_file_attributes", 0) & 0x400
                ):
                    raise PermissionError("private account sidecar changed")
        identity = (str(raw), meta.st_dev, meta.st_ino)
        if self._account_identity is not None and identity != self._account_identity:
            raise PermissionError("account source changed")
        if conn is not None:
            main = next(
                (r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"),
                None,
            )
            if main is None or Path(main).resolve() != raw:
                raise PermissionError(
                    "account connection differs from configured source"
                )
        return identity

    def _config(self):
        config = self._config_provider()
        if (
            not config.app_control.enabled
            or app_control_errors(config)
            or self._initial is not None
            and config != self._initial
        ):
            raise ControlError(503, "APP_CONTROL_UNAVAILABLE")
        secret = admin_auth._secret()
        if (
            type(secret) is not str
            or len(secret) < 32
            or len(set(secret)) < 8
            or secret == admin_auth.PUBLIC_DEV_SECRET
        ):
            raise ControlError(503, "APP_CONTROL_UNAVAILABLE")
        self._private()
        self._source()
        return config

    def start(self):
        if not self._config_provider().app_control.enabled:
            return
        config = self._config()
        self.catalog.snapshot()
        path = Path(self._fleet_path()).resolve()
        _ensure_schema(str(path))
        names = AppControlLimits.__dataclass_fields__
        limits = AppControlLimits(
            **{name: getattr(config.app_control, name) for name in names}
        )
        self.store = SQLiteAppControlStore(path, limits=limits, clock=self._clock)
        self._initial = config
        self._account_identity = self._source()

    def transport_allowed(self, *, listener, raw_peer, origin):
        from dataclasses import replace

        config = self._config()
        if config.app_control.proxy_only_backend and not any(
            ipaddress.ip_address(raw_peer) in ipaddress.ip_network(cidr)
            for cidr in config.app_control.proxy_cidrs
        ):
            return False
        actual = replace(config, server=replace(config.server, host=listener))
        return app_control_transport(actual, raw_peer=raw_peer, origin=origin)

    def _account(self, conn, token, expected=None):
        self._config()
        self._source(conn)
        account = admin_auth.authenticate_session(conn, token)
        self._source(conn)
        if (
            account is None
            or account.role != "admin"
            or expected is not None
            and account != expected
        ):
            raise ControlError(401, "APP_CONTROL_AUTH_REQUIRED")
        return account

    def _grant(self, account, project):
        return self.catalog.resolve(project, account.username, account.role)

    def _current(self, conn, token, account, grant):
        self._account(conn, token, account)
        self.catalog.require_current(grant)

    def _session(self, account, credential):
        match = re.fullmatch(r"sac1\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})", credential)
        if match is None:
            raise ControlError(401, "APP_CONTROL_AUTH_REQUIRED")
        sid, secret = match.groups()
        session = self.store.atomic(
            lambda tx: tx.read_session(
                principal_id=_principal(account), control_session_id=sid
            )
        )
        if (
            session is None
            or session.account_session_ref != account.reference
            or session.account_expires_at != account.expires_at
            or session.runtime_id != self._config().app_control.runtime_id
            or session.revoked_at is not None
            or not session.issued_at <= self._clock() < session.expires_at
            or not hmac.compare_digest(
                session.verifier, _verifier(session.salt, secret)
            )
        ):
            raise ControlError(401, "APP_CONTROL_AUTH_REQUIRED")
        session = self.store.atomic(
            lambda tx: tx.require_session(
                principal_id=_principal(account), control_session_id=sid
            )
        )
        grant = self._grant(account, session.grant.project_handle)
        if grant_snapshot(grant) != session.grant:
            raise ControlError(409, "APP_CONTROL_GRANT_CHANGED")
        return session, grant

    def perform(self, action, payload, *, account_token, control_token, publish):
        # Publication stays in the same process-local account admission region.
        # This never claims atomicity with external writers or catalog file I/O.
        conn = None
        try:
            if self.store is None:
                raise ControlError(503, "APP_CONTROL_UNAVAILABLE")
            self._config()
            conn = self._open()
            with account_admission(conn):
                try:
                    account = self._account(conn, account_token)
                    result, grant = self._perform(
                        conn, account, account_token, action, payload, control_token
                    )
                    self._current(conn, account_token, account, grant)
                except Exception as error:
                    result = self._error(error)
                # Do not retry publication if the socket fails after a secret
                # was written; the committed enrollment remains unknown-delivery.
                publish(*result)
        except Exception as error:
            if conn is not None:
                raise
            publish(*self._error(error))
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _error(error):
        if isinstance(error, ControlError):
            status, code = error.status, error.code
        elif isinstance(error, (ValueError, TypeError, KeyError)):
            status, code = 400, "INVALID_APP_CONTROL_REQUEST"
        elif isinstance(error, NotFound):
            status, code = 404, "APP_BINDING_NOT_FOUND"
        elif isinstance(error, CommandConflict):
            status, code = 409, "APP_CONTROL_CONFLICT"
        elif isinstance(error, CapacityExceeded):
            status, code = 429, "APP_CONTROL_CAPACITY"
        elif isinstance(error, PermissionError):
            status, code = 403, "APP_CONTROL_REFUSED"
        else:
            status, code = 503, (
                "APP_CONTROL_OUTCOME_UNKNOWN"
                if isinstance(error, OutcomeUnknown)
                else "APP_CONTROL_UNAVAILABLE"
            )
        return status, dict(ok=False, error=dict(code=code))

    def _perform(self, conn, account, token, action, payload, credential):
        fields = {
            "enroll": ({"command_id", "project", "password"}, {"replace_session_id"}),
            "create_binding": ({"command_id"}, {"local_history_alias", "title"}),
            "select_binding": (
                {
                    "command_id",
                    "binding_id",
                    "expected_binding_revision",
                    "expected_epoch",
                },
                set(),
            ),
            "clear_selection": ({"command_id", "expected_epoch"}, set()),
            "revoke_binding": (
                {"command_id", "binding_id", "expected_revision"},
                set(),
            ),
            "list_bindings": (set(), {"after_position", "limit"}),
            "read_selection": (set(), set()),
            "recovery": ({"binding_id"}, {"after_position", "limit"}),
        }
        if action not in fields or type(payload) is not dict:
            raise ValueError()
        required, optional = fields[action]
        if not required <= set(payload) or set(payload) - required - optional:
            raise ValueError()
        if "command_id" in payload:
            identifier(payload["command_id"])
        if "binding_id" in payload:
            identifier(payload["binding_id"])
        if action == "enroll":
            identifier(payload["project"])
            if "replace_session_id" in payload:
                identifier(payload["replace_session_id"])
            grant = self._grant(account, payload["project"])
            key = CommandKey(
                _principal(account),
                "account:" + account.reference,
                payload["command_id"],
            )
            # Password is validated live but never hashed into a persistent
            # low-entropy command receipt. Identity binds account and project.
            arguments = {k: v for k, v in payload.items() if k != "password"}
            argument_digest = canonical_digest(arguments)
            with password_admission(conn, _principal(account)):
                checked = admin_auth.reauthenticate(conn, token, payload["password"])
            if checked["username"] != account.username or checked["role"] != "admin":
                raise ControlError(401, "APP_CONTROL_AUTH_REQUIRED")
            self._current(conn, token, account, grant)
            prior = self.store.atomic(
                lambda tx: tx.command(
                    key, action="enroll", argument_digest=argument_digest
                )
            )
            if prior:
                raise ControlError(409, "CREDENTIAL_DELIVERY_UNKNOWN")
            now = self._clock()
            sid = secrets.token_hex(16)
            secret = secrets.token_urlsafe(32)
            salt = secrets.token_hex(32)
            session = ControlSessionRecord(
                sid,
                _principal(account),
                grant.runtime_id,
                account.reference,
                grant_snapshot(grant),
                salt,
                _verifier(salt, secret),
                account.expires_at,
                now,
                min(
                    account.expires_at,
                    grant.expires_at,
                    now + self._config().app_control.session_ttl_seconds,
                ),
            )
            receipt = self.store.atomic(
                lambda tx: tx.commit_enrollment(
                    key,
                    argument_digest=argument_digest,
                    session=session,
                    replace_session_id=payload.get("replace_session_id"),
                )
            )
            if receipt.entity_id != sid:
                raise ControlError(409, "CREDENTIAL_DELIVERY_UNKNOWN")
            return (
                201,
                dict(
                    ok=True,
                    control_session_id=sid,
                    control_token="sac1." + sid + "." + secret,
                    runtime_id=session.runtime_id,
                    expires_at=session.expires_at,
                ),
            ), grant
        session, grant = self._session(account, credential)
        self._current(conn, token, account, grant)
        if action == "read_selection":
            selected = self.store.atomic(
                lambda tx: tx.read_selection(
                    principal_id=session.principal_id,
                    control_session_id=session.control_session_id,
                )
            )
            public = (
                None
                if selected is None
                else dict(
                    selection_id=selected.selection_id,
                    epoch=selected.epoch,
                    binding_id=selected.binding_id,
                    binding_revision=selected.binding_revision,
                )
            )
            return (200, dict(ok=True, selection=public)), grant
        if action in {"list_bindings", "recovery"}:
            limit = payload.get(
                "limit",
                min(
                    32 if action == "recovery" else 50,
                    self._config().app_control.page_cap,
                ),
            )
            cursor = payload.get("after_position", 0)
            if (
                type(limit) is not int
                or not 1 <= limit <= self._config().app_control.page_cap
                or type(cursor) is not int
                or not 0 <= cursor < 2**63
            ):
                raise ValueError("bounded page required")
            payload = {**payload, "limit": limit, "after_position": cursor}
        if action == "list_bindings":
            page = self.store.atomic(
                lambda tx: tx.list_bindings(
                    principal_id=_principal(account),
                    after_position=payload.get("after_position", 0),
                    limit=payload.get("limit", 50),
                )
            )
            # The control session is bound to exactly one immutable project.
            items = [
                _binding(v)
                for v in page.items
                if v.runtime_id == session.runtime_id and v.grant == session.grant
            ]
            return (
                200,
                dict(ok=True, items=items, next_position=page.next_position),
            ), grant
        if action == "recovery":
            return (200, self._recovery(account, session, payload)), grant
        key = CommandKey(
            _principal(account),
            "control:" + session.control_session_id,
            payload["command_id"],
        )
        args = {k: v for k, v in payload.items() if k != "command_id"}
        argument_digest = canonical_digest(payload)

        def mutate(tx):
            if action == "create_binding":
                now = self._clock()
                bid = secrets.token_hex(16)
                value = BindingRecord(
                    bid,
                    "app-session:" + bid,
                    session.principal_id,
                    session.runtime_id,
                    session.grant,
                    now,
                    min(
                        session.account_expires_at,
                        grant.expires_at,
                        now + self._config().app_control.binding_ttl_seconds,
                    ),
                    local_history_alias=args.get("local_history_alias", ""),
                    display_title=args.get("title", ""),
                )
                return tx.create_binding(
                    key,
                    argument_digest=argument_digest,
                    control_session_id=session.control_session_id,
                    binding=value,
                )
            return getattr(tx, action)(
                key,
                argument_digest=argument_digest,
                control_session_id=session.control_session_id,
                **args
            )

        receipt = self.store.atomic(mutate)
        return (200, dict(ok=True, receipt=asdict(receipt))), grant

    def _recovery(self, account, session, payload):
        value = self.store.atomic(
            lambda tx: tx.read_binding(
                principal_id=session.principal_id, binding_id=payload["binding_id"]
            )
        )
        if (
            value is None
            or value.grant != session.grant
            or value.runtime_id != session.runtime_id
        ):
            raise NotFound("binding unavailable")
        if value.revoked_at is not None or value.expires_at <= self._clock():
            raise CommandConflict("binding expired or revoked")
        if self._lanes is None:
            raise ControlError(503, "APP_RECOVERY_UNAVAILABLE")
        from ..application.agents.lane_continuation import LaneContinuationService
        from ..application.ports.lane_continuation import HostContinuationGrant
        from ..application.context import OperationContext

        lanes = self._lanes()
        if Path(lanes.store.path).resolve() != Path(self.store.path):
            raise PermissionError("recovery store identity mismatch")
        context = OperationContext(
            "app-recovery",
            session.principal_id,
            "admin",
            "http",
            time.monotonic() + 10,
            None,
            tuple(Path(p) for p in session.grant.roots),
        )

        # Event adapter provides the immutable context cancellation protocol.
        class Cancel:
            cancelled = False

            def wait(self, timeout=None):
                return False

        from dataclasses import replace

        context = replace(context, cancellation=Cancel())

        def authorize(current, host):
            if current is not context or host != value.canonical_host_id:
                raise PermissionError("private recovery scope mismatch")
            self.catalog.require_current(
                self._grant(account, session.grant.project_handle)
            )
            return HostContinuationGrant(
                session.principal_id,
                host,
                session.grant.grant_id,
                session.grant.revision,
                min(value.expires_at, session.expires_at),
                session.grant.roots,
                session.grant.tools,
            )

        service = LaneContinuationService(
            lanes,
            authorize_host=authorize,
            model_writable_roots=lambda: self._config().state.workspace_roots,
        )
        page = service.recovery_page(
            context,
            cursor=payload.get("after_position", 0),
            limit=payload.get("limit", 32),
            host_conversation_id=value.canonical_host_id,
        )
        result = dict(
            ok=True,
            binding=_binding(value),
            items=[asdict(item) for item in page.items],
            next_position=page.next_cursor if page.has_more else None,
            execution_available=False,
        )
        if len(json.dumps(result, ensure_ascii=False).encode()) > 65536:
            raise CapacityExceeded("recovery page byte bound exceeded")
        return result

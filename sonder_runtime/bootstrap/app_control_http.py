"""Private HTTP app-control composition, with no lane execution authority."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import ipaddress
import stat
import threading
import time

from ..adapters.persistence.app_control import SQLiteAppControlStore
from ..adapters.persistence.fleet_store import _ensure_schema, database_path
from ..adapters.security.account_admission import account_admission, password_admission
from ..adapters.security.account_auth import account_auth as admin_auth
from ..adapters.security.control_plane_paths import (
    ControlPlanePaths,
    ControlPlaneInventory,
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
from ..platform import paths as runtime_paths
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


class _PrivateInventoryCapability:
    """Opaque binding-issued handle; it deliberately carries no inventory."""

    __slots__ = ()

    def __repr__(self):
        return "<private inventory capability>"

    def __reduce__(self):
        raise TypeError("private inventory capability cannot be persisted")


class _PrivateInventoryLease:
    """Opaque issuer handle for one active admission continuation."""

    __slots__ = ()

    def __repr__(self):
        return "<private inventory admission lease>"

    def __reduce__(self):
        raise TypeError("private inventory admission lease cannot be persisted")


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
        # The live inventory is never placed in a ContextVar, an admission, or
        # a caller-owned object.  The context carries only an issuer-owned
        # opaque capability; this binding retains the actual snapshot privately.
        self._private_inventory_context = ContextVar(
            "app_control_private_inventory_scope", default=None
        )
        # A copied Context carries values but cannot reset a Token created in
        # its owner Context.  Keep that token private so a same-task copied
        # context cannot rebind a live scope by replaying an old capability.
        self._private_inventory_scope_marker = ContextVar(
            "app_control_private_inventory_scope_marker", default=None
        )
        self._private_inventory_issuer = object()
        self._private_inventory_lock = threading.RLock()
        self._private_inventory_records = {}
        self._private_inventory_leases = {}
        self._private_inventory_scopes = {}
        # ContextVars can be copied before a rebind.  The issuer therefore
        # retains the single generic capability that remains current per
        # lexical scope instead of trusting whichever capability a copied
        # context restores later.
        self._private_inventory_currents = {}
        self.catalog = AppProjectGrantCatalog(
            config_provider=config_provider,
            workspace_roots=lambda: self._config_provider().state.workspace_roots,
            private_inventory=self._private,
            clock=clock,
        )

    def issue_selection(self, *, account_token, control_token, context):
        authority = getattr(self, "_managed_authority", None)
        if authority is None:
            raise PermissionError("private app managed authority is not composed")
        return authority.issue_selection(
            account_token=account_token, control_token=control_token, context=context
        )

    def _private_requirements(self, config, additional=None):
        base = ControlPlanePaths(
            databases=(Path(self._fleet_path()), Path(self._account_path())),
            files=tuple(Path(p) for p in config.private_source_paths),
        )
        if additional is None:
            return base
        if type(additional) is not ControlPlanePaths:
            raise PermissionError("typed private inventory requirements required")
        try:
            return ControlPlanePaths(
                databases=(*base.databases, *additional.databases),
                files=(*base.files, *additional.files),
                owned_directories=(
                    *base.owned_directories,
                    *additional.owned_directories,
                ),
                owner_lock_directories=(
                    *base.owner_lock_directories,
                    *additional.owner_lock_directories,
                ),
                audit_files=(*base.audit_files, *additional.audit_files),
                atomic_files=(*base.atomic_files, *additional.atomic_files),
            )
        except (TypeError, ValueError, OSError):
            raise PermissionError("private inventory requirements unavailable") from None

    @staticmethod
    def _private_scope_digest(required):
        """Fingerprint trusted path-resolution inputs without retaining values."""
        digest = hashlib.sha256()
        try:
            configured = runtime_paths._configured_home()
            values = (*sorted(os.environ.items()), ("cwd", os.getcwd()))
            for name, value in (*values, ("configured_home", configured or "")):
                digest.update(os.fsencode(name))
                digest.update(b"\0")
                digest.update(os.fsencode(str(value)))
                digest.update(b"\0")
            for name in (
                "databases",
                "files",
                "owned_directories",
                "owner_lock_directories",
                "audit_files",
                "atomic_files",
            ):
                for value in getattr(required, name):
                    digest.update(os.fsencode(name))
                    digest.update(b"\0")
                    digest.update(os.fsencode(str(value)))
                    digest.update(b"\0")
        except (OSError, TypeError, UnicodeError, ValueError):
            raise PermissionError("private inventory scope unavailable") from None
        return digest.digest()

    @staticmethod
    def _inventory_copy(inventory):
        """Detach caller-visible data from the issuer's retained snapshot."""
        return ControlPlaneInventory(
            frozenset(inventory.exact_files),
            tuple(inventory.owned_directories),
            tuple(inventory.owner_lock_directories),
            tuple(inventory.audit_files),
            tuple(inventory.admission_directories),
            tuple(inventory.atomic_files),
        )

    @staticmethod
    def _private_roots(config, context_roots):
        if not isinstance(context_roots, tuple) or len(context_roots) > 32:
            raise PermissionError("bounded immutable context roots required")
        try:
            roots = tuple(
                dict.fromkeys(
                    Path(p).resolve()
                    for p in (*config.state.workspace_roots, *context_roots)
                )
            )
        except (TypeError, ValueError, OSError):
            raise PermissionError("complete model roots unavailable") from None
        if not 1 <= len(roots) <= 256 or any(not p.is_dir() for p in roots):
            raise PermissionError("complete model roots unavailable")
        return roots

    @staticmethod
    def _private_execution_owner():
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        return threading.get_ident(), task

    def _active_private_scope(self):
        entry = self._private_inventory_context.get()
        if (
            type(entry) is not tuple
            or len(entry) != 3
            or entry[0] is not self._private_inventory_issuer
            or type(entry[1]) is not _PrivateInventoryCapability
        ):
            return None
        capability, scope = entry[1], entry[2]
        owner = self._private_execution_owner()
        if not self._scope_has_current_context(scope, owner):
            return None
        return scope, capability

    def _scope_has_current_context(self, scope, owner):
        """Prove this is the owner Context, rather than a copied ContextVar."""
        with self._private_inventory_lock:
            scope_owner = self._private_inventory_scopes.get(scope)
            if (
                scope_owner is None
                or scope_owner[0] != owner[0]
                or scope_owner[1] is not owner[1]
                or self._private_inventory_scope_marker.get() is not scope
            ):
                return False
            try:
                self._private_inventory_scope_marker.reset(scope_owner[2])
            except ValueError:
                # Tokens are Context-bound.  A copied Context cannot use the
                # owner's token to turn its inherited marker into authority.
                return False
            self._private_inventory_scopes[scope] = (
                owner[0],
                owner[1],
                self._private_inventory_scope_marker.set(scope),
            )
        return True

    def _has_live_copied_private_scope(self):
        """Reject a copied owner Context while its lexical scope remains live."""
        entry = self._private_inventory_context.get()
        if (
            type(entry) is not tuple
            or len(entry) != 3
            or entry[0] is not self._private_inventory_issuer
            or type(entry[1]) is not _PrivateInventoryCapability
        ):
            return False
        owner = self._private_execution_owner()
        with self._private_inventory_lock:
            scope_owner = self._private_inventory_scopes.get(entry[2])
            return (
                scope_owner is not None
                and scope_owner[0] == owner[0]
                and scope_owner[1] is owner[1]
            )

    def _record_for(self, capability, *, config, required, require_current=True):
        """Resolve only a live issuer capability in its owning execution scope."""
        if type(capability) is not _PrivateInventoryCapability:
            return None
        active = self._active_private_scope()
        if active is None:
            return None
        scope, current = active
        if require_current and capability is not current:
            return None
        with self._private_inventory_lock:
            record = self._private_inventory_records.get(capability)
            if record is None:
                return None
            (
                issuer,
                record_scope,
                record_config,
                record_digest,
                record_required,
                inventory,
            ) = record
            if (
                issuer is not self._private_inventory_issuer
                or record_scope is not scope
                or scope not in self._private_inventory_scopes
                or (record_config is not config and record_config != config)
                or record_digest != self._private_scope_digest(record_required)
                or not inventory.covers(required)
                or (
                    require_current
                    and self._private_inventory_currents.get(scope) is not capability
                )
            ):
                return None
        return inventory

    def _current_scope_stale(self, scope, *, config):
        active = self._active_private_scope()
        if active is None or active[0] is not scope:
            return True
        with self._private_inventory_lock:
            record = self._private_inventory_records.get(active[1])
            if record is None:
                return True
            _, record_scope, record_config, record_digest, required, _ = record
            return (
                record_scope is not scope
                or record_config is not config
                or record_digest != self._private_scope_digest(required)
            )

    def _scoped_private_capability(self, *, config, required):
        entry = self._private_inventory_context.get()
        if type(entry) is not tuple or len(entry) != 3:
            return None
        capability = entry[1]
        if self._record_for(capability, config=config, required=required) is None:
            return None
        return capability

    def _issue_private_capability(self, *, config, required, roots, scope):
        """Create a record from issuer-controlled live sources only.

        The injected provider remains an availability/coverage guard for the
        composed deployment, but its object is never retained.  The binding
        stores a fresh global inventory built here, so a caller cannot inject,
        mutate, or later replace the snapshot an admission consumes.
        """
        try:
            supplied = self._inventory()
            binding_required = self._private_requirements(config)
            if (
                type(supplied) is not ControlPlaneInventory
                or not supplied.covers(binding_required)
            ):
                raise PermissionError("private inventory snapshot unavailable")
            supplied.require_disjoint(roots)
            inventory = live_control_plane_inventory(additional=lambda: required)
            if type(inventory) is not ControlPlaneInventory or not inventory.covers(
                required
            ):
                raise PermissionError("private inventory snapshot unavailable")
            inventory.require_disjoint(roots)
        except PermissionError:
            raise
        except (OSError, TypeError, ValueError):
            raise PermissionError("private inventory snapshot unavailable") from None
        capability = _PrivateInventoryCapability()
        record = (
            self._private_inventory_issuer,
            scope,
            config,
            self._private_scope_digest(required),
            required,
            self._inventory_copy(inventory),
        )
        with self._private_inventory_lock:
            owner = self._private_inventory_scopes.get(scope)
            current = self._private_execution_owner()
            if (
                owner is None
                or owner[0] != current[0]
                or owner[1] is not current[1]
                or not self._scope_has_current_context(scope, current)
            ):
                raise PermissionError("private inventory scope unavailable")
            self._private_inventory_records[capability] = record
            # A requirements rebind retires the old generic capability even
            # if a copied ContextVar later restores it.  Admission leases keep
            # their explicit, separately validated continuation path.
            self._private_inventory_currents[scope] = capability
        return capability

    def _activate_private_scope(self):
        scope = object()
        owner = self._private_execution_owner()
        with self._private_inventory_lock:
            self._private_inventory_scopes[scope] = (
                owner[0],
                owner[1],
                self._private_inventory_scope_marker.set(scope),
            )
        return scope

    def _invalidate_private_scope_records(self, scope):
        """Retire old records before a configuration/digest rebind."""
        with self._private_inventory_lock:
            for capability, record in tuple(self._private_inventory_records.items()):
                if record[1] is scope:
                    del self._private_inventory_records[capability]
            for lease, record in tuple(self._private_inventory_leases.items()):
                if record[1] is scope:
                    del self._private_inventory_leases[lease]
            self._private_inventory_currents.pop(scope, None)

    def _revoke_private_scope(self, scope):
        """Revoke before ContextVar reset so copied contexts fail closed."""
        self._invalidate_private_scope_records(scope)
        with self._private_inventory_lock:
            owner = self._private_inventory_scopes.pop(scope, None)
            if owner is not None:
                try:
                    self._private_inventory_scope_marker.reset(owner[2])
                except ValueError:
                    # A copied Context cannot revive a deleted scope if its
                    # context-bound token is no longer resettable.
                    pass

    def _private_snapshot(
        self, capability, *, config, required, roots, require_current=True
    ):
        inventory = self._record_for(
            capability,
            config=config,
            required=required,
            require_current=require_current,
        )
        if inventory is None:
            raise PermissionError("private inventory snapshot unavailable")
        try:
            inventory.require_disjoint(roots)
        except (OSError, TypeError, ValueError):
            raise PermissionError("private inventory snapshot unavailable") from None
        return self._inventory_copy(inventory)

    def _private_capability(self, *, context_roots=(), requirements=None):
        config = self._config_provider()
        roots = self._private_roots(config, context_roots)
        required = self._private_requirements(config, requirements)
        capability = self._scoped_private_capability(config=config, required=required)
        if capability is not None:
            self._private_snapshot(
                capability, config=config, required=required, roots=roots
            )
            return capability
        active = self._active_private_scope()
        if active is None:
            if self._has_live_copied_private_scope():
                raise PermissionError("private inventory scope unavailable")
            raise PermissionError("private inventory scope required")
        scope, _ = active
        if self._current_scope_stale(scope, config=config):
            self._invalidate_private_scope_records(scope)
        capability = self._issue_private_capability(
            config=config, required=required, roots=roots, scope=scope
        )
        self._private_inventory_context.set(
            (self._private_inventory_issuer, capability, scope)
        )
        return capability

    def _require_private_capability(
        self, capability, *, context_roots=(), requirements=None
    ):
        config = self._config_provider()
        roots = self._private_roots(config, context_roots)
        required = self._private_requirements(config, requirements)
        return self._private_snapshot(
            capability, config=config, required=required, roots=roots
        )

    def _private_admission_lease(self, capability):
        """Whitelist one current issuer capability for an active admission only."""
        config = self._config_provider()
        required = self._private_requirements(config)
        if self._record_for(capability, config=config, required=required) is None:
            raise PermissionError("private inventory snapshot unavailable")
        active = self._active_private_scope()
        if active is None:
            raise PermissionError("private inventory scope required")
        scope, _ = active
        lease = _PrivateInventoryLease()
        with self._private_inventory_lock:
            record = self._private_inventory_records.get(capability)
            if record is None or record[1] is not scope:
                raise PermissionError("private inventory snapshot unavailable")
            self._private_inventory_leases[lease] = (
                self._private_inventory_issuer,
                scope,
                capability,
            )
        return lease

    def _require_private_admission_lease(
        self, lease, *, context_roots=(), requirements=None
    ):
        if type(lease) is not _PrivateInventoryLease:
            raise PermissionError("private inventory admission lease required")
        config = self._config_provider()
        roots = self._private_roots(config, context_roots)
        required = self._private_requirements(config, requirements)
        active = self._active_private_scope()
        if active is None:
            raise PermissionError("private inventory snapshot unavailable")
        scope, _ = active
        with self._private_inventory_lock:
            record = self._private_inventory_leases.get(lease)
            if (
                record is None
                or record[0] is not self._private_inventory_issuer
                or record[1] is not scope
            ):
                raise PermissionError("private inventory snapshot unavailable")
            capability = record[2]
        return self._private_snapshot(
            capability,
            config=config,
            required=required,
            roots=roots,
            require_current=False,
        )

    @contextmanager
    def _private_inventory_scope(self, *, context_roots=(), requirements=None):
        """Issue or reuse a binding-owned record for one owned operation.

        Nested same-execution scopes can reuse an enclosing record only after the
        exact normalized requirements are covered.  A larger nested operation
        gets a temporary rebind; ordinary consumers use :meth:`_private` to
        upgrade the enclosing scope once. Each scope is revoked before lexical
        exit, so copied asynchronous contexts cannot retain its records.
        """
        config = self._config_provider()
        roots = self._private_roots(config, context_roots)
        required = self._private_requirements(config, requirements)
        capability = self._scoped_private_capability(config=config, required=required)
        if capability is not None:
            self._private_snapshot(
                capability, config=config, required=required, roots=roots
            )
            yield
            return
        active = self._active_private_scope()
        if active is not None and self._current_scope_stale(active[0], config=config):
            self._invalidate_private_scope_records(active[0])
            capability = self._issue_private_capability(
                config=config, required=required, roots=roots, scope=active[0]
            )
            self._private_inventory_context.set(
                (self._private_inventory_issuer, capability, active[0])
            )
            yield
            return
        if active is None and self._has_live_copied_private_scope():
            raise PermissionError("private inventory scope unavailable")
        scope = self._activate_private_scope()
        try:
            capability = self._issue_private_capability(
                config=config, required=required, roots=roots, scope=scope
            )
        except BaseException:
            self._revoke_private_scope(scope)
            raise
        token = self._private_inventory_context.set(
            (self._private_inventory_issuer, capability, scope)
        )
        try:
            yield
        finally:
            self._revoke_private_scope(scope)
            self._private_inventory_context.reset(token)

    def _private(self, *, context_roots=(), requirements=None):
        config = self._config_provider()
        roots = self._private_roots(config, context_roots)
        required = self._private_requirements(config, requirements)
        capability = self._scoped_private_capability(config=config, required=required)
        if capability is None:
            active = self._active_private_scope()
            if active is None:
                if self._has_live_copied_private_scope():
                    raise PermissionError("private inventory scope unavailable")
                with self._private_inventory_scope(
                    context_roots=context_roots, requirements=requirements
                ):
                    return self._private(
                        context_roots=context_roots, requirements=requirements
                    )
            scope, _ = active
            if self._current_scope_stale(scope, config=config):
                self._invalidate_private_scope_records(scope)
            capability = self._issue_private_capability(
                config=config, required=required, roots=roots, scope=scope
            )
            # This is an authorized exact-requirement rebind of the active
            # scope, never a caller-supplied replacement.
            self._private_inventory_context.set(
                (self._private_inventory_issuer, capability, scope)
            )
        return self._private_snapshot(
            capability, config=config, required=required, roots=roots
        )

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
            try:
                value = side.lstat()
            except FileNotFoundError:
                # SQLite can remove its rollback journal as another owned
                # connection commits. Absence is valid; other inspection
                # failures and every unsafe existing sidecar still refuse.
                continue
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

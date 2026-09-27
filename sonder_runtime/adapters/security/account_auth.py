"""Dynamic compatibility provider for the hosted account engine."""
from __future__ import annotations

import importlib


def _engine():
    return importlib.import_module("admin_auth")


class AccountAuthProvider:
    def __getattr__(self, name):
        return getattr(_engine(), name)

    def register(self, connection, username, password, role="user", *,
                 trusted_local=True, bootstrap_secret="",
                 allow_additional=False, actor=None):
        # The hosted HTTP route depends on the keyword-only registration
        # policy (``trusted_local=False`` and the bootstrap secret). Forward it
        # verbatim so the engine, not this adapter, decides admission.
        return _engine().register(
            connection, username, password, role,
            trusted_local=trusted_local,
            bootstrap_secret=bootstrap_secret,
            allow_additional=allow_additional,
            actor=actor,
        )

    def login(self, connection, username, password):
        return _engine().login(connection, username, password)

    def reauthenticate(self, connection, token, password):
        return _engine().reauthenticate(connection, token, password)

    def revoke_session(self, connection, token):
        return _engine().revoke_session(connection, token)

    def authenticate_session(self, connection, token):
        return _engine().authenticate_session(connection, token)

    def read_session_reference(self, connection, reference):
        return _engine().read_session_reference(connection, reference)

    def require(self, account, role="user"):
        return _engine().require(account, role)

    def rate_limit(self, connection, account, cost=1):
        return _engine().rate_limit(connection, account, cost=cost)


account_auth = AccountAuthProvider()

__all__ = ["AccountAuthProvider", "account_auth"]

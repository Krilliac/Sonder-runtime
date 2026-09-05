# Private exact-login references

`AccountAuth.authenticate_session(connection, token)` validates one explicit
account bearer and returns a frozen `AccountSessionIdentity`, or `None` for an
invalid, expired, revoked or banned login. The identity includes normalized
username, current role, strict absolute expiry and a repr-hidden private reference.
The reference is a version-tagged existing token HMAC lookup key, not a new bearer.
It is never part of public account JSON. No token, login, schema or account row is
created or updated by these helpers.

`read_session_reference(connection, reference)` repeats the exact joined login
and account lookup without retaining the original bearer. Trusted background
composition can use this to observe a role change, expiry or committed revocation.
The helper must never be exposed as an endpoint accepting a caller-supplied
reference. Keep references out of transcripts, logs, model context and public
receipts; only private control records may retain them.

Both methods require an idle caller-owned account connection and preserve any
existing caller transaction by refusing before database work. A successful lookup
uses its own read transaction and releases it before returning. Missing schema or
storage failure remains an exception, not an implicit account or fresh schema.

An identity is a point-in-time lookup result. It does not provide password
step-up, project permission, proof of human intent, cross-database atomicity or a
distributed lease. App-control composition must still require the exact account
session, live role and project grant at every admission, coordinate local authority
mutations, and retain existing permission/owner/verification guards. No HTTP
control routes are enabled by this change.

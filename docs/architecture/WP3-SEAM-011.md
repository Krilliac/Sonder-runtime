# WP3 SEAM-011 — WebProvider / CredentialProvider

This slice adds the provider-neutral application port in
`sonder_runtime.application.ports.web`. It does not modify or wrap the
existing `web_tools` or external MCP adapters.

## Contract

`EgressPolicy` is an explicit, fail-closed constraint: callers provide an
allowlist of hostnames, permitted HTTP(S) schemes, response and redirect
limits, and whether operation cloud consent is required. `WebRequest` rejects
userinfo, query strings, fragments, and credential-bearing headers. A provider
may narrow a policy but may not silently widen it. The port does not resolve
DNS, open sockets, follow redirects, or implement SSRF protection; those are
provider responsibilities at the transport boundary.

`CredentialProvider` is separate from `WebProvider`. A caller requests a
named credential for an explicit audience and scope. The default
`CredentialScope.REQUEST` prevents a lease from becoming a provider-global
secret; `PROVIDER` is available only where the host intentionally grants that
longer lifetime. Credential values are not part of request headers, health
snapshots, or diagnostics, and `CredentialLease.__repr__` is redacted.

`redact()` is the minimum boundary helper for known secret values before
diagnostics or telemetry export. Providers must apply equivalent redaction to
errors and logs; redaction must never be treated as permission to return
credential material in a `WebResponse`.

`ProviderHealthSnapshot` reports only status, timestamp, failure count, and a
short single-line safe detail. Health is observational and does not grant
egress or credential access.

## Ownership and threading

The provider owns transport resources and credential storage. Application
callers own immutable request/context values. Port methods are documented as
thread-safe or async-safe; concrete providers must enforce operation deadline
and cancellation. No global credential cache or adapter migration is implied.

## Scope and verification

This is an additive seam: only the new port, focused tests, exports, and this
document are included. Existing web and external MCP adapters remain
unchanged. No specification checkbox, evidence-ledger, commit, or push is
part of this slice.

```text
python -m pytest -q tests/test_wp3_seam011_web.py
python -m compileall -q sonder_runtime/application/ports/web.py
```

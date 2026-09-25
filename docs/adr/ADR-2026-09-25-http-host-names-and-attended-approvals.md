# ADR 2026-09-25: HTTP Host names by threat, and HTTP approvals as an attended surface

**Status:** Accepted (operator decision for the Host policy; the approvals
surface is pending a security review before release, see below)
**Date:** 2026-09-25
**Context:** app plan lane S (S1, S2, S8); `sonder_runtime/interfaces/http/host_policy.py`,
`sonder_runtime/interfaces/http/facades/approvals.py`

## Decision 1: accept Host names by what DNS rebinding needs

The `Host` allowlist existed to stop DNS rebinding against the
unauthenticated loopback listener. It also refused every phone that reached
the PC by emulator alias (`10.0.2.2`), a port forward, `<hostname>.local`,
or a Tailscale MagicDNS name, even on listeners that require an API key.

A rebinding attack needs two things: a hostname the attacker controls, and a
listener that answers without credentials. The policy now follows that:

- IP literals are accepted on any port (except the unspecified address);
- `localhost`, `*.localhost` and the machine's own names (host name, FQDN,
  `<hostname>.local`, computed once at startup, with the FQDN lookup bounded)
  are accepted on any port;
- `[server].allowed_hosts` entries are accepted as before;
- any other well-formed name is accepted when the listener requires
  credentials, and refused with `421 HOST_NOT_ALLOWED` (with a remedy naming
  the setting) only in `local-open` mode.

A name accepted only because credentials are required does not earn the
loopback-peer exemptions (`/ready`, `/health`, `/version`, `/metrics` without
a key, and the local log page): a rebinding page runs in a browser on the
same machine, so a loopback peer is no proof of who is asking.
`tests/test_serve_host_policy.py` keeps a canary that a `local-open`
listener still refuses `evil.example`. The host launcher (port `11436`)
applies the same policy, with "requires credentials" meaning a launcher
token and its own `SONDER_LAUNCHER_ALLOWED_HOSTS` list.

## Decision 2: an authenticated developer/admin POST may approve one refused call

`POST /v1/approvals/<call_id>` issues a one-shot approval in the existing
approval ledger. This makes an authenticated HTTP request an attended
approval surface, following the precedent of `POST /v1/permission-mode`
(which already counts as "a person confirmed" for raising the mode). The
approval is narrower than a mode change:

- it applies only to a call that was refused and is still pending;
- it is bound to that call's full digest (a 16-character call id or the full
  digest, never a shorter prefix; a disagreeing body `digest`/`tool` is
  refused);
- at most one open approval exists per call, it is spent atomically once,
  and it expires;
- the approver (`developer:<user>`, `admin-key` or `local-open`) and surface
  `http` are recorded, and the action is audited as `permission_approve`.

The chat response carries the refused call as data
(`sonder_receipt.refusal`), so no client needs to parse the refusal prose.

## Review

The approvals surface widens who can answer the gate's ask, from the console
to any holder of a developer or administrator credential. It needs a
sonder-security-and-privacy review before release. If that review rejects it,
remove the POST routes; clients fall back to "approve from the console"
(`/approve <call id>`), which the receipt also names.

## Consequences

- Phones and emulators connect without `allowed_hosts` edits whenever the
  server has credentials or they use an address.
- `local-open` remains the one mode with a name allowlist; its refusals are
  logged (rate-limited) with the name to add.
- A developer or administrator credential can now release one refused file
  change or host program; a leaked credential of that role already had
  broader reach through `/v1/permission-mode` (admin) and slash tools.

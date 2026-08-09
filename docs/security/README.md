# Sonder Runtime — Security Review

This directory holds a **read-only** security review of Sonder's most sensitive
surfaces. It changed no runtime code; it only records what was inspected, what
holds up, and what does not.

- **[REVIEW.md](./REVIEW.md)** — the ranked findings list (CONFIRMED,
  NEEDS-VERIFICATION, and WELL-DEFENDED sections, each with `file:line` anchors,
  a concrete failure scenario, and a remediation).

See also **[ISOLATED_EXECUTION.md](./ISOLATED_EXECUTION.md)** for the optional
Docker/Podman-backed `isolated_run` contract, fixed controls, availability, and
external-runtime/kernel limitations.

## Scope

The review targeted the surfaces where a local agent, an authenticated HTTP
client, a fetched web page, or an update publisher can influence what Sonder
reads, writes, executes, or trusts:

- **Guarded file operations & root containment** — `file_ops.py`, `workbench.py`
- **Argv-only program/script execution & process-tree kill** — `code_runner.py`,
  `workbench.py`
- **Web + location tools** (SSRF, DNS-rebinding, decompression bombs, IP
  location) — `web_tools.py`
- **Consent gates** (cloud / remote-Ollama / web / location) and where each is
  enforced — `sonder_config.py`, `server.py`, `web_tools.py`
- **HTTP admission / auth** (bind security, token checks, roles) —
  `sonder_serve.py`, `admin_auth.py`
- **Secret handling / redaction / rotation** — `sonder_secrets.py`
- **Update trust chain** (TUF gate, manifest validation, safe extraction,
  atomic activation) — `sonder_updates.py`

Out of scope: exhaustive review of the ~13k-line `server.py` and ~2k-line
`sonder_serve.py` request handlers (covered only at the auth/admission and
tool-dispatch level), the training/model pipelines, and the Flutter clients.

## Methodology (date-agnostic)

The review is static and manual — no code was executed against a live model,
network, or database.

1. **Read the real code.** Each surface above was read in full or to the
   relevant boundary; nothing was inferred from documentation alone.
2. **Trace the path end to end.** A finding is marked **CONFIRMED** only when the
   path from an attacker-influenced input to the bad outcome was followed through
   every hop (e.g. tool argument → `server.py` dispatch → `file_ops` resolver →
   filesystem). Where the last hop lives in a module not fully traced, the item
   is **NEEDS-VERIFICATION** with the exact check to perform.
3. **Record the defenses too.** Surfaces that resist a class of attack are
   documented under **WELL-DEFENDED** with the reason, so the guarantee is not
   silently re-litigated later.
4. **Anchor precisely.** Every item cites `file:line` and the enclosing function
   name. Line numbers can drift; the function name is the durable anchor.
5. **No fixes, no invented issues.** This pass proposes remediations but changes
   no code, and every finding is grounded in a specific line — no generic
   boilerplate.

Severities: **high** (direct integrity/confidentiality loss or code execution on
a realistic path), **medium** (real gap gated by a precondition such as an
enabled tool or an egress channel), **low** (defense-in-depth / narrow
precondition), **info** (observation, no action required).

## How to regenerate / re-verify

There is no generator script — this is a human/agent reading pass. To reproduce
or refresh it:

1. Re-read the files listed under **Scope** on the current branch.
2. For each finding in `REVIEW.md`, jump to the named function (line numbers may
   have drifted) and re-trace the failure scenario against the current code.
3. Confirm the anchors still point at the described logic; update line numbers
   where they moved.
4. For the **NEEDS-VERIFICATION** items, perform the specific check named in the
   finding (e.g. for V1, read `sonder_update_engine.py` and determine whether
   `manifest.json` is a signed target and whether `health_checks[].argv` reaches
   a subprocess).
5. Spot-check the anchors mechanically, e.g.:

   ```sh
   cd <repo-root>
   grep -n "def resolve_path\|def read_file\|def _is_secret_path" file_ops.py
   grep -n "def _validated_public_target\|def _resolve_public_addresses" web_tools.py
   grep -n "def safe_extract\|def verify_bundle_trust" sonder_updates.py
   grep -n "def check_auth\|def _validate_bind_security" sonder_serve.py
   ```

All relative links in this directory point only within
`docs/security/`, so they remain valid wherever the repository is checked out.

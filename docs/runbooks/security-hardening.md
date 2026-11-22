# Security hardening (setup time)

Checked against the code on 2026-11-20. Run this once after any install
profile ([workstation](install-workstation-local.md) or
[server-private](install-server-private.md)) and again before you change who
can reach Sonder. The concepts and the full threat-model scope are in the
[security model](../wiki/09-security-model.md); this page is the checklist.

Treat access to a Sonder endpoint as shell access to the host
([SECURITY.md](../../SECURITY.md)). `<sonder-home>` below is the state home of
the running server. The order is the same for every command: `SONDER_STATE_HOME`,
then `SONDER_HOME`, then `[state].home` from the TOML file, then the platform
default (`env_home_override`, `sonder_runtime/platform/paths.py:157-167`, is
used by both `default_home()` and the config loader, `platform/config.py:1597`;
the environment overrides the TOML; `serve`, the console and the managed/owned runtimes then call
`configure_home(config.state.home)`: `__main__.py` `_configure_typed_home`,
`bootstrap/app.py`, `managed_http_runtime.py`, `owned_http_runtime.py`), and
`paths.py` `default_home` honours that override first. The platform default is
`%LOCALAPPDATA%\sonder` on Windows, the macOS default home
(`macos_default_home`) on macOS, and `~/.local/share/sonder` on Linux
(`$XDG_DATA_HOME/sonder` when that is set). Only the early secrets-file
discovery (`__main__.py` `_configured_path`) sees the environment variables
alone. Set only one of `SONDER_HOME` / `SONDER_STATE_HOME`.

On the [server-private](install-server-private.md) profile the packaged
`/etc/sonder/sonder.toml` sets `[state].home = "/var/lib/sonder"`
(`packaging/sonder.toml.example`) and the unit runs
`serve --config /etc/sonder/sonder.toml` (`packaging/systemd/sonder.service`),
so `<sonder-home>` is `/var/lib/sonder` there, not your own `~/.local/share/sonder`.
Run the commands below as a user that may write it (for example with `sudo -u
sonder`, using the service account named in the unit).

## Before you expose anything

1. Know what the default exposes. With no API key and no account requirement
   the auth mode is `local-open`: the loopback listener accepts every local
   client as an administrator **and** developer
   (`sonder_runtime/interfaces/http/serve.py` `_resolve_auth_mode`,
   `_developer_authorized`, `_admin_authorized`). That includes
   `POST /v1/permission-mode`, the drain route, the Observatory routes and
   fanout. Any process or logged-in user on the machine can use all of it.
   Separately, in every auth mode (not only `local-open`) the browser log page
   at `/` and `/v1/local/server-log` are served without credentials to
   loopback peers with a trusted `Host`, unless `tls_terminated_by_proxy` is
   on (`_local_log_dashboard_allowed`, `_host_trusted`). A published web app
   is matched first (`web_app.handle_web_app` runs before the log-page
   branch) and may answer `/` instead.
2. Stay on loopback until authentication is configured. The server logs
   `auth mode is local-open` at startup when this is your state
   (`_warn_if_local_open`). Configure a key or accounts first:
   [configure-authentication.md](configure-authentication.md).
3. A non-loopback bind is refused before the socket opens unless
   `SONDER_TLS_TERMINATED_BY_PROXY=1` (`[server].tls_terminated_by_proxy`) is
   declared **and** the auth mode has strong credentials, which depend on
   `SONDER_AUTH_MODE`: `api-key` needs an API key of at least 24 characters;
   `account` needs an account secret of at least 32; `both` and `either` each
   need **both** (so `either` with only a strong key is refused)
   (`serve.py` `_validate_bind_security`, `MIN_API_KEY_LENGTH` in
   `sonder_runtime/platform/config.py`, `MIN_ACCOUNT_SECRET_LENGTH` in
   `serve.py`). `local-open` never passes. Even then,
   keep Sonder on `127.0.0.1` and put a TLS proxy in front:
   [secure-remote-access.md](secure-remote-access.md). A `421 HOST_NOT_ALLOWED`
   is explained in
   [wiki 05](../wiki/05-http-api-and-lifecycle.md#host-names-and-421-host_not_allowed).

Verify (any OS; default port 11435 from `[server].port`):

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:11435/v1/permission-mode
```

`200` with no credentials means the listener is `local-open`; `401` means a
credential is required. Do not continue to remote exposure on a `200`.

## Choose a permission mode

Modes are exactly `plan`, `manual` (the default), `acceptEdits` and `auto`
(`permission_modes.py` `MODES`, `DEFAULT_MODE`). There is deliberately no
bypass mode. What each mode does with a tool, by its risk class
(`permission_modes.py` `_MATRIX`):

| Mode | reads | asks | mutation | execution | dangerous | unclassified |
|---|---|---|---|---|---|---|
| `plan` | allow | deny | deny | deny | deny | deny |
| `manual` | allow | ask | ask | ask | ask | ask |
| `acceptEdits` | allow | allow | allow | ask | ask | ask |
| `auto` | allow | allow | allow | allow | ask | ask |

"Unclassified" is a tool or command nothing could grade. For a caller with
nobody to ask it is refused outright, never downgraded; only an explicit
`allow` rule lets it run (`permission_modes.py` `decide`, step 5a).

"Dangerous" tools (`file_delete`, `sqlite_mutate`, `git_merge`,
`admin_register`, `permission_rule_set` and others) ask in every mode,
including `auto`. A caller with nobody to answer a prompt (an MCP client, the
HTTP chat, a control command) is refused for mutation, execution and
dangerous tools instead of being asked (`UNATTENDED_REFUSED_RISKS`).

There is no environment variable, config key or CLI flag for the mode. It is
stored in `<sonder-home>/permission_mode.json` as `{"mode": "<name>"}`.
The saved mode **is** restored after a restart, including `acceptEdits` and
`auto`. Only the separate elevation flag is session-only: it is never written to
disk and starts off (`permission_modes.py` `_load`, `_STATE`). So a host that
was left in `auto` comes back in `auto`; confirm the mode after every restart
(step 4 below).

Set it, attended:

1. At the console, run `/mode manual` (or `/mode plan`, `/mode acceptEdits`,
   `/mode auto`). Shift+Tab cycles the mode only when the console has a
   Shift+Tab handler installed (`interfaces/repl/repl.py` `_mode_cycle_key`).
   Check it with `/mode`, or with the read-only `GET /v1/permission-mode` curl
   in the first section.
2. Or, as an administrator over HTTP:

   ```bash
   curl -s -X POST http://127.0.0.1:11435/v1/permission-mode \
     -H "Authorization: Bearer $SONDER_API_KEY" \
     -H "Content-Type: application/json" -d '{"mode":"manual"}'
   ```

   Expected: JSON describing the current mode and the matrix. An unknown name
   returns `400` listing the valid modes (`serve.py`
   `_handle_permission_mode_post`).
3. Or edit `<sonder-home>/permission_mode.json` while Sonder is stopped.
4. After every restart, confirm the mode: `/mode` at the console, or the
   `GET /v1/permission-mode` curl above (with `Authorization: Bearer ...` when
   a key is set). Expected: the mode you chose, not a persisted `acceptEdits`
   or `auto` from an earlier session.

MCP clients and the HTTP chat may only lower the mode, or go from `plan` back
to `manual`; raising it is refused with the remedy named
(`permission_modes.py` `is_unattended_escalation`). A safe starting point for
an exposed or shared host is `manual`, or `plan` for read-only use.

Rollback: `/mode manual`.

## Permission rules and approvals

Rules sit on top of the mode. `<sonder-home>/permissions.json` holds ordered
`allow` / `ask` / `deny` glob rules, first match wins, and a tool with no
match asks (`sonder_runtime/domain/execution/policy.py`; the file is
`permissions.json` under the state home per `permission_rules.py`
`policy_path`). Shipped defaults: `file_delete` is `deny`; `file_read`,
`file_find`, `file_write`, `file_edit`, `file_copy`, `file_move`, `run_*`,
`isolated_run`, `web_*` and `local_service_probe` are `ask`; `status`,
`context_*` and `tool_manifest` are `allow`;
`admin_private_chain_of_thought` is `deny`. A `deny` rule beats any mode.

Editing rules with the `permission_rule_set` tool needs a developer token or
`SONDER_ALLOW_PERMISSION_EDITS=1` (repo-root `server.py` `permission_rule_set`).
Leave that variable unset on a shared or served host: while it is set, policy
edits and approvals need no token.

Approving one refused call (an alternative to loosening a rule):

1. `/approvals` at the console lists calls refused unattended.
2. `/approve <call id> [ttl seconds]` approves that exact call once.
   The TTL must be 60..86400 seconds (an out-of-range value is refused, not
   clamped), default 900
   (`sonder_runtime/adapters/security/approval_ledger.py`).
3. Approving works from the console, with a developer token, or with
   `SONDER_ALLOW_PERMISSION_EDITS=1` (`server.py` `_approval_authority`
   region, error text at the `one-shot approvals need the console` string).

The ledger is `<sonder-home>/approvals.db`, or the path in
`SONDER_APPROVALS_DB`. It stores digests and a redacted preview, never
arguments. The shared codes `SONDER_FILE_APPROVAL_CODE`,
`SONDER_ISOLATED_APPROVAL_CODE` and `SONDER_ISOLATED_WRITE_APPROVAL_CODE` are
retired; setting the first only logs a warning (`server.py`). Remove them from
old env files.

## File roots

The guarded file tools may touch only these roots
(`sonder_runtime/adapters/filesystem/file_ops.py` `allowed_roots`):

The four sources (checkout, state home, `SONDER_FILE_ROOTS`, `file_roots.local`)
are listed in [wiki 09](../wiki/09-security-model.md#workspace--tool-containment).
Operator details: `SONDER_FILE_ROOTS` is paths joined by `os.pathsep` (`;` on
Windows, `:` on Linux); `<sonder-home>/file_roots.local` (or the file named by
`SONDER_FILE_ROOTS_FILE`) takes one path per line, blank lines and `#` comments
ignored. The TOML key `[state].workspace_roots`
is exported to `SONDER_FILE_ROOTS` at startup
(`sonder_runtime/__main__.py`), and a `SONDER_FILE_ROOTS` in the environment or
secrets file **replaces** `workspace_roots`
(`sonder_runtime/platform/config.py`). Each `workspace_roots` entry must be
absolute with a unique final folder name, and `default` may only be first.

Example, Windows (PowerShell), one extra project folder:

```powershell
$f = "$env:LOCALAPPDATA\sonder\file_roots.local"
if ((Test-Path $f) -and -not (Get-Content $f -Raw).EndsWith("`n")) { Add-Content $f "" -Encoding utf8 }
Add-Content $f 'D:\projects\app' -Encoding utf8
```

Use `-Encoding utf8` and keep the file's last line terminated: Windows
PowerShell 5.1 otherwise writes ANSI, and the file is read as UTF-8
(`file_ops.py` `_roots_from_file`), so a non-ASCII path would fail to decode.

Linux:

```bash
# Use your <sonder-home>: on the server-private profile that is /var/lib/sonder
# (run as the service user); otherwise the platform default shown above.
home="/var/lib/sonder"   # or "${XDG_DATA_HOME:-$HOME/.local/share}/sonder"
echo "/srv/projects/app" | sudo -u sonder tee -a "$home/file_roots.local" >/dev/null
```

The Windows example above assumes the default `%LOCALAPPDATA%\sonder`; use your
`SONDER_HOME` or `[state].home` if you set one. On a workstation Linux install
drop `sudo -u sonder` and append with plain `>>`. Roots are read on each check, so no restart
is needed. Verify by asking Sonder to read a file inside the root; a path
outside returns `path is outside allowed roots`.

Root containment can be switched off entirely, which makes this the most
dangerous control on this page. Containment is skipped for every path, with no
`extra_roots` needed (except inside managed console work, where containment is
kept and bypass only adds `extra_roots`), when any of these holds (`server.py`
`_file_bypass_allowed`; `file_ops.py` `if bypass and not _MANAGED_ROOTS.get()`):
`SONDER_FILE_BYPASS` is set (`1|true|yes|on`; process-wide, so it applies to
every caller), the caller has a developer token, unsafe lab mode is active, or
the host passes its in-process trusted-repository sentinel. Keep
`SONDER_FILE_BYPASS` unset (off by default); it is scrubbed from child
processes. The only way to widen the roots for one call **with containment still
checked** is a one-shot approval of that exact call, which honours the call's own
`extra_roots` (`file_ops.py` `_CALL_REACH`, `_reach_roots`). Bypass never
reaches credential stores.
Credential stores (`.ssh`, `.aws`, `.env*` and the rest of the list in
[wiki 09](../wiki/09-security-model.md)) are refused even inside a root;
naming the store itself as a root is what allows reading it, so never add
`~/.ssh` or a whole home directory casually.

## Secrets file

Secrets (`SONDER_API_KEY`, `SONDER_INFERENCE_API_KEY`, and the others in
`SECRET_ENV_VARS`, `sonder_runtime/platform/logging.py`) belong in a
`KEY=VALUE` file, not in a unit file or the command line. Discovery:
`--secrets <path>`, else `SONDER_SECRETS`, else `<sonder-home>/sonder.env` when
it exists (`sonder_runtime/__main__.py` `_configured_path`). Format: one
`KEY=VALUE` per line, `#` comments, optional matching quotes; a line without
`=` or a control character is refused
(`sonder_runtime/platform/config_environment.py` `parse_env_file`). A variable
in the real process environment overrides the same key in the file
(`config.py` `load_config`). The same secrets are removed from the
environment of every child process Sonder launches.

Permissions:

- Linux/macOS: the file must have no group or world bits (mode `0600`).
  Otherwise startup fails with `secrets file ... must not be group/world
  accessible` (`config.py`). Fix: `chmod 600 <path>`.
- Windows: there is no mode check on the secrets file. Instead, whenever
  Sonder opens its state home it makes a best-effort attempt to give the home
  directory, its `secrets`, `certs` and `audit` subfolders and the secret files
  directly in it (`*.env`, `*.key`, `*.pem`, `*.pfx`, `*.p12`, the stores) a
  protected ACL granting only your account, SYSTEM and Administrators
  (`sonder_runtime/platform/private_files.py` module docstring,
  `_windows_tighten_state_home`; `paths.py` `_ensure_state_home`). It skips
  paths you do not own and reparse points, and failures are only logged at
  debug level. So a `sonder.env` **inside the state home** is covered once
  Sonder has started; a secrets file kept **elsewhere** (a custom
  `--secrets`/`SONDER_SECRETS` path) is not, and `rotate-key` there writes a
  new file that inherits whatever the folder allows (it sets `0600` on POSIX
  only, `sonder_runtime/adapters/secrets.py` `_write_private`). Restrict such a
  folder yourself (PowerShell; this removes inherited access, so add SYSTEM or
  Administrators back if you need them):

  ```powershell
  $dir = "<secrets-folder>"
  icacls $dir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F"
  icacls $dir
  ```

  Expected: only your account is listed, and files created in the folder
  afterwards show it as inherited.

Rollback: `icacls $dir /reset /T` (restores inherited access).

Rotation and compromise: [rotate-credentials.md](rotate-credentials.md),
[suspected-secret-exposure.md](suspected-secret-exposure.md).

## Consent gates checklist

Every gate is off by default and runtime policy can never turn one on. Turn
on only what you need, in `sonder.toml` or the secrets file. Truthy values are
`1`, `true`, `yes`, `on` unless stated.

- [ ] Cloud/hosted models: `SONDER_ALLOW_CLOUD` / `[features].cloud`. Prompts
  leave the machine. Home:
  [wiki 08](../wiki/08-model-tiers-and-gateway.md#remote--cloud-opt-in).
- [ ] Web tools: `SONDER_WEB_TOOLS` / `[features].web`.
- [ ] Remote Ollama: `SONDER_ALLOW_REMOTE_OLLAMA` / `[ollama].allow_remote`,
  with an `https://` origin that names an explicit port and no path. Home:
  [remote-ollama-caddy.md](remote-ollama-caddy.md#coordinator-settings).
- [ ] Remote compute: `SONDER_ALLOW_REMOTE_COMPUTE` / `[compute].allow_remote`,
  plus a per-workload opt-in. Home: [compute-fabric.md](compute-fabric.md).
- [ ] Remote Sonder Inference: `SONDER_ALLOW_REMOTE_INFERENCE`, exactly `0` or
  `1`, plus `https://`, `SONDER_INFERENCE_API_KEY` and a cloud-allowed
  context. Home: [sonder-inference.md](sonder-inference.md).
- [ ] Model reasoning: `SONDER_EXPOSE_REASONING` / `[features].expose_reasoning`.
  Private chain of thought: `SONDER_ALLOW_PRIVATE_COT`, which also needs an
  explicit `allow` rule for `admin_private_chain_of_thought` in
  `permissions.json`.
- [ ] Approximate location: `SONDER_LOCATION_CONSENT`.

(`sonder_runtime/platform/config.py` `_apply_environment`;
`sonder_runtime/domain/cloud_access.py`;
`sonder_runtime/adapters/inference/sonder_inference_gateway.py`.)

Browser access is separate: `SONDER_CORS_ORIGINS` is an exact-match,
comma-separated list that drops `*` and is global, applying to every route
(`serve.py` `_cors`);
`SONDER_OBSERVATORY_ORIGINS` / `[observability].live_export_origins` applies
to the three Observatory GET routes only, so prefer it for a dashboard
(`serve.py`).

## Unsafe lab

Unsafe lab mode removes the host-tool guardrails and is for disposable,
isolated machines only. It needs an exact acknowledgement phrase, refuses a
non-loopback host and a root/elevated process, and writes an audit line
(audit location and override: [unsafe-lab.md](unsafe-lab.md); with `SONDER_HOME`
unset it is under the OS per-user state folder, not the state home, and
`[state].home` does not move it). Do not enable it on a machine you are
hardening. Procedure and the phrase: [unsafe-lab.md](unsafe-lab.md).

## Final check

1. Auth mode is not `local-open` if anything other than you can reach the host
   (the curl above returns `401` without a key).
2. `manual` or stricter mode is set.
3. `SONDER_FILE_BYPASS`, `SONDER_ALLOW_PERMISSION_EDITS` and every consent
   variable you do not use are unset.
4. On Windows, any secrets folder outside the state home has a restricted ACL;
   on Linux the secrets file is `0600`.
5. File roots list only folders you intend the model to read and write.

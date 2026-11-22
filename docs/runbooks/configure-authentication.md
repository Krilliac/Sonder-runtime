# Configure HTTP API authentication

First-time setup of who may call the Sonder HTTP API (`python -m sonder_runtime
serve`, default `http://127.0.0.1:11435`): the deployment API key, user
accounts, the first administrator, and sign-in. Verified against the code on
2026-11-20.

Prerequisites: a working install ([install-workstation-local](install-workstation-local.md)
or [install-server-private](install-server-private.md)) that starts and passes
`preflight`. Paths below use `<home>` for the state home (Windows default
`%LOCALAPPDATA%\sonder`; `SONDER_STATE_HOME`, else `SONDER_HOME`, when set,
overrides every default, the same order for every command, see
[wiki 03](../wiki/03-configuration.md#state-home); Linux default `$XDG_DATA_HOME/sonder` when that
variable is set, else `~/.local/share/sonder`; macOS the per-user application
support folder; packaged install `/var/lib/sonder`;
`platform/paths.py:170-189`).

## How the server decides its auth mode

`SONDER_AUTH_MODE` accepts exactly `api-key`, `account`, `both` or `either`,
in lowercase with a hyphen. On the documented `python -m sonder_runtime serve`
path the value is only stripped of surrounding whitespace
(`config.py:1517-1518`), so `API_KEY`, `api_key` and `Account` are all refused
with `[server].auth_mode invalid: '<value>'` (`config.py:1919-1922`). Only the
standalone `serve.py` entry point normalises case and `_`/`-` and fails with
`invalid SONDER_AUTH_MODE` (`serve.py:706-716`); do not rely on it. The
effective mode is resolved in this order:

| Situation | Effective mode |
|---|---|
| `SONDER_AUTH_MODE` / `[server].auth_mode` set explicitly, plus `SONDER_REQUIRE_ACCOUNT` unset | that value |
| Mode `api-key` (default **or set explicitly**) and `SONDER_REQUIRE_ACCOUNT=1` | `account`: the code cannot tell an explicit `api-key` from the default (`__main__.py:1056-1059`, `config.py:1997-2000`). Unset `SONDER_REQUIRE_ACCOUNT` to keep `api-key`. |
| Mode `api-key` but no API key, and `[server].host` is loopback | `local-open`: no authentication, with a WARNING in the log (`serve.py:1227-1230`, `:1988-1996`) |
| Mode `api-key` but no API key, non-loopback host | refused at startup (`serve.py:2350-2352`, `config.py:1982-1988`) |
| Any mode (including `account`), non-loopback host **or** `profile = "server-private"`, and no `SONDER_API_KEY` of 24+ characters | refused at startup (`config.py:1976-1995`). Account-only deployments on server-private still need a deployment key in `sonder.env`. |
| Only the standalone adapter is used and nothing is set | `api-key` if a key exists, else `account` if `SONDER_REQUIRE_ACCOUNT`, else `local-open` (`serve.py:717-725`) |

Meaning of each mode (`serve.py:2014-2019`):

- `api-key`: the request carries the deployment key.
- `account`: the request carries an account session token.
- `both`: key **and** account token.
- `either`: key **or** account token.

Precedence of settings, lowest to highest: built-in defaults, TOML, the
secrets file (`sonder.env`), process environment, `--set` flags
(`config.py:4-9`). A secret placed in TOML is a validation error.

**Check for the silent downgrade.** A loopback server with no key is open to
every local process. After starting, search the log for
`auth mode is local-open` or `downgraded to local-open`. If you see it and did
not intend it, set a key (next section).

## Set an API key

1. Generate a key (43 characters):

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. Put it in `SONDER_API_KEY` in the secrets file, never in TOML:

   - Windows/workstation: `<default home>\sonder.env` (picked up
     automatically when it exists) or any path passed with `--secrets` /
     `SONDER_SECRETS`. The automatic pickup uses the default home
     (`__main__.py:61-88`), not a `[state].home` set in TOML: if your TOML
     moves the home, pass `--secrets <path>` explicitly.
   - Packaged install: `/etc/sonder/sonder.env`, mode 0600 (the installer
     already wrote a key; `packaging/install_sonder.sh:143-173`).
   - Or export `SONDER_API_KEY` in the server's environment.

   ```text
   SONDER_API_KEY=<paste-key-here>
   ```

   On Linux the loader refuses a secrets file that is group/world accessible
   (`chmod 0600`; `config.py:2523-2529`).

3. Length: at least 24 characters is **required** when `[server].host` is not
   loopback or `profile = "server-private"`; shorter fails configuration
   (`config.py:128`, `:1974-1995`). Use the 43-character generated value
   everywhere.

4. Restart the server (the key is read at startup).

5. Verify:

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:11435/v1/models
   curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $SONDER_API_KEY" \
       http://127.0.0.1:11435/v1/models
   ```

   Expected: `401` without the key (body `{"error": {"message":
   "authentication required", "code": "UNAUTHENTICATED", ...}}`), then `200`.
   On Windows use `curl.exe` and `$env:SONDER_API_KEY`. If the first call
   returns `200`, the server is in `local-open`: the key was not loaded.

In `api-key` mode the key is both administrator and developer credential
(`serve.py:2034-2072`). Rotating it later: [rotate-credentials](rotate-credentials.md).

## Account sign-in

Accounts give per-person sign-in, roles and rate limits.

1. Pick the mode: `SONDER_AUTH_MODE=account` (accounts only), `both` (key and
   account), or `either`. `SONDER_REQUIRE_ACCOUNT=1` is the older spelling of
   `account` and turns even an explicit `api-key` into `account`.
2. `SONDER_AUTH_SECRET` keys the session tokens. Rules:
   - It must not be `sonder-local-dev-secret` (`config.py:2002-2014`).
   - If unset, a random per-install secret is created on first use at
     `<home>/secrets/auth_secret` (mode 0600) and reused (`admin_auth.py:34-73`).
     The packaged installer instead writes `SONDER_AUTH_SECRET` into `sonder.env`.
   - Non-loopback bind: at least 32 characters, otherwise startup is refused
     (`serve.py:913`, `:2378`, `:2381-2382`).
   - `both` needs `SONDER_API_KEY` **and** an explicit `SONDER_AUTH_SECRET` at
     startup even on loopback (`serve.py:2354-2357`).
3. Restart. Then create the first administrator (next section); until an
   account exists nobody can sign in.

Account tokens are sent as `Authorization: Bearer <token>` or
`X-Sonder-Account-Token: <token>`. When the bearer value is a valid API key,
the account token **must** use the `X-Sonder-Account-Token` header
(`serve.py:2001-2007`), so use that header in `both`/`either`.

## Create the first admin

The first account created is always `admin`, whichever way it is created, and
the HTTP route is open to exactly one such creation.

**Over HTTP (remote or scripted)**

1. Choose a one-time bootstrap secret of at least 16 characters (you choose it;
   the server prints nothing).
2. It must be in the server **process environment** as
   `SONDER_BOOTSTRAP_SECRET`. It is not a config-loader secret and is not read
   from `sonder.env` by the loader (`admin_auth.py:75-76`; not in
   `config.py:97-108`). Under systemd, add the line to `/etc/sonder/sonder.env`:
   `EnvironmentFile=` turns it into a process variable
   (`packaging/systemd/sonder.service:20`). Elsewhere:

   ```powershell
   $env:SONDER_BOOTSTRAP_SECRET = "<one-time-secret-16+chars>"
   python -m sonder_runtime serve
   ```

3. Call the route with the secret in a header (no deployment key is needed for
   this route). Windows PowerShell 5.1 drops the inner double quotes of a
   single-quoted JSON argument passed to `curl.exe`, so use the PowerShell
   variant there.

   ```bash
   curl -s -X POST http://127.0.0.1:11435/v1/sonder/register \
       -H "Content-Type: application/json" \
       -H "X-Sonder-Bootstrap-Secret: <one-time-secret-16+chars>" \
       -d '{"username":"admin","password":"<password, 8+ characters>"}'
   ```

   ```powershell
   $body = '{"username":"admin","password":"<password, 8+ characters>"}' -replace '"', '\"'
   curl.exe -s -X POST http://127.0.0.1:11435/v1/sonder/register `
       -H "Content-Type: application/json" `
       -H "X-Sonder-Bootstrap-Secret: <one-time-secret-16+chars>" `
       -d $body
   ```

   Expected: HTTP 201 `{"ok": true, "account": {...}, "message": "Account
   admin created (role admin)."}` (`login_throttle_http.py:52-66`,
   `serve.py:2223-2228`).
4. The secret is consumed: a record is written in the same transaction and a
   second first-admin call is refused (`admin_auth.py:242-248`). Remove
   `SONDER_BOOTSTRAP_SECRET` from the environment/`sonder.env` and restart.

**Locally, without a secret (trusted-local)**

On the machine that owns the state home, the REPL or MCP server can register
directly. Use the same `--config`/`--secrets` as the server so both open the
same database:

```bash
python -m sonder_runtime repl --config <your-sonder.toml>
```

Then at the REPL prompt type `/register admin <password, 8+ characters>`.
The password is typed on the command line, so it lands in the REPL line
history; prefer the HTTP route or clear the history afterwards. `/login` with
no arguments prompts with the input masked (`repl.py:4669-4675`).

The MCP tool `admin_register(username, password)` does the same
(repo-root `server.py:10452-10463`; `repl.py:4661-4666`). These calls run as trusted
local, so they also create further accounts with no authorisation: protect
shell and MCP access to the host accordingly.

## Add more accounts

Over HTTP both must hold (`admin_auth.py:230-236`, `serve.py:7094-7099`):

1. `SONDER_ALLOW_REGISTRATION=1` (or `[server].allow_registration = true`,
   default false, `config.py:156-160`, `:1581-1588`), then restart.
2. The caller is an **admin** account (send its session token, see
   [Sign in and out](#sign-in-and-out)). In `api-key` and `both` mode the
   deployment key is also required: the register route passes the actor only
   when the request is authorised (`adapters/web/login_throttle_http.py:58`), which needs the
   key in those modes (`serve.py:2014-2019`). In `account` and `either` the
   token alone is enough; drop the `Authorization` line below.

```bash
curl -s -X POST http://127.0.0.1:11435/v1/sonder/register \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $SONDER_API_KEY" \
    -H "X-Sonder-Account-Token: $ADMIN_TOKEN" \
    -d '{"username":"alice","password":"<password, 8+ characters>"}'
```

New accounts get role `user`. Change role, tier, flags or ban state with the
admin route. The request must first be authorised (in `api-key` and `both`
mode that needs the deployment key, otherwise 401 `authentication required`,
`serve.py:7105-7107`) and the caller must be an admin **account**: the bare API
key alone gets 403 `login required` (`serve.py:7104-7139`). Drop the
`Authorization` line in `account`/`either` mode:

```bash
curl -s -X POST http://127.0.0.1:11435/v1/sonder/admin/account \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $SONDER_API_KEY" \
    -H "X-Sonder-Account-Token: $ADMIN_TOKEN" \
    -d '{"username":"alice","role":"developer","tier":"pro","dev_flags":"","banned":""}'
```

Fields: `username`, `role`, `tier`, `dev_flags`, `banned` (`true`/`false`;
empty/omitted leaves it unchanged). Equivalents without HTTP: MCP
`admin_set_account`, `admin_accounts` (list), `admin_login`, `admin_whoami`,
`admin_status` (`server.py:10466-10573`); REPL `/accounts`,
`/setaccount alice role=developer tier=pro dev_flags=x banned=false`, `/login`,
`/whoami` (`repl.py:4661-4700`). There is no HTTP route that lists accounts.
`admin_status` reports `api-key` or `local-open` only, not the real mode.

## Sign in and out

```bash
curl -s -X POST http://127.0.0.1:11435/v1/sonder/login \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $SONDER_API_KEY" \
    -d '{"username":"alice","password":"<password>"}'
```

Response `{"ok": true, "token": "...", "account": {...}}`. In `api-key` and
`both` mode the deployment key is required on this call first; without it you
get 401 (`login_throttle_http.py:87`). In `account` and `either` mode the key is not
needed for this call. Use the token on
later calls as `X-Sonder-Account-Token: <token>`.

Sign out revokes only the token you name:

```bash
curl -s -X POST http://127.0.0.1:11435/v1/sonder/logout \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $SONDER_API_KEY" \
    -H "X-Sonder-Account-Token: $TOKEN" -d '{}'
```

Expected `{"ok": true}`. The token must be in exactly one
`X-Sonder-Account-Token` header, the body must be `{}`, and in
`api-key`/`both` the key header is required (`serve.py:6568-6601`). Revoked
and unknown tokens answer identically, so retrying is safe.

## Limits and gotchas

- Roles: `user`, `developer`, `admin` (`ACCOUNT_ROLES`, `admin_auth.py:24`).
  `set_account` validates `role` before it touches anything: an unknown value
  such as `admn` raises `role must be one of: user, developer, admin` and the
  stored account is unchanged (`admin_auth.py:485-492`).
- Usernames: trimmed, lower-cased, 3 to 128 printable characters. Passwords:
  8 to 1024 characters (`admin_auth.py:83-106`).
- Sessions last 14 days (`admin_auth.py:16`).
- Banning an account revokes all its sessions at once; unbanning does not
  restore them (`admin_auth.py:509-517`).
- Per-account rate limit: 120 requests per 60 s; tier `pro` 600; `enterprise`
  3000; `dev_flags` containing `unlimited` bypasses it (`admin_auth.py:17-18`,
  `:520-529`).
- Failed credential attempts are limited per client address (bucket of 10,
  refilling one every 2 s; `adapters/web/lifecycle.py:85-86`) and per username:
  backoff starts after `SONDER_LOGIN_USERNAME_THRESHOLD` failures (default 20),
  one failure decays every `SONDER_LOGIN_USERNAME_DECAY_SECONDS` (default 30),
  at most `SONDER_LOGIN_USERNAME_MAX_ENTRIES` (default 4096) usernames are
  tracked (`adapters/security/login_throttle.py:25-27`,
  `domain/login_throttle.py:24-26`).
- The thin client's local fallback (when the hosted server is unreachable)
  sends **no** key to the local server (`adapters/client_fallback.py:32`):
  a local server in `api-key` mode will answer 401 to it.
- `SONDER_AUTH_SECRET` rotation invalidates every account token
  ([rotate-credentials](rotate-credentials.md#account-auth-secret)).
- Behind a reverse proxy, each client address must reach Sonder as itself
  (`tls_terminated_by_proxy` plus trusted proxy CIDRs), or all users share one
  throttle bucket: [secure-remote-access](secure-remote-access.md).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| 403 `first-admin bootstrap is not authorized` | The server has no `SONDER_BOOTSTRAP_SECRET` (or under 16 characters) in its process environment, the header differs, or the first admin was already created (`admin_auth.py:229`). Check the variable reached the process, then restart. If an admin exists, use [Add more accounts](#add-more-accounts) instead. |
| 403 `registration is disabled` | An account exists and `SONDER_ALLOW_REGISTRATION` is off (`admin_auth.py:232-235`). Enable it and restart. |
| 403 `login required` / `admin role required` | Registration is on but the actor is missing or not an admin (`admin_auth.py:232-235`): no valid admin account token, **or the deployment key is missing** in `api-key`/`both` mode (the actor is dropped when the request is not authorised, `adapters/web/login_throttle_http.py:58`). Send the key and `X-Sonder-Account-Token`. On `/v1/sonder/admin/account` a missing key gives 401 `authentication required` instead. |
| 400 `username must be 3..128 printable characters` / `password must be 8..1024 characters` | Input rules above. |
| 409 `account already exists` | Pick another username (names are case-insensitive). |
| 401 `invalid username or password` | Wrong credentials or unknown user (same answer by design). |
| 429 `AUTH_RATE_LIMITED`, header `Retry-After` | Too many failed attempts from this address or for this username. Wait the number of seconds in `Retry-After` (default 2) before retrying (`login_throttle_http.py:20-30`). |
| 401 `authentication required` (`UNAUTHENTICATED`) on normal calls | Missing/wrong key or token for the effective mode, an expired (14-day) or revoked session, a banned account, or a role row written by an older version that is not one of the three roles. |
| Server refuses to start: `[server].auth_mode invalid: '<value>'` (main `serve` path) or `invalid SONDER_AUTH_MODE` (standalone `serve.py`) | Use exactly lowercase `api-key`, `account`, `both` or `either`; `API_KEY`, `api_key` and `Account` are refused on the main path. |
| Startup error about the auth secret | Under 32 characters on a non-loopback bind, the built-in dev value, or `both` without `SONDER_AUTH_SECRET`. |
| Everything answers 200 without a key | `local-open`: no key was loaded. See [How the server decides its auth mode](#how-the-server-decides-its-auth-mode). |

## Rollback

- Remove a bad `SONDER_API_KEY` or mode setting and restart. On a loopback
  bind with no key the server returns to `local-open`; a non-loopback bind
  refuses to start without a strong key.
- Locked out of every admin account (forgotten password): there is no reset
  command. Set `SONDER_AUTH_MODE=api-key` (with a key) to keep the API
  available, or restore the state home from a backup
  ([backup-restore](backup-restore.md)). A trusted-local `/register` can add
  a new `user` account but cannot promote it.

Next: [rotate-credentials](rotate-credentials.md),
[suspected-secret-exposure](suspected-secret-exposure.md),
[secure-remote-access](secure-remote-access.md).

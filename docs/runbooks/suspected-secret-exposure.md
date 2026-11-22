# Suspected secret exposure

Treat any plausible exposure (key in a pasted log, screen share, stolen
laptop, world-readable secrets file) as real until proven otherwise. Terminal
scrollback and shell logs count: `deploy_sonder.sh --serve` no longer prints the
API key (it points at `/etc/sonder/sonder-local.env`, `deploy_sonder.sh:186-189`),
but an older copy of the script did, so rotate if that output was captured,
recorded or shared.

## 1. Contain — rotate with minimal overlap

```bash
sudo systemctl stop sonder
sudo /opt/sonder/current/venv/bin/python -m sonder_runtime rotate-key \
    --config /etc/sonder/sonder.toml \
    --secrets /etc/sonder/sonder.env --overlap-seconds 60
sudo chown sonder:sonder /etc/sonder/sonder.env
sudo chmod 0600 /etc/sonder/sonder.env
sudo chown sonder:sonder /var/lib/sonder/secrets/rotation.json
sudo chmod 0600 /var/lib/sonder/secrets/rotation.json
sudo chown sonder:sonder /var/lib/sonder/operations.db*
sudo systemctl start sonder
```

`--config` is required on a packaged install: it selects the state home that
receives `rotation.json` and the `API_KEY_ROTATED` event that section 5 relies
on; without it the command uses the invoking user's default home (or
`SONDER_STATE_HOME` before `SONDER_HOME`,
`sonder_runtime/platform/paths.py:157-194`,
`sonder_runtime/__main__.py:60-88`, `:1681-1706`). 60 seconds is the shortest
overlap `rotate-key` accepts (`sonder_runtime/adapters/secrets.py:24-26`,
`:136-139`). Since #808, `/etc/sonder/sonder.env` is owned by `sonder:sonder`
mode `0600` (`packaging/install_sonder.sh:160-161`) while `/etc/sonder` remains
`root` `0755` (`:87`); see
[rotate-credentials](rotate-credentials.md#why-not-sudo--u-sonder) for why the
command runs as `root` and why the `chown` lines are needed.

If account auth is in use, also rotate `SONDER_AUTH_SECRET` (invalidates
all tokens, keeping `/etc/sonder/sonder.env` owned by `sonder:sonder` mode
`0600`) and force re-login.

## 2. Assess exposure window

```bash
python -m sonder_runtime diagnostics --skip-ollama   # redacted bundle
```

Check operations.db `AUTH_FAILED` events and the reverse-proxy access log
for unfamiliar source addresses during the window. On a loopback-only
workstation deployment, network exposure requires local access — check
who had it.

## 3. Assess impact

The API key grants chat plus any developer-gated commands in api-key
mode. Review during the window:

- `/v1/sonder/status` activity and execution evidence for tool use.
- Workspace roots for unexpected writes (`git status` in workspaces).
- operations.db events (backups triggered? drain? rotation you didn't do?).

## 4. Harden

- Verify `/etc/sonder/sonder.env` is owned by `sonder:sonder` and mode `0600`
  (matching `packaging/install_sonder.sh:160-161`; the loader refuses any
  group/world access).
- Verify the plaintext port is loopback-only: `ss -tlnp | grep 11435`.
- Confirm logs pass redaction (grep the journal for the *old* key — it
  should never appear; if it does, file a redaction bug and purge logs).

## 5. Record

Write an incident note: when, what, exposure window, actions, follow-ups.
The `API_KEY_ROTATED` event in operations.db anchors the timeline.

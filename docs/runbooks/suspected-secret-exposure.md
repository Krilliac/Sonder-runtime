# Suspected secret exposure

Treat any plausible exposure (key in a pasted log, screen share, stolen
laptop, world-readable secrets file) as real until proven otherwise.

## 1. Contain — rotate with minimal overlap

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime rotate-key \
    --secrets /etc/sonder/sonder.env --overlap-seconds 60
sudo systemctl restart sonder
```

If account auth is in use, also rotate `SONDER_AUTH_SECRET` (invalidates
all tokens) and force re-login.

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

- Verify `/etc/sonder/sonder.env` is mode 0600 (the loader enforces it).
- Verify the plaintext port is loopback-only: `ss -tlnp | grep 11435`.
- Confirm logs pass redaction (grep the journal for the *old* key — it
  should never appear; if it does, file a redaction bug and purge logs).

## 5. Record

Write an incident note: when, what, exposure window, actions, follow-ups.
The `API_KEY_ROTATED` event in operations.db anchors the timeline.

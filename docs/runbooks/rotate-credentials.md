# Rotate credentials

## API key (planned rotation)

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime rotate-key \
    --secrets /etc/sonder/sonder.env --overlap-seconds 86400
sudo systemctl restart sonder
```

What happens:

- A new `SONDER_API_KEY` is written into the secrets file (mode 0600).
  Keys are never printed; read the new value from the file when updating
  clients.
- The SHA-256 of the previous key is stored with a mandatory expiry
  (default 24h). Until expiry the server accepts either key, so clients
  can be updated without an outage. After expiry only the new key works —
  no cleanup job required.
- The rotation is recorded in operations.db as `API_KEY_ROTATED`.

Update each client with the new key before the overlap expires.

## API key (suspected compromise)

Use a zero-tolerance overlap so the old key dies immediately:

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime rotate-key \
    --secrets /etc/sonder/sonder.env --overlap-seconds 60
sudo systemctl restart sonder
```

Then follow suspected-secret-exposure.md.

## Account auth secret

Rotating `SONDER_AUTH_SECRET` invalidates all account tokens. Edit the
secrets file, restart, and have users log in again. Do this during a
maintenance window unless responding to compromise.

# Start, stop, and drain

## Start

```bash
sudo systemctl start sonder
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime status --json
```

The unit's `ExecStartPre` runs preflight; a failing preflight prevents
the listener from ever opening. Investigate with:

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime preflight \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env --json
```

## Stop (graceful)

```bash
sudo systemctl stop sonder
```

`systemctl stop` delivers SIGTERM. The runtime then:

1. moves to DRAINING and rejects new mutating work,
2. cancels non-durable foreground requests,
3. lets active durable task steps reach a safe checkpoint,
4. marks unfinished ownership as interrupted at the deadline,
5. flushes logs and operation events, closes databases, exits.

The drain deadline (default 25s) is below the unit's
`TimeoutStopSec=35`, so a healthy drain is never SIGKILLed.

## Drain without stopping

The authenticated `POST /v1/admin/drain` endpoint ships with SPEC-2 WP3.
Until then, draining is coupled to service stop.

## Emergency stop

Only if a graceful stop hangs past its deadline:

```bash
sudo systemctl kill -s SIGKILL sonder
```

Afterwards check for interrupted durable work before restarting:
interrupted Autopilot/fleet tasks stay explicit and are never silently
replayed.

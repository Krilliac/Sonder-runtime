# Publish a signed release (TUF signing ceremony)

The publisher turns a built engine bundle into a TUF-signed offline
update bundle that clients verify through their own trust chain — a
compromised mirror cannot forge, downgrade, or substitute a release
(SPEC-4). Nothing here uses custom signature code; it is python-tuf end
to end.

## Roles and key custody (SPEC-4 §1)

| Role | Threshold | Custody |
|---|---:|---|
| root | 2 of 3 | Offline, hardware-backed or encrypted removable media |
| targets | 2 of 3 | Release maintainers; at least one offline |
| snapshot | 1 of 1 | Restricted CI signing identity |
| timestamp | 1 of 1 | Restricted online signing identity |

`tools/tuf_repo.py init` generates all keys into a repo-local
`keystore/` for the automated build/test ceremony and CI. **In
production the root and at least one targets key never live on a build
worker** — generate them on an offline machine, keep the private PEMs on
hardware/encrypted media, and place only the public keys and the signed
`root.json` where the build runs.

## One-time trust bootstrap

```bash
# On the offline signing machine:
python tools/tuf_repo.py init /secure/sonder-tuf-repo
```

This writes `metadata/{root,targets,snapshot,timestamp}.json` and the
private keys under `keystore/` (mode 0600). Vendor the resulting
`metadata/root.json` into the client as its initial trusted root. Root
rotation is a new sequentially-versioned `root.json` signed by the old
root's threshold — never a key swap in place.

## Per-release ceremony

```bash
# 1. Build the engine bundle from the exact tag (hermetic worker):
python -m sonder_runtime update build /path/to/checkout /build/out \
    --bundle-version 1.4.0 --channel stable

# 2. Sign it into the TUF repo and assemble the offline bundle. On an
#    air-gapped signer this step runs where the offline keys live:
python tools/tuf_repo.py bundle /secure/sonder-tuf-repo /build/out /publish/1.4.0
```

`bundle` registers the archive as a TUF target, re-signs `targets`
(threshold), bumps and signs `snapshot`, and finally `timestamp` — the
freshness role, published last. It lays out `/publish/1.4.0/` with
`manifest.json`, `metadata/`, and `targets/`, exactly what
`update import` verifies.

## Verify before distributing

```bash
# Confirm a client accepts it through the real trust path (no unsigned gate):
python - <<'PY'
import sys; sys.path.insert(0, "tools")
from pathlib import Path
from sonder_updates import BundleManifest, verify_bundle_trust
b = Path("/publish/1.4.0")
print(verify_bundle_trust(b, BundleManifest.load(b / "manifest.json"),
                          allow_unverified=False))  # must print: tuf
PY
```

Then publish `/publish/1.4.0/` to any untrusted mirror or removable
medium. The mirror is never trusted — metadata and hashes establish
trust, so a hostile mirror can at worst withhold or corrupt a release
(which the client rejects), never forge one.

## Freshness and freeze protection

`timestamp` expires in 1 day and `snapshot` in 7 by design: an attacker
who freezes a mirror cannot indefinitely hide a security release, and an
expired-metadata client refuses online install with an actionable error.
Re-run the ceremony (or a re-sign of timestamp/snapshot) before those
windows lapse for a channel you keep live.

## Dependency policy (SPEC-4 §16)

Pin the exact tested `tuf` and `cryptography`/`securesystemslib` releases
in the production lock file. A dependency bump requires re-running the
adversarial acceptance suite (`tests/production/test_tuf_publisher.py`
plus the archive-safety and rollback/freeze tests) before shipping.

# Update Manager

Signed engine distribution and staged, rollback-capable installation
(`sonder_updates.py`, `sonder_update_engine.py`). A compromised mirror
cannot forge, downgrade, or substitute a release; the active release is
never modified in place.

## Trust (The Update Framework)

Releases are accepted only through a signed TUF metadata chain with
threshold keys:

| Role | Threshold | Purpose |
|---|---:|---|
| root | 2 of 3 | trust anchors, delegation (offline keys) |
| targets | 2 of 3 | authorizes release targets |
| snapshot | 1 of 1 | binds metadata versions |
| timestamp | 1 of 1 | freshness / freeze protection |

The client ships an initial trusted `root.json`; rotation is a
sequentially-versioned, threshold-signed root. Verification uses
python-tuf — no custom signature code. Bundles without TUF metadata are
refused unless an explicit double gate (`--allow-unverified` **and**
`SONDER_UPDATE_ALLOW_UNSIGNED=1`) is set, which is documented as
non-production only.

## `updates.db`

Update plans (with a validated state machine and compare-and-set
revisions), a durable per-step journal, installed releases (one active,
enforced by a partial unique index), trusted-root history, and channels.

## Adversarially-safe download & extraction

- **Resumable download** — HTTP Range resumes an interrupted transfer only
  when the server's validators still match the persisted `.partial`; else
  it discards and restarts. Length + SHA-256 verified after assembly.
- **Safe extraction** — refuses absolute paths, `..` traversal, symlinks/
  hardlinks, device/fifo members, truncated members, and expansion beyond
  a byte budget.

## Install (staged, gated)

```
sonder update import <bundle> [--channel stable]
sonder update install <update-id> --confirm <nonce>
```

The install runs, in order: admin auth + confirmation nonce → maintenance
lock → trust revalidation → staged extraction with manifest verification
(never in place) → compatibility preflight → **verified SPEC-2 backup** →
drain → atomic publish into a versioned release dir → migrations run by
the *target* release → manifest health checks → **atomic pointer switch**
→ commit. Every step is journaled.

The active-release pointer switch is portable: an atomic symlink swap on
POSIX, and an atomic pointer-file fallback where directory symlinks are
unprivileged (Windows).

## Rollback

```
sonder update rollback --confirm <last-8-of-previous-release-id>
```

Migration or health failure during install rolls back automatically with
the previous release still active and the failed release retained as
evidence. Operator rollback switches to the previous release; it is
**refused** when the previous release directory is missing (a state
restore from the pre-update backup is required instead). See
[upgrade-rollback](../runbooks/upgrade-rollback.md).

## Offline updates

An offline bundle (metadata + targets + manifest) imports through the
**same** trust and install flow — the update path is inherently local
files, verified by a filesystem-aware TUF fetcher. Fits air-gapped and
portable-media workflows.

## Publishing (the signing ceremony)

`tools/tuf_repo.py` initializes a TUF repo at the role thresholds above,
signs a built bundle's archive as a target, and assembles a
client-importable offline bundle. Full ceremony, key custody, and freshness
guidance: [publish-release](../runbooks/publish-release.md). Optional deps
pinned in `requirements-update.txt`.

## Flutter System page

`GET /v1/admin/updates/status` backs the app's System page (SPEC-4 §14):
running version/commit, active/previous releases, available/in-flight
plans with verification state and confirm nonce, and impact-aware
install/rollback affordances (the privileged operations stay on the admin
CLI). It polls durable state, so it survives the service restart during a
switch.

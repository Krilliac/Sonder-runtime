# Update Manager

Signed engine distribution and staged, rollback-capable installation
(`sonder_runtime/adapters/updates/service.py` and `engine.py`). A compromised mirror
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

The operator installs the vendor's initial trusted `root.json` at
`<SONDER_HOME>/updates/trusted_root.json` (or points
`SONDER_UPDATE_TRUSTED_ROOT` at it); signed bundles are refused until it
exists, and a bundle's own `metadata/root.json` is never a trust anchor.
Accepted metadata persists under `<SONDER_HOME>/updates/tuf-metadata/`, so
rotation is a sequentially-versioned, threshold-signed root chained from that
anchor and older metadata is refused. Verification uses
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

## Install and rollback

The commands, the operator conventions (`update` takes no `--config`, it
resolves its home from the environment), the identity that can run install and
rollback on the packaged layout, the install step order, the drain behaviour of
the command-line install and rollback are in one place: the
[upgrade and rollback runbook](../runbooks/upgrade-rollback.md). Concepts
that matter here:

- Installation is staged and gated by an admin confirmation nonce, a
  maintenance lock, trust revalidation and a verified backup taken before
  anything changes. Every step is journaled in `updates.db`.
- The active release is never modified in place. A new release is published
  into a versioned directory and activated by an atomic pointer switch,
  portable as a symlink swap on POSIX and an atomic pointer-file fallback
  where directory symlinks are unprivileged (Windows).
- A migration or health failure rolls back automatically with the previous
  release still active and the failed release retained as evidence.
  Operator rollback is **refused** when the previous release directory is
  missing; a state restore from the pre-update backup is required instead.
- Draining: the command-line install runs against a stopped service and
  records its drain step as skipped (`no running service attached`); stop
  the service first, or use `drain`
  ([start-stop-drain](../runbooks/start-stop-drain.md)).

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

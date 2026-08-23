# Pre-tag release smoke check

Before proposing a release tag, run one command that answers both questions
a release manager actually needs answered: does the version triad agree, and
does the checkout actually work end to end?

```bash
# Linux/macOS
scripts/release_smoke.sh
scripts/release_smoke.sh --tag app-v1.2.3 \
  --revision 0123456789abcdef0123456789abcdef01234567 --require-release
```

```powershell
# Windows PowerShell
powershell -NoProfile -File scripts\release_smoke.ps1
powershell -NoProfile -File scripts\release_smoke.ps1 -Tag app-v1.2.3 `
  -Revision 0123456789abcdef0123456789abcdef01234567 -RequireRelease
```

## What it checks

1. **Version policy** — `scripts/check_release_version.py`: the runtime
   (`sonder_version.VERSION`), the Flutter app (`app/pubspec.yaml`), and the
   proposed tag agree, per [release-version-policy](release-version-policy.md).
2. **Runtime smoke** — `python -m sonder_runtime smoke --skip-ollama`: config
   loads, pending schema migrations apply, and an operations-store event
   round-trips. This is the same check CI's release job depends on
   transitively through `preflight`, run directly so it catches a broken
   checkout before a tag is even proposed.

Both checks always run even if the first fails, so one invocation reports
every problem instead of stopping at the first. Neither check contacts
Ollama, a package registry, or any network endpoint, and neither writes
outside the configured `SONDER_HOME`; this script commits, tags, or pushes
nothing itself.

## Exit codes

`0` only if both checks pass. `1` if either fails — the summary line names
which one(s). Wire this into a pre-tag checklist or a CI job the same way
[publish-release](publish-release.md) already wires in
`check_release_version.py --require-release --json` for the tagged release
job; this script is the local, fast version a maintainer runs first.

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
   round-trips. Tagged builds call the reusable Python CI workflow at the tag's
   exact commit; that workflow runs this smoke check before its test suite.
   Run it locally as well to catch a broken checkout before proposing a tag.

Both checks always run even if the first fails, so one invocation reports
every problem instead of stopping at the first. Neither check contacts
Ollama, a package registry, or any network endpoint, and neither writes
outside the configured `SONDER_HOME`; this script commits, tags, or pushes
nothing itself.

## Exit codes

`0` only if both checks pass. `1` if either fails — the summary line names
which one(s). The [tagged build](publish-release.md) runs this script through
its Python gate, and the release job repeats the version check before publishing.

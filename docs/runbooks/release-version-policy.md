# Release version policy

Sonder has one public release version at the point of publication. The
runtime, Flutter application, and Git tag keep distinct development syntax,
but a tagged application release may publish only when their public SemVer
values agree.

## Sources and syntax

| Source | Development syntax | Release syntax |
|---|---|---|
| `sonder_version.VERSION` | `MAJOR.MINOR.PATCH.devN` | `MAJOR.MINOR.PATCH` |
| `app/pubspec.yaml` | `MAJOR.MINOR.PATCH+BUILD` | `MAJOR.MINOR.PATCH+BUILD` |
| Git tag | none | `app-vMAJOR.MINOR.PATCH` |

Flutter's positive integer `+BUILD` is an installer/build counter. It is not
part of the public release version and need not equal a Git revision or tag.
Pre-release spellings other than the runtime's `.devN` are intentionally not
accepted; expand the checker and this contract together if a beta channel is
introduced.

## Compatibility rules

1. An untagged `.devN` runtime is a development build. Its base version may
   differ from Flutter's while work is in progress, but the checker reports
   that divergence and marks the build not release-ready.
2. An untagged stable runtime is a release candidate. Its base version must
   already match Flutter's base version, but it remains not release-ready
   without a tag.
3. For `app-vX.Y.Z`, the runtime must be stable `X.Y.Z`, Flutter must be
   `X.Y.Z+BUILD`, and the build revision must be a full 40-character Git SHA.
   Any mismatch fails closed.

The current source values are deliberately unchanged by this policy. They are
valid development identities, not a releasable tag combination.

## Check before tagging

```bash
# Development diagnostics; exits nonzero only for invalid/incompatible input.
python scripts/check_release_version.py --json

# Exact command for a proposed tagged release; any mismatch exits 1.
python scripts/check_release_version.py \
  --tag app-v1.2.3 \
  --revision 0123456789abcdef0123456789abcdef01234567 \
  --require-release --json
```

In GitHub Actions, omit `--tag` only when `GITHUB_REF_TYPE` is not `tag`.
For a tag event the checker reads `GITHUB_REF_NAME`; it also reads
`GITHUB_SHA` for the build identity. A release workflow should run the second
form before packaging or publishing.

The JSON report includes parsed source versions, build counter, mode,
release-readiness, the exact commit, a compact display identity, and stable
diagnostic codes suitable for CI annotations. The checker uses only the Python
standard library and parses `sonder_version.py` without importing or executing
it.

Before proposing a tag, prefer
[release-smoke-check](release-smoke-check.md), which runs this checker
together with a real end-to-end runtime smoke test in one command.

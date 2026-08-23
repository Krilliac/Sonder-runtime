# Contributing

Thanks for looking. This is a single-maintainer project, so the most useful
contributions are small, verified, and self-explaining.

## Before you open a PR

Run the focused regression set while iterating, then the complete suite before
you open a PR. The suite contains more than ten thousand tests, so use the
repository selector instead of guessing which files cover a change:

```bash
python -m venv venv && venv/Scripts/pip install -r requirements-dev.txt
venv/Scripts/python scripts/select_regression_tests.py --since main --format args
venv/Scripts/python scripts/profile_tests.py --since main
venv/Scripts/python -m pytest -q
```

`profile_tests.py` is serial by default and writes a bounded, content-free
timing report to `.pytest_cache/sonder-test-profile.json`. `--workers 2` opts
into file-grouped xdist execution; the harness refuses more than four workers.
Selection is an iteration aid, not a replacement for the final full-suite gate.
See [the performance runbook](docs/runbooks/performance.md) for profiling,
diagnostic, and privacy details.

A green suite is expected, not impressive — say what you *verified*, not what
you believe. "Reproduced the failure, fixed it, the new test fails without the
fix" is worth more than a paragraph of description.

If you change behaviour, add the test that would have caught the bug. If you
cannot write that test, say so in the PR and explain why; that is useful
information, not a failure.

### Reproducing CI exactly

`.github/workflows/ci.yml`'s `tests` job runs, in order: the MCP compatibility
import check, four `scripts/check_*.py` gate scripts, then the full suite. To
reproduce the same run locally:

```bash
python -m pip install -r requirements-dev.txt
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python scripts/check_error_signals.py
python scripts/check_history_privacy.py --json
python -m pytest -q -n auto --dist load --durations=25
```

`-n auto` picks worker count from local CPU count, same as `pytest-xdist` does
on the runner; it will differ from CI's if your machine doesn't have 4 cores,
but the tests themselves don't depend on the exact count. A run that fails in
CI but passes locally is worth reproducing with the exact same flags before
assuming it's environmental.

If a CI run fails, its `pytest-report` artifact (JUnit XML) is attached to the
workflow run summary — pull it down before re-running blind, it names the
failing tests and their durations without needing to re-run the suite.

### Test markers

Defined in `pytest.ini`, applied selectively (most tests carry none and run
unconditionally):

| Marker | Meaning | Run it |
|---|---|---|
| `unit` | isolated, no external services | `pytest -m unit` |
| `integration` | spans multiple Sonder modules/processes | `pytest -m integration` |
| `network` | needs explicit network access | `pytest -m network --run-network` |
| `model` | needs a live Ollama or hosted model | `pytest -m model --run-model` |

`network` and `model` tests are **skipped by default** everywhere, including
CI (see `pytest_collection_modifyitems` in the root `conftest.py`) — CI never
opts in, since the runner has no live model or outbound network access. Pass
`--run-network` / `--run-model` yourself if you're validating one of those
paths locally.

### CI job names are load-bearing

`tests` (in `ci.yml`) and `analyze`, `android`, `linux`, `windows`, `macos`,
`integrity` (in `build-apps.yml`) are required branch-protection status
checks, matched by exact job id/context name. Turning one into a matrix job
or renaming it changes the reported context name and silently strands every
PR that can never satisfy the now-vanished required check — fix that in the
repo's branch protection settings first if it's ever genuinely needed, not by
editing the workflow alone.
CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs five
additional gates before the test suite, none of which `pytest` alone
exercises. Run them locally if your change touches `sonder_runtime/`, error
strings, Git history, or the operator docs — a change that only fails one of
these can otherwise look green all the way to the PR:

```bash
venv/Scripts/python scripts/check_architecture.py         # layer/import boundaries inside sonder_runtime/
venv/Scripts/python scripts/check_requirement_evidence.py # master-spec requirement IDs vs the evidence ledger
venv/Scripts/python scripts/check_error_signals.py         # shrink-only ratchet on legacy "ERROR:"-prefixed returns
venv/Scripts/python scripts/check_history_privacy.py --json # no new sensitive Git-history debt
venv/Scripts/python scripts/check_doc_links.py              # relative links in README/wiki/runbooks resolve
```

Each is silent and exits `0` on success; a nonzero exit lists exactly what it
found. `check_architecture.py` is the one most contributors hit first — it
rejects, for example, a new `sqlite3.connect` or `subprocess` call outside
`sonder_runtime/adapters/`, or a domain module importing anything outside
`sonder_runtime/domain/` plus the standard library.

## What tends to get merged

- A bug with a reproduction, a fix, and a test that pins it.
- A correction to documentation that claims something the code does not do.
- Platform fixes. This is developed on Windows against WSL and an NVIDIA GPU;
  macOS, AMD, CPU-only, and multi-GPU paths get far less exercise and are
  where real breakage hides.
- Anything that makes a failure louder. A probe that fails silently to a
  plausible-looking default has cost this project more than one bad afternoon.

## What to raise as an issue first

Large refactors, new tool surfaces, new dependencies, and anything that
changes the security posture described in [SECURITY.md](SECURITY.md). A
2000-line PR that arrives unannounced is hard to review honestly, and being
told "no" after you wrote it is worse than being told "no" before.

## Never commit

- **Personal training data or adapter weights trained on it.**
  `build_personal_dataset.py` builds from private code and its output is
  local-only. `personal_dataset.jsonl`, `combined_personal.jsonl`,
  `sonder-personal-lora/`, `sonder-personal-merged/`, and `Modelfile.personal`
  are all gitignored for that reason. Model weights can memorise their training
  data; publishing an adapter fitted to a private codebase can leak it.
- **`memory.db` or anything derived from it that has not been scrubbed.** It
  holds raw prompts and responses. To share what the runtime *learned*, use the
  opt-in export instead, which passes every row through the privacy classifier:

  ```bash
  venv/Scripts/python contribute.py     # -> contrib/lessons_contrib.jsonl
  ```

  Read the file before you attach it to anything. The script says the same.
- **Credentials of any kind**, including in test fixtures. Use obviously fake
  values; the existing tests use AWS's published example key and similar.

## Style

Match the file you are editing. The codebase favours comments that explain
*why* a thing is the way it is — especially when the obvious approach is wrong
— over comments that restate the code. If a fix is subtle, the comment
explaining what bit you is part of the fix.

## Licence

Contributions are accepted under the [Apache License 2.0](LICENSE), the same
terms as the project.

# Git-history privacy debt

This record is the evidence for the repository's historical private-object
gate. It describes reachability metadata only; it does not contain private
training data, adapter contents, credentials, or blob excerpts.

## Audited revision

The audit was run against `fdff77405ddd444a70cdb43f812a43889d6113fa`, the
current `origin/main` revision when this record was prepared. The exact
object/path baseline was pinned at
`655b75ec9950e03c0a0c4701a18f8e04b0dea51c` and contains exactly seven
object/path pairs across three unique object IDs.

```text
python scripts/check_history_privacy.py --json
```

Observed result at the audited revision:

```text
passed=true
ok=true
known_debt_count=7
unexpected_count=0
clean=false
baseline_entry_count=7
baseline_revision=655b75ec9950e03c0a0c4701a18f8e04b0dea51c
```

The debt-aware check therefore permits ordinary CI while proving that no new
flagged object/path pair was added. It does not clear the release gate.

## Release gate

Tagged publishing must run:

```text
python scripts/check_history_privacy.py --require-clean --json
```

At the audited revision this returns `passed=false` because the seven pinned
pairs remain reachable. `--require-clean` is the fail-closed release gate:
an inspection error, an unexpected pair, or any reachable pinned pair blocks
publication. The release workflow performs this check before downloading
artifacts or invoking the GitHub Release publisher.

The pairs are historical and already public in the repository's reachable
Git graph. Removing files from the current checkout cannot remove their blob
objects from that graph. Clearing the gate requires a separately authorized
history rewrite followed by remote-reference and mirror cleanup. This change
does not rewrite history, remove user data, or claim that the release history
is clean.

## Inspection limits

The checker is intentionally bounded and content-free:

- Git commands have a 30-second total inspection deadline.
- Git inventory output is capped at 16 MiB.
- At most 400 bytes of one-line Git stderr are surfaced for diagnostics.
- The checkout must contain complete, non-shallow history; blob filtering is
  allowed because the checker reads object IDs and NUL-delimited paths only.
- Replacement refs and legacy grafts are rejected, so they cannot hide the
  historical pairs.

Normal CI must continue using the debt-aware command to enforce shrink-only
growth protection. Tagged release jobs must continue using `--require-clean`.
Do not remove a baseline entry until the corresponding object/path pair is no
longer reachable and the checker reports that removal; otherwise the entry
becomes unexpected debt and the gate fails.

# Claim checker

`claim_check` is a deterministic, read-only check of a model's security or code
claim, run before a model or a person spends time on it. It does not judge
whether the claim is true. It answers three narrower questions from the source
text alone:

1. Is the citation real? The file is inside the repository root (no escape
   through `..`, symlinks or junctions), the cited line exists, and each named
   symbol appears at or near that line.
2. Is a guard of the claimed category visible nearby? Guard shapes are length or
   count bounds, `Math.Min`/`Clamp`, null checks, validation `throw`/`return`,
   authorization calls, path containment, rate limits, timeouts, locks and
   output encoding. They are searched within a window of the cited lines and
   around direct callers found by a word search of the named symbols (and the
   entry point, when given). A name the claim says is missing ("without
   `CanActOn`") is searched too, never required.
3. Is this claim a duplicate? Claims on the same file whose cited lines are
   within `duplicate_window_lines` collapse into the earliest one.

Every check returns `pass`, `fail` or `not_checkable` with evidence
(`file:line` and the line text). The verdict is `reject` when a citation check
failed, `flag` when a guard-shaped line was found nearby, and `keep` otherwise.
`flag` carries no signal about whether the claim is true: on a private labelled
set of 943 security claims it fired on 64% of confirmed and 52% of refuted
claims. `reject` catches only bad citations: 0 of 53 confirmed claims were
rejected and 17 of 888 refuted claims (about 2%). Files that are binary,
unreadable or over `max_file_bytes` are `not_checkable` and never rejected. A
file the read policy refuses answers exactly like a missing file (`fail`, "no
such file in the repository"), so the checker cannot be used to probe which
paths exist.

## Surfaces

| Surface | Use |
| --- | --- |
| Python | `sonder_runtime.adapters.claim_checker.check_claim(root, Claim(...))`, `ClaimChecker(root).check_many(claims)`, `check_jsonl(src, dst, root)` |
| MCP tool | `claim_check(root, file, line, line_end, symbols, category, guard, entry_point, claim_text, claim_id, claims_json)`. Pass one claim by fields, or a JSON array in `claims_json`. `claim_text` fills a missing line and symbols from prose (`Line 134`, `src/A.cs:120`, backticked identifiers). Returns JSON. |
| Batch | `python scripts/claim_check_batch.py claims.jsonl report.jsonl --root <repo>`: one output line per input line; a bad row becomes a typed `error` entry. |
| Measurement | `python scripts/claim_check_eval.py labelled.jsonl [--root <repo>] [--symbols all\|any\|off] [--set guard_window_lines=8]` prints counts by label and verdict only. |

The MCP tool is graded `safe`. `root` must be inside the operator's authorized
file roots, and each file read passes the repository read guard (credentials and
control state are neither checked nor quoted as evidence).

## Settings

The `[claim_check]` section holds every window and limit: `symbol_window_lines`,
`guard_window_lines`, `caller_window_lines`, `duplicate_window_lines`,
`max_file_bytes`, `max_callers`, `max_caller_files`, `max_cache_bytes`,
`max_evidence`, `max_batch_claims`, `skip_dirs`, `extra_guard_patterns` and
`symbol_match` (`all` or `any`). Defaults and ranges are in
`sonder_runtime/platform/claim_check_config.py`.

## What it can and cannot tell you

- It catches wrong citations (a path that does not exist or escapes the root, a
  line past the end of the file, a symbol that is nowhere near the cited line).
- It cannot tell whether an attacker can reach the code, or whether a guard
  elsewhere makes the claim false. Most refuted claims in practice are of that
  kind. A `flag` (a guard-shaped line was found nearby) is not evidence for or
  against the claim: it fired on 64% of confirmed and 52% of refuted claims in
  the private set of 943, so do not rank, discard or accept claims by it.
- A `reject` is reliable but rare: it caught 17 of 888 refuted claims (about 2%)
  and none of the 53 confirmed ones. Most refuted claims pass the citation checks.
- The guard shapes are regular expressions over text, not a parser. Comment
  lines are skipped; code in languages other than the cited file's extension is
  not searched for callers.

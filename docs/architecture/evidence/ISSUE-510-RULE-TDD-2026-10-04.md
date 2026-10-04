# Original issue 510 runtime-rule TDD qualification

This is a measured, deterministic pressure comparison for original section 9 of
[issue 510](https://github.com/Krilliac/Sonder-runtime/issues/510). The significant
rule under test is the existing batching/coalescing runtime guard, which steers
repeated independent file reads toward the registered bounded `context_pack`.
No production behavior or promotion threshold is changed by this qualification.

## Scenario and authority

`tests/test_issue510_rule_tdd_pressure.py` creates twelve ordinary synthetic text
files containing distinct numeric values. The task is to read every file and
return their sum, under the real agent loop's fixed ten-tool-step budget. The
filesystem dispatcher and both read tools are real. The decision producer is a
deterministic fixture: it normally chooses one unread file; when it observes the
actual host advisory, it follows the stated four-file batching bound. Its sum is
computed only from observed tool output. The evaluator separately computes the
expected sum from the fixture specification. Source digests confirm neither
trial changes the files.

The same producer and task first run with no active batch counterpart (the
existing rule is absent), then with the production counterpart resolver and
production default thresholds. Other guard and dispatch behavior remains active.
Background speculation is disabled equally in both trials so the synchronous
round-trip measurement has a stable boundary. There are no external model calls,
paid API keys, assigned success scores, or fabricated held-out publication
records. This exercises the actual host rule rather than claiming to measure a
language model's response to a prose skill.

## Recorded failure and improvement

| Measurement | Without rule | With rule |
|---|---:|---:|
| Observed files | 10 of 12 | 12 of 12 |
| Returned result | `partial sum=55; files=10` | `sum=78; files=12` |
| Correct task completion | false | true |
| Model decisions, including final synthesis | 11 | 7 |
| Tool round trips | 10 | 6 |
| Single `file_read` calls | 10 | 3 |
| `context_pack` calls | 0 | 3 |
| Host guard actions | none | one advisory |

The baseline failure is budget exhaustion before two required files are read.
The rule reduces model decisions by four and tool round trips by four while
completing the previously incomplete task. It preserves every required value.
The pressure comparison checks the exact metrics rather than inferring success
from the presence of a guard path. These counters establish reduced orchestration
cost; they do not measure provider tokens, wall-clock performance or physical
filesystem read count.

## Regression boundary

The same comparison with two files completes correctly in both cases, with three
model decisions, two single reads and no steering. Existing guard canaries cover
bounded refusal and exhaustion, successful batch/reset windows, ordinary retries
of failed reads, rereading covered targets, preserved single-tool contracts,
unavailable batch forms, other read families, state changes, and untouched
mutating tools. They also check bounded per-file model views through real packs.

Reproduce the recorded comparison and its ordinary regression cohort with:

```bash
python -m pytest -q -s tests/test_issue510_rule_tdd_pressure.py
python -m pytest -q tests/test_batch_coalescing_guard.py tests/test_remaining_procedural_publication.py
```

The combined pressure, guard and publication cohort passes 57 tests without
skips. All seven repository gates pass. This qualification changes tests and
documentation only; the existing production batching rule is unchanged.

This completes a meaningful original skill/rule TDD cycle for the existing host
batching rule: recorded pressure failure without it, the same task with it,
measured improvement, and regression controls. It supports this scenario and
rule boundary. It does not qualify every skill, universal live-model quality,
the later A–H strategy ablation program, or automatic policy promotion. Future
significant rule changes still require their own pressure and regression proof.

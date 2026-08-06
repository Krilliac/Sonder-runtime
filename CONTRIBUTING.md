# Contributing

Thanks for looking. This is a single-maintainer project, so the most useful
contributions are small, verified, and self-explaining.

## Before you open a PR

Run the suite. It is fast and it is the gate:

```bash
python -m venv venv && venv/Scripts/pip install -r requirements-dev.txt
venv/Scripts/python -m pytest -q
```

A green suite is expected, not impressive — say what you *verified*, not what
you believe. "Reproduced the failure, fixed it, the new test fails without the
fix" is worth more than a paragraph of description.

If you change behaviour, add the test that would have caught the bug. If you
cannot write that test, say so in the PR and explain why; that is useful
information, not a failure.

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

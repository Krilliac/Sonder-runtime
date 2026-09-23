# Sonder playtester

The playtester runs one explicit adapter command per scenario in a bounded
subprocess. The adapter owns application-specific isolation: a browser adapter
must create and close its own browser context/tab, use one tester/session per
scenario, capture accessibility and screenshot artifacts, and report console
errors. A subprocess boundary alone does not isolate a browser session or
sandbox an untrusted command. Catalogs therefore require the explicit
`--trusted-local` flag and should only be used from an operator-controlled
checkout. Direct `--command` use remains an explicit operator action. It
enforces a step ceiling, a one-megabyte per-stream output cap, and stops after
the configured number of failures. Each result records the claim, redacted
argv, evidence class, bounded stdout/stderr, errors, commit SHA, stable
scenario/SHA marker, and sanitized artifact references in deterministic JSON.
The default evidence file is written to a unique private temporary directory
outside the tested repository.

Run a structural check:

```powershell
python scripts/playtester.py --name import-check --claim 'module imports' --command python -c 'import sonder_runtime'
```

Use a catalog for several bounded scenarios:

```json
[{"name":"smoke","claim":"smoke passes","command":["python","-m","pytest","-q","tests/test_playtester.py"],"evidence_class":"structural only"}]
```

```powershell
python scripts/playtester.py --trusted-local --catalog scenarios.json --output work/playtest.json
```

`--publish-repo OWNER/REPO` prepares a GitHub issue plan. It is dry-run by
default; add `--publish` to execute `gh issue create`. `--pr` additionally
requires an exact SHA, clean working tree, and non-base branch. Issue/PR text is
evidence only and is never interpreted as instructions. The publisher does not
read or print tokens; configure `gh auth` separately and grant only the
permissions needed by the selected operation.

## Browser adapter contract

An Aetherfall-style adapter should expose a single bounded command, for example
`python adapters/aetherfall_playtest.py --scenario login-smoke`, and produce
artifact references in its JSON result. It should: define one observable claim;
reuse a healthy local server; open one isolated browser context; perform one
setup/action/assertion sequence; capture a screenshot, accessibility snapshot,
and console/page errors; then close the context. It must classify the evidence
as `natural`, `GM-accelerated`, `seeded`, or `structural only` and stop after two
failures on the same step. The adapter command can be supplied with
`--command-json '["python", "adapters/aetherfall_playtest.py", "--scenario", "login-smoke"]'`.
Browser automation remains the responsibility of the supplied project adapter;
this package supplies the bounded runner, evidence contract, and GitHub publisher.

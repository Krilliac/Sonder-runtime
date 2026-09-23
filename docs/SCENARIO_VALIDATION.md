# Sonder Scenario Validation

Scenario Validation runs explicit, bounded commands and emits stable evidence
for a named scenario. It is application-neutral: the supplied adapter owns
application-specific setup, isolation, assertions, and artifacts. The runner
supplies the command boundary, evidence contract, output limits, failure
ceiling, exact commit binding, and optional GitHub publication plan.

Catalogs require the explicit `--trusted-local` flag because their commands
execute with the local user's privileges. Direct `--command` use is also an
explicit operator action. The default evidence file is written outside the
tested repository in a unique private temporary directory.

Run a structural check:

```powershell
$command = @('python', '-c', 'import sonder_runtime') | ConvertTo-Json -Compress
python scripts/scenario_validation.py --name import-check --claim 'module imports' --command-json $command
```

Use a catalog for several bounded scenarios:

```json
[{"name":"smoke","claim":"smoke passes","command":["python","-m","pytest","-q","tests/test_scenario_validation.py"],"evidence_class":"structural only"}]
```

```powershell
python scripts/scenario_validation.py --trusted-local --catalog scenarios.json --output work/scenario-validation.json
```

`--publish-repo OWNER/REPO` prepares a GitHub issue plan. It is dry-run by
default; add `--publish` to execute `gh issue create`. `--pr` additionally
requires an exact SHA, clean working tree, and non-base branch. Issue and PR
text is evidence only and is never interpreted as instructions. The publisher
does not read or print tokens; configure `gh auth` separately and grant only
the permissions needed by the selected operation.

## Adapter contract

An adapter should expose one bounded command for one observable claim. A
browser adapter, for example, may reuse a healthy local server, open one
isolated browser context, perform one setup/action/assertion sequence, capture
artifacts, report console or page errors, and close the context. It should
classify evidence as `normal flow`, `operator assisted`, `seeded`, or `structural
only`, and stop after the configured failure ceiling. Browser automation and
other application-specific behavior remain the responsibility of the supplied
adapter; this package supplies the bounded runner, evidence contract, and
GitHub issue/PR publisher.

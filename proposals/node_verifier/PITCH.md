# node_verifier

A `verifiers`-registry-compatible backend, `node_run(artifact, spec) -> Verdict(passed, reason, detail)`,
that grounds JavaScript artifacts by actually executing them with a real `node` subprocess
(writing the code plus an optional appended `check` snippet to a temp `.js` file), instead of
trusting the model's own claim that the code works. It follows the exact shape of
`verifiers.python_exec`/`cpp_compile`: a monkeypatchable `_run` seam, a `VerifierUnavailable`
raise when `node` isn't on PATH (detected via `FileNotFoundError` or a "not recognized"/"command
not found" shell message), and a truncated `detail` for the repair loop.

This is valuable because Sonder Runtime's solver/reward/ladder loop is verifier-agnostic by design —
today it only grounds Python, C++, pytest, and mypy, so any JS-generation task (a common request
class) gets no real pass/fail oracle and silently falls back to ungrounded self-report. Adding
`node_run` extends real execution-grounded self-repair to JavaScript with zero changes to
`solver.py`/`reward.py`/`game_ladder.py`.

To integrate: `import verifiers, node_verifier; verifiers.REGISTRY["node_run"] = node_verifier.node_run`,
then call `verifiers.verify("node_run", artifact, {"check": "..."})` anywhere a verifier name is
accepted (e.g. `solver.solve_verified(..., verifier="node_run")`). No existing file needs editing.

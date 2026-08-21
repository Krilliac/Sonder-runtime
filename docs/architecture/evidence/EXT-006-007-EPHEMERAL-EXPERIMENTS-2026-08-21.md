# EXT-006/007 ephemeral experiment evidence

Date: 2026-08-21

`EphemeralExperimentManager` provides bounded inspect/define/start/stop/delete
transitions behind explicit startup authority. Definitions are held in memory,
children run in temporary directories through an injected host boundary, and
cleanup removes temporary material. There is no persistence or promotion API.

Verification: `python -m pytest -q tests/test_extension_host.py tests/test_extension_experiments.py --basetemp .pytest-next-lanes` — **14 passed** including the host boundary; compileall, architecture, and diff checks pass.

Limitations: the lifecycle is not yet exposed through the production CLI/API,
and the formal EXT-006/007 rows remain unverified.

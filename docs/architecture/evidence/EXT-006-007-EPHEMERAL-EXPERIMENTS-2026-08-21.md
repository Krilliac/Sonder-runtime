# EXT-006/007 ephemeral experiment evidence

Date: 2026-08-21

`EphemeralExperimentManager` provides bounded inspect/define/start/stop/delete
transitions behind explicit startup authority. Definitions are held in memory,
children run in temporary directories through an injected host boundary, and
cleanup removes temporary material. A typed `ExperimentLimits` contract now
allows the production CLI/HTTP/application path to request a native memory cap;
the bootstrap translates it into the Windows Job Object host limiter. There is
still no persistence or promotion API.

Verification: `python -m pytest -q tests/test_extension_host.py tests/test_extension_experiments.py tests/production/test_extension_composition.py --basetemp C:\\Users\\Nathan\\Documents\\Codex\\pytest-extension-limits-1` — **19 passed** including the production native-limit boundary; compileall, architecture, and diff checks pass.

Limitations: the lifecycle is exposed through typed CLI/API routes, and a
trusted persisted installation can enter the same ephemeral host boundary, but
definitions remain ephemeral and there is no promotion/deployment API. The
formal EXT-006/007 rows remain unverified pending broader production UI and
operator acceptance evidence.

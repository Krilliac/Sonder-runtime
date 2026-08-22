# WP1 artifact-risk root retirement evidence

## Closure item

The root `artifact_risk.py` compatibility redirect has been retired. The
packaged `sonder_runtime.adapters.artifact_risk` adapter is now the sole owner
of bounded artifact inspection and execution-risk policy enforcement.

## Verification

- `tests/test_artifact_risk.py`, `tests/test_artifact_risk_policy.py`, and
  `tests/test_artifact_risk_server.py` import the packaged adapter directly.
- `tests/test_artifact_risk_compatibility.py` verifies root absence, packaged
  API ownership, and the direct server import boundary.
- `server.py` retains the same `artifact_risk_module` patch point, so existing
  monkeypatch-based server behavior remains available through the packaged
  module.
- `scripts/check_architecture.py` permanently ratchets `artifact_risk.py` as
  a retired root module.
- The local-system package manifest no longer requires the retired root file.

## Focused command

`python -m pytest -q tests/test_artifact_risk.py tests/test_artifact_risk_compatibility.py tests/test_artifact_risk_policy.py tests/test_artifact_risk_server.py`

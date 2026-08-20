# WP2 SESSION-010 — retention and privacy policy

`session_privacy` defines explicit event privacy classes and immutable rules
for retention, export, redaction, and deletion eligibility. It is a pure policy
boundary: applying a rule to storage remains an adapter concern, and this slice
performs no data deletion.

Evidence: `tests/test_session_privacy.py`, architecture/evidence gates, and
compileall.

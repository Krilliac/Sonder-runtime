# Recovery HTTP draft — not release ready

This isolated slice adds runtime-owned, bounded recovery handles and explicit HTTP preparation, attachment, verification resume, status and close. It preserves separate attachment and verifier approval calls and the original output receipt. It does not grant approval through HTTP.

**Do not merge or deploy this slice as verified recovery support.** The current real loopback HTTP acceptance test fails its bounded wait while the callback performs fresh authorization/private-path checks and terminal publication. Its test fixture uses a scripted model and verifier gateway. The recorded run took447.54s;28 architecture checks passed and the one HTTP acceptance test failed. No timeout is evidence that the callback stopped.

Other recorded checks:7 registry/route checks passed; the later combined run had41passes including the actual isolated owned recovery slot and existing ownership checks. Its architecture cycle was corrected and architecture was subsequently verified. The full registry capacity, foreign selection, ambiguous submission, callback reconciliation, failed cleanup and restart matrix is not complete. Daybreak review is pending. Generated catalogs are not refreshed for this draft.

The registry retains at most32 records and allows one active callback. Closed records remain retained; there is no eviction or restart adoption claim. No public original-output retrieval or recovery UI is included.

Reproduce remaining acceptance failure from this worktree:

```
python -m pytest tests/test_app_recovery_http.py -q --tb=short
```

The installed runtime was not modified by this development lane. This draft requires further implementation, verification and security review before promotion.

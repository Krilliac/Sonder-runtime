# WP1 web-fetch packaging evidence — 2026-08-21

## Scope

The canonical bounded public web-text fetch implementation now lives in
`sonder_runtime.adapters.web_fetch`. The root `web_tools.py` module remains a
compatibility surface for the pinned HTTP resolver/connection and `_request`
transport seam, while its public `web_fetch` function delegates to the
packaged `fetch_raw` entrypoint.

## Preserved invariants

- DNS resolution and globally-routable URL validation remain in the pinned
  transport path.
- Redirect targets are revalidated and each connection uses the validated
  address rather than an unpinned opener.
- Raw response and decompression bounds remain enforced by the compatibility
  transport.
- The packaged adapter preserves allowlisted charset/BOM decoding, binary
  signature/control rejection, HTML/XML text extraction, and max-character
  bounds.
- Existing monkeypatch seams for `web_tools._request` and `web_tools._urlopen`
  remain effective through the root compatibility delegate.
- Consent and block-page handling remain in the typed adapter surface.

## Verification

Focused command:

```text
python -m pytest -q tests/test_web_tools.py tests/test_web_tools_security.py tests/test_web_fetch_adapter.py tests/test_web_fetch_compatibility.py
```

Result: **55 passed**.

Additional gates:

- `python -m compileall -q sonder_runtime/adapters/web_fetch.py web_tools.py`: passed.
- `python scripts/check_architecture.py`: still reports the pre-existing
  `sonder_runtime/interfaces/http/facades/extensions.py` urllib violation;
  no new web-fetch ownership violation is reported.
- The broader architecture test file retains unrelated existing failures in
  the extensions/selfmod migration area and was not changed by this slice.

## Ownership ratchet

`scripts/check_architecture.py` now requires the packaged `fetch_raw`
entrypoint and rejects reintroducing `_decode_web_document` into `web_tools.py`.
`tests/test_web_fetch_compatibility.py` proves root delegation and transport
seam preservation.

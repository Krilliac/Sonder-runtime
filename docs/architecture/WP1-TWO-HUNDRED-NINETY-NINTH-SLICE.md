# WP1 Two-Hundred-Ninety-Ninth Slice — natural model request grammar

## Boundary

The imperative whole-turn grammar that recognizes explicit model, fanout,
profiled-fanout and ensemble requests now lives in
`sonder_runtime/domain/natural_model_request.py`, together with
`FANOUT_SELECTION_PROFILES`, `INTERPRETER_LIKE_MODEL_SELECTOR_PREFIXES`,
`is_interpreter_like_bare_model_selector` and `fanout_profile_scope`. Every
regular expression, the preference-word guard and the delimiter contract are
unchanged. The grammar takes two injected callables: `profile_scope`, which
maps a reviewed profile to its scope, and `bare_tagged_request`, which
resolves a terse `<name>:<tag>` selector against the live catalog.

`server.py` keeps `natural_model_request(text)` and `_fanout_profile_scope`
as thin compatibility delegates: the first injects the root resolvers so the
existing `resolve_discovered_model` monkeypatch seam keeps working, and the
second turns the domain refusal message into the adapter's `ModelCallError`
exactly as before. `FANOUT_SELECTION_PROFILES`,
`_INTERPRETER_LIKE_MODEL_SELECTOR_PREFIXES` and
`_is_interpreter_like_bare_model_selector` remain identity-preserving
aliases. `_bare_tagged_model_request` deliberately did not move: it performs
catalog discovery, which is adapter I/O.

## Evidence

- `tests/test_natural_model_request_boundary.py` verifies the alias identities, the profile-scope message and its root `ModelCallError` wrapping, the interpreter-like guard, every recognized whole-turn form through injected callables, the terse-tag deferral to the resolver, and the root wrapper wiring.
- `python -m pytest -q tests/test_natural_model_request_boundary.py tests/test_model_fanout.py tests/test_server_helpers.py -k "boundary or natural or fanout or model_request or ensemble"`
- `python scripts/check_architecture.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`

# WP1 HTTP facade — bounded health/status route family

The HTTP entry point still contains legacy model and control routes, but the
lifecycle family is now behind a root-free facade:

- `/live`
- `/ready`
- `/health`
- `/version`
- `/metrics`

`sonder_runtime/interfaces/http/facades/health_status.py` classifies these
routes and renders them through an injected lifecycle application port. It has
no import of the legacy `server` root and performs no model, network, or socket
work. `serve.py` retains authentication, CORS, metrics headers, and response
writing at the HTTP boundary; only the selected route-family translation is
adapted in this slice.

Evidence: `tests/test_wp1_http_facade.py`.

# Sonder Inference federation: which faults move a request to another endpoint

A request moves to another endpoint (or the Ollama fallback) only when the
gateway can prove nothing was executed, or when the only work done delivered
no output (the 429 `overloaded` row below). Everything else is final after
one attempt. See `docs/architecture/sonder-inference-provider.md` for the
full gateway design.

| Wire result | Gateway error | Retried elsewhere |
|---|---|---|
| connection refused, unresolvable host, 503 `not_ready` | `SonderInferenceUnreachable` | yes |
| 503 `overloaded` | `SonderInferenceOverloaded` | yes |
| 429 with error code `overloaded` (SI: queue full, or engine-scheduler rejection) | `SonderInferenceOverloaded` | yes |
| 503 `upstream_unreachable` with header `X-Sonder-Proxy-Upstream: unreachable` | `SonderInferenceUnreachable` | yes |
| any other 429 | `CapacityExceeded` | no |
| 504 (the caller's deadline expired) | `DependencyUnavailable` | no |
| bare 502 (may follow an upstream reset after send), 500, mid-stream reset | `DependencyUnavailable` | no |

### Accepted exception: scheduler rejection

SI answers the engine-scheduler rejection as `429 overloaded` too
(`openai.cpp:1322`, set from `session.cpp:2464` when `runtime->submit` fails),
which can run after prefill, so some backend work may have happened. It cannot
be told apart from the queue-full 429 on the wire. The gateway retries both on
purpose: no output was delivered to the caller and generation has no external
side effect. SI's `federation-worker-contract.md` "Nothing executed" table
lists only the queue-full 429; adding the scheduler rejection there is a
cross-repo follow-up.

## Proxy contract (Node1 Caddy)

The gateway trusts the dial-failure signal only when both the header and the
JSON code are present. Caddy must answer upstream dial failures (and only
those) from `handle_errors` with a 503:

```caddyfile
handle_errors {
    @dial expression {http.error.status_code} == 502 && {http.error.message}.contains("dial")
    handle @dial {
        header X-Sonder-Proxy-Upstream unreachable
        header Content-Type application/json
        respond `{"error":{"code":"upstream_unreachable","message":"upstream dial failed"}}` 503
    }
}
```

Applying the Caddyfile change is an owner live step on Node1; until it is
applied, a proxy dial failure stays a final 502.

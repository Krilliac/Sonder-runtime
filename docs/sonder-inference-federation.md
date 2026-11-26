# Sonder Inference federation: which faults move a request to another endpoint

A request moves to another endpoint (or the Ollama fallback) only when the
gateway can prove nothing was executed. Everything else is final after one
attempt.

| Wire result | Gateway error | Retried elsewhere |
|---|---|---|
| connection refused, unresolvable host, 503 `not_ready` | `SonderInferenceUnreachable` | yes |
| 503 `overloaded` | `SonderInferenceOverloaded` | yes |
| 429 with error code `overloaded` (SI: "nothing was executed") | `SonderInferenceOverloaded` | yes |
| 503 `upstream_unreachable` with header `X-Sonder-Proxy-Upstream: unreachable` | `SonderInferenceUnreachable` | yes |
| any other 429 (scheduler rejection) | `CapacityExceeded` | no |
| 504 (the caller's deadline expired) | `DependencyUnavailable` | no |
| bare 502 (may follow an upstream reset after send), 500, mid-stream reset | `DependencyUnavailable` | no |

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

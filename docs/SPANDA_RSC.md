# Spanda Exact-Match Epistemic Uncertainty (R_sc)

Additive, **default OFF** guardrail for `/v1/chat/completions`.

## What it does

When enabled, Sonder samples **K** completions (temperature > 0), clusters
assistant strings by deterministic normalize (casefold, strip punctuation /
whitespace), and computes Exact-Match Normalized Entropy:

```
w_i    = |C_i| / K
H_norm = 0 if n==1 else (-Σ w_i ln w_i) / ln K
R_sc   = α · H_norm + (1 − α) · (1 − w_max)
```

The **dominant consensus** string is returned as the primary assistant message.
Response headers report the score:

| Header | Meaning |
|--------|---------|
| `X-Sonder-Spanda-Rsc` | R_sc in `[0,1]` |
| `X-Sonder-Spanda-Clusters` | number of lexical clusters |
| `X-Sonder-Spanda-Decision` | `consensus` or `uncertain` |
| `X-Sonder-Spanda-Uncertain` | `1` if `rsc >= threshold` else `0` |

## Enable

Config (`sonder.toml`), all defaults safe/off:

```toml
[spanda]
enabled = false
k = 3
threshold = 0.35
block = false
alpha = 0.5
sample_temperature = 0.2
```

Per-request (without flipping global config):

```
X-Sonder-Spanda: 1
```

If `block = true` and `rsc >= threshold`, the server returns **409** with an
`epistemic_uncertainty` error body (existing JSON error style). Auth is
unchanged — api-key / account gates still apply before sampling.

## Caveat: Confident Mode Collapse

Unanimous **wrong** answers still produce a **low** R_sc. Lexical consensus is
not ground truth. Do not treat R_sc≈0 as correctness on frontier models doing
factual recall.

## Implementation

- Vendored pure Python: `sonder_runtime/platform/spanda_rsc.py` (no hard dep on
  BSL Rust gateway / PyPI `spnda`).
- Config: `sonder_runtime/platform/spanda_config.py` + `[spanda]` in typed loader.
- HTTP: `sonder_runtime/interfaces/http/spanda_serve.py` wired from `serve.py`.

Reference: [Spnda](https://github.com/Adarshent/Spnda), article intent on
lexical entropy for production latency.

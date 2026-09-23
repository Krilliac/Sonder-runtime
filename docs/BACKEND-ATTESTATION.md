# Backend route attestation

`scripts/backend_attest.py` probes one OpenAI-compatible model route for chat,
structured JSON, and cancellation behavior. It stores capability results and
reason codes in the private state directory; provider responses and API keys
are not written to the evidence file.

Preview a local route without a model call or state write:

```powershell
python scripts/backend_attest.py --base-url http://127.0.0.1:8080 --model my-model --dry-run
```

Run the bounded probe by omitting `--dry-run`. A non-loopback endpoint requires
both HTTPS and `--allow-cloud`; the probe prompts are fixed and contain no
project data. Set `SONDER_OPENAI_API_KEY` in the process environment when the
endpoint requires it. Keep the key out of command arguments and logs.

The recorded backend is `openai-compatible`. A route profile must use that
backend label and the same model name to consume this evidence. A failed,
missing, synthetic, or stale record keeps the optional route ineligible.

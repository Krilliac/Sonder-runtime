"""Pure projections for local runtime configuration summaries."""


def local_runtime_summary(options, requested_context):
    """Return the stable public summary projection for local model options.

    The option builder and context policy stay injectable at the caller
    boundary; this module owns only the shape and fallback semantics of the
    resulting summary.
    """
    return {
        "num_thread": options.get("num_thread", "ollama-default"),
        "num_gpu": options.get("num_gpu", "ollama-default"),
        "num_batch": options.get("num_batch", "ollama-default"),
        "num_ctx_native": options.get("num_ctx", "ollama-default"),
        "num_ctx_requested": requested_context,
    }

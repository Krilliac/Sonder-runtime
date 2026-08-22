"""Policies for environment-backed runtime options."""

import os


def cpu_thread_default(*, cpu_count=None):
    """Return the safe default worker count for local model requests.

    ``cpu_count`` is injectable for deterministic callers and tests; when it
    is omitted the host's current CPU count is used.  A missing or invalid
    host count still produces a usable single-thread minimum.
    """
    count = os.cpu_count() if cpu_count is None else cpu_count
    return max(1, count or 4)


def env_int_option(name, default=None, *, environ=None):
    """Return an integer environment option with the historical fallbacks."""
    values = environ if environ is not None else os.environ
    raw = values.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if raw.lower() in ("", "auto", "default", "none", "off"):
        return None
    try:
        return int(raw)
    except ValueError:
        return default


def local_model_options(
    temperature,
    num_predict,
    num_ctx,
    *,
    native_context=None,
    environ=None,
):
    """Build the runtime options sent with a local Ollama model request.

    The context normalizer is injected so this platform policy does not depend
    on the domain context policy.  ``environ`` is likewise injectable for
    deterministic tests while callers may still observe live process changes.
    """
    values = environ if environ is not None else os.environ
    normalize_context = native_context or (lambda value: value)
    options = {
        "temperature": temperature,
        "num_predict": num_predict,
        "num_ctx": normalize_context(num_ctx),
    }
    runtime = {
        "num_thread": env_int_option(
            "SONDER_NUM_THREAD", cpu_thread_default(), environ=values
        ),
        # Let Ollama select the available accelerator unless the operator
        # explicitly pins one.
        "num_gpu": env_int_option("SONDER_NUM_GPU", environ=values),
        "num_batch": env_int_option(
            "SONDER_NUM_BATCH", 512, environ=values
        ),
    }
    for key, value in runtime.items():
        if value is not None:
            options[key] = value
    return options

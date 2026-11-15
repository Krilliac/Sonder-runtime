"""Per-process memory that keeps one model on one context window.

Ollama reloads a model whenever ``num_ctx`` differs between requests, so the
window chosen for a model must not depend on whether the latest ``/api/show``
probe happened to succeed.  This tracker remembers, per model:

* the last SUCCESSFUL ``(context_length, parameter_size)``, replayed when a
  later probe fails;
* the ``num_ctx`` the model declares in its own parameters, already clamped by
  its advertised ``context_length``;
* which never-probed models were already reported, so the log fires once.

Thread-safe: every method takes the tracker's own lock.  Cloud names are never
tracked or probed.
"""

import logging
import threading

import sonder_runtime.platform.context_policy as context_policy
import sonder_runtime.platform.declared_context as declared_context

_LOG = logging.getLogger("sonder.server")


def model_key(name) -> str:
    """One key per model as Ollama sees it: ``x`` and ``x:latest`` are the same.

    Case-folded; ``:latest`` is appended when the last path segment (after the
    last ``/``) carries no tag, so ``host:5000/model`` still gets one.
    """
    key = str(name or "").strip().casefold()
    if key and ":" not in key.rsplit("/", 1)[-1]:
        key += ":latest"
    return key


class ModelContextMemory:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_success = {}
        self._declared = {}
        self._unprobed_logged = set()

    def clear(self) -> None:
        with self._lock:
            self._last_success.clear()
            self._declared.clear()
            self._unprobed_logged.clear()

    def recall(self, model):
        """Return the last successful ``(context_length, parameter_size)``."""
        with self._lock:
            return self._last_success.get(model_key(model), (None, None))

    def replay(self, model, context_length, parameter_size):
        """Return cached metadata, substituting the last success for a failure."""
        if context_length is None and parameter_size is None:
            return self.recall(model)
        return context_length, parameter_size

    def declared_for(self, model):
        with self._lock:
            return self._declared.get(model_key(model))

    def settle(self, model, declared, context_length, parameter_size):
        """Record a finished probe and return the metadata to use.

        ``declared`` is the model's own ``num_ctx`` (or ``None``) when the probe
        fully succeeded and ``False`` when it failed.  A success is stored (the
        declared window after its clamp to the advertised length, or cleared
        when none is declared).  A failure replays the last success, if any.
        """
        key = model_key(model)
        with self._lock:
            if declared is not False:
                self._last_success[key] = (context_length, parameter_size)
                resolved = declared_context.resolve_declared_context(declared, context_length)
                if resolved:
                    self._declared[key] = resolved
                else:
                    self._declared.pop(key, None)
                return context_length, parameter_size
            # A probe that did not finish is a failure, partial fields included.
            return self._last_success.get(key, (None, None))

    def metadata_unavailable(self, model, context_length, parameter_size, *, cloud, pinned) -> bool:
        """True for a local model that has never been probed successfully.

        A probe that succeeded with no usable fields is a success, not this.
        The first such sighting of a model is logged unless an operator pin
        decides the window anyway.
        """
        if cloud or context_length is not None or parameter_size is not None:
            return False
        key = model_key(model)
        if not key:
            return False
        with self._lock:
            if key in self._last_success:
                return False
            first = key not in self._unprobed_logged and not pinned
            if first:  # a pin skips the log, so the line still appears once unpinned
                self._unprobed_logged.add(key)
        if first:
            _LOG.info(
                "model %r has no successful metadata probe yet; context is capped at %d",
                key[:128], context_policy.METADATA_UNAVAILABLE_CONTEXT,
            )
        return True

    def auto_window(self, model, context_length, parameter_size, residency_ceiling, *, cloud) -> int:
        """Choose the native window for a model the caller did not size.

        The context policy picks the automatic window (with the never-probed
        floor for local models); the model's declared window then only lowers
        it.  Cloud models and operator pins get exactly the policy's value.  A
        declared ``num_ctx`` below ``MIN_CONTEXT`` is sent as declared: it is the
        model's own Modelfile value (and normally its advertised maximum), so a
        larger window cannot help.
        """
        pinned = declared_context.operator_pinned()
        unavailable = self.metadata_unavailable(
            model, context_length, parameter_size, cloud=cloud, pinned=pinned,
        )
        auto = context_policy.auto_context(
            context_length, parameter_size, residency_ceiling, unavailable,
        )
        if cloud or pinned:
            return auto
        return declared_context.apply_declared_ceiling(auto, self.declared_for(model))

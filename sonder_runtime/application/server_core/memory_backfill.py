"""Shared local embedding backfill engine for lessons and interactions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class BackfillSpec:
    """Presentation and validation strings for an embedding backfill domain."""

    header: str
    id_list_label: str
    probe_text: str
    text_field: str
    dry_run_hint: str
    cloud_error_template: str
    loopback_error: str
    probe_error: str
    revision_error: str
    dimension_error_template: str


LESSON_BACKFILL_SPEC = BackfillSpec(
    header="memory embedding backfill",
    id_list_label="lesson IDs",
    probe_text="Sonder embedding compatibility probe",
    text_field="text",
    dry_run_hint=(
        "dry-run only; apply probes the live local model, then refreshes "
        "missing or incompatible vectors."
    ),
    cloud_error_template=(
        "embedding backfill requires a local model; configured model "
        "%r looks cloud-hosted."
    ),
    loopback_error=(
        "embedding refresh is local-only; configured Ollama endpoint "
        "is not loopback."
    ),
    probe_error="embedding refresh could not probe the configured local model.",
    revision_error="embedding model revision changed during compatibility probe.",
    dimension_error_template=(
        "local embedding probe dimension %d does not match "
        "the configured/known dimension %d; update SONDER_EMBED_DIM "
        "or the model configuration before refreshing stored text."
    ),
)

INTERACTION_BACKFILL_SPEC = BackfillSpec(
    header="memory interaction embedding backfill",
    id_list_label="interaction IDs",
    probe_text="Sonder interaction embedding compatibility probe",
    text_field="task",
    dry_run_hint=(
        "dry-run only; apply probes the live local model, then refreshes "
        "bounded task vectors from their locally stored task text."
    ),
    cloud_error_template=(
        "interaction embedding backfill requires a local model; "
        "configured model %r looks cloud-hosted."
    ),
    loopback_error=(
        "interaction embedding refresh is local-only; configured "
        "Ollama endpoint is not loopback."
    ),
    probe_error=(
        "interaction embedding refresh could not probe the "
        "configured local model."
    ),
    revision_error=(
        "interaction embedding model revision changed during "
        "compatibility probe."
    ),
    dimension_error_template=(
        "local interaction embedding probe dimension %d does "
        "not match the configured/known dimension %d; update "
        "SONDER_EMBED_DIM or the model configuration before "
        "refreshing stored task text."
    ),
)


@dataclass(frozen=True)
class BackfillResult:
    """Structured outcome of an embedding backfill run."""

    report: str = ""
    error_detail: str = ""


def run_embedding_backfill(
    spec: BackfillSpec,
    *,
    limit: int,
    apply: bool,
    approved_model: str,
    approved_base: str,
    expected_dimension: int | None,
    is_cloud_model: Callable[[str], bool],
    endpoint_is_loopback: Callable[[str], bool],
    canonical_model_name: Callable[[str], str],
    current_revision: Callable[..., Any],
    embed_fn: Callable[..., Any],
    valid_vector: Callable[[Any], bool],
    to_blob: Callable[[Any], bytes],
    open_db: Callable[[], Any],
    select_rows: Callable[..., Sequence[dict[str, Any]]],
    refresh_row: Callable[..., bool],
    count_remaining: Callable[..., int],
) -> BackfillResult:
    """Validate local-only constraints, optionally refresh embeddings, and format report."""
    if is_cloud_model(approved_model):
        return BackfillResult(error_detail=spec.cloud_error_template % approved_model)
    if not endpoint_is_loopback(approved_base):
        return BackfillResult(error_detail=spec.loopback_error)

    conn = open_db()
    updated = 0
    failed: list[str] = []
    conflicted: list[str] = []
    target_dimension = expected_dimension
    target_model = canonical_model_name(approved_model)
    target_revision = current_revision(model=approved_model, base=approved_base)
    revision_changed = False
    try:
        if apply:
            probe = embed_fn(
                spec.probe_text,
                timeout=30,
                base=approved_base,
                model=approved_model,
            )
            if not valid_vector(probe):
                return BackfillResult(error_detail=spec.probe_error)
            if current_revision(model=approved_model, base=approved_base) != target_revision:
                return BackfillResult(error_detail=spec.revision_error)
            probed_dimension = len(probe)
            if target_dimension is not None and probed_dimension != target_dimension:
                return BackfillResult(
                    error_detail=spec.dimension_error_template
                    % (probed_dimension, target_dimension)
                )
            target_dimension = probed_dimension
        rows = list(
            select_rows(
                conn,
                target_model,
                revision=target_revision,
                dimension=target_dimension,
                limit=limit,
            )
        )
        if apply:
            for row in rows:
                row_id = str(row["id"])
                try:
                    if (
                        current_revision(model=approved_model, base=approved_base)
                        != target_revision
                    ):
                        failed.append(row_id)
                        revision_changed = True
                        break
                    vector = embed_fn(
                        row.get(spec.text_field) or "",
                        timeout=30,
                        base=approved_base,
                        model=approved_model,
                    )
                    if not valid_vector(vector) or len(vector) != target_dimension:
                        failed.append(row_id)
                        continue
                    blob = to_blob(vector)
                    if refresh_row(
                        conn,
                        row_id,
                        blob,
                        target_model,
                        revision=target_revision,
                        dimension=target_dimension,
                        expected=row,
                    ):
                        updated += 1
                    else:
                        conflicted.append(row_id)
                except (OSError, TypeError, ValueError, OverflowError):
                    failed.append(row_id)
        remaining = count_remaining(
            conn,
            target_model,
            revision=target_revision,
            dimension=target_dimension,
        )
    finally:
        conn.close()

    lines = [
        spec.header,
        "  mode: %s | local model: %s" % ("apply" if apply else "dry-run", approved_model),
        "  target dimension: %s"
        % (target_dimension if target_dimension else "unknown until apply probe"),
        "  selected stale/missing: %d | updated: %d | conflicted: %d | failed: %d | remaining: %d"
        % (len(rows), updated, len(conflicted), len(failed), remaining),
    ]
    if rows:
        lines.append("  %s: %s" % (spec.id_list_label, ", ".join(str(r["id"]) for r in rows)))
    if failed:
        lines.append("  failed IDs: %s" % ", ".join(failed))
    if conflicted:
        lines.append("  skipped concurrently changed IDs: %s" % ", ".join(conflicted))
    if revision_changed:
        lines.append("  model revision changed during refresh; rerun the batch.")
    if not apply and rows:
        lines.append("  " + spec.dry_run_hint)
    return BackfillResult(report="\n".join(lines))

"""Attribute parsed diagnostics to translation units, projects or steps.

Ninja and make print a failing step's whole output together, so a
diagnostic belongs to the step whose line range contains it -- including a
header error printed while compiling that TU. MSBuild under ``/MP`` and
``-m`` interleaves, so it is attributed per project from the raw
``[x.vcxproj]`` suffix, and per TU only where the echo order proves it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..diagnostics.model import Diagnostic, DiagnosticSet, render_diagnostic
from .model import BuildModel, BuildSystem
from .output import StepSegment, parse_msbuild_projects


MAX_ATTRIBUTION_DIAGNOSTICS = 50
MAX_FIRST_ERRORS = 24
_ERRORISH = frozenset({"error", "fatal"})
# Localized cl/link severities and the /showIncludes note (the English
# language pack missing is why they appear).
_LOCALIZED_SEVERITY_RE = re.compile(
    r"\)\s*:\s*(?:schwerwiegender Fehler|Fehler|Warnung|erreur fatale|erreur|avertissement|"
    r"errore irreversibile|errore|avviso|error grave|advertencia|erro fatal|erro|aviso|"
    r"ошибка|предупреждение|fout|waarschuwing|błąd|ostrzeżenie|"
    r"エラー|警告|错误|錯誤|오류|경고)\s+"
    r"[A-Z]{1,3}\d{4}"
)
_LOCALIZED_NOTES = (
    "Hinweis: Einlesen der Datei:",
    "Remarque : inclusion du fichier :",
    "Remarque : inclusion du fichier :",
    "Nota: file incluso",
    "Nota: inclusión del archivo:",
    "注意: 包含文件:",
)


@dataclass(frozen=True, slots=True)
class Attribution:
    kind: str
    label: str
    project: str
    first_error: Diagnostic | None
    error_count: int
    warning_count: int
    diagnostics: tuple[Diagnostic, ...]


def _attribution(kind: str, label: str, project: str, items: list[Diagnostic]) -> Attribution:
    ordered = sorted(items, key=lambda d: d.raw_line_no)
    first = next((item for item in ordered if item.severity in _ERRORISH), None)
    return Attribution(
        kind=kind, label=label[:1024], project=project[:256], first_error=first,
        error_count=sum(1 for item in ordered if item.severity in _ERRORISH),
        warning_count=sum(1 for item in ordered if item.severity == "warning"),
        diagnostics=tuple(ordered[:MAX_ATTRIBUTION_DIAGNOSTICS]),
    )


def attribute_steps(text: str, dset: DiagnosticSet, segments: tuple[StepSegment, ...], *,
                    model: BuildModel | None = None) -> tuple[Attribution, ...]:
    """Attributions in first-appearance order.

    ``text`` must be the exact string ``dset`` and ``segments`` came from.
    Diagnostics outside any segment are grouped as one ``step`` attribution
    labelled ``(unattributed)``.
    """
    system = model.system if model is not None else None
    if system is BuildSystem.MSBUILD or (not segments and _looks_msbuild(text)):
        return _attribute_msbuild(text, dset)
    ordered_segments = sorted(segments, key=lambda s: s.first_line)
    buckets: dict[int, list[Diagnostic]] = {}
    loose: list[Diagnostic] = []
    for item in dset.diagnostics:
        owner = None
        for index, segment in enumerate(ordered_segments):
            if segment.contains(item.raw_line_no):
                owner = index
                break
        if owner is None:
            loose.append(item)
        else:
            buckets.setdefault(owner, []).append(item)
    out: list[Attribution] = []
    for index in sorted(buckets, key=lambda i: ordered_segments[i].first_line):
        segment = ordered_segments[index]
        kind = "tu" if segment.tu_label else "step"
        out.append(_attribution(kind, segment.tu_label or segment.step_label or "(step)",
                                segment.project, buckets[index]))
    if loose:
        out.append(_attribution("step", "(unattributed)", "", loose))
    return tuple(out)


def _looks_msbuild(text: str) -> bool:
    return ".vcxproj]" in str(text or "")[:4_000_000]


def _attribute_msbuild(text: str, dset: DiagnosticSet) -> tuple[Attribution, ...]:
    rows = parse_msbuild_projects(text, dset)
    by_line = {row.raw_line_no: row for row in rows}
    groups: dict[tuple[str, str, str], list[Diagnostic]] = {}
    order: list[tuple[str, str, str]] = []
    for item in dset.diagnostics:
        row = by_line.get(item.raw_line_no)
        if row is None or not row.project:
            key = ("step", "(unattributed)", "")
        elif row.tu_label:
            key = ("tu", row.tu_label, row.project)
        else:
            key = ("project", row.project, row.project)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    return tuple(_attribution(kind, label, project, groups[(kind, label, project)])
                 for kind, label, project in order)


def first_errors(atts: tuple[Attribution, ...], *, limit: int = MAX_FIRST_ERRORS) -> tuple[Diagnostic, ...]:
    """The first error of each attribution, in output order, bounded."""
    limit = max(1, min(int(limit), MAX_FIRST_ERRORS))
    found = [item.first_error for item in atts if item.first_error is not None]
    found.sort(key=lambda d: d.raw_line_no)
    return tuple(found[:limit])


def normalized_error_lines(diags: tuple[Diagnostic, ...], *, max_chars: int = 240) -> tuple[str, ...]:
    """One rendered line per diagnostic (<= 24 lines of <= ``max_chars``)."""
    limit = max(40, min(int(max_chars), 240))
    return tuple(render_diagnostic(item, max_chars=limit) for item in diags[:MAX_FIRST_ERRORS])


def detect_non_english_msvc(text: str) -> bool:
    """True when cl/link output is localized (no English language pack for VSLANG)."""
    sample = str(text or "")[:8_000_000]
    if any(note in sample for note in _LOCALIZED_NOTES):
        return True
    return bool(_LOCALIZED_SEVERITY_RE.search(sample))


__all__ = [
    "Attribution", "MAX_FIRST_ERRORS", "attribute_steps", "detect_non_english_msvc",
    "first_errors", "normalized_error_lines",
]

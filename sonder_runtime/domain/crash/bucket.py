"""Group crash reports by signature (QA dump folders, core directories)."""
from __future__ import annotations

from typing import Sequence

from ..common.errors import InvalidInput
from .model import CrashBucket, CrashReport, frame_label


MAX_BUCKET_INPUTS = 64
MAX_SAMPLES = 5


def top_frame_label(report: CrashReport) -> str:
    thread = report.crashing_thread()
    if thread is None or not thread.frames:
        return ""
    for frame in thread.frames:
        if frame.in_project:
            return frame_label(frame)
    return frame_label(thread.frames[0])


def bucket_reports(reports: Sequence[CrashReport]) -> tuple[CrashBucket, ...]:
    """One ``CrashBucket`` per signature, most frequent first (<= 64 inputs)."""
    items = list(reports)
    if len(items) > MAX_BUCKET_INPUTS:
        raise InvalidInput("at most %d reports can be bucketed" % MAX_BUCKET_INPUTS)
    groups: dict[str, list[CrashReport]] = {}
    order: list[str] = []
    for report in items:
        if report.signature not in groups:
            groups[report.signature] = []
            order.append(report.signature)
        groups[report.signature].append(report)
    buckets = []
    for signature in order:
        members = groups[signature]
        first = members[0]
        buckets.append(CrashBucket(
            signature=signature, basis=first.signature_basis, count=len(members),
            exception_name=first.exception.name if first.exception is not None else "NO_EXCEPTION",
            top_frame=top_frame_label(first),
            sample_labels=tuple(member.source_label for member in members[:MAX_SAMPLES]),
        ))
    buckets.sort(key=lambda bucket: (-bucket.count, order.index(bucket.signature)))
    return tuple(buckets)


__all__ = ["MAX_BUCKET_INPUTS", "bucket_reports", "top_frame_label"]

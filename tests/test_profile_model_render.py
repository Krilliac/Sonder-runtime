"""ProfileDigest construction bounds, content digest, wire size and text rendering."""
from __future__ import annotations

import json

from sonder_runtime.domain.profiling.model import (
    ELISION,
    SCHEMA,
    AllocationHotspot,
    CaptureMetadata,
    FrameStats,
    HotPath,
    ProfileDigest,
    ProfileFunction,
    Spike,
    elide_frames,
    with_source,
)
from sonder_runtime.domain.profiling.render import (
    UNTRUSTED_LABEL,
    digest_from_wire,
    digest_to_wire,
    render_digest,
)

SHA = "ab" * 32


def _wire_bytes(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _sample() -> ProfileDigest:
    return ProfileDigest(
        source_kind="callgrind",
        metric="Ir",
        unit="events",
        metadata=CaptureMetadata(tool="callgrind", tool_version="3.22.0", event="Ir",
                                 process="./spark"),
        top_self=(ProfileFunction(name="Physics::Integrate", file="src/physics.cpp", line=88,
                                  self_pct=60.0, total_pct=61.0, self_value=600,
                                  total_value=610, calls=3, in_project=True),),
        hot_paths=(HotPath(frames=("main", "Game::Tick", "Physics::Integrate"), pct=61.0, value=610),),
        spikes=(Spike(kind="frame", label="frame #30", start_ns=5, duration_ns=40_000_000,
                      ratio_to_median=2.4, thread="MainThread"),),
        frames=FrameStats(count=120, p50_ms=16.6, p95_ms=16.9, p99_ms=40.0, max_ms=40.0,
                          budget_ms=16.6, over_budget=4),
        allocations=(AllocationHotspot(function="leak_buffer", allocations=10, peak_bytes=40960,
                                       leaked_bytes=40960, file="leak.cpp", line=7),),
        notes=("n1",),
    )


def test_strings_are_cleaned_and_clipped_at_construction():
    hostile = "\x1b[31mevil\x1b[0m\x00name\n" + "A" * 10_000
    fn = ProfileFunction(name=hostile, module="\x07mod", file="f\r\n.c")
    assert fn.name.startswith("evil") and "\x1b" not in fn.name and "\x00" not in fn.name
    assert len(fn.name) == 240
    assert fn.module == "mod" and fn.file == "f .c"
    assert ProfileFunction(name="").name == "?"


def test_numbers_are_finite_and_non_negative():
    fn = ProfileFunction(name="x", self_pct=float("nan"), total_pct=float("inf"),
                         self_value=-5, total_value="12", calls="junk")
    assert fn.self_pct == 0.0 and fn.total_pct == 0.0
    assert fn.self_value == 0 and fn.total_value == 12 and fn.calls == 0


def test_lists_are_capped_and_hot_paths_elided():
    many = tuple(ProfileFunction(name="f%d" % i) for i in range(100))
    digest = ProfileDigest(top_self=many, top_total=many,
                           hot_paths=tuple(HotPath(frames=("a",)) for _ in range(40)),
                           notes=tuple("note %d" % i for i in range(100)))
    assert len(digest.top_self) == 25 and len(digest.top_total) == 25
    assert len(digest.hot_paths) == 10 and len(digest.notes) == 16
    path = HotPath(frames=tuple("f%d" % i for i in range(100)))
    assert len(path.frames) == 24 and ELISION in path.frames
    assert path.frames[0] == "f0" and path.frames[-1] == "f99"
    assert elide_frames(("a", "b")) == ("a", "b")


def test_schema_enums_and_sha_are_validated():
    digest = ProfileDigest(schema="evil/9", source_kind="bogus", engines=("pure", "rm -rf"),
                           input_sha256="xyz", egress_isolation="wide-open")
    assert digest.schema == SCHEMA
    assert digest.source_kind == "perf_text"
    assert digest.engines == ("pure",)
    assert digest.input_sha256 == ""
    assert digest.egress_isolation == "n/a"
    assert digest.untrusted_strings is True
    assert ProfileDigest(input_sha256=SHA.upper()).input_sha256 == SHA


def test_content_digest_is_recomputed_and_stable():
    first, second = _sample(), _sample()
    assert len(first.digest) == 64 and first.digest == second.digest
    forged = ProfileDigest(digest="0" * 64)
    assert forged.digest != "0" * 64
    bound = with_source(first, source_label="capture.out", input_sha256=SHA,
                        engines=("perf",), egress_isolation="netns", extra_notes=("extra",))
    assert bound.source_label == "capture.out" and bound.input_sha256 == SHA
    assert bound.engines == ("perf",) and bound.egress_isolation == "netns"
    assert bound.notes[-1] == "extra"
    assert bound.digest != first.digest


def test_wire_roundtrip_preserves_content():
    digest = with_source(_sample(), source_label="callgrind.out.1", input_sha256=SHA)
    payload = digest_to_wire(digest)
    assert payload["schema"] == SCHEMA and payload["untrusted_strings"] is True
    rebuilt = digest_from_wire(json.loads(json.dumps(payload)))
    assert rebuilt == digest
    assert rebuilt.digest == digest.digest


def test_wire_payload_is_at_most_48kb_and_marks_truncation():
    long = "N" * 240
    fns = tuple(ProfileFunction(name=long + str(i), module=long, file=long) for i in range(25))
    digest = ProfileDigest(
        top_self=fns, top_total=fns,
        hot_paths=tuple(HotPath(frames=tuple(long for _ in range(24))) for _ in range(10)),
        spikes=tuple(Spike(kind="zone", label=long, start_ns=1, duration_ns=2,
                           ratio_to_median=3.0, thread=long) for _ in range(10)),
        allocations=tuple(AllocationHotspot(function=long, file=long) for _ in range(15)),
        notes=tuple(long + str(i) for i in range(16)),
    )
    payload = digest_to_wire(digest)
    assert _wire_bytes(payload) <= 48_000
    assert payload["truncated"] is True
    small = digest_to_wire(digest, max_bytes=4_000)
    assert _wire_bytes(small) <= 4_000


def test_render_labels_untrusted_and_is_bounded():
    text = render_digest(_sample())
    assert UNTRUSTED_LABEL in text
    assert "Physics::Integrate" in text and "*project*" in text
    assert "p99 40.00 ms" in text and "leaked=40960B" in text
    assert "main > Game::Tick > Physics::Integrate" in text
    fns = tuple(ProfileFunction(name="F" * 240 + str(i)) for i in range(25))
    big = render_digest(ProfileDigest(top_self=fns, top_total=fns), max_chars=1000)
    assert len(big) <= 1000 and big.endswith("[truncated]")

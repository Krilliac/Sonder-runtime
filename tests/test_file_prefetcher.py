"""FilePrefetcher: argument-level (stream) prediction for file_read."""
from __future__ import annotations

import pytest

import server
import sonder_speculation
from sonder_speculation import FilePrefetcher


FILE_FIND_OBS = """file find: *.md under /repo (3 match(es))
  file README.md (100 bytes)
  file docs/GUIDE.md (200 bytes)
  file NOTES.md (50 bytes)
"""

TREE_OBS = """directory tree: /repo
  [D] docs
    [F] docs/a.py (10 bytes)
  [F] main.py (20 bytes)
"""


def test_extracts_paths_from_file_find():
    assert FilePrefetcher.extract_paths(FILE_FIND_OBS) == [
        "README.md", "docs/GUIDE.md", "NOTES.md",
    ]


def test_extracts_paths_from_directory_tree():
    assert FilePrefetcher.extract_paths(TREE_OBS) == ["docs/a.py", "main.py"]


def test_no_prediction_before_stream_confirmed():
    prefetcher = FilePrefetcher()
    prefetcher.observe("file_find", {"query": "*.md"}, FILE_FIND_OBS)
    # A listing alone is not a stream; one read must confirm the pattern.
    assert prefetcher.predict_read() is None


def test_predicts_next_unread_after_first_read():
    prefetcher = FilePrefetcher()
    prefetcher.observe("file_find", {"query": "*.md"}, FILE_FIND_OBS)
    prefetcher.observe("file_read", {"path": "README.md"}, "contents")
    assert prefetcher.predict_read() == {"path": "docs/GUIDE.md"}
    # The claimed path is not re-predicted; the stream advances.
    assert prefetcher.predict_read() == {"path": "NOTES.md"}
    assert prefetcher.predict_read() is None  # exhausted


def test_reads_out_of_order_still_advance_the_stream():
    prefetcher = FilePrefetcher()
    prefetcher.observe("file_find", {}, FILE_FIND_OBS)
    prefetcher.observe("file_read", {"path": "docs/GUIDE.md"}, "x")
    # Next unread in listing order is README.md.
    assert prefetcher.predict_read() == {"path": "README.md"}


def test_new_listing_resets_stream():
    prefetcher = FilePrefetcher()
    prefetcher.observe("file_find", {}, FILE_FIND_OBS)
    prefetcher.observe("file_read", {"path": "README.md"}, "x")
    prefetcher.observe("directory_tree", {}, TREE_OBS)
    # Fresh listing, unconfirmed stream: no prediction yet.
    assert prefetcher.predict_read() is None


def test_failed_observations_are_ignored():
    prefetcher = FilePrefetcher()
    prefetcher.observe("file_find", {}, FILE_FIND_OBS, ok=False)
    prefetcher.observe("file_read", {"path": "README.md"}, "x")
    assert prefetcher.predict_read() is None


def test_reads_not_in_listing_do_not_confirm_stream():
    prefetcher = FilePrefetcher()
    prefetcher.observe("file_find", {}, FILE_FIND_OBS)
    prefetcher.observe("file_read", {"path": "unrelated.txt"}, "x")
    assert prefetcher.predict_read() is None


# -- integration through the real loop --------------------------------------

@pytest.fixture()
def isolated_predictor(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_BRANCH_PREDICTOR", str(tmp_path / "bp.json"))
    monkeypatch.setenv("SONDER_SPECULATION", "1")
    # Exercise the mechanism regardless of the adaptive cost model: with mock
    # sub-millisecond tools the model would otherwise correctly go dormant.
    monkeypatch.setenv("SONDER_SPECULATION_MIN_SAVING_MS", "0")
    sonder_speculation.reset_for_tests()
    yield
    sonder_speculation.reset_for_tests()


def test_prefetched_file_read_is_retired_in_the_loop(
    isolated_predictor, monkeypatch, tmp_path,
):
    (tmp_path / "alpha.md").write_text("alpha body", encoding="utf-8")
    (tmp_path / "beta.md").write_text("beta body", encoding="utf-8")
    (tmp_path / "gamma.md").write_text("gamma body", encoding="utf-8")

    dispatch_paths = []
    real_dispatch = server._agent_dispatch_observed

    def counting_dispatch(tool_name, args, **kwargs):
        if tool_name == "file_read":
            dispatch_paths.append(str((args or {}).get("path", "")))
        return real_dispatch(tool_name, args, **kwargs)

    monkeypatch.setattr(server, "_agent_dispatch_observed", counting_dispatch)

    decisions = [
        '{"tool": "file_find", "args": {"query": "*.md"}}',
        '{"tool": "file_read", "args": {"path": "alpha.md"}}',
        '{"tool": "file_read", "args": {"path": "beta.md"}}',
        '{"tool": "file_read", "args": {"path": "gamma.md"}}',
        '{"final": "done"}',
    ]
    queue = list(decisions)
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: (
            lambda prompt, history=None: queue.pop(0) if queue
            else '{"final": "done"}'
        ),
    )

    out = server._agent_impl(
        "read every markdown file",
        tier="code", max_steps=8, read_only=True, project=str(tmp_path),
    )
    assert not out.startswith("ERROR"), out[:200]
    # alpha confirmed the stream; beta and gamma were prefetched. Each path
    # was dispatched exactly once overall — the retired ones ran only
    # speculatively, never a second time.
    import os

    basenames = [os.path.basename(p) for p in dispatch_paths]
    for path in ("alpha.md", "beta.md", "gamma.md"):
        assert basenames.count(path) == 1, dispatch_paths
    stats = sonder_speculation.default_predictor().stats()
    assert stats["speculations"] >= 2
    # At least one argful prefetch retired (beta or gamma, depending on
    # listing order in the observation).
    assert stats["speculations"] - stats["squashes"] >= 1

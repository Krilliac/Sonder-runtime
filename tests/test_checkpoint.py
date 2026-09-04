"""Tests for sonder_runtime.domain.automation.checkpoint."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sonder_runtime.domain.automation.checkpoint import (
    Checkpoint,
    CheckpointStore,
    can_resume,
    resume_point,
)


class TestCheckpoint(unittest.TestCase):
    def test_auto_id_generation(self):
        cp = Checkpoint(session_id="sess-1", step_index=3, status="running")
        self.assertTrue(cp.checkpoint_id.startswith("sess-1-3-"))

    def test_explicit_id_preserved(self):
        cp = Checkpoint(
            session_id="s", step_index=0, status="ready",
            checkpoint_id="custom-id",
        )
        self.assertEqual(cp.checkpoint_id, "custom-id")

    def test_default_context(self):
        cp = Checkpoint(session_id="s", step_index=0, status="running")
        self.assertEqual(cp.context, {})


class TestCheckpointStore(unittest.TestCase):
    def setUp(self):
        self.store = CheckpointStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_save_and_retrieve(self):
        cp = Checkpoint(
            session_id="sess-1", step_index=1, status="running",
            context={"task": "build"},
        )
        saved_id = self.store.save(cp)
        retrieved = self.store.get(saved_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.session_id, "sess-1")
        self.assertEqual(retrieved.step_index, 1)
        self.assertEqual(retrieved.context["task"], "build")

    def test_latest_returns_highest_step(self):
        for i in range(5):
            self.store.save(Checkpoint(
                session_id="sess-1", step_index=i, status="running",
            ))
        latest = self.store.latest("sess-1")
        self.assertEqual(latest.step_index, 4)

    def test_latest_returns_none_for_unknown_session(self):
        self.assertIsNone(self.store.latest("nonexistent"))

    def test_list_checkpoints(self):
        for i in range(5):
            self.store.save(Checkpoint(
                session_id="sess-1", step_index=i, status="running",
            ))
        cps = self.store.list_checkpoints("sess-1", limit=3)
        self.assertEqual(len(cps), 3)
        self.assertEqual(cps[0].step_index, 4)
        self.assertEqual(cps[2].step_index, 2)

    def test_delete_checkpoint(self):
        cp = Checkpoint(session_id="s", step_index=0, status="running")
        cp_id = self.store.save(cp)
        self.assertTrue(self.store.delete(cp_id))
        self.assertIsNone(self.store.get(cp_id))

    def test_delete_nonexistent_returns_false(self):
        self.assertFalse(self.store.delete("nope"))

    def test_clear_session(self):
        for i in range(3):
            self.store.save(Checkpoint(
                session_id="sess-1", step_index=i, status="running",
            ))
        self.store.save(Checkpoint(
            session_id="sess-2", step_index=0, status="running",
        ))
        cleared = self.store.clear_session("sess-1")
        self.assertEqual(cleared, 3)
        self.assertIsNone(self.store.latest("sess-1"))
        self.assertIsNotNone(self.store.latest("sess-2"))

    def test_prune_keeps_max_checkpoints(self):
        store = CheckpointStore(":memory:", max_checkpoints=3)
        for i in range(10):
            store.save(Checkpoint(
                session_id="s", step_index=i, status="running",
            ))
        cps = store.list_checkpoints("s", limit=100)
        self.assertEqual(len(cps), 3)
        self.assertEqual(cps[0].step_index, 9)
        self.assertEqual(cps[2].step_index, 7)
        store.close()

    def test_file_backed_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sub" / "checkpoints.db"
            store1 = CheckpointStore(db)
            store1.save(Checkpoint(
                session_id="s", step_index=5, status="paused",
                context={"key": "value"},
            ))
            store1.close()

            store2 = CheckpointStore(db)
            cp = store2.latest("s")
            self.assertIsNotNone(cp)
            self.assertEqual(cp.step_index, 5)
            self.assertEqual(cp.context["key"], "value")
            store2.close()

    def test_upsert_same_checkpoint_id(self):
        cp = Checkpoint(
            session_id="s", step_index=0, status="running",
            checkpoint_id="fixed-id", context={"v": 1},
        )
        self.store.save(cp)
        cp2 = Checkpoint(
            session_id="s", step_index=0, status="paused",
            checkpoint_id="fixed-id", context={"v": 2},
        )
        self.store.save(cp2)
        retrieved = self.store.get("fixed-id")
        self.assertEqual(retrieved.status, "paused")
        self.assertEqual(retrieved.context["v"], 2)


class TestHelperFunctions(unittest.TestCase):
    def setUp(self):
        self.store = CheckpointStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_can_resume_true(self):
        self.store.save(Checkpoint(
            session_id="s", step_index=3, status="paused",
        ))
        self.assertTrue(can_resume("s", self.store))

    def test_can_resume_false_completed(self):
        self.store.save(Checkpoint(
            session_id="s", step_index=3, status="completed",
        ))
        self.assertFalse(can_resume("s", self.store))

    def test_can_resume_false_no_checkpoint(self):
        self.assertFalse(can_resume("s", self.store))

    def test_resume_point(self):
        self.store.save(Checkpoint(
            session_id="s", step_index=7, status="running",
        ))
        self.assertEqual(resume_point("s", self.store), 7)

    def test_resume_point_no_checkpoint(self):
        self.assertEqual(resume_point("s", self.store), 0)


if __name__ == "__main__":
    unittest.main()

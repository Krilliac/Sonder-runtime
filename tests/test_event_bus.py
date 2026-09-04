"""Tests for sonder_runtime.domain.events."""
from __future__ import annotations

import threading
import time
import unittest

from sonder_runtime.domain.events import Event, EventBus, emit, recent, reset, subscribe


class TestEventBus(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_publish_no_subscribers(self):
        event = Event(kind="test.event", source_type="test", source_id="1")
        delivered = self.bus.publish(event)
        self.assertEqual(delivered, 0)

    def test_subscribe_and_receive(self):
        received = []
        self.bus.subscribe("test.event", received.append)
        event = Event(kind="test.event", source_type="test", source_id="1")
        delivered = self.bus.publish(event)
        self.assertEqual(delivered, 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].kind, "test.event")

    def test_wildcard_subscriber(self):
        received = []
        self.bus.subscribe("*", received.append)
        self.bus.publish(Event(kind="a", source_type="t", source_id="1"))
        self.bus.publish(Event(kind="b", source_type="t", source_id="2"))
        self.assertEqual(len(received), 2)

    def test_unsubscribe(self):
        received = []
        handler = received.append
        self.bus.subscribe("test.event", handler)
        self.bus.unsubscribe("test.event", handler)
        self.bus.publish(Event(kind="test.event", source_type="t", source_id="1"))
        self.assertEqual(len(received), 0)

    def test_failing_handler_does_not_block(self):
        def bad_handler(event):
            raise ValueError("boom")

        received = []
        self.bus.subscribe("test.event", bad_handler)
        self.bus.subscribe("test.event", received.append)
        delivered = self.bus.publish(Event(kind="test.event", source_type="t", source_id="1"))
        self.assertEqual(delivered, 1)
        self.assertEqual(len(received), 1)

    def test_history(self):
        self.bus.publish(Event(kind="a", source_type="t", source_id="1"))
        self.bus.publish(Event(kind="b", source_type="t", source_id="2"))
        self.bus.publish(Event(kind="a", source_type="t", source_id="3"))
        all_events = self.bus.recent()
        self.assertEqual(len(all_events), 3)
        a_events = self.bus.recent(kind="a")
        self.assertEqual(len(a_events), 2)

    def test_history_limit(self):
        self.bus._max_history = 5
        for i in range(10):
            self.bus.publish(Event(kind="t", source_type="t", source_id=str(i)))
        self.assertEqual(len(self.bus.recent(limit=100)), 5)

    def test_event_data(self):
        received = []
        self.bus.subscribe("test", received.append)
        self.bus.publish(Event(
            kind="test", source_type="autopilot", source_id="run-1",
            data={"status": "completed", "objective": "test goal"},
        ))
        self.assertEqual(received[0].data["status"], "completed")
        self.assertEqual(received[0].source_type, "autopilot")

    def test_thread_safety(self):
        counter = {"value": 0}
        lock = threading.Lock()

        def handler(event):
            with lock:
                counter["value"] += 1

        self.bus.subscribe("concurrent", handler)
        threads = []
        for i in range(20):
            t = threading.Thread(
                target=self.bus.publish,
                args=(Event(kind="concurrent", source_type="t", source_id=str(i)),),
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(counter["value"], 20)

    def test_clear_handlers(self):
        self.bus.subscribe("test", lambda e: None)
        self.assertEqual(self.bus.handler_count(), 1)
        self.bus.clear_handlers()
        self.assertEqual(self.bus.handler_count(), 0)


class TestModuleLevelAPI(unittest.TestCase):
    def setUp(self):
        reset()

    def tearDown(self):
        reset()

    def test_emit_and_recent(self):
        emit("test.fired", "unit", "test-1", {"key": "value"})
        events = recent("test.fired")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data["key"], "value")

    def test_subscribe_module_level(self):
        received = []
        subscribe("test.sub", received.append)
        emit("test.sub", "unit", "test-2")
        self.assertEqual(len(received), 1)


if __name__ == "__main__":
    unittest.main()

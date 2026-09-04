"""Tests for sonder_runtime.domain.execution.sandbox."""
from __future__ import annotations

import sys
import unittest

from sonder_runtime.domain.execution.sandbox import (
    IsolationLevel,
    SandboxPolicy,
    SandboxResult,
    run_isolated,
    run_python_isolated,
)


class TestSandboxResult(unittest.TestCase):
    def test_ok_property(self):
        r = SandboxResult(exit_code=0)
        self.assertTrue(r.ok)

    def test_not_ok_nonzero(self):
        r = SandboxResult(exit_code=1)
        self.assertFalse(r.ok)

    def test_not_ok_timeout(self):
        r = SandboxResult(exit_code=0, timed_out=True)
        self.assertFalse(r.ok)


class TestSandboxPolicy(unittest.TestCase):
    def test_effective_env_filters(self):
        policy = SandboxPolicy(env_allowlist=("PATH",))
        env = policy.effective_env()
        self.assertIn("PATH", env)
        self.assertNotIn("TERM", env)

    def test_default_values(self):
        p = SandboxPolicy()
        self.assertEqual(p.level, IsolationLevel.SUBPROCESS)
        self.assertEqual(p.timeout_seconds, 30.0)
        self.assertFalse(p.allow_network)


class TestRunIsolated(unittest.TestCase):
    def test_echo_command(self):
        result = run_isolated(
            ["echo", "hello world"],
            policy=SandboxPolicy(level=IsolationLevel.SUBPROCESS),
        )
        self.assertTrue(result.ok)
        self.assertIn("hello world", result.stdout)

    def test_nonzero_exit(self):
        result = run_isolated(
            [sys.executable, "-c", "import sys; sys.exit(42)"],
            policy=SandboxPolicy(level=IsolationLevel.SUBPROCESS),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 42)

    def test_timeout(self):
        result = run_isolated(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            policy=SandboxPolicy(timeout_seconds=0.5),
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_none_isolation(self):
        result = run_isolated(
            ["echo", "direct"],
            policy=SandboxPolicy(level=IsolationLevel.NONE),
        )
        self.assertTrue(result.ok)
        self.assertIn("direct", result.stdout)

    def test_container_falls_back(self):
        result = run_isolated(
            ["echo", "fallback"],
            policy=SandboxPolicy(level=IsolationLevel.CONTAINER),
        )
        self.assertTrue(result.ok)
        self.assertIn("fallback", result.stdout)

    def test_bad_command(self):
        result = run_isolated(
            ["/nonexistent/binary/path"],
            policy=SandboxPolicy(level=IsolationLevel.SUBPROCESS),
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_stdin_data(self):
        result = run_isolated(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
            policy=SandboxPolicy(level=IsolationLevel.SUBPROCESS),
            stdin_data="hello",
        )
        self.assertTrue(result.ok)
        self.assertIn("HELLO", result.stdout)

    def test_duration_tracked(self):
        result = run_isolated(
            ["echo", "fast"],
            policy=SandboxPolicy(level=IsolationLevel.SUBPROCESS),
        )
        self.assertGreater(result.duration_ms, 0)


class TestRunPythonIsolated(unittest.TestCase):
    def test_simple_script(self):
        result = run_python_isolated("print(2 + 2)")
        self.assertTrue(result.ok)
        self.assertIn("4", result.stdout)

    def test_script_error(self):
        result = run_python_isolated("raise ValueError('boom')")
        self.assertFalse(result.ok)
        self.assertIn("ValueError", result.stderr)

    def test_script_timeout(self):
        result = run_python_isolated(
            "import time; time.sleep(60)",
            policy=SandboxPolicy(timeout_seconds=0.5),
        )
        self.assertTrue(result.timed_out)


if __name__ == "__main__":
    unittest.main()

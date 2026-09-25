"""Interactive REPL logging: file sink, notice queue, and handler selection."""
from __future__ import annotations

import io
import json
import logging
import os
import stat
import sys
import types

import pytest

from sonder_runtime.application.ports import repl_notices
from sonder_runtime.platform import logging as runtime_logging


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            try:
                handler.close()
            except Exception:
                pass
    root.handlers[:] = handlers
    root.setLevel(level)
    repl_notices.reset_repl_notices()


class _Tty(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def _config(log_format="json"):
    return types.SimpleNamespace(
        observability=types.SimpleNamespace(log_format=log_format, log_level="INFO"),
        secrets=None,
        private_source_paths=(),
    )


# --- handler selection ------------------------------------------------------


def test_repl_tty_logs_to_private_file_and_queues_warnings(tmp_path):
    queue = repl_notices.ReplNoticeQueue()
    plan = runtime_logging.configure_repl_logging(
        home=tmp_path, interactive=True, notice_sink=queue.push, env={},
    )
    assert plan.console == runtime_logging.CONSOLE_NOTICES
    assert plan.file_path == str(tmp_path / "logs" / "repl.log")
    assert plan.file_level == "INFO"
    root = logging.getLogger()
    kinds = [type(h) for h in root.handlers]
    assert kinds == [
        runtime_logging.PrivateRotatingFileHandler,
        runtime_logging.NoticeQueueHandler,
    ]
    # No handler writes to a terminal stream.
    assert not any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )

    log = logging.getLogger("sonder.test.repl")
    log.info("routine startup fact")
    log.warning("something worth a notice")
    for handler in root.handlers:
        handler.flush()
    records = [
        json.loads(line)
        for line in open(plan.file_path, encoding="utf-8").read().splitlines()
    ]
    messages = [r["message"] for r in records]
    assert "routine startup fact" in messages
    assert "something worth a notice" in messages
    drained = queue.drain()
    assert [n.message for n in drained] == ["something worth a notice"]
    assert drained[0].levelname == "WARNING"
    assert drained[0].component == "sonder.test.repl"


def test_repl_piped_logs_to_file_and_only_errors_to_stderr_as_text(tmp_path):
    stream = io.StringIO()
    plan = runtime_logging.configure_repl_logging(
        home=tmp_path, interactive=False, notice_sink=None, env={}, stream=stream,
    )
    assert plan.console == runtime_logging.CONSOLE_STDERR_ERRORS
    log = logging.getLogger("sonder.test.piped")
    log.warning("quiet warning")
    log.error("loud error")
    text = stream.getvalue()
    assert "quiet warning" not in text
    assert "loud error" in text
    assert not text.lstrip().startswith("{")  # text, not JSON
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = open(plan.file_path, encoding="utf-8").read()
    assert "quiet warning" in content and "loud error" in content


def test_repl_stderr_escape_hatch_restores_json_on_stderr(tmp_path):
    stream = io.StringIO()
    plan = runtime_logging.configure_repl_logging(
        home=tmp_path, interactive=True, notice_sink=lambda *a: None,
        env={"SONDER_REPL_LOG_STDERR": "1"}, stream=stream,
    )
    assert plan.console == runtime_logging.CONSOLE_STDERR_JSON
    assert plan.file_path is None
    logging.getLogger("sonder.test.hatch").warning("to stderr")
    record = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert record["message"] == "to stderr"
    assert not (tmp_path / "logs" / "repl.log").exists()


def test_repl_log_level_env_controls_the_file(tmp_path):
    plan = runtime_logging.configure_repl_logging(
        home=tmp_path, interactive=False, env={"SONDER_REPL_LOG_LEVEL": "debug"},
        stream=io.StringIO(),
    )
    assert plan.file_level == "DEBUG"
    assert logging.getLogger().level == logging.DEBUG


def test_unwritable_home_falls_back_to_stderr(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    stream = io.StringIO()
    plan = runtime_logging.configure_repl_logging(
        home=blocker, interactive=True, notice_sink=lambda *a: None, env={},
        stream=stream,
    )
    assert plan.console == runtime_logging.CONSOLE_STDERR_JSON
    assert plan.fallback_reason


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_repl_log_file_is_0600_including_rotated_generations(tmp_path):
    plan = runtime_logging.configure_repl_logging(
        home=tmp_path, interactive=False, env={}, stream=io.StringIO(),
    )
    path = plan.file_path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    handler = logging.getLogger().handlers[0]
    assert handler.maxBytes == runtime_logging.REPL_LOG_MAX_BYTES
    assert handler.backupCount == runtime_logging.REPL_LOG_BACKUPS
    handler.doRollover()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path + ".1").st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_existing_wide_log_file_is_tightened(tmp_path):
    (tmp_path / "logs").mkdir()
    existing = tmp_path / "logs" / "repl.log"
    existing.write_text("")
    os.chmod(existing, 0o644)
    runtime_logging.configure_repl_logging(
        home=tmp_path, interactive=False, env={}, stream=io.StringIO(),
    )
    assert stat.S_IMODE(os.stat(existing).st_mode) == 0o600


def test_notice_handler_redacts_before_queueing():
    queue = repl_notices.ReplNoticeQueue()
    handler = runtime_logging.NoticeQueueHandler(queue.push)
    record = logging.LogRecord(
        "sonder.x", logging.WARNING, __file__, 1,
        "token=abcd1234secret leaked", None, None,
    )
    handler.handle(record)
    (notice,) = queue.drain()
    assert "abcd1234secret" not in notice.message
    assert runtime_logging.REDACTED in notice.message


def test_notice_handler_ignores_info_and_survives_a_broken_sink():
    handler = runtime_logging.NoticeQueueHandler(lambda *a: 1 / 0)
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, None)
    handler.handle(record)  # must not raise
    assert handler.level == logging.WARNING
    with pytest.raises(TypeError):
        runtime_logging.NoticeQueueHandler(None)


# --- notice queue -----------------------------------------------------------


def test_notice_queue_is_bounded_and_counts_drops():
    queue = repl_notices.ReplNoticeQueue(capacity=3)
    for index in range(5):
        queue.push(30, "WARNING", "c", "n%d" % index, float(index))
    assert queue.pending() == 3
    drained = queue.drain()
    assert [n.message for n in drained] == ["n2", "n3", "n4"]
    assert queue.last_dropped == 2
    assert queue.drain() == ()
    assert queue.last_dropped == 0


def test_notice_queue_truncates_long_messages_and_marks_errors():
    queue = repl_notices.ReplNoticeQueue()
    queue.push(40, "ERROR", "c", "x" * 5000, 0.0)
    (notice,) = queue.drain()
    assert len(notice.message) <= 500
    assert notice.is_error


def test_drain_limit_leaves_the_rest():
    queue = repl_notices.ReplNoticeQueue()
    for index in range(4):
        queue.push(30, "WARNING", "c", str(index), 0.0)
    assert len(queue.drain(limit=1)) == 1
    assert queue.pending() == 3


def test_module_api_is_empty_without_an_installed_queue():
    repl_notices.reset_repl_notices()
    assert repl_notices.drain_repl_notices() == ()
    assert repl_notices.pending_repl_notices() == 0
    assert repl_notices.last_drain_dropped() == 0
    assert repl_notices.repl_log_path() is None


def test_module_api_drains_the_installed_queue():
    queue = repl_notices.ReplNoticeQueue()
    repl_notices.install_repl_notices(queue, log_path="/x/repl.log")
    queue.push(30, "WARNING", "c", "hello", 0.0)
    assert repl_notices.pending_repl_notices() == 1
    assert [n.message for n in repl_notices.drain_repl_notices()] == ["hello"]
    assert repl_notices.repl_log_path() == "/x/repl.log"
    with pytest.raises(TypeError):
        repl_notices.install_repl_notices(object())


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        repl_notices.ReplNoticeQueue(capacity=0)


# --- entry-point wiring (cmd_repl) -----------------------------------------


def _main_module(monkeypatch, tmp_path):
    import sonder_runtime.__main__ as entry

    monkeypatch.setattr(entry.runtime_paths, "default_home", lambda: tmp_path)
    monkeypatch.setattr(entry, "_redactor_for_config", lambda config: None)
    monkeypatch.delenv("SONDER_REPL_LOG_STDERR", raising=False)
    monkeypatch.delenv("SONDER_REPL_LOG_LEVEL", raising=False)
    return entry


def test_cmd_repl_wiring_on_a_tty_installs_the_notice_queue(monkeypatch, tmp_path):
    entry = _main_module(monkeypatch, tmp_path)
    plan = entry._configure_repl_logging(
        _config(), machine_output=False, stdin=_Tty(True), stdout=_Tty(True),
    )
    assert plan.console == "notices"
    logging.getLogger("sonder.wiring").warning("queued")
    assert [n.message for n in repl_notices.drain_repl_notices()] == ["queued"]
    assert repl_notices.repl_log_path() == str(tmp_path / "logs" / "repl.log")


@pytest.mark.parametrize("stdin_tty,stdout_tty,machine", [
    (False, True, False),   # piped input
    (True, False, False),   # piped output
    (True, True, True),     # --json
])
def test_cmd_repl_wiring_off_a_tty_has_no_queue(monkeypatch, tmp_path,
                                                stdin_tty, stdout_tty, machine):
    entry = _main_module(monkeypatch, tmp_path)
    plan = entry._configure_repl_logging(
        _config(), machine_output=machine,
        stdin=_Tty(stdin_tty), stdout=_Tty(stdout_tty),
    )
    assert plan.console == "stderr-errors"
    logging.getLogger("sonder.wiring").warning("not queued")
    assert repl_notices.drain_repl_notices() == ()
    assert repl_notices.repl_log_path() == plan.file_path


def test_cmd_repl_uses_the_repl_wiring_and_serve_mcp_do_not():
    import inspect
    import sonder_runtime.__main__ as entry

    repl_src = inspect.getsource(entry.cmd_repl)
    assert "_configure_repl_logging(" in repl_src
    assert "configure_logging(" not in repl_src.replace("_configure_repl_logging(", "")
    for name in ("cmd_serve", "cmd_mcp"):
        source = inspect.getsource(getattr(entry, name))
        assert "configure_logging(" in source
        assert "configure_repl_logging" not in source


def test_serve_style_configure_logging_is_unchanged():
    stream = io.StringIO()
    runtime_logging.configure_logging(level="INFO", log_format="json", stream=stream)
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1 and handlers[0].stream is stream
    logging.getLogger("sonder.serve").info("serve line")
    assert json.loads(stream.getvalue())["message"] == "serve line"

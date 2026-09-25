"""REPL ``/crash`` and ``/profile`` against a fake DebugDigestService.

``application.debugging.ports`` / ``presenters`` belong to lane C. When they
are not importable yet, spec-exact doubles are installed by monkeypatching the
facade's two lazy accessors; production imports stay at the spec'd paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

import sonder_runtime.interfaces.repl.repl as repl
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.errors import NotFound, SonderError
from sonder_runtime.interfaces.repl.facades import debug_tools as facade


MS_SYMBOL_SERVER = "https://msdl.microsoft.com/download/symbols"


# --- lane C port doubles -----------------------------------------------------------------


@dataclass(frozen=True)
class CrashDigestRequest:
    path: str
    executable: str = ""
    symbol_dirs: tuple = ()
    engine: str = "auto"
    symbol_server: bool = False
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class CrashTriageRequest:
    path: str
    max_threads: int = 16
    max_files: int = 64


@dataclass(frozen=True)
class ProfileDigestRequest:
    path: str
    executable: str = ""
    engine: str = "auto"
    symbol_dirs: tuple = ()
    top_n: int = 25
    frame_budget_ms: int | None = None
    thread: str = ""
    frame_zone: str = ""
    timeout_seconds: int | None = None


FAKE_PORTS = SimpleNamespace(
    CrashDigestRequest=CrashDigestRequest, CrashTriageRequest=CrashTriageRequest,
    ProfileDigestRequest=ProfileDigestRequest,
)


def _render_report(report, max_chars=12_000):
    return ("crash report %s %s" % (report.signature, report.process_name))[:max_chars]


def _render_bucket_table(buckets):
    rows = ["%-16s %5s  %s" % ("signature", "count", "top frame")]
    rows += ["%-16s %5d  %s" % (b.signature, b.count, b.top_frame) for b in buckets]
    return "\n".join(rows)


FAKE_PRESENTERS = SimpleNamespace(
    render_report=_render_report,
    render_digest=lambda digest: "profile digest %s" % digest.label,
    render_bucket_table=_render_bucket_table,
    report_to_wire=lambda report, max_bytes=48_000: {"signature": report.signature},
    digest_to_wire=lambda digest: {"label": digest.label},
)


@pytest.fixture(autouse=True)
def _lane_c_modules(monkeypatch):
    monkeypatch.setattr(facade, "_ports", lambda: FAKE_PORTS)
    monkeypatch.setattr(facade, "_presenters", lambda: FAKE_PRESENTERS)
    facade._LAST.clear()
    facade._LAST.update({"run_id": "", "report": None})
    facade._REPRO_BY_RUN.clear()


class CodedError(SonderError):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


@dataclass
class Outcome:
    run_id: str
    status: str
    crash: object = None
    profile: object = None
    error_code: str = ""
    notes: tuple = ()


@dataclass
class Plan:
    network: bool
    stores: tuple

    def resolved_command(self):
        return {
            "kind": "crash", "engines": ["cdb"],
            "display_argvs": [["cdb", "-z", "{input}", "-y", "{sympath}"]],
            "input_label": "dumps/game.dmp", "input_sha256": "ab" * 32,
            "network": self.network, "stores_display": list(self.stores),
            "isolation": "none", "command_digest": "cd" * 32,
        }


REPORT = SimpleNamespace(signature="3f2a9c0d11b2e4f5", process_name="game.exe",
                         signature_basis="functions", threads=(), hints=(), exception=None,
                         crashing_thread_id=None)


@dataclass
class FakeService:
    consent: bool = False
    launched: list = field(default_factory=list)
    planned: list = field(default_factory=list)
    results: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    receipts: list = field(default_factory=list)
    interrupt_on_result: bool = False

    def set_session_symbol_consent(self, context, allowed):
        assert context.source == "repl"
        self.consent = allowed

    def plan_crash(self, request, context, *, console_confirmed=False):
        self.planned.append((request, console_confirmed))
        stores = (MS_SYMBOL_SERVER,) if request.symbol_server else ()
        return Plan(network=request.symbol_server, stores=stores)

    def crash(self, request, context, *, wait_seconds=60, console_confirmed=False):
        if request.symbol_server and not console_confirmed:
            return Outcome("", "refused", error_code="SYMBOL_SERVER_NEEDS_CONSOLE")
        if request.symbol_server and not self.consent:
            return Outcome("", "refused", error_code="SYMBOL_SERVER_CONSENT_REQUIRED")
        self.launched.append(request)
        plan = self.plan_crash(request, context, console_confirmed=console_confirmed)
        self.receipts.append(plan.resolved_command())
        if self.interrupt_on_result:
            return Outcome("debug-run-1", "running")
        return Outcome("debug-run-1", "complete", crash=REPORT)

    def triage(self, request, context):
        if request.path.endswith("/"):
            return (
                SimpleNamespace(signature="aaaa000011112222", count=3, top_frame="game!Player::update"),
                SimpleNamespace(signature="bbbb000011112222", count=1, top_frame="game!World::tick"),
            )
        return REPORT

    def result(self, run_id, context, *, wait_seconds=0):
        self.results.append(run_id)
        if self.interrupt_on_result:
            raise KeyboardInterrupt
        if run_id != "debug-run-1":
            raise NotFound("no such run")
        return Outcome(run_id, "complete", crash=REPORT)

    def cancel(self, run_id, context):
        self.cancelled.append(run_id)
        return Outcome(run_id, "cancelled")

    def profile_pure(self, request, context):
        if request.path.endswith(".data"):
            raise CodedError("CAPTURE_NEEDS_HOST_TOOL")
        return SimpleNamespace(label="pure:" + request.path)

    def profile(self, request, context, *, wait_seconds=60):
        self.launched.append(request)
        return Outcome("debug-run-2", "complete", profile=SimpleNamespace(label="perf"))


def _ctx():
    return local_owner_context(correlation_id="c1", source="repl")


def _run(service, arg, *, answers=(), **kwargs):
    lines = []
    prompts = []
    replies = list(answers)

    def confirm(prompt):
        prompts.append(prompt)
        return replies.pop(0) if replies else ""

    facade.crash_command(service, arg, _ctx(), out=lines.append, confirm=confirm,
                         poll_seconds=0.01, **kwargs)
    return "\n".join(lines), prompts


# --- usage and composition ------------------------------------------------------------------


def test_usage_strings():
    assert facade.CRASH_USAGE.startswith("usage: /crash <dump|core|log|dir>")
    assert "/crash symbols on|off" in facade.CRASH_USAGE
    assert "/crash fix <run_id|last>" in facade.CRASH_USAGE
    assert facade.PROFILE_USAGE.startswith("usage: /profile <capture>")
    service = FakeService()
    for arg in ("", "a.dmp b.dmp", "a.dmp --engine windbg", "a.dmp --exe", "--sym x",
                "symbols maybe", "fix", "triage"):
        text, _ = _run(service, arg)
        assert text == facade.CRASH_USAGE, arg
    assert not service.launched


def test_not_composed_gives_the_developer_tools_style_message(monkeypatch):
    text, _ = _run(None, "dumps/game.dmp")
    assert text == "debug tools are not composed in this runtime"
    lines = []
    facade.profile_command(None, "out.json", _ctx(), out=lines.append)
    assert lines == [facade.NOT_COMPOSED]
    monkeypatch.setattr(facade, "_ports", lambda: None)
    text, _ = _run(FakeService(), "dumps/game.dmp")
    assert text == facade.NOT_COMPOSED


# --- symbol-server consent flow ---------------------------------------------------------------


def test_symbols_online_declined_launches_nothing():
    service = FakeService(consent=True)
    text, prompts = _run(service, "dumps/game.dmp --symbols-online", answers=("n",))
    assert len(prompts) == 1 and "[y/N]" in prompts[0]
    assert MS_SYMBOL_SERVER in text and "sha256=" + "ab" * 32 in text
    assert "nothing was launched" in text
    assert service.launched == []


def test_symbols_online_confirmed_without_consent_is_refused():
    service = FakeService(consent=False)
    text, _ = _run(service, "dumps/game.dmp --symbols-online", answers=("y",))
    assert "SYMBOL_SERVER_CONSENT_REQUIRED" in text
    assert service.launched == []


def test_consent_then_yes_runs_with_network_and_the_ms_url_in_the_receipt():
    service = FakeService()
    text, _ = _run(service, "symbols on")
    assert "allowed for this console session" in text
    assert service.consent is True
    text, _ = _run(service, "dumps/game.dmp --symbols-online --sym C:\\syms", answers=("y",))
    assert len(service.launched) == 1
    request = service.launched[0]
    assert request.symbol_server is True and request.symbol_dirs == ("C:\\syms",)
    receipt = service.receipts[0]
    assert receipt["network"] is True
    assert receipt["stores_display"] == [MS_SYMBOL_SERVER]
    assert "crash run debug-run-1: complete" in text
    _run(service, "symbols off")
    assert service.consent is False


def test_plain_digest_never_prompts():
    service = FakeService()
    text, prompts = _run(service, "dumps/game.dmp --exe build/game.exe --engine gdb")
    assert prompts == []
    assert service.launched[0].engine == "gdb" and service.launched[0].symbol_server is False
    assert "crash report 3f2a9c0d11b2e4f5" in text


# --- waiting and cancel -------------------------------------------------------------------------


def test_ctrl_c_while_waiting_cancels_the_run():
    service = FakeService(interrupt_on_result=True)
    text, _ = _run(service, "dumps/game.dmp")
    assert "started crash run debug-run-1" in text
    assert service.cancelled == ["debug-run-1"]
    assert "cancelled crash run debug-run-1" in text


def test_followups():
    service = FakeService()
    text, _ = _run(service, "status debug-run-1")
    assert text == "crash run debug-run-1: complete"
    text, _ = _run(service, "result nope-1")
    assert text == "no crash run nope-1"
    text, _ = _run(service, "cancel debug-run-1")
    assert text.startswith("cancelled crash run debug-run-1")
    text, _ = _run(service, "status ../../etc")
    assert text == facade.CRASH_USAGE


# --- triage and fix -------------------------------------------------------------------------------


def test_triage_of_a_folder_renders_the_bucket_table():
    text, _ = _run(FakeService(), "triage dumps/qa/")
    assert "signature" in text and "aaaa000011112222" in text and "game!World::tick" in text
    text, _ = _run(FakeService(), "triage dumps/one.dmp")
    assert text.startswith("crash report")


def test_fix_last_uses_the_remembered_run_and_repro():
    service = FakeService()
    _run(service, "dumps/game.dmp --repro crash_repro_test")
    text, _ = _run(service, "fix last")
    assert text.startswith("crash fix brief for debug-run-1")
    assert "repro: /test ctest crash_repro_test" in text


def test_fix_without_a_report():
    text, _ = _run(FakeService(), "fix last")
    assert text.startswith("no crash report for last")


def test_invalid_repro_selector_is_refused_before_launch():
    service = FakeService()
    text, _ = _run(service, "dumps/game.dmp --repro -Rx")
    assert "INVALID_SELECTOR" in text
    assert service.launched == []


# --- /profile ------------------------------------------------------------------------------------


def test_profile_pure_and_host_capture_paths():
    service = FakeService()
    lines = []
    facade.profile_command(service, "trace/out.json --budget 16 --top 10", _ctx(),
                           out=lines.append)
    assert lines == ["profile digest pure:trace/out.json"]
    assert service.launched == []
    lines = []
    facade.profile_command(service, "perf.data --exe build/game", _ctx(), out=lines.append,
                           poll_seconds=0.01)
    assert service.launched[0].executable == "build/game"
    assert "profile run debug-run-2: complete" in "\n".join(lines)
    lines = []
    facade.profile_command(service, "x.json --top 500", _ctx(), out=lines.append)
    assert lines == [facade.PROFILE_USAGE]


# --- repl.py wiring --------------------------------------------------------------------------------


def test_repl_branches_route_through_the_debug_service(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(repl, "_debug_services", lambda: service)
    seen = {}

    def fake_crash(svc, arg, context, **kwargs):
        seen["crash"] = (svc, arg, context.source, sorted(kwargs))

    def fake_profile(svc, arg, context, **kwargs):
        seen["profile"] = (svc, arg, context.source)

    monkeypatch.setattr(repl, "_render_crash_command", fake_crash)
    monkeypatch.setattr(repl, "_render_profile_command", fake_profile)
    repl._crash_command("dumps/game.dmp", "")
    repl._profile_command("out.json", "")
    assert seen["crash"][:3] == (service, "dumps/game.dmp", "repl")
    assert "confirm" in seen["crash"][3]
    assert seen["profile"] == (service, "out.json", "repl")


def test_unattended_console_never_confirms(monkeypatch):
    monkeypatch.setattr(repl, "_console_has_operator", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert repl._confirm_answer("go? [y/N] ") == ""


def test_help_lists_crash_and_profile():
    assert "/crash <dump|core|log>" in repl.HELP
    assert "/profile <capture>" in repl.HELP
    lines = repl.HELP.splitlines()
    digest = next(i for i, line in enumerate(lines) if line.strip().startswith("/digest"))
    crash = next(i for i, line in enumerate(lines) if line.strip().startswith("/crash"))
    assert crash == digest + 1
    # HELP is a literal (the catalog parses it from source); the facade's
    # HELP_LINES must stay in it verbatim.
    for line in facade.HELP_LINES:
        assert line in lines

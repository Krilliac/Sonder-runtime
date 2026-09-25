"""DebugDigestService over port fakes: consent rules, ownership, runs, assembly.

The fakes stand in for the adapters (capture source, pure triage, planner,
launcher, the -4 job output reader); the lane A/B report types are replaced
by a minimal report double because the service treats reports as opaque.
Other ``test_debug_*`` modules import these fakes.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace

import pytest

from sonder_runtime.application.context import OperationContext, local_owner_context
from sonder_runtime.application.debugging.ports import (
    CaptureIdentity,
    CrashDigestRequest,
    CrashTriageRequest,
    DebugPlan,
    DebugRunState,
    DebugStep,
    ProfileDigestRequest,
    TextWindow,
)
from sonder_runtime.application.debugging.service import DebugDigestService
from sonder_runtime.domain.common.errors import Forbidden, SonderError

SHA = "ab" * 32


@dataclass(frozen=True)
class ReportDouble:
    source_kind: str = "elf_core"
    engines: tuple[str, ...] = ("pure",)
    notes: tuple[str, ...] = ()
    threads: tuple = ()
    merged: tuple[str, ...] = ()
    egress_isolation: str = "n/a"
    truncated: bool = False
    module_symbols: tuple = ()
    signature: str = "0123456789abcdef"


class FakeReader:
    def __init__(self, data: bytes = b"x" * 16) -> None:
        self.data = data
        self.closed = False
        self.bytes_read = len(data)

    @property
    def size(self) -> int:
        return len(self.data)

    def read(self, offset: int, length: int) -> bytes:
        return self.data[offset:offset + length]

    def close(self) -> None:
        self.closed = True


def identity(kind: str = "elf_core", path: str = "/w/core.1", label: str = "core.1",
             size: int = 1000) -> CaptureIdentity:
    return CaptureIdentity(path=path, label=label, size=size, dev=1, ino=2, mtime_ns=3,
                           sha256=SHA, kind=kind)


class FakeSource:
    def __init__(self, kind: str = "elf_core") -> None:
        self.kind = kind
        self.opened: list[str] = []
        self.readers: list[FakeReader] = []
        self.directory: tuple[CaptureIdentity, ...] | None = None
        self.kinds: dict[str, str] = {}

    def open_reader(self, path, *, extra_roots="", max_bytes=None):
        self.opened.append(path)
        reader = FakeReader()
        self.readers.append(reader)
        return reader, identity(self.kinds.get(path, self.kind), path=path, label=path.rsplit("/", 1)[-1])

    def sniff(self, reader, name):
        return self.kind

    def is_dir(self, path, *, extra_roots=""):
        return self.directory is not None and path.endswith("/")

    def list_dir(self, path, *, extra_roots="", max_files=64):
        return tuple(self.directory or ())[:max_files]

    def contained_file(self, path, *, extra_roots=""):
        return path


class FakeTriage:
    def __init__(self) -> None:
        self.merges: list[tuple[str, str, str, str]] = []
        self.finished: list[dict] = []
        self.fail_crash = False

    def crash(self, ident, reader):
        if self.fail_crash:
            from sonder_runtime.application.debugging.ports import PARSE_FAILED, debug_error

            raise debug_error(PARSE_FAILED, "unreadable")
        return ReportDouble(source_kind=ident.kind, notes=(ident.label,))

    def profile(self, ident, reader, request):
        return {"profile": ident.label, "top_n": request.top_n}

    def bucket(self, reports):
        return tuple(("bucket", report.notes[0]) for report in reports)

    def merge_crash(self, base, parser, text, nonce, engine):
        self.merges.append((parser, text, nonce, engine))
        return replace(base, engines=base.engines + (engine,), merged=base.merged + (text,))

    def finish_crash(self, report, *, engines, module_symbols, egress_isolation, notes, truncated):
        self.finished.append({"engines": engines, "module_symbols": module_symbols,
                              "egress": egress_isolation, "notes": notes, "truncated": truncated})
        return replace(report, engines=tuple(dict.fromkeys(report.engines + tuple(engines))),
                       egress_isolation=egress_isolation, truncated=truncated,
                       module_symbols=tuple(module_symbols))

    def profile_from_steps(self, label, sha, source_kind, outputs, request, *, engines,
                           egress_isolation, notes, truncated):
        return {"profile": label, "outputs": outputs, "engines": engines}

    def crash_to_wire(self, report):
        return {"source_kind": report.source_kind, "engines": list(report.engines),
                "merged": list(report.merged), "module_symbols": [list(m) for m in report.module_symbols]}

    def crash_from_wire(self, data):
        return ReportDouble(source_kind=data["source_kind"], engines=tuple(data["engines"]),
                            merged=tuple(data.get("merged", ())),
                            module_symbols=tuple(tuple(m) for m in data.get("module_symbols", ())))

    def profile_to_wire(self, digest):
        return dict(digest, outputs=[list(item) for item in digest.get("outputs", ())])

    def profile_from_wire(self, data):
        return dict(data)


def step(engine: str = "gdb", parser: str = "gdb") -> DebugStep:
    return DebugStep(engine=engine, template_argv=("/usr/bin/" + engine, "{input}"),
                     display_argv=("/usr/bin/" + engine, "{input}"), environment=(("PATH", "/usr/bin"),),
                     timeout_seconds=60, max_output_bytes=1 << 20, memory_limit_bytes=1 << 32,
                     parser=parser)


def plan_for(ident: CaptureIdentity, steps=(), *, kind: str = "crash", network: bool = False,
             digest: str = "d" * 64, **extra) -> DebugPlan:
    return DebugPlan(kind=kind, source_kind=ident.kind, input_label=ident.label,
                     input_sha256=ident.sha256, input_bytes=ident.size, input_identity=ident,
                     staging="copy", steps=tuple(steps), network=network, stores_display=(),
                     verified_modules=extra.pop("verified_modules", ()), checked_executables=(),
                     notes=extra.pop("notes", ()), command_digest=digest,
                     engines=("pure",) + tuple(s.engine for s in steps), **extra)


class FakePlanner:
    def __init__(self, steps=(step(),)) -> None:
        self.steps = tuple(steps)
        self.calls: list[dict] = []
        self.refusal: Exception | None = None

    def plan_crash(self, request, context, *, network_allowed, identity, tier0):
        self.calls.append({"request": request, "network_allowed": network_allowed, "tier0": tier0})
        if self.refusal is not None:
            raise self.refusal
        return plan_for(identity, self.steps, network=network_allowed)

    def plan_profile(self, request, context, *, identity):
        self.calls.append({"request": request, "identity": identity})
        steps = self.steps if identity.kind in ("perf_data", "heaptrack_capture") else ()
        return plan_for(identity, steps, kind="profile")


class FakeLauncher:
    """Runs nothing; ``finish`` completes a run with the given state fields."""

    def __init__(self) -> None:
        self.started: list[tuple[DebugPlan, str, str]] = []
        self.states: dict[str, DebugRunState] = {}
        self.meta: dict[str, dict] = {}
        self.json: dict[tuple[str, str], dict] = {}
        self.cancelled: list[tuple[str, str]] = []
        self.running = 0
        self.auto_finish: dict | None = {}

    def start(self, plan, context, run_id):
        self.started.append((plan, context.principal_id, run_id))
        state = DebugRunState(run_id=run_id, principal_id=context.principal_id,
                              kind="tool.crash_digest" if plan.kind == "crash" else "tool.profile_digest",
                              status="running",
                              step_job_ids=tuple("%s-s%d" % (run_id, i) for i in range(len(plan.steps))),
                              nonce="0123456789abcdef", staging=plan.staging,
                              egress_isolation="netns", command_digest=plan.command_digest,
                              input_sha256=plan.input_sha256)
        self.states[run_id] = state
        self.meta[run_id] = {"principal_id": context.principal_id, "kind": state.kind}
        self.json[(run_id, "plan.json")] = {
            "kind": plan.kind, "source_kind": plan.source_kind, "input_label": plan.input_label,
            "engines": list(plan.engines), "network": plan.network, "notes": list(plan.notes),
            "module_symbols": [list(m) for m in plan.module_symbols],
            "verified_modules": list(plan.verified_modules),
            "steps": [{"engine": s.engine, "parser": s.parser, "display_argv": list(s.display_argv),
                       "reads_output_via": s.reads_output_via} for s in plan.steps],
        }
        if self.auto_finish is not None:
            self.finish(run_id, **self.auto_finish)
        return state

    def finish(self, run_id, **fields):
        fields.setdefault("status", "complete")
        self.states[run_id] = replace(self.states[run_id], **fields)

    def wait(self, run_id, timeout):
        if run_id not in self.states:
            raise KeyError(run_id)
        state = self.states[run_id]
        return state, state.status == "running"

    def cancel(self, run_id, reason):
        self.cancelled.append((run_id, reason))
        self.finish(run_id, status="cancelled")
        return True

    def metadata(self, run_id):
        return self.meta.get(run_id)

    def running_for(self, principal_id):
        return self.running

    def step_job_ids(self, run_id):
        return self.states[run_id].step_job_ids

    def step_output(self, run_id, index):
        return self.json.get((run_id, "step-%d" % index), {}).get("text")

    def store_json(self, run_id, name, payload):
        self.json[(run_id, name)] = dict(payload)

    def load_json(self, run_id, name):
        return self.json.get((run_id, name))


class FakeOutput:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def job_metadata(self, job_id):
        return {}

    def read_output(self, job_id, *, max_bytes=2_000_000, head_bytes=65_536):
        self.reads.append(job_id)
        text = "output of %s" % job_id
        return TextWindow(text=text, label=job_id, bytes_read=len(text), source_bytes=len(text),
                          truncated=False)


class FakeConsent:
    def __init__(self, allowed=False, mode_ok=True, stores=()) -> None:
        self.value = allowed
        self.mode_ok = mode_ok
        self._stores = tuple(stores)
        self.session: list[tuple[str, bool]] = []

    def allowed(self, context):
        return self.value

    def stores(self):
        return self._stores

    def set_session(self, context, allowed):
        self.session.append((context.principal_id, allowed))
        self.value = allowed

    def mode_permits_network(self):
        return self.mode_ok


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 0.25
        return self.now


@dataclass
class Stack:
    source: FakeSource = field(default_factory=FakeSource)
    triage: FakeTriage = field(default_factory=FakeTriage)
    planner: FakePlanner = field(default_factory=FakePlanner)
    launcher: FakeLauncher = field(default_factory=FakeLauncher)
    output: FakeOutput = field(default_factory=FakeOutput)
    consent: FakeConsent = field(default_factory=FakeConsent)

    def service(self, **kwargs) -> DebugDigestService:
        return DebugDigestService(self.source, self.triage, self.planner, self.launcher, self.output,
                                  consent=self.consent, source_map=None, redact=lambda t: t,
                                  clock=_Clock(), **kwargs)


def ctx(source: str = "repl", principal: str = "owner") -> OperationContext:
    base = local_owner_context(correlation_id=uuid.uuid4().hex, source=source)
    return replace(base, principal_id=principal)


@pytest.fixture
def stack() -> Stack:
    return Stack()


# -- network consent -----------------------------------------------------------


@pytest.mark.parametrize("source", ["mcp", "http", "worker", "system"])
def test_symbol_server_from_any_non_console_source_needs_the_console(stack, source):
    stack.consent.value = True
    with pytest.raises(SonderError) as caught:
        stack.service().crash(CrashDigestRequest("/w/core.1", symbol_server=True), ctx(source),
                              console_confirmed=True)
    assert caught.value.code == "SYMBOL_SERVER_NEEDS_CONSOLE"
    assert stack.planner.calls == [] and stack.launcher.started == []


def test_symbol_server_from_the_repl_model_path_is_refused_without_confirmation(stack):
    stack.consent.value = True
    with pytest.raises(SonderError) as caught:
        stack.service().crash(CrashDigestRequest("/w/core.1", symbol_server=True), ctx("repl"))
    assert caught.value.code == "SYMBOL_SERVER_NEEDS_CONSOLE"


def test_console_confirmation_without_consent_is_refused(stack):
    with pytest.raises(SonderError) as caught:
        stack.service().plan_crash(CrashDigestRequest("/w/core.1", symbol_server=True), ctx("repl"),
                                   console_confirmed=True)
    assert caught.value.code == "SYMBOL_SERVER_CONSENT_REQUIRED"


def test_plan_mode_refuses_network_even_with_consent(stack):
    stack.consent.value = True
    stack.consent.mode_ok = False
    with pytest.raises(SonderError) as caught:
        stack.service().plan_crash(CrashDigestRequest("/w/core.1", symbol_server=True), ctx("repl"),
                                   console_confirmed=True)
    assert caught.value.code == "SYMBOL_SERVER_CONSENT_REQUIRED"


def test_console_confirmation_with_consent_plans_with_network(stack):
    stack.consent.value = True
    plan = stack.service().plan_crash(CrashDigestRequest("/w/core.1", symbol_server=True),
                                      ctx("repl"), console_confirmed=True)
    assert plan.network is True
    assert stack.planner.calls[-1]["network_allowed"] is True


def test_without_symbol_server_nothing_asks_for_consent(stack):
    plan = stack.service().plan_crash(CrashDigestRequest("/w/core.1"), ctx("mcp"))
    assert plan.network is False and stack.planner.calls[-1]["network_allowed"] is False


def test_session_consent_is_set_only_by_the_attended_console(stack):
    service = stack.service()
    for source in ("mcp", "http", "worker"):
        with pytest.raises(SonderError):
            service.set_session_symbol_consent(ctx(source), True, attended=True)
    with pytest.raises(SonderError):
        service.set_session_symbol_consent(ctx("repl"), True)
    assert service.set_session_symbol_consent(ctx("repl"), True, attended=True) is True
    assert stack.consent.session == [("owner", True)]


# -- pure paths ----------------------------------------------------------------


def test_triage_runs_the_pure_reader_and_closes_the_capture(stack):
    report = stack.service().triage(CrashTriageRequest("/w/core.1"), ctx("mcp"))
    assert report.source_kind == "elf_core"
    assert stack.launcher.started == [] and all(r.closed for r in stack.source.readers)


def test_triage_of_a_directory_buckets_at_most_the_requested_files(stack):
    stack.source.directory = tuple(identity(path="/w/d/core.%d" % i, label="core.%d" % i)
                                   for i in range(70))
    stack.source.kinds["/w/d/core.3"] = "perf_data"
    buckets, notes = stack.service().triage_detail(CrashTriageRequest("/w/d/", max_files=64),
                                                   ctx("mcp"))
    assert len(stack.source.opened) == 64
    assert len(buckets) == 63
    assert any("core.3" in note and "not a crash capture" in note for note in notes)


def test_directory_triage_stops_at_its_wall_clock_budget(stack):
    """Opening each capture hashes it (up to 5 s); 64 slow files must not take minutes."""
    stack.source.directory = tuple(identity(path="/w/d/core.%d" % i, label="core.%d" % i)
                                   for i in range(64))
    service = stack.service()
    clock = service._clock
    original = stack.source.open_reader

    def slow_open(*args, **kwargs):
        clock.now += 5.0  # one budgeted hash per file
        return original(*args, **kwargs)

    stack.source.open_reader = slow_open
    buckets, notes = service.triage_detail(CrashTriageRequest("/w/d/", max_files=64), ctx("mcp"))
    assert len(stack.source.opened) <= 7
    assert any("budget exhausted" in note for note in notes)


def test_triage_refuses_profiles_and_perfetto(stack):
    stack.source.kind = "chrome_trace"
    with pytest.raises(SonderError) as caught:
        stack.service().triage(CrashTriageRequest("/w/t.json"), ctx())
    assert caught.value.code == "CAPTURE_FORMAT_UNKNOWN" and "profile_digest" in str(caught.value)
    stack.source.kind = "perfetto_protobuf"
    with pytest.raises(SonderError) as caught:
        stack.service().triage(CrashTriageRequest("/w/t.pb"), ctx())
    assert "traceconv" in str(caught.value)


@pytest.mark.parametrize("kind,code", [
    ("perf_data", "CAPTURE_NEEDS_HOST_TOOL"),
    ("etw_etl", "CAPTURE_NEEDS_HOST_TOOL"),
    ("tracy_capture", "CAPTURE_NEEDS_HOST_TOOL"),
    ("heaptrack_capture", "CAPTURE_NEEDS_HOST_TOOL"),
    ("perfetto_protobuf", "CAPTURE_FORMAT_UNKNOWN"),
    ("elf_core", "CAPTURE_FORMAT_UNKNOWN"),
    ("unknown", "CAPTURE_FORMAT_UNKNOWN"),
])
def test_profile_pure_refuses_what_it_cannot_read(stack, kind, code):
    stack.source.kind = kind
    with pytest.raises(SonderError) as caught:
        stack.service().profile_pure(ProfileDigestRequest("/w/p"), ctx())
    assert caught.value.code == code
    if code == "CAPTURE_NEEDS_HOST_TOOL":
        assert "profile_capture_digest" in str(caught.value)


def test_profile_pure_reads_pure_formats(stack):
    stack.source.kind = "callgrind"
    assert stack.service().profile_pure(ProfileDigestRequest("/w/callgrind.out.1", top_n=7),
                                        ctx())["top_n"] == 7


@pytest.mark.parametrize("path", ["", "   ", "x" * 1025, "a\x00b", None])
def test_bad_paths_are_invalid_input(stack, path):
    with pytest.raises(SonderError) as caught:
        stack.service().triage(CrashTriageRequest(path), ctx())
    assert caught.value.code == "INVALID_INPUT"


# -- runs ----------------------------------------------------------------------


def test_a_plan_without_steps_returns_the_tier0_result_immediately(stack):
    stack.planner.steps = ()
    outcome = stack.service().crash(CrashDigestRequest("/w/core.1"), ctx())
    assert outcome.status == "complete" and outcome.run_id == ""
    assert outcome.crash.source_kind == "elf_core"
    assert stack.launcher.started == []


def test_tier0_failure_without_steps_is_parse_failed(stack):
    stack.planner.steps = ()
    stack.triage.fail_crash = True
    outcome = stack.service().crash(CrashDigestRequest("/w/core.1"), ctx())
    assert outcome.status == "failed" and outcome.error_code == "PARSE_FAILED"


def test_a_run_merges_each_step_with_the_run_nonce_and_caches_the_result(stack):
    stack.planner.steps = (step("gdb", "gdb"), step("llvm_symbolizer", "symbolizer_json"))
    service = stack.service()
    outcome = service.crash(CrashDigestRequest("/w/core.1"), ctx(), wait_seconds=5)
    run_id = outcome.run_id
    assert outcome.status == "complete"
    assert [m[0] for m in stack.triage.merges] == ["gdb", "symbolizer_json"]
    assert {m[2] for m in stack.triage.merges} == {"0123456789abcdef"}
    assert stack.output.reads == [run_id + "-s0", run_id + "-s1"]
    assert outcome.crash.egress_isolation == "netns"
    assert (run_id, "result.json") in stack.launcher.json
    before = len(stack.triage.merges)
    again = service.result(run_id, ctx(), wait_seconds=0)
    assert again.status == "complete" and len(stack.triage.merges) == before
    assert again.crash.engines == outcome.crash.engines


def test_dump_syms_steps_are_not_parsed(stack):
    stack.planner.steps = (step("minidump_stackwalk", "dump_syms"),
                           step("minidump_stackwalk", "stackwalk_json"))
    stack.service().crash(CrashDigestRequest("/w/core.1"), ctx(), wait_seconds=5)
    assert [m[0] for m in stack.triage.merges] == ["stackwalk_json"]


def test_verified_modules_are_marked_loaded_after_a_completed_chain(stack):
    stack.planner.steps = (step("llvm_symbolizer", "symbolizer_json"),)
    original = stack.planner.plan_crash

    def plan_crash(*args, **kwargs):
        return replace(original(*args, **kwargs), verified_modules=("spark.exe",),
                       module_symbols=(("other.dll", "mismatch"),))

    stack.planner.plan_crash = plan_crash
    outcome = stack.service().crash(CrashDigestRequest("/w/core.1"), ctx(), wait_seconds=5)
    assert set(outcome.crash.module_symbols) == {("other.dll", "mismatch"), ("spark.exe", "loaded")}


def test_a_changed_input_discards_the_result(stack):
    stack.launcher.auto_finish = {"input_changed": True}
    outcome = stack.service().crash(CrashDigestRequest("/w/core.1"), ctx(), wait_seconds=5)
    assert outcome.status == "failed" and outcome.error_code == "INPUT_CHANGED"
    assert outcome.crash is None and stack.triage.merges == []


def test_the_output_limit_gives_a_partial_truncated_result(stack):
    stack.launcher.auto_finish = {"output_limit": True, "status": "partial"}
    outcome = stack.service().crash(CrashDigestRequest("/w/core.1"), ctx(), wait_seconds=5)
    assert outcome.status == "partial" and outcome.error_code == "OUTPUT_LIMIT"
    assert outcome.crash.truncated is True


@pytest.mark.parametrize("status", ["timed_out", "cancelled"])
def test_a_stopped_run_returns_the_tier0_report_unmerged(stack, status):
    stack.launcher.auto_finish = {"status": status}
    outcome = stack.service().crash(CrashDigestRequest("/w/core.1"), ctx(), wait_seconds=5)
    assert outcome.status == status and outcome.crash is not None
    assert stack.triage.merges == []


def test_a_running_chain_returns_the_run_id_to_poll(stack):
    stack.launcher.auto_finish = None
    outcome = stack.service().crash(CrashDigestRequest("/w/core.1"), ctx(), wait_seconds=0)
    assert outcome.status == "running" and outcome.run_id.startswith("debug-run-")
    stack.launcher.finish(outcome.run_id)
    assert stack.service().result(outcome.run_id, ctx()).status == "complete"


def test_cancelling_the_callers_operation_cancels_the_run(stack):
    stack.launcher.auto_finish = None

    class Token:
        cancelled = True

        def wait(self, timeout=None):
            return True

    context = replace(ctx(), cancellation=Token())
    service = stack.service()
    # _start refuses an already-cancelled context, so cancel after start.
    original = stack.launcher.start

    def start(plan, context_, run_id):
        return original(plan, replace(context_, cancellation=ctx().cancellation), run_id)

    stack.launcher.start = start
    plan, base, ident = service._prepare_crash(CrashDigestRequest("/w/core.1"), ctx(), False)
    run_id = service._start(plan, ctx(), {"kind": "crash"}, base)
    outcome = service._await(run_id, context, 5.0, cancel_on_abort=True)
    assert stack.launcher.cancelled and outcome.status == "cancelled"


def test_the_per_caller_cap_refuses_a_third_run(stack):
    stack.launcher.running = 2
    with pytest.raises(SonderError) as caught:
        stack.service().crash(CrashDigestRequest("/w/core.1"), ctx())
    assert caught.value.code == "DEBUG_RUN_BUSY"
    assert stack.launcher.started == []


def test_the_cap_check_and_the_start_are_one_step():
    stack = Stack()
    stack.launcher.auto_finish = None
    service = stack.service(max_concurrent_per_principal=1)
    original = stack.launcher.start
    gate = threading.Barrier(2)

    def start(plan, context, run_id):
        result = original(plan, context, run_id)
        stack.launcher.running += 1
        return result

    stack.launcher.start = start
    results = []

    def attempt():
        gate.wait()
        try:
            results.append(service.crash(CrashDigestRequest("/w/core.1"), ctx(), wait_seconds=0).status)
        except SonderError as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["DEBUG_RUN_BUSY", "running"]


def test_results_are_owner_checked(stack):
    outcome = stack.service().crash(CrashDigestRequest("/w/core.1"), ctx(principal="alice"),
                                    wait_seconds=5)
    service = stack.service()
    for run_id, who in ((outcome.run_id, "mallory"), ("debug-run-" + "0" * 32, "alice"),
                        ("not-a-run", "alice"), (outcome.run_id + "x", "alice")):
        with pytest.raises(SonderError) as caught:
            service.result(run_id, ctx(principal=who))
        assert caught.value.code == "JOB_NOT_FOUND"
    assert service.result(outcome.run_id, ctx(principal="alice")).status == "complete"


def test_cancel_is_console_or_http_only_and_owner_checked(stack):
    stack.launcher.auto_finish = None
    service = stack.service()
    run_id = service.crash(CrashDigestRequest("/w/core.1"), ctx(), wait_seconds=0).run_id
    for source in ("mcp", "worker", "system"):
        with pytest.raises(Forbidden):
            service.cancel(run_id, ctx(source))
    with pytest.raises(SonderError) as caught:
        service.cancel(run_id, ctx("http", principal="mallory"))
    assert caught.value.code == "JOB_NOT_FOUND"
    assert service.cancel(run_id, ctx("http")).status == "cancelled"
    assert stack.launcher.cancelled and stack.launcher.cancelled[0][0] == run_id


def test_a_host_profile_run_parses_every_step_output(stack):
    stack.source.kind = "perf_data"
    stack.planner.steps = (step("perf", "perf_folded"), step("perf", "perf_flat"))
    outcome = stack.service().profile(ProfileDigestRequest("/w/perf.data", top_n=9), ctx(),
                                      wait_seconds=5)
    assert outcome.status == "complete"
    assert [parser for parser, _ in outcome.profile["outputs"]] == ["perf_folded", "perf_flat"]


def test_a_pure_profile_through_the_host_tool_entry_needs_no_run(stack):
    stack.source.kind = "callgrind"
    outcome = stack.service().profile(ProfileDigestRequest("/w/callgrind.out.1"), ctx())
    assert outcome.status == "complete" and outcome.run_id == ""
    assert stack.launcher.started == []

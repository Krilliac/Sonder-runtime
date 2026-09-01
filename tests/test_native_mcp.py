from __future__ import annotations

import io
import json
from types import SimpleNamespace

from sonder_runtime.platform.config import SonderConfig
from sonder_runtime.bootstrap.native_mcp import native_tool_registry, run_native_mcp
from sonder_runtime.application.protocol.mcp_compatibility import SubscriptionNotificationRouter
from sonder_runtime.interfaces.mcp.transport import StdioMcpTransport
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.adapters.inspection import content_digest


class _Executor:
    def execute(self, call, context):
        from sonder_runtime.application.ports.tool_executor import ToolResult
        assert context.source == "mcp"
        return ToolResult(ok=True, output=call.tool + ":ok", evidence={"tool": call.tool})


def test_bounded_digest_fits_standard_mcp_frame_after_json_duplication():
    data = {
        "manifest": [
            {"path": "雪-%04d.txt" % index, "bytes": 1, "sha256": "a" * 64}
            for index in range(2_000)
        ],
        "errors": [], "complete": True, "truncated": False,
        "truncation_reasons": [],
    }
    rendered = content_digest.format_digest(data, max_output_bytes=48_000)
    structured = {"output": rendered, "isError": False, "error": None, "evidence": {}}
    frame = {
        "jsonrpc": "2.0", "id": 1,
        "result": StdioMcpTransport._standard_tool_result(structured),
    }

    assert len(json.dumps(frame, separators=(",", ":"), ensure_ascii=True).encode()) <= 256_000


def _app():
    class _State:
        workspace_roots = ()

    class _Config:
        state = _State()

    return type("App", (), {"config": _Config(), "tool_executor": _Executor()})()


class _Inspections:
    def inspect(self, name, arguments, context):
        from sonder_runtime.application.ports.tool_executor import ToolResult
        assert context.source == "mcp"
        return ToolResult(ok=True, output=name + ":inspection", evidence={"args": arguments})


class _Vision:
    def analyze(self, path, prompt, context):
        from sonder_runtime.application.ports.vision_gateway import VisionResponse
        assert path == "image.png"
        assert prompt == "describe"
        assert context.source == "mcp"
        return VisionResponse("a local image", "llava-local", "vision")


class _Jobs:
    def __init__(self):
        self.record = JobRecord(
            JobIdentity("job-1", "workflow", "op-1", "idem-1"),
            JobStatus.RUNNING, 2, "created", "updated",
        )

    def get(self, task_id):
        from sonder_runtime.domain.common.errors import NotFound
        if task_id != self.record.identity.job_id:
            raise NotFound("job not found")
        return self.record

    def cancel(self, task_id, *, reason):
        self.record = JobRecord(
            self.record.identity, JobStatus.CANCELLED, self.record.revision + 1,
            self.record.created_at, "cancelled", error=reason,
        )
        return (self.record,)


def test_native_catalog_is_bounded_and_deterministic():
    assert [item.name for item in native_tool_registry().list_all()] == [
        "approximate_location_lookup", "archive_create", "archive_extract", "archive_list", "artifact_risk_inspect",
        "compute_artifact_fetch", "compute_cancel", "compute_status", "compute_submit", "data_inspect", "data_query", "dependency_inventory",
        "directory_create", "directory_digest", "directory_tree", "edit_file", "fetch_artifact",
        "file_batch_write", "file_copy", "file_delete", "file_digest", "file_edit",
        "file_find", "file_move", "file_read", "file_read_range", "file_write", "image_inspect",
        "json_patch", "log_inspect", "make_directory", "process_list", "process_memory_risk_inspect",
        "program_search", "project_detect", "read_file", "run_program", "run_script", "script_search", "secret_scan", "text_patch", "text_search",
        "verify_artifact", "vision_analyze", "weather_lookup", "web_fetch", "web_search", "workspace_compare", "workspace_run", "write_file",
    ]


def test_native_catalog_has_exact_packaged_adapter_executor_parity():
    packaged_executor = {
        "approximate_location_lookup", "archive_create", "archive_extract",
        "artifact_risk_inspect", "directory_tree", "edit_file", "fetch_artifact",
        "file_batch_write", "file_copy", "file_delete", "file_find", "file_move",
        "file_read_range", "image_inspect", "json_patch", "make_directory",
        "process_list", "process_memory_risk_inspect", "program_search", "read_file",
        "run_program", "run_script", "script_search", "secret_scan", "text_patch",
        "text_search", "verify_artifact", "weather_lookup", "web_fetch", "web_search",
        "write_file",
    }
    packaged_inspections = {
        "archive_list", "data_inspect", "data_query", "dependency_inventory",
        "directory_digest", "file_digest", "log_inspect", "project_detect",
        "workspace_compare",
    }
    native_names = {item.name for item in native_tool_registry().list_all()}
    compatibility_aliases = {
        name for name, target in __import__(
            "sonder_runtime.bootstrap.native_mcp", fromlist=["_LEGACY_ALIASES"]
        )._LEGACY_ALIASES.items() if name != target
    }
    canonical_native = native_names - compatibility_aliases - {
        "vision_analyze", "compute_submit", "compute_status", "compute_cancel",
        "compute_artifact_fetch",
    }
    assert canonical_native == packaged_executor | packaged_inspections
    assert len(canonical_native) == 40
    assert compatibility_aliases == {
        "directory_create", "file_edit", "file_read", "file_write", "workspace_run",
    }


def test_native_compute_tools_are_bounded_and_require_explicit_remote_consent():
    registry = native_tool_registry()
    submit = registry.require("compute_submit").input_schema
    assert submit["additionalProperties"] is False
    assert submit["required"] == [
        "request_id", "workload", "catalog_entry_id", "workspace_mapping",
        "allow_remote",
    ]
    assert "inference" not in submit["properties"]["workload"]["enum"]
    assert submit["properties"]["deadline_seconds"]["maximum"] == 86_400
    assert submit["properties"]["arguments"]["maxItems"] == 64
    assert registry.require("compute_status").input_schema["required"] == [
        "controller_job_id",
    ]
    assert registry.require("compute_cancel").input_schema["required"] == [
        "controller_job_id", "reason",
    ]
    assert registry.require("compute_artifact_fetch").input_schema[
        "properties"
    ]["max_bytes"]["maximum"] == 98_304


def test_native_compute_submit_status_and_cancel_route_to_compute_service():
    from sonder_runtime.application.compute_fabric.jobs import RemoteJobReceipt
    from sonder_runtime.application.compute_fabric.service import ComputeSubmission
    from sonder_runtime.domain.compute_fabric import PlacementDecision

    class _Compute:
        def __init__(self):
            self.submitted = None
            self.cancelled = None

        def _result(self, controller_id="controller-1"):
            return ComputeSubmission(
                "linux-node",
                PlacementDecision(controller_id, "linux-node", (), ("linux-node",), ()),
                RemoteJobReceipt(
                    worker_id="linux-node",
                    remote_job_id="remote-1",
                    controller_job_id=controller_id,
                    idempotency_key="idem-1",
                    request_sha256="a" * 64,
                    state="running",
                ),
            )

        def submit(self, request, envelope):
            self.submitted = (request, envelope)
            return self._result(request.request_id)

        def status(self, controller_job_id):
            return self._result(controller_job_id)

        def cancel(self, controller_job_id, *, reason):
            self.cancelled = (controller_job_id, reason)
            return self._result(controller_job_id)

    compute = _Compute()
    app = _app()
    app.compute_service = lambda: compute
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2.0", "capabilities": {"tools": {}},
        }},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "compute_submit", "arguments": {
                "request_id": "controller-1", "idempotency_key": "idem-1",
                "workload": "test", "catalog_entry_id": "pytest",
                "workspace_mapping": "sonder", "relative_cwd": "tests",
                "arguments": ["test_api.py"], "deadline_seconds": 60,
                "idempotent": True, "allow_remote": True,
                "allow_local_fallback": False,
            },
        }},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "compute_status", "arguments": {
                "controller_job_id": "controller-1",
            },
        }},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "compute_cancel", "arguments": {
                "controller_job_id": "controller-1", "reason": "operator stop",
            },
        }},
    ]
    output = io.StringIO()
    run_native_mcp(
        app,
        input_stream=io.StringIO("\n".join(json.dumps(item) for item in requests) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert compute.submitted[0].allow_remote is True
    assert compute.submitted[0].allow_local_fallback is False
    assert compute.submitted[1].deadline_seconds == 60
    assert json.loads(rows[1]["result"]["output"])["remote_job_id"] == "remote-1"
    assert rows[2]["result"]["isError"] is False
    assert compute.cancelled == ("controller-1", "operator stop")


def test_native_catalog_contains_legacy_filesystem_alias_schemas():
    registry = native_tool_registry()
    read = registry.require("file_read")
    write = registry.require("file_write")
    run = registry.require("workspace_run")
    assert read.input_schema["required"] == ["path"]
    assert write.input_schema["properties"]["mode"]["enum"] == [
        "create", "overwrite", "append",
    ]
    assert run.input_schema["properties"]["program"]["type"] == "string"


def test_native_file_edit_schema_is_bounded_and_omits_legacy_bypass_fields():
    schema = native_tool_registry().require("file_edit").input_schema
    assert schema["required"] == ["path", "old", "new"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["path"]["minLength"] == 1
    assert "token" not in schema["properties"]
    assert "approval" not in schema["properties"]


def test_native_file_edit_routes_to_canonical_typed_executor():
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "file_edit", "arguments": {
            "path": "notes.txt", "old": "before", "new": "after", "count": 1,
        }},
    }
    output = io.StringIO()
    run_native_mcp(
        _app(), input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[1]["result"]["output"] == "edit_file:ok"
    assert rows[1]["result"]["isError"] is False


def test_native_archive_extract_schema_declares_safe_bounds():
    schema = native_tool_registry().require("archive_extract").input_schema
    assert schema["required"] == ["source", "destination"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["max_entries"] == {
        "type": "integer", "minimum": 1, "maximum": 10_000,
    }
    assert schema["properties"]["max_file_bytes"]["maximum"] == 256_000_000
    assert schema["properties"]["max_total_bytes"]["maximum"] == 1_000_000_000
    assert schema["properties"]["max_ratio"]["maximum"] == 1_000.0
    assert schema["properties"]["max_path_depth"]["maximum"] == 128
    assert schema["properties"]["max_results"]["maximum"] == 10_000
    assert schema["properties"]["max_seconds"]["maximum"] == 60.0


def test_native_archive_extract_routes_to_typed_executor():
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "archive_extract", "arguments": {
            "source": "bundle.zip", "destination": "unpacked",
            "max_entries": 10, "max_seconds": 5,
        }},
    }
    output = io.StringIO()
    run_native_mcp(
        _app(), input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[1]["result"]["output"] == "archive_extract:ok"
    assert rows[1]["result"]["isError"] is False


def test_native_archive_create_schema_is_bounded_and_omits_legacy_bypass_fields():
    schema = native_tool_registry().require("archive_create").input_schema
    assert schema["required"] == ["root", "inputs_json", "destination"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["archive_format"]["enum"] == ["zip", "tar"]
    assert schema["properties"]["max_files"]["maximum"] == 10_000
    assert schema["properties"]["max_entries"]["maximum"] == 20_000
    assert schema["properties"]["max_depth"]["maximum"] == 64
    assert schema["properties"]["inputs_json"]["maxLength"] == 1_000_000
    assert "token" not in schema["properties"]
    assert "approval" not in schema["properties"]


def test_native_archive_create_routes_to_typed_executor():
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "archive_create", "arguments": {
            "root": "project", "inputs_json": '["src", "README.md"]',
            "destination": "release.zip", "archive_format": "zip",
            "max_files": 10, "max_entries": 20,
        }},
    }
    output = io.StringIO()
    run_native_mcp(
        _app(), input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[1]["result"]["output"] == "archive_create:ok"
    assert rows[1]["result"]["isError"] is False


def test_native_archive_list_schema_is_bounded_and_omits_legacy_bypass_fields():
    schema = native_tool_registry().require("archive_list").input_schema
    assert schema["required"] == ["path"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["max_entries"] == {
        "type": "integer", "minimum": 1, "maximum": 10_000,
    }
    assert schema["properties"]["max_file_bytes"]["maximum"] == 256_000_000
    assert schema["properties"]["max_total_bytes"]["maximum"] == 1_000_000_000
    assert schema["properties"]["max_ratio"]["maximum"] == 1_000.0
    assert schema["properties"]["max_path_depth"]["maximum"] == 128
    assert schema["properties"]["max_results"]["maximum"] == 10_000
    assert schema["properties"]["max_seconds"]["maximum"] == 60.0
    assert "token" not in schema["properties"]
    assert "approval" not in schema["properties"]


def test_native_archive_list_routes_to_packaged_typed_inspection_executor():
    app = _app()
    app.inspections = _Inspections()
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "archive_list", "arguments": {
            "path": "bundle.zip", "max_entries": 10, "max_file_bytes": 1000,
            "max_total_bytes": 2000, "max_ratio": 10.0, "max_path_depth": 8,
            "max_results": 10, "max_seconds": 5.0,
        }},
    }
    output = io.StringIO()
    run_native_mcp(
        app, input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[1]["result"]["output"] == "archive_list:inspection"
    assert rows[1]["result"]["evidence"]["args"]["max_ratio"] == 10.0
    assert rows[1]["result"]["isError"] is False


def test_native_transport_calls_application_tool_port():
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "x.txt"}},
    }
    output = io.StringIO()
    count = run_native_mcp(
        _app(), input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert count == 2
    assert rows[1]["result"]["output"] == "read_file:ok"
    assert rows[1]["result"]["isError"] is False


def test_packaged_native_mcp_2x_stream_negotiates_lists_calls_and_delivers_subscription(
    monkeypatch,
):
    """Exercise the packaged native path as one MCP 2.x client/server stream."""
    import sonder_runtime.bootstrap.native_mcp as native_mcp

    router = SubscriptionNotificationRouter()
    captured = []

    class _Input(io.StringIO):
        def readline(self, *args):
            line = super().readline(*args)
            if line == "" and not getattr(self, "published", False):
                self.published = True
                assert router.publish("job.updated", {"job": "j1", "state": "ready"}) == 1
            return line

    class _RoutedTransport(StdioMcpTransport):
        def __init__(self, *args, **kwargs):
            kwargs["notifications"] = router
            kwargs["connection_id"] = "api-003-client"
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr(native_mcp, "StdioMcpTransport", _RoutedTransport)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersions": ["2.0"],
            "capabilities": {"tools": {}, "notifications": {}},
        }},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "read_file", "arguments": {"path": "notes.txt"},
        }},
        {"jsonrpc": "2.0", "id": 4, "method": "sonder/subscribe", "params": {
            "event": "job.updated",
        }},
    ]
    output = io.StringIO()
    count = run_native_mcp(
        _app(),
        input_stream=_Input("\n".join(json.dumps(request) for request in requests) + "\n"),
        output_stream=output,
    )

    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert count == 4
    assert captured[0].negotiation.agreed_version == "2.0"
    assert rows[0]["result"]["protocolVersion"] == "2.0"
    assert "read_file" in {tool["name"] for tool in rows[1]["result"]["tools"]}
    assert rows[2]["result"] == {
        "output": "read_file:ok", "isError": False,
        "error": "", "evidence": {"tool": "read_file"},
    }
    assert rows[3]["result"] == {"subscribed": "job.updated"}
    assert rows[4] == {
        "jsonrpc": "2.0", "method": "notifications/event",
        "params": {"event": "job.updated", "payload": {
            "job": "j1", "state": "ready",
        }},
    }


def test_native_mcp_composes_durable_tasks_when_job_service_is_available():
    app = _app()
    app.job_service = lambda: _Jobs()
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersions": ["2.0"], "capabilities": {"tasks": {}},
        }},
        {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {
            "taskId": "job-1",
        }},
    ]
    output = io.StringIO()
    run_native_mcp(
        app,
        input_stream=io.StringIO("\n".join(json.dumps(item) for item in requests) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0]["result"]["capabilities"] == {"tasks": {}}
    assert rows[1]["result"]["taskId"] == "job-1"
    assert rows[1]["result"]["contentRedacted"] is True


def test_native_legacy_file_read_alias_calls_canonical_executor():
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "file_read", "arguments": {"path": "x.txt"}},
    }
    output = io.StringIO()
    run_native_mcp(
        _app(), input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[1]["result"]["output"] == "read_file:ok"


def test_native_schema_rejects_unknown_arguments_as_protocol_error():
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "file_read", "arguments": {"path": "x.txt", "token": "secret"}},
    }
    output = io.StringIO()
    run_native_mcp(
        _app(), input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    row = [json.loads(line) for line in output.getvalue().splitlines()][1]
    assert row["error"]["code"] == -32602


def test_native_read_only_inspection_routes_through_application_service():
    app = _app()
    app.inspections = _Inspections()
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "file_digest", "arguments": {"path": "x.txt"}},
    }
    output = io.StringIO()
    run_native_mcp(
        app, input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    row = [json.loads(line) for line in output.getvalue().splitlines()][1]
    assert row["result"]["output"] == "file_digest:inspection"


def test_native_vision_routes_through_application_vision_service():
    app = _app()
    app.vision = _Vision()
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}},
    }
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "vision_analyze", "arguments": {
            "path": "image.png", "prompt": "describe",
        }},
    }
    output = io.StringIO()
    run_native_mcp(
        app, input_stream=io.StringIO(json.dumps(request) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    row = [json.loads(line) for line in output.getvalue().splitlines()][1]
    assert row["result"]["output"] == "a local image"
    assert row["result"]["evidence"]["model"] == "llava-local"


def test_native_entrypoint_fences_safety_before_configuration(monkeypatch):
    import sonder_runtime.__main__ as entrypoint
    import sonder_runtime.adapters.security.unsafe_lab as unsafe_lab
    import sonder_runtime.bootstrap.app as bootstrap_app
    import sonder_runtime.bootstrap.native_mcp as native_mcp

    calls = []
    monkeypatch.setattr(unsafe_lab, "require_startup", lambda: calls.append("safety"))
    monkeypatch.setattr(
        entrypoint, "_load_config", lambda args: calls.append("config") or SonderConfig()
    )
    monkeypatch.setattr(
        bootstrap_app, "build_application",
        lambda **kwargs: calls.append("build") or _app(),
    )
    monkeypatch.setattr(
        native_mcp, "run_native_mcp", lambda application: calls.append("run") or 0,
    )

    assert entrypoint.cmd_mcp(SimpleNamespace(native=True)) == 0
    assert calls == ["safety", "config", "build", "run"]


def test_native_entrypoint_reports_safety_refusal_without_traceback(monkeypatch, capsys):
    import sonder_runtime.__main__ as entrypoint
    import sonder_runtime.adapters.security.unsafe_lab as unsafe_lab

    monkeypatch.setattr(
        unsafe_lab,
        "require_startup",
        lambda: (_ for _ in ()).throw(unsafe_lab.UnsafeLabError("elevated host")),
    )
    monkeypatch.setattr(
        entrypoint,
        "_load_config",
        lambda _args: pytest.fail("configuration must not load after safety refusal"),
    )

    assert entrypoint.cmd_mcp(SimpleNamespace(native=True)) == 2
    assert capsys.readouterr().err == "native MCP startup refused: elevated host\n"

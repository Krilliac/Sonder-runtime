"""Security, atomicity, bounds, and integration tests for data_convert."""
import hashlib
import inspect
import json
import os
from pathlib import Path
import time

import pytest

import activity_tracker
import data_convert
import file_ops
import server


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    return root


def _convert(project, name="input.json", output="output.csv", **kwargs):
    return data_convert.convert_data(
        project / name, project / output, ["name", "id"], **kwargs,
    )


def test_preview_is_deterministic_ordered_and_does_not_touch_disk(project):
    source = project / "input.json"
    source.write_text(
        '[{"id":2,"name":"beta","ignored":true},'
        '{"id":1,"name":"alpha","ignored":false}]',
        encoding="utf-8",
    )
    output = project / "output.csv"

    first = _convert(project)
    second = _convert(project)

    assert data_convert.encode_result(first) == data_convert.encode_result(second)
    assert first["mode"] == "preview" and first["applied"] is False
    assert first["fields"] == ["name", "id"]
    assert first["preview_rows"] == [
        {"name": "beta", "id": 2}, {"name": "alpha", "id": 1},
    ]
    expected = b"name,id\nbeta,2\nalpha,1\n"
    assert first["converted_bytes"] == len(expected)
    assert first["converted_sha256"] == hashlib.sha256(expected).hexdigest()
    assert not output.exists()
    assert not list(project.glob("*.sonder-convert-*.tmp"))


def test_apply_atomically_creates_exact_output_without_overwrite(project):
    (project / "input.json").write_text(
        '[{"id":2,"name":"beta"},{"id":1,"name":"alpha"}]',
        encoding="utf-8",
    )
    report = _convert(project, apply=True)
    output = project / "output.csv"
    assert report["applied"] is True
    assert output.read_bytes() == b"name,id\nbeta,2\nalpha,1\n"
    assert not list(project.glob("*.sonder-convert-*.tmp"))

    before = output.read_bytes()
    with pytest.raises(data_convert.DataConvertError, match="never overwrites"):
        _convert(project, apply=True)
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    ("name", "payload", "output", "fields", "expected"),
    [
        (
            "input.jsonl", '{"id":1,"meta":{"b":2,"a":1}}\n', "out.tsv",
            ["id", "meta"], b'id\tmeta\n1\t"{""a"":1,""b"":2}"\n',
        ),
        (
            "input.csv", "id,name\n1,alpha\n", "out.json",
            ["name", "id"], b'[{"name":"alpha","id":"1"}]\n',
        ),
        (
            "input.tsv", "id\tname\n1\talpha\n", "out.jsonl",
            ["id", "name"], b'{"id":"1","name":"alpha"}\n',
        ),
    ],
)
def test_supported_input_output_formats(project, name, payload, output, fields, expected):
    (project / name).write_text(payload, encoding="utf-8")
    report = data_convert.convert_data(
        project / name, project / output, fields, apply=True,
    )
    assert report["rows"] == 1
    assert (project / output).read_bytes() == expected


def test_field_selection_is_exact_top_level_data_not_an_expression(project):
    (project / "input.json").write_text(
        '[{"a.b":"literal","a":{"b":"nested"}}]', encoding="utf-8",
    )
    report = data_convert.convert_data(
        project / "input.json", project / "out.jsonl", ["a.b"],
    )
    assert report["preview_rows"] == [{"a.b": "literal"}]
    with pytest.raises(data_convert.DataConvertError, match="JSON list"):
        data_convert.convert_data(
            project / "input.json", project / "out.jsonl",
            {"$eval": "a.b"},
        )


@pytest.mark.parametrize(
    ("name", "payload", "error"),
    [
        ("bad.json", '[{"value":NaN}]', "non-finite"),
        ("bad.jsonl", '{"value": Infinity}\n', "non-finite"),
        ("bad.csv", "id,id\n1,2\n", "duplicate headers"),
    ],
)
def test_nonfinite_and_duplicate_headers_are_rejected(project, name, payload, error):
    (project / name).write_text(payload, encoding="utf-8")
    with pytest.raises(data_convert.DataConvertError, match=error):
        data_convert.convert_data(
            project / name, project / "out.json", ["value"],
        )
    assert not (project / "out.json").exists()


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("duplicate.json", '[{"id":1,"id":2}]'),
        ("duplicate.jsonl", '{"id":1,"id":2}\n'),
    ],
)
def test_duplicate_json_object_keys_are_rejected(project, name, payload):
    (project / name).write_text(payload, encoding="utf-8")

    with pytest.raises(data_convert.DataConvertError, match="duplicate JSON object key: id"):
        data_convert.convert_data(
            project / name, project / "out.csv", ["id"], apply=True,
        )

    assert not (project / "out.csv").exists()


@pytest.mark.parametrize(("suffix", "delimiter"), [("csv", ","), ("tsv", "\t")])
def test_delimited_fields_can_use_configured_limit_above_csv_default(
    project, suffix, delimiter,
):
    payload = "x" * 150_000
    source = project / ("large." + suffix)
    source.write_text("id%spayload\n1%s%s\n" % (delimiter, delimiter, payload), encoding="utf-8")
    prior_parser_limit = data_convert.csv.field_size_limit()

    report = data_convert.convert_data(
        source, project / "out.json", ["id", "payload"],
        max_field_bytes=200_000,
    )

    assert report["rows"] == 1
    assert report["preview_rows"][0]["payload"] == payload
    assert data_convert.csv.field_size_limit() == prior_parser_limit


def test_invalid_utf8_is_rejected(project):
    (project / "bad.jsonl").write_bytes(b'{"id":1}\n\xff')
    with pytest.raises(data_convert.DataConvertError, match="UTF-8"):
        data_convert.convert_data(
            project / "bad.jsonl", project / "out.csv", ["id"],
        )
    (project / "bad.jsonl").write_bytes(b'{"id":"a\\u0000b"}\n')
    with pytest.raises(data_convert.DataConvertError, match="NUL"):
        data_convert.convert_data(
            project / "bad.jsonl", project / "out.csv", ["id"],
        )


def test_input_row_column_field_depth_and_output_caps(project):
    path = project / "input.json"
    path.write_text('[{"a":"xxxx","b":2},{"a":"yyyy","b":3}]', encoding="utf-8")
    cases = [
        ({"max_input_bytes": 4}, "input byte ceiling"),
        ({"max_rows": 1}, "row ceiling"),
        ({"max_columns": 1}, "column ceiling"),
        ({"max_field_bytes": 3}, "field byte ceiling"),
        ({"max_output_bytes": 4}, "output byte ceiling"),
    ]
    for kwargs, error in cases:
        with pytest.raises(data_convert.DataConvertError, match=error):
            data_convert.convert_data(path, project / "out.jsonl", ["a", "b"], **kwargs)
    with pytest.raises(data_convert.DataConvertError, match="field ceiling"):
        data_convert.convert_data(
            path, project / "out.jsonl", ["a", "b"], max_fields=1,
        )
    path.write_text('[{"a":"ok","unselected":"toolong"}]', encoding="utf-8")
    with pytest.raises(data_convert.DataConvertError, match="field byte ceiling"):
        data_convert.convert_data(
            path, project / "out.jsonl", ["a"], max_field_bytes=3,
        )
    path.write_text('[{"a":{"b":{"c":1}}}]', encoding="utf-8")
    with pytest.raises(data_convert.DataConvertError, match="depth ceiling"):
        data_convert.convert_data(
            path, project / "out.jsonl", ["a"], max_depth=2,
        )


def test_invalid_late_row_leaves_no_output_or_temporary(project):
    (project / "input.jsonl").write_text(
        '{"id":1}\n{"id":2}\nnot-json\n', encoding="utf-8",
    )
    with pytest.raises(data_convert.DataConvertError, match="line 3"):
        data_convert.convert_data(
            project / "input.jsonl", project / "out.csv", ["id"], apply=True,
        )
    assert not (project / "out.csv").exists()
    assert not list(project.glob("*.sonder-convert-*.tmp"))


def test_timeout_includes_record_validation_and_serialization(project, monkeypatch):
    (project / "input.json").write_text('[{"id":1}]', encoding="utf-8")
    original = data_convert._selected_record

    def slow_record(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.12)
        return result

    monkeypatch.setattr(data_convert, "_selected_record", slow_record)
    with pytest.raises(data_convert.DataConvertError, match="timeout ceiling"):
        data_convert.convert_data(
            project / "input.json", project / "out.jsonl", ["id"],
            timeout=0.05,
        )


def test_publication_race_never_overwrites_competing_output(project, monkeypatch):
    (project / "input.json").write_text('[{"id":1}]', encoding="utf-8")
    output = project / "out.jsonl"
    original_link = data_convert.os.link

    def competing_link(source, target):
        Path(target).write_text("competitor", encoding="utf-8")
        return original_link(source, target)

    monkeypatch.setattr(data_convert.os, "link", competing_link)
    with pytest.raises(data_convert.DataConvertError, match="appeared"):
        data_convert.convert_data(
            project / "input.json", output, ["id"], apply=True,
        )
    assert output.read_text(encoding="utf-8") == "competitor"
    assert not list(project.glob("*.sonder-convert-*.tmp"))


def test_sensitive_control_outside_and_symlink_paths_are_rejected(project, tmp_path):
    secret = project / ".env"
    secret.write_text('[{"id":1}]', encoding="utf-8")
    with pytest.raises(data_convert.DataConvertError, match="secret|control"):
        data_convert.convert_data(secret, project / "out.jsonl", ["id"])

    source = project / "input.json"
    source.write_text('[{"id":1}]', encoding="utf-8")
    with pytest.raises(data_convert.DataConvertError, match="secret|control"):
        data_convert.convert_data(source, project / "credentials.json", ["id"])

    outside = tmp_path / "outside.json"
    outside.write_text('[{"id":1}]', encoding="utf-8")
    other = project.parent / "other"
    other.mkdir()
    with pytest.raises(data_convert.DataConvertError, match="authorized root"):
        data_convert.convert_data(source, other / "out.jsonl", ["id"])

    link = project / "link.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    with pytest.raises(data_convert.DataConvertError, match="symlink|junction"):
        data_convert.convert_data(link, project / "out.jsonl", ["id"])

    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    linked_parent = project / "linked-parent"
    linked_parent.symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(data_convert.DataConvertError, match="symlink|junction"):
        data_convert.convert_data(source, linked_parent / "out.jsonl", ["id"])


def test_input_replacement_race_is_rejected(project, tmp_path, monkeypatch):
    source = project / "input.json"
    source.write_text('[{"id":1}]', encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text('[{"id":999}]', encoding="utf-8")

    # The race this test stages IS a symlink swap, so probe the capability up
    # front: inside the monkeypatched callback below a failure would surface as
    # an unrelated error from convert_data. Same guard the sibling symlink test
    # in this file already uses.
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    probe.unlink()
    original = data_convert._resolve_input
    replaced = False

    def race(path, roots):
        nonlocal replaced
        target = original(path, roots)
        if not replaced:
            replaced = True
            source.unlink()
            source.symlink_to(outside)
        return target

    monkeypatch.setattr(data_convert, "_resolve_input", race)
    with pytest.raises((data_convert.DataConvertError, PermissionError), match="symlink|guarded|source"):
        data_convert.convert_data(source, project / "out.jsonl", ["id"], apply=True)
    assert replaced and not (project / "out.jsonl").exists()


def test_no_expression_execution_or_network_surface():
    parameters = inspect.signature(data_convert.convert_data).parameters
    assert "expression" not in parameters and "regex" not in parameters
    source = Path(data_convert.__file__).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "urllib", "requests", "socket", "shell=True", "eval(", "exec("):
        assert forbidden not in source


def test_server_project_activity_mutation_validation_and_autopilot(project):
    source = project / "input.json"
    source.write_text('[{"id":1,"name":"alpha"}]', encoding="utf-8")
    activity_tracker.reset_for_tests()

    assert server.mcp._tool_manager.get_tool("data_convert") is not None
    assert "data_convert" in server.tool_manifest()
    assert "data_convert" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "data_convert" in server._WORK_MUTATION_TOOLS
    assert "data_convert" in server._AUTOPILOT_WORKSPACE_TOOLS
    assert "data_convert" not in server._AUTOPILOT_OBSERVE_TOOLS

    with activity_tracker.response_span("test", "convert data"):
        output = server._agent_dispatch_observed(
            "data_convert",
            {
                "input_path": "input.json", "output_path": "output.csv",
                "fields_json": ["name", "id"], "apply": True,
            },
            project=str(project),
        )
    report = json.loads(output)
    assert report["applied"] is True
    assert (project / "output.csv").read_bytes() == b"name,id\nalpha,1\n"
    mutation = server._agent_mutation_records(
        "data_convert", {"output_path": str(project / "output.csv"), "apply": True},
    )
    assert server._agent_mutation_records(
        "data_convert", {"output_path": str(project / "preview.csv"), "apply": False},
    ) == []
    assert mutation[0]["path"] == server._agent_normalized_path(project / "output.csv")
    assert server._agent_validation_covers(
        "file_read", {"path": str(project / "output.csv")}, mutation, "converted output",
    )
    event = next(
        row for row in activity_tracker.latest()["events"]
        if row.get("kind") == "data_convert"
    )
    assert event["path"] == str((project / "output.csv").resolve())


def test_project_scope_rebases_both_paths_and_rejects_escape(project):
    scoped = server._project_scope_args(
        "data_convert",
        {"input_path": "data/in.json", "output_path": "out.csv"},
        str(project),
    )
    assert Path(scoped["input_path"]).resolve() == (project / "data/in.json").resolve()
    assert Path(scoped["output_path"]).resolve() == (project / "out.csv").resolve()
    escaped = server._project_scope_args(
        "data_convert",
        {"input_path": "../in.json", "output_path": "out.csv"},
        str(project),
    )
    assert "outside" in server._repository_scope_path_error(
        "data_convert", escaped, str(project),
    )

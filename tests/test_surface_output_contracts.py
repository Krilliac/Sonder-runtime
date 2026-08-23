import io
import json

import pytest

import sonder_runtime.interfaces.http.serve as http_serve
import sonder_runtime.interfaces.repl.repl as sonder_repl
from sonder_runtime import __main__ as runtime_main


def test_json_lines_writer_emits_ordered_parseable_records_and_flushes_tail():
    target = io.StringIO()
    writer = sonder_repl._JsonLinesWriter(target)

    writer.write("first\nsecond")
    writer.write(" line\n")
    writer.write("unicode: café")
    writer.close()

    records = [json.loads(line) for line in target.getvalue().splitlines()]
    assert [record["seq"] for record in records] == [1, 2, 3]
    assert {record["schema"] for record in records} == {
        "sonder.repl-output.v1"
    }
    assert {record["event"] for record in records} == {"output"}
    assert [record["text"] for record in records] == [
        "first", "second line", "unicode: café",
    ]


def test_json_repl_uses_machine_mode_without_changing_the_normal_loop(monkeypatch):
    target = io.StringIO()
    calls = []

    def fake_main(*, machine_output=False):
        calls.append(machine_output)
        print("answer\nstatus: complete")

    monkeypatch.setattr(sonder_repl, "main", fake_main)
    original_ansi = sonder_repl._Ansi.enabled

    sonder_repl.run_jsonl(target)

    records = [json.loads(line) for line in target.getvalue().splitlines()]
    assert calls == [True]
    assert [record["text"] for record in records] == ["answer", "status: complete"]
    assert sonder_repl._Ansi.enabled is original_ansi


def test_repl_json_cli_help_advertises_json_lines_contract(capsys):
    parser = runtime_main.build_parser()
    args = parser.parse_args(["repl", "--json"])

    assert args.json is True
    assert args.func is runtime_main.cmd_repl
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["repl", "--help"])
    assert raised.value.code == 0
    assert "sonder.repl-output.v1 JSON Lines" in capsys.readouterr().out


def test_loopback_dashboard_is_accessible_and_overlap_safe():
    page = http_serve._LOCAL_LOG_PAGE

    assert '<html lang="en">' in page
    assert 'role="status" aria-live="polite"' in page
    assert 'aria-label="Redacted server log"' in page
    assert 'id="pause"' in page and 'aria-pressed="false"' in page
    assert 'id="refresh"' in page and 'id="copy"' in page
    assert "if(inFlight)return" in page
    assert "AbortController" in page
    assert "visibilitychange" in page
    assert "MAX_BACKOFF_MS" in page
    assert "log.textContent=payload.log" in page
    assert "innerHTML" not in page
    assert "/v1/local/server-log" in page

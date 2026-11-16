"""Runner discovery by weights blob, and the single-flight measurement scheduler."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from sonder_runtime.adapters.inference import runner_footprint as rf

HEX = "ab" * 32
OTHER = "cd" * 32


def test_blob_digest_is_found_in_paths_and_urls_case_insensitively():
    assert rf.blob_digest_from_text("--model C:\\o\\blobs\\sha256-%s --x" % HEX.upper()) == HEX
    assert rf.blob_digest_from_text("sha256:%s" % HEX) == HEX
    assert rf.blob_digest_from_text("no blob here") is None
    assert rf.blob_digest_from_text(None) is None


def test_the_modelfile_from_line_names_the_weights_blob():
    text = "# comment sha256-%s\nFROM /root/blobs/sha256-%s\nPARAMETER x 1\n" % (OTHER, HEX)
    assert rf.weights_blob_from_modelfile(text) == HEX
    assert rf.weights_blob_from_modelfile("FROM llama3\n") is None


def test_the_windows_lister_parses_one_and_many_processes():
    def runner(_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {"ProcessId": 5, "CommandLine": "x --model sha256-%s" % HEX}))
    found = rf.list_runner_processes(["llama-server.exe"], runner=runner, platform_name="win32")
    assert found == [rf.RunnerProcess(5, "x --model sha256-%s" % HEX)]

    def failing(_args, **_kwargs):
        raise OSError("no powershell")
    assert rf.list_runner_processes(["a"], runner=failing, platform_name="win32") == []


def test_the_linux_lister_reads_proc(tmp_path):
    proc = tmp_path / "123"
    proc.mkdir()
    (proc / "comm").write_text("llama-server\n")
    (proc / "cmdline").write_bytes(b"llama-server\0--model\0sha256-" + HEX.encode())
    other = tmp_path / "124"
    other.mkdir()
    (other / "comm").write_text("bash\n")
    found = rf.list_runner_processes(["llama-server"], platform_name="linux",
                                     proc_root=str(tmp_path))
    assert [p.pid for p in found] == [123]
    assert rf.blob_digest_from_text(found[0].command_line) == HEX


def test_measurement_picks_the_matching_runner_with_the_largest_private_bytes():
    processes = [rf.RunnerProcess(1, "--model sha256-%s" % HEX),
                 rf.RunnerProcess(2, "--model sha256-%s" % HEX),
                 rf.RunnerProcess(3, "--model sha256-%s" % OTHER)]
    memory = {1: (10, 20), 2: (30, 40), 3: (999, 999)}

    def reader(pid):
        ws, private = memory[pid]
        return SimpleNamespace(working_set_bytes=ws, private_bytes=private)

    got = rf.measure_runner_for_blob(HEX, ["x"], lister=lambda _n: processes, reader=reader)
    assert got == rf.Measurement(30, 40)
    assert rf.measure_runner_for_blob("ee" * 32, ["x"], lister=lambda _n: processes,
                                      reader=reader) is None


def test_the_scheduler_is_single_flight_and_survives_a_failing_job():
    jobs = []
    scheduler = rf.MeasurementScheduler(background=jobs.append)
    ran = []
    assert scheduler.schedule(lambda: ran.append(1)) is True
    assert scheduler.busy
    assert scheduler.schedule(lambda: ran.append(2)) is False
    jobs.pop()()
    assert ran == [1] and not scheduler.busy

    def boom():
        raise RuntimeError("x")
    assert scheduler.schedule(boom) is True
    jobs.pop()()
    assert not scheduler.busy


def test_the_default_scheduler_runs_off_the_calling_thread():
    done = threading.Event()
    names = []
    scheduler = rf.MeasurementScheduler()
    scheduler.schedule(lambda: (names.append(threading.current_thread().name), done.set()))
    assert done.wait(5)
    assert names != [threading.current_thread().name]

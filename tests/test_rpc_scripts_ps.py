"""Behavior tests for the llama.cpp RPC sharding PowerShell helpers (rpc-policy.ps1)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "rpc"
POLICY = SCRIPTS / "rpc-policy.ps1"
_SHELLS = [name for name in ("powershell", "pwsh") if shutil.which(name)]

pytestmark = pytest.mark.skipif(not _SHELLS, reason="PowerShell is not available")


def _run(shell: str, tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "probe.ps1"
    script.write_text(f". '{POLICY}'\n$ErrorActionPreference = 'Stop'\n{body}\n", encoding="utf-8-sig")
    return subprocess.run(
        [shell, "-NoProfile", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture(params=_SHELLS)
def shell(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_argument_string_quotes_per_argv_rules(shell: str, tmp_path: Path) -> None:
    body = r"""
$args1 = @('--model', 'C:\My $models\x "y".gguf', '--n', "a'b", 'plain', 'trail\')
ConvertTo-RpcArgumentString $args1
"""
    result = _run(shell, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        r'--model "C:\My $models\x \"y\".gguf" --n a'
        + "'"
        + "b plain trail" + chr(92)
    )


def test_base_url_brackets_ipv6(shell: str, tmp_path: Path) -> None:
    result = _run(shell, tmp_path, "ConvertTo-RpcBaseUrl '::1' 11438; ConvertTo-RpcBaseUrl '127.0.0.1' 11438")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["http://[::1]:11438", "http://127.0.0.1:11438"]


@pytest.mark.parametrize(
    "url", ["http://u:p@127.0.0.1:1", "http://127.0.0.1:1/?a=b", "http://127.0.0.1:1/#f"]
)
def test_base_url_check_rejects_userinfo_and_query(shell: str, tmp_path: Path, url: str) -> None:
    result = _run(shell, tmp_path, f"Assert-RpcBaseUrl '{url}'")
    assert result.returncode != 0


@pytest.mark.parametrize(
    "name",
    ["*", "Sonder llama-rpc *", "Sonder llama-rpc [ab]", "Sonder llama-rpc ?", "Other rule", "sonder llama-rpc x"],
)
def test_firewall_display_name_policy_refuses(shell: str, tmp_path: Path, name: str) -> None:
    result = _run(shell, tmp_path, f"Assert-RpcFirewallDisplayName '{name}'")
    assert result.returncode != 0


def test_firewall_display_name_policy_accepts_default(shell: str, tmp_path: Path) -> None:
    result = _run(
        shell, tmp_path, "Assert-RpcFirewallDisplayName 'Sonder llama-rpc worker (main peer only)'"
    )
    assert result.returncode == 0, result.stderr


def test_firewall_script_matches_rules_exactly() -> None:
    text = (SCRIPTS / "ensure-rpc-worker-firewall.ps1").read_text(encoding="utf-8")
    assert "Assert-RpcFirewallDisplayName" in text
    assert "Where-Object { $_.DisplayName -ceq $DisplayName }" in text


def _make_ollama(tmp_path: Path, digest: str) -> Path:
    root = tmp_path / "om"
    manifests = root / "manifests" / "registry.ollama.ai" / "library" / "demo"
    manifests.mkdir(parents=True)
    (root / "blobs").mkdir()
    (manifests / "latest").write_text(
        json.dumps(
            {"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": digest}]}
        ),
        encoding="utf-8",
    )
    return root


def test_resolve_ollama_blob_happy_path(shell: str, tmp_path: Path) -> None:
    digest = "sha256:" + "ab" * 32
    root = _make_ollama(tmp_path, digest)
    blob = root / "blobs" / ("sha256-" + "ab" * 32)
    blob.write_bytes(b"GGUF")
    result = _run(shell, tmp_path, f"Resolve-OllamaBlob 'demo:latest' '{root}'")
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == blob


@pytest.mark.parametrize("tag", ["demo:a/../../x", "..:latest", "demo:..", "a/b/..:latest", "demo:-x", "C:" + chr(92) + "x:latest"])
def test_resolve_ollama_blob_rejects_bad_segments(shell: str, tmp_path: Path, tag: str) -> None:
    root = _make_ollama(tmp_path, "sha256:" + "ab" * 32)
    result = _run(shell, tmp_path, f"Resolve-OllamaBlob '{tag}' '{root}'")
    assert result.returncode != 0
    assert "invalid" in (result.stderr + result.stdout).lower()


def test_resolve_ollama_blob_rejects_bad_digest(shell: str, tmp_path: Path) -> None:
    root = _make_ollama(tmp_path, "sha256:../../escape")
    result = _run(shell, tmp_path, f"Resolve-OllamaBlob 'demo:latest' '{root}'")
    assert result.returncode != 0
    assert "digest" in (result.stderr + result.stdout).lower()


def test_owned_process_check_requires_matching_exe_and_start_time(shell: str, tmp_path: Path) -> None:
    body = r"""
$me = Get-Process -Id $PID
$exe = $me.Path
$started = $me.StartTime.ToUniversalTime().ToString('o')
$a = Test-RpcOwnedProcess -ProcessId $PID -ExePath $exe -StartedUtc $started
$b = Test-RpcOwnedProcess -ProcessId $PID -ExePath 'C:\nope\llama-server.exe' -StartedUtc $started
$c = Test-RpcOwnedProcess -ProcessId $PID -ExePath $exe -StartedUtc '2001-01-01T00:00:00.0000000Z'
$d = Test-RpcOwnedProcess -ProcessId $PID -ExePath $exe -StartedUtc ''
"$a $b $c $d"
"""
    result = _run(shell, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True False False False"


@pytest.mark.parametrize("name", ["start-rpc-worker.ps1", "start-rpc-sharded.ps1"])
def test_launchers_use_start_process_not_encoded_wrapper(name: str) -> None:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "EncodedCommand" not in text
    assert "Win32_Process" not in text
    assert "Start-Process" in text
    assert "-ArgumentList" in text
    assert "Test-RpcOwnedProcess" in text


def test_sharded_launcher_hardening_markers() -> None:
    text = (SCRIPTS / "start-rpc-sharded.ps1").read_text(encoding="utf-8")
    assert "[ValidateRange(1, 65535)]" in text
    assert "-MaximumRedirection 0" in text or "MaximumRedirection = 0" in text
    assert "NoProxy" in text
    assert "ConvertTo-RpcBaseUrl" in text
    assert os.path.basename(str(SCRIPTS)) == "rpc"

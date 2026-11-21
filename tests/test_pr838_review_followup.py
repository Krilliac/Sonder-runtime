"""Review follow-up for #838: lexical network-path refusal before any realpath/stat."""
from __future__ import annotations

import os

import pytest

from sonder_runtime.adapters.claim_checker import ClaimChecker
from sonder_runtime.adapters.security_eval import dataset as dataset_module
from sonder_runtime.adapters.security_eval import output_store as store_module
from sonder_runtime.adapters.security_eval.output_store import OutputStore
from sonder_runtime.bootstrap import claim_check_agent_tools
from sonder_runtime.domain.claim_check import ClaimError
from sonder_runtime.domain.security_eval.errors import DatasetError, OutputDirRefused
from tests.test_security_eval_repo_reader import make_dir_link

pytestmark = pytest.mark.unit

UNC_SPELLINGS = [
    r"\\attacker-host\share\d.jsonl",
    "//attacker-host/share/d.jsonl",
    r"\\?\UNC\attacker-host\share\d.jsonl",
    r"\\.\pipe\d.jsonl",
]


def _spy(monkeypatch, touched):
    for name in ("stat", "lstat"):
        real = getattr(os, name)
        monkeypatch.setattr(os, name, lambda p, *a, _r=real, _n=name, **k: (touched.append(str(p)), _r(p, *a, **k))[1])
    real_rp = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: (touched.append(str(p)), real_rp(p, *a, **k))[1])


@pytest.mark.parametrize("spelling", UNC_SPELLINGS)
def test_dataset_loader_refuses_a_network_path_before_any_stat(monkeypatch, spelling):
    touched = []
    _spy(monkeypatch, touched)
    with pytest.raises(DatasetError, match="UNC, network or device"):
        dataset_module.load_dataset(spelling, max_bytes=1000, max_records=10)
    assert not [t for t in touched if "attacker-host" in t or "pipe" in t]


@pytest.mark.parametrize("spelling", UNC_SPELLINGS[:3])
def test_claim_checker_refuses_a_network_root_before_realpath(monkeypatch, spelling):
    touched = []
    _spy(monkeypatch, touched)
    with pytest.raises(ClaimError, match="UNC or network"):
        ClaimChecker(spelling)
    assert not [t for t in touched if "attacker-host" in t]


def test_output_store_refuses_a_dataset_reached_through_a_link(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "items.jsonl").write_text('{"original": true}\n', encoding="utf-8")
    make_dir_link(tmp_path / "alias", out)
    with pytest.raises(OutputDirRefused, match="dataset"):
        OutputStore(str(out), dataset_path=str(tmp_path / "alias" / "items.jsonl"))


def test_home_expansion_onto_a_share_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(os.path, "expanduser", lambda p: r"\\corp-fileserver\home\alice")
    touched = []
    _spy(monkeypatch, touched)
    guard = claim_check_agent_tools._within_roots([str(tmp_path)])
    with pytest.raises(PermissionError):
        guard("~")
    assert not [t for t in touched if "corp-fileserver" in t]


def test_output_store_does_not_resolve_roots_outside_the_allowlist(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    resolved = []
    real = store_module._real
    monkeypatch.setattr(store_module, "_real", lambda p: (resolved.append(p), real(p))[1])
    OutputStore(str(tmp_path / "out"), protected_roots=[str(allowed), r"C:\corp-local-root"],
                allowlist=[str(allowed)])
    assert not [p for p in resolved if "corp-local-root" in p]

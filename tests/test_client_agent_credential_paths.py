"""Write-class client tools refuse credential stores, as the server's mutation guard does.

A credential file changed without a prompt is either a stolen-token swap or a
program a later command runs (``~/.ssh/config`` ProxyCommand, an
``authorized_keys`` entry, an ``.npmrc`` script-shell). The list mirrors the
secret-name rules in ``sonder_runtime.adapters.filesystem.file_ops``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.interfaces.client_agent import (
    ClientAgentPolicy,
    execute_client_tool,
)

_REFUSED = [
    ".ssh/authorized_keys",
    ".ssh/config",
    "sub/.SSH/known_hosts",
    ".aws/credentials",
    ".gnupg/gpg.conf",
    ".kube/config",
    "id_rsa",
    "keys/id_ed25519",
    ".netrc",
    "_netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".env",
    ".env.local",
    "prod.env",
    "server.pem",
    "tls.KEY",
    "vault.kdbx",
    "credentials.json",
    "db_secret.yaml",
    ".docker/config.json",
    ".cargo/credentials.toml",
    ".m2/settings.xml",
    ".config/gh/hosts.yml",
    ".config/gcloud/access_tokens.db",
]


@pytest.mark.parametrize("rel_path", _REFUSED)
@pytest.mark.parametrize("mode", ["acceptEdits", "bypass"])
def test_write_to_credential_path_is_refused(tmp_path: Path, rel_path: str, mode: str):
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="manual")
    policy.set_mode(mode, confirm_bypass=True)

    res = execute_client_tool("file_write", {"path": rel_path, "content": "x\n"}, policy=policy)

    assert res.ok is False
    assert res.denied is True
    assert "credential" in res.error
    assert not (tmp_path / rel_path).exists()


def test_edit_and_delete_of_existing_credential_file_are_refused(tmp_path: Path):
    (tmp_path / ".ssh").mkdir()
    keys = tmp_path / ".ssh" / "authorized_keys"
    keys.write_text("ssh-ed25519 AAAA owner\n", encoding="utf-8")
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="acceptEdits")

    edit = execute_client_tool(
        "file_edit", {"path": ".ssh/authorized_keys", "old": "owner", "new": "attacker"}, policy=policy,
    )
    delete = execute_client_tool("file_delete", {"path": ".ssh/authorized_keys"}, policy=policy)
    mkdir = execute_client_tool("directory_create", {"path": ".ssh/extra"}, policy=policy)

    for res in (edit, delete, mkdir):
        assert res.ok is False and res.denied is True
    assert keys.read_text(encoding="utf-8") == "ssh-ed25519 AAAA owner\n"
    assert not (tmp_path / ".ssh" / "extra").exists()


def test_credential_path_reached_through_dotdot_is_refused(tmp_path: Path):
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="acceptEdits")

    res = execute_client_tool("file_write", {"path": "sub/../.ssh/config", "content": "x"}, policy=policy)

    assert res.ok is False and res.denied is True
    assert not (tmp_path / ".ssh" / "config").exists()


# A workspace root inside the home directory: the context rules (pairs,
# directory pairs, triples) see the root's trailing components, and a root
# that is itself a credential directory makes every write there a credential
# write. The server's mutation guard judges the full path the same way.
_REFUSED_UNDER_ROOT = [
    (".aws", "credentials"),
    (".aws", "config"),
    (".ssh", "authorized_keys"),
    (".config", "gcloud/application_default_credentials.json"),
    (".config", "gcloud/access_tokens.db"),
    (".config", "gh/hosts.yml"),
    (".config/gh", "hosts.yml"),
    (".docker", "config.json"),
    (".cargo", "credentials"),
    ("AppData/Roaming", "gcloud/credentials.db"),
]


@pytest.mark.parametrize(("root_rel", "rel_path"), _REFUSED_UNDER_ROOT)
@pytest.mark.parametrize("mode", ["acceptEdits", "bypass"])
def test_credential_path_is_refused_when_the_root_is_part_of_the_store(
    tmp_path: Path, root_rel: str, rel_path: str, mode: str,
):
    root = tmp_path / "home" / root_rel
    root.mkdir(parents=True)
    policy = ClientAgentPolicy(workspace_root=root, mode="manual")
    policy.set_mode(mode, confirm_bypass=True)

    res = execute_client_tool("file_write", {"path": rel_path, "content": "x\n"}, policy=policy)
    absolute = execute_client_tool(
        "file_write", {"path": str(root / rel_path), "content": "x\n"}, policy=policy,
    )

    for result in (res, absolute):
        assert result.ok is False
        assert result.denied is True
        assert "credential" in result.error
    assert not (root / rel_path).exists()


@pytest.mark.parametrize(
    ("root_rel", "rel_path"),
    [
        (".ssh/projects/app", "notes.txt"),
        (".config", "app/settings.json"),
        (".config", "gh/config.yml"),
        ("docker", "config.json"),
        ("gcloud", "readme.md"),
    ],
)
def test_root_context_does_not_refuse_ordinary_files(tmp_path: Path, root_rel: str, rel_path: str):
    root = tmp_path / "home" / root_rel
    root.mkdir(parents=True)
    policy = ClientAgentPolicy(workspace_root=root, mode="acceptEdits")

    res = execute_client_tool("file_write", {"path": rel_path, "content": "ok\n"}, policy=policy)

    assert res.ok is True, res.error


@pytest.mark.parametrize(
    "rel_path",
    ["config.json", "env.py", "docs/ssh.md", "environment.yml", "keys.txt", "src/secretary.py", "pem/readme.md"],
)
def test_ordinary_files_with_similar_names_stay_writable(tmp_path: Path, rel_path: str):
    policy = ClientAgentPolicy(workspace_root=tmp_path, mode="acceptEdits")

    res = execute_client_tool("file_write", {"path": rel_path, "content": "ok\n"}, policy=policy)

    assert res.ok is True, res.error

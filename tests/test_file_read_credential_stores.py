"""Credential stores inside an allowed root are denied by default (finding #32).

``SONDER_FILE_ROOTS=<ws>`` with ``<ws>/.ssh/id_rsa`` present let
``file_ops.read_file`` return the private key, while ``file_copy`` of the same
source was refused. The direct read tools now refuse ``.ssh``/``.aws``/
``.azure``/``.gnupg``/``.kube``, ``.docker/config.json``, ``.git/config``,
``.netrc``/``.git-credentials``/``.pgpass``, OpenSSH key files and ``.env*``
-- even with a developer token or bypass -- unless an operator-configured root
names the store itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import server
import sonder_runtime.adapters.filesystem.file_ops as file_ops


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(home / "file_roots.local"))
    return ws


CREDENTIAL_PATHS = [
    ".ssh/id_rsa",
    ".ssh/config",
    ".aws/credentials",
    ".azure/accessTokens.json",
    ".gnupg/private-keys-v1.d/key.key",
    ".kube/config",
    ".docker/config.json",
    ".git/config",
    ".netrc",
    ".git-credentials",
    ".pgpass",
    ".env",
    ".env.local",
    ".envrc",
    "nested/project/.env.production",
    "keys/id_ed25519",
]


def _plant(workspace: Path, relative: str) -> Path:
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("CREDENTIAL-MATERIAL", encoding="utf-8")
    return target


@pytest.mark.parametrize("relative", CREDENTIAL_PATHS)
def test_read_file_refuses_credential_store(workspace, relative):
    _plant(workspace, relative)
    with pytest.raises(PermissionError, match="credential store"):
        file_ops.read_file(relative)


@pytest.mark.parametrize("relative", [".ssh/id_rsa", ".env", ".aws/credentials"])
def test_developer_token_and_bypass_do_not_open_credential_stores(workspace, relative):
    target = _plant(workspace, relative)
    with pytest.raises(PermissionError, match="credential store"):
        file_ops.read_file(relative, developer_authorized=True)
    with pytest.raises(PermissionError, match="credential store"):
        file_ops.read_file(str(target), bypass=True)


def test_every_direct_read_family_refuses(workspace):
    _plant(workspace, ".ssh/id_rsa")
    with pytest.raises(PermissionError, match="credential store"):
        file_ops.require_read_access(".ssh/id_rsa")
    with pytest.raises(PermissionError, match="credential store"):
        file_ops.inspect_data(".ssh/id_rsa")
    for out in (
        server.file_read(".ssh/id_rsa"),
        server.file_read_range(".ssh/id_rsa"),
    ):
        assert out.startswith("ERROR:")
        assert "CREDENTIAL-MATERIAL" not in out


def test_an_explicit_root_naming_the_store_allows_it(workspace, monkeypatch):
    config = _plant(workspace, ".ssh/config")
    key = _plant(workspace, ".ssh/id_rsa")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(workspace / ".ssh"))
    assert file_ops.read_file(str(config))["text"] == "CREDENTIAL-MATERIAL"
    # A key file is also a classified secret: naming the store lifts the
    # credential default-deny, and the developer token is still required.
    with pytest.raises(PermissionError, match="without an authenticated developer token"):
        file_ops.read_file(str(key))
    assert file_ops.read_file(str(key), developer_authorized=True)["text"] == "CREDENTIAL-MATERIAL"


def test_an_explicit_file_root_names_a_dotenv(workspace):
    dotenv = _plant(workspace, "proj/.env")
    roots_file = Path(file_ops.roots_file_path())
    roots_file.write_text(str(dotenv) + "\n", encoding="utf-8")
    out = file_ops.read_file(str(dotenv), developer_authorized=True)
    assert out["text"] == "CREDENTIAL-MATERIAL"
    # Naming the project directory is not naming its .env.
    roots_file.write_text(str(workspace / "proj") + "\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="credential store"):
        file_ops.read_file(str(dotenv), developer_authorized=True)


def test_ordinary_files_and_git_objects_stay_readable(workspace):
    _plant(workspace, "src/app.py")
    _plant(workspace, ".git/HEAD")
    _plant(workspace, "docs/ssh-setup.md")
    _plant(workspace, ".environment.md")
    assert file_ops.read_file("src/app.py")["text"] == "CREDENTIAL-MATERIAL"
    assert file_ops.read_file(".git/HEAD")["text"] == "CREDENTIAL-MATERIAL"
    assert file_ops.read_file("docs/ssh-setup.md")["text"] == "CREDENTIAL-MATERIAL"
    assert file_ops.read_file(".environment.md")["text"] == "CREDENTIAL-MATERIAL"


def test_openssh_key_names_are_classified_secrets_for_mutation(workspace):
    _plant(workspace, "deploy/id_ed25519")
    with pytest.raises(PermissionError, match="mutate protected"):
        file_ops.write_file("deploy/id_ed25519", "x", mode="overwrite")


# Token stores added by the secret-path coverage change. Each entry names one
# rule so a mutation (removing it from file_ops) fails exactly its own tests.
EXTRA_CREDENTIAL_PATHS = [
    ".npmrc",
    ".pypirc",
    "kubeconfig",
    ".kube/config",
    "service-account.json",
    "service_account.json",
    "serviceaccount.json",
    "application_default_credentials.json",
    ".terraformrc",
    "terraform.rc",
    ".boto",
    ".s3cfg",
    ".htpasswd",
    ".vault-token",
    "vault/passwords.kdbx",
    ".config/gh/hosts.yml",
    ".config/gcloud/credentials.db",
    ".config/gcloud/legacy_credentials/me/adc.json",
    ".gcloud/access_tokens.db",
    ".azure/msal_token_cache.json",
    ".azure/accessTokens.json",
    ".ssh/id_ed25519",
    ".ssh/id_ecdsa",
    ".ssh/id_dsa",
]


def _mixed(relative: str) -> str:
    return "/".join(
        "".join(c.upper() if i % 2 else c for i, c in enumerate(part))
        for part in relative.split("/")
    )


@pytest.mark.parametrize("relative", EXTRA_CREDENTIAL_PATHS)
@pytest.mark.parametrize("prefix", ["", "nested/project/"])
def test_extra_token_stores_refused_top_level_and_nested(workspace, relative, prefix):
    _plant(workspace, prefix + relative)
    with pytest.raises(PermissionError, match="credential store"):
        file_ops.read_file(prefix + relative)


@pytest.mark.parametrize("relative", EXTRA_CREDENTIAL_PATHS)
def test_extra_token_stores_refused_in_mixed_case(workspace, relative):
    # The rules lower-case every component, so case never unlocks a store.
    mixed = _mixed(relative)
    _plant(workspace, mixed)
    with pytest.raises(PermissionError, match="credential store"):
        file_ops.read_file(mixed)


@pytest.mark.parametrize("relative", EXTRA_CREDENTIAL_PATHS)
def test_extra_token_store_refusal_names_the_file(workspace, relative):
    target = _plant(workspace, relative)
    with pytest.raises(PermissionError) as info:
        file_ops.read_file(relative)
    assert str(relative) in str(info.value) or target.name in str(info.value)
    assert "is a credential store" in str(info.value)


@pytest.mark.parametrize("relative", [
    "npmrc.md",
    "kube-config-guide.md",
    "service.json",
    "config.json",
    "terraform.md",
    "notes.kdbx.md",
    "gh/config.yml",
    ".config/gh/config.yml",
    ".config/other/credentials.db",
    "hosts.yml",
    "docs/gcloud.md",
    "id_ed25519.pub",
    ".ssh-notes.md",
])
def test_near_miss_names_stay_readable(workspace, relative):
    _plant(workspace, relative)
    assert file_ops.read_file(relative)["text"] == "CREDENTIAL-MATERIAL"


@pytest.mark.parametrize("name", [
    ".npmrc", ".pypirc", "kubeconfig", "service-account.json", ".terraformrc",
    "terraform.rc", ".boto", ".s3cfg", ".htpasswd", ".vault-token", "vault.kdbx",
    "id_ed25519", "id_ecdsa", "id_dsa",
])
def test_extra_token_stores_are_classified_secrets_for_mutation(name):
    assert file_ops._is_secret_path(Path("proj") / name), name
    assert file_ops._is_secret_path(Path("proj") / name.upper()), name


@pytest.mark.parametrize("name", ["id_ed25519.pub", "npmrc.md", "service.json", "config.json"])
def test_near_miss_names_are_not_classified_secrets(name):
    assert not file_ops._is_secret_path(Path("proj") / name), name

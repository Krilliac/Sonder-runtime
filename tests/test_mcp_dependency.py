import re
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _has_bare_mcp_install(text):
    """Whether a `pip [-opts] install [-opts] mcp` command installs mcp unpinned.

    One pass over whitespace-separated tokens: a pip token (`pip`,
    `/usr/bin/pip`, `foo-pip`), options (tokens of two or more characters
    starting with `-`), `install`, more options, then a bare `mcp` token. The
    regex this replaces could read every `--opt` two ways, exponential in a run
    of options (CodeQL py/redos), and rescanned option runs from each `-pip`.
    """
    pip_ready = False
    install_ready = False
    for match in re.finditer(r"\S+", text):
        token = match.group()
        if install_ready and token == "mcp":
            return True
        option = token.startswith("-") and len(token) > 1
        pip_at_end = token.endswith("pip") and (
            len(token) == 3 or not (token[-4].isalnum() or token[-4] == "_")
        )
        install_ready = (install_ready and option) or (pip_ready and token == "install")
        pip_ready = (pip_ready and option) or pip_at_end
    return False


def test_mcp_runtime_dependency_is_exactly_pinned():
    runtime = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
    assert "mcp==2.0.0" in runtime.splitlines()
    assert "cryptography==50.0.0" in runtime.splitlines()
    # An unpinned or ranged MCP is what makes an unattended bootstrap install a
    # release nobody probed. 1.x specifically no longer satisfies the imports.
    assert not any(
        line.startswith(("mcp>=", "mcp<=", "mcp<", "cryptography>=", "cryptography<="))
        for line in runtime.splitlines()
    )


def test_update_trust_dependencies_are_exactly_pinned():
    update = (ROOT / "requirements-update.txt").read_text(encoding="utf-8")
    assert {
        "tuf==7.0.0",
        "cryptography==50.0.0",
        "securesystemslib==1.4.0",
    }.issubset(update.splitlines())


def test_installers_use_the_shared_runtime_dependency_contract():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy_sonder.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    assert "-r requirements-runtime.txt" in dev
    assert not _has_bare_mcp_install(workflow)
    assert not _has_bare_mcp_install(deploy)
    assert not _has_bare_mcp_install(readme)
    assert "pip install -r requirements-dev.txt" in workflow
    assert 'pip install -r "$CLONE_DIR/requirements-runtime.txt"' in deploy
    assert "from mcp.server.mcpserver import MCPServer" in workflow
    assert "from mcp.server.mcpserver import MCPServer" in deploy
    assert "from mcp.server.mcpserver.tools import ToolManager" in workflow
    assert "from mcp.server.mcpserver.tools import ToolManager" in deploy
    # The 1.x module must not survive anywhere in the installer contract: a
    # probe that still imports it passes only on the version being retired.
    assert "mcp.server.fastmcp" not in workflow
    assert "mcp.server.fastmcp" not in deploy


def test_bare_mcp_install_detector_keeps_option_and_pin_checks():
    for command in ("pip install mcp", "pip --isolated install -q mcp\n",
                    "foo-pip -! install -- mcp ", "pip -pip install mcp"):
        assert _has_bare_mcp_install(command)
    for command in ("pip install mcp==2.0.0", "pipeline install mcp",
                    "pip install -r requirements-runtime.txt", "pip - install mcp"):
        assert not _has_bare_mcp_install(command)
    # '--!' is the option CodeQL reported: the old regex took seconds at 26 of
    # them, doubling with each one. A run of '-pip' tokens was quadratic.
    for text in ("pip" + " --!" * 26 + " -", "pip" + " --!" * 10_000 + " -",
                 "pip" + " -pip" * 10_000 + " -"):
        started = time.perf_counter()
        assert not _has_bare_mcp_install(text)
        assert time.perf_counter() - started < 2.0

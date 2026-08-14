import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mcp_runtime_dependency_excludes_breaking_v2():
    runtime = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
    assert "mcp==1.29.0" in runtime.splitlines()
    assert "cryptography==50.0.0" in runtime.splitlines()
    assert not any(
        line.startswith(("mcp>=", "mcp<=", "cryptography>=", "cryptography<="))
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
    bare_mcp_install = re.compile(
        r"\bpip(?:\s+--?\S+)*\s+install(?:\s+--?\S+)*\s+mcp(?:\s|$)"
    )
    assert not bare_mcp_install.search(workflow)
    assert not bare_mcp_install.search(deploy)
    assert not bare_mcp_install.search(readme)
    assert "pip install -r requirements-dev.txt" in workflow
    assert 'pip install -r "$CLONE_DIR/requirements-runtime.txt"' in deploy
    assert "from mcp.server.fastmcp import FastMCP" in workflow
    assert "from mcp.server.fastmcp import FastMCP" in deploy
    assert "from mcp.server.fastmcp.tools import ToolManager" in workflow
    assert "from mcp.server.fastmcp.tools import ToolManager" in deploy

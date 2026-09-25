"""macOS discovery (Homebrew, Xcode/CLT, app bundles) with fakes."""
import plistlib

from sonder_runtime.adapters.host_tools import darwin
from sonder_runtime.adapters.host_tools.bounded_process import BoundedRun
from sonder_runtime.adapters.host_tools.discovery import HostToolDiscovery
from sonder_runtime.domain.host_tools.model import DiscoverySource, VersionStatus
from sonder_runtime.domain.host_tools.registry import spec_for
from tests.test_tools_inventory_fakes import FakeHost

BREW = "/opt/homebrew/bin/brew"


def _mac(**kwargs):
    return FakeHost("Darwin", home="/Users/alice", **kwargs)


def test_brew_prefix_and_opt_dirs_are_searched():
    host = _mac(path="/usr/bin")
    host.add_exe(BREW, "1:1")
    host.outputs[(BREW, "--prefix")] = BoundedRun("ok", "/opt/homebrew\n", 0, 5)
    host.dirs.update({"/opt/homebrew/bin", "/opt/homebrew/sbin", "/opt/homebrew/opt/llvm/bin",
                      "/opt/homebrew/opt/openjdk/bin"})
    host.add_exe("/opt/homebrew/opt/llvm/bin/clang", "4:4", "Homebrew clang version 18.1.8")
    dirs = darwin.discover_brew(host.probes())
    assert dirs[:2] == ["/opt/homebrew/bin", "/opt/homebrew/sbin"]
    assert "/opt/homebrew/opt/llvm/bin" in dirs
    brew_env = host.envs[-1]
    assert brew_env["HOMEBREW_NO_AUTO_UPDATE"] == "1"
    snapshot = HostToolDiscovery(host.probes(), specs=(spec_for("clang"),)).discover(previous=None, full=False)
    clang = {r.name: r for r in snapshot.tools}["clang"]
    assert clang.source is DiscoverySource.BREW and clang.version == "18.1.8"


def test_brew_prefix_rejects_relative_output():
    host = _mac()
    host.add_exe(BREW, "1:1")
    host.outputs[(BREW, "--prefix")] = BoundedRun("ok", "../evil\n", 0, 5)
    assert darwin.discover_brew(host.probes()) == []


def test_command_line_tools_version_from_pkgutil():
    host = _mac()
    host.add_exe("/usr/bin/xcode-select", "1:1")
    host.add_exe("/usr/sbin/pkgutil", "2:2")
    host.dirs.add("/Library/Developer/CommandLineTools")
    host.outputs[("/usr/bin/xcode-select", "-p")] = BoundedRun(
        "ok", "/Library/Developer/CommandLineTools\n", 0, 5)
    host.outputs[("/usr/sbin/pkgutil", f"--pkg-info={darwin.CLT_PACKAGE}")] = BoundedRun(
        "ok", "package-id: com.apple.pkg.CLTools_Executables\nversion: 15.3.0.0.1.1708646388\n", 0, 5)
    records = darwin.discover_xcode(host.probes())
    assert [r.name for r in records] == ["xcode-clt"]
    assert records[0].version == "15.3.0.0" and records[0].version_status is VersionStatus.FROM_METADATA
    assert ("variant", "command_line_tools") in records[0].details
    assert not any("xcodebuild" in run[0] for run in host.runs)


def test_xcode_app_version_from_xcodebuild_only_inside_bundle():
    developer = "/Applications/Xcode.app/Contents/Developer"
    host = _mac()
    host.add_exe("/usr/bin/xcode-select", "1:1")
    host.dirs.add(developer)
    host.add_exe(developer + "/usr/bin/xcodebuild", "3:3")
    host.outputs[("/usr/bin/xcode-select", "-p")] = BoundedRun("ok", developer + "\n", 0, 5)
    host.outputs[(developer + "/usr/bin/xcodebuild", "-version")] = BoundedRun(
        "ok", "Xcode 15.4\nBuild version 15F31d\n", 0, 5)
    records = darwin.discover_xcode(host.probes())
    assert records[0].version == "15.4" and ("variant", "xcode") in records[0].details


def test_app_bundle_versions_from_plist_are_never_launched():
    host = _mac(path="/usr/bin")
    bundle = "/Applications/Visual Studio Code.app"
    host.dirs.update({"/Applications", bundle, "/Applications/PyCharm CE.app", "/Applications/Other.app"})
    host.contents[bundle + "/Contents/Info.plist"] = plistlib.dumps({"CFBundleShortVersionString": "1.90.2"})
    host.contents["/Applications/PyCharm CE.app/Contents/Info.plist"] = b"not a plist"
    records = {r.name: r for r in darwin.discover_app_bundles(host.probes())}
    assert set(records) == {"code", "pycharm"}
    assert records["code"].version == "1.90.2" and records["code"].source is DiscoverySource.APP_BUNDLE
    assert records["pycharm"].version == "" and records["pycharm"].version_status is VersionStatus.NOT_PROBED
    snapshot = HostToolDiscovery(host.probes(), specs=(spec_for("code"), spec_for("pycharm"))).discover(
        previous=None, full=False)
    assert {r.name for r in snapshot.tools} == {"code", "pycharm"}
    assert host.runs == []


def test_project_local_brew_is_never_run():
    host = _mac(path="/work/proj/bin:/usr/bin")
    planted = host.add_exe("/work/proj/bin/brew", "9:9")
    host.outputs[(planted, "--prefix")] = BoundedRun("ok", "/opt/homebrew\n", 0, 5)
    host.local.add("/work/proj")
    assert darwin.discover_brew(host.probes(), planted) == []
    assert host.runs == []


def test_project_local_xcodebuild_is_never_run():
    developer = "/work/proj/Fake.app/Contents/Developer"
    host = _mac()
    host.add_exe("/usr/bin/xcode-select", "1:1")
    host.dirs.add(developer)
    host.add_exe(developer + "/usr/bin/xcodebuild", "3:3")
    host.outputs[("/usr/bin/xcode-select", "-p")] = BoundedRun("ok", developer + "\n", 0, 5)
    host.outputs[(developer + "/usr/bin/xcodebuild", "-version")] = BoundedRun("ok", "Xcode 99.0\n", 0, 5)
    host.local.add("/work/proj")
    records = darwin.discover_xcode(host.probes())
    assert records[0].version == ""
    assert host.runs == [("/usr/bin/xcode-select", "-p")]

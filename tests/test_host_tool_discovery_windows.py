"""Windows discovery (vswhere, SDK, App Paths, py launcher, prefixes) with fakes."""
import json

from sonder_runtime.adapters.host_tools import windows
from sonder_runtime.adapters.host_tools.bounded_process import BoundedRun
from sonder_runtime.adapters.host_tools.discovery import HostToolDiscovery, batch_arguments_safe
from sonder_runtime.domain.host_tools.model import DiscoverySource, VersionStatus
from sonder_runtime.domain.host_tools.registry import HOST_TOOL_SPECS, spec_for
from tests.test_tools_inventory_fakes import FakeHost

PF86 = "C:\\Program Files (x86)"
VSWHERE = PF86 + "\\Microsoft Visual Studio\\Installer\\vswhere.exe"
VS_A = "C:\\Program Files\\Microsoft Visual Studio\\2022\\Community"
VS_B = "C:\\Program Files\\Microsoft Visual Studio\\2022\\BuildTools"
KITS = "C:\\Program Files (x86)\\Windows Kits\\10\\"
SDK_KEY = "SOFTWARE\\Microsoft\\Windows Kits\\Installed Roots"
APP_PATHS = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths"


def _host(**kwargs):
    env = {"ProgramFiles(x86)": PF86, "ProgramFiles": "C:\\Program Files",
           "ProgramData": "C:\\ProgramData", "USERPROFILE": "C:\\Users\\alice",
           "LOCALAPPDATA": "C:\\Users\\alice\\AppData\\Local"}
    env.update(kwargs.pop("env", {}))
    return FakeHost("Windows", path=kwargs.pop("path", "C:\\Windows\\System32"), env=env,
                    home="C:\\Users\\alice", **kwargs)


def _add_vs_install(host, root, toolset):
    host.contents[root + "\\VC\\Auxiliary\\Build\\Microsoft.VCToolsVersion.default.txt"] = (
        toolset + "\r\n").encode()
    bin_dir = root + f"\\VC\\Tools\\MSVC\\{toolset}\\bin\\Hostx64\\x64"
    host.add_exe(bin_dir + "\\cl.exe", "100:1")
    host.add_exe(bin_dir + "\\link.exe", "101:1")
    host.add_exe(root + "\\VC\\Auxiliary\\Build\\vcvars64.bat", "5:5")
    host.add_exe(root + "\\MSBuild\\Current\\Bin\\MSBuild.exe", "102:1")
    host.add_exe(root + "\\Common7\\IDE\\devenv.exe", "103:1")


def _vswhere_host():
    host = _host()
    host.add_exe(VSWHERE, "1:1")
    installs = [
        {"installationPath": VS_A, "displayName": "Visual Studio Community 2022",
         "installationVersion": "17.10.35004.147"},
        {"installationPath": VS_B, "displayName": "Visual Studio Build Tools 2022",
         "installationVersion": "17.9.1"},
    ]
    host.outputs[VSWHERE] = BoundedRun("ok", json.dumps(installs), 0, 50)
    _add_vs_install(host, VS_A, "14.40.33807")
    _add_vs_install(host, VS_B, "14.39.33519")
    return host


def _names(snapshot):
    return {record.name: record for record in snapshot.tools}


def test_vswhere_two_installs_yield_metadata_records_and_never_run_cl():
    host = _vswhere_host()
    notes = []
    records = windows.discover_visual_studio(host.probes(), notes)
    assert {r.name for r in records} == {"cl", "link", "msbuild", "devenv"}
    assert len(records) == 8 and notes == []
    cl = next(r for r in records if r.name == "cl")
    assert cl.version == "14.40.33807" and cl.version_status is VersionStatus.FROM_METADATA
    details = dict(cl.details)
    assert details["toolset"] == "14.40.33807"
    assert details["vs_display_name"] == "Visual Studio Community 2022"
    assert details["vcvars64"].endswith("vcvars64.bat")
    assert host.runs == [(VSWHERE, *windows.VSWHERE_ARGS)]

    snapshot = HostToolDiscovery(host.probes(), specs=HOST_TOOL_SPECS).discover(previous=None, full=False)
    tools = _names(snapshot)
    assert tools["cl"].source is DiscoverySource.VSWHERE
    assert tools["cl"].alternatives and "BuildTools" in tools["cl"].alternatives[0]
    assert all(run[0] == VSWHERE for run in host.runs), host.runs  # cl/link/msbuild/devenv never ran


def test_vswhere_timeout_adds_a_note_not_a_crash():
    host = _host()
    host.add_exe(VSWHERE, "1:1")
    host.outputs[VSWHERE] = BoundedRun("timeout", "", None, 5000)
    snapshot = HostToolDiscovery(host.probes(), specs=HOST_TOOL_SPECS).discover(previous=None, full=False)
    assert "cl" not in _names(snapshot)
    assert "vswhere timeout" in snapshot.notes


def test_windows_sdk_newest_version_wins():
    host = _host()
    host.registry_values[("HKLM", SDK_KEY, "KitsRoot10")] = KITS
    host.registry_keys[("HKLM", SDK_KEY)] = ("10.0.19041.0", "not-a-version")
    host.dirs.add(KITS + "bin\\10.0.22621.0")
    host.dirs.add(KITS + "bin\\10.0.9999.0")
    host.add_exe(KITS + "bin\\10.0.22621.0\\x64\\rc.exe", "7:7")
    host.add_exe(KITS + "Debuggers\\x64\\cdb.exe", "8:8")
    records = {r.name: r for r in windows.discover_windows_sdk(host.probes())}
    assert records["windows-sdk"].version == "10.0.22621.0"
    assert records["windows-sdk"].source is DiscoverySource.WINDOWS_SDK
    assert "10.0.19041.0" in dict(records["windows-sdk"].details)["sdk_versions"]
    assert records["rc"].path.endswith("10.0.22621.0\\x64\\rc.exe")
    assert records["cdb"].version_status is VersionStatus.FROM_METADATA
    assert host.runs == []


def test_app_paths_lookup_hklm_then_hkcu():
    host = _host()
    host.registry_values[("HKLM", APP_PATHS + "\\git.exe", "")] = '"C:\\Tools\\Git\\git.exe"'
    host.registry_values[("HKCU", APP_PATHS + "\\pwsh.exe", "")] = "C:\\Users\\alice\\pwsh\\pwsh.exe"
    host.registry_values[("HKCU", APP_PATHS + "\\evil.exe", "")] = "relative\\evil.exe"
    host.add_exe("C:\\Tools\\Git\\git.exe", "1:1", "git version 2.45.1.windows.1")
    host.add_exe("C:\\Users\\alice\\pwsh\\pwsh.exe", "2:2", "PowerShell 7.4.2")
    found = dict(windows.discover_app_paths(host.probes(), ["git", "pwsh", "evil", "bad name"]))
    assert found == {"git": "C:\\Tools\\Git\\git.exe", "pwsh": "C:\\Users\\alice\\pwsh\\pwsh.exe"}
    snapshot = HostToolDiscovery(host.probes(), specs=(spec_for("git"), spec_for("pwsh"))).discover(
        previous=None, full=False)
    tools = _names(snapshot)
    assert tools["git"].source is DiscoverySource.APP_PATHS and tools["git"].version == "2.45.1"
    assert tools["pwsh"].version == "7.4.2"


def test_py_launcher_both_output_formats():
    new = (" -V:3.12 *        C:\\Python312\\python.exe\n"
           " -V:3.11          C:\\Python311\\python.exe\n"
           " -V:ContinuumAnalytics/Anaconda39-64 C:\\Anaconda\\python.exe\n")
    old = (" -3.10-64 *      C:\\Python310\\python.exe\n"
           " -3.9-32         C:\\Python39-32\\python.exe\n")
    assert windows.parse_py_launcher(new)[:2] == [
        ("3.12", "C:\\Python312\\python.exe", True), ("3.11", "C:\\Python311\\python.exe", False)]
    assert windows.parse_py_launcher(old) == [
        ("3.10-64", "C:\\Python310\\python.exe", True), ("3.9-32", "C:\\Python39-32\\python.exe", False)]
    assert windows.parse_py_launcher("garbage\n -V:3.8 relative\\python.exe") == []


def test_py_launcher_supplies_python_when_path_has_only_the_store_alias():
    alias_dir = "C:\\Users\\alice\\AppData\\Local\\Microsoft\\WindowsApps"
    host = _host(path=f"C:\\Windows;{alias_dir}")
    host.add_exe("C:\\Windows\\py.exe", "1:1", "Python 3.12.3")
    host.outputs[("C:\\Windows\\py.exe", "-0p")] = BoundedRun(
        "ok", " -V:3.12 *        C:\\Python312\\python.exe\n", 0, 5)
    host.add_exe(alias_dir + "\\python.exe", "0:0", "Python 3.99")
    host.add_exe("C:\\Python312\\python.exe", "3:3")
    snapshot = HostToolDiscovery(host.probes(), specs=(spec_for("py"), spec_for("python"))).discover(
        previous=None, full=False)
    tools = _names(snapshot)
    assert tools["python"].path == "C:\\Python312\\python.exe"
    assert tools["python"].source is DiscoverySource.PY_LAUNCHER
    assert tools["python"].version == "3.12" and tools["python"].version_status is VersionStatus.FROM_METADATA
    assert alias_dir + "\\python.exe" in tools["python"].alternatives
    assert ("py:3.12", "C:\\Python312\\python.exe") in tools["py"].details
    assert not any(run[0].startswith(alias_dir) for run in host.runs)


def test_windows_apps_alias_is_recorded_and_never_run():
    alias_dir = "C:\\Users\\alice\\AppData\\Local\\Microsoft\\WindowsApps"
    host = _host(path=alias_dir)
    host.add_exe(alias_dir + "\\winget.exe", "0:0", "v1.8")
    snapshot = HostToolDiscovery(host.probes(), specs=(spec_for("winget"),)).discover(previous=None, full=False)
    assert _names(snapshot)["winget"].version_status is VersionStatus.ALIAS
    assert host.runs == []


def test_scoop_and_choco_dirs_from_env():
    host = _host(env={"SCOOP": "D:\\scoop", "ChocolateyInstall": "D:\\choco"}, path="")
    host.add_exe("D:\\scoop\\shims\\rg.exe", "1:1", "ripgrep 14.1.0")
    host.add_exe("D:\\choco\\bin\\jq.exe", "2:2", "jq-1.7.1")
    dirs = windows.extra_dirs(host.probes())
    assert dirs[0] == "D:\\scoop\\shims" and dirs[1] == "D:\\choco\\bin"
    snapshot = HostToolDiscovery(host.probes(), specs=(spec_for("rg"), spec_for("jq"))).discover(
        previous=None, full=False)
    tools = _names(snapshot)
    assert tools["rg"].source is DiscoverySource.SCOOP and tools["rg"].version == "14.1.0"
    assert tools["jq"].source is DiscoverySource.CHOCO and tools["jq"].version == "1.7.1"
    default = windows.extra_dirs(_host(path="").probes())
    assert "C:\\Users\\alice\\scoop\\shims" in default and "C:\\ProgramData\\chocolatey\\bin" in default


def test_batch_launcher_with_unsafe_arguments_is_refused():
    assert batch_arguments_safe("C:\\tools\\gradle.bat", ("--version",))
    assert not batch_arguments_safe("C:\\tools\\gradle.bat", ("--version&calc",))
    assert not batch_arguments_safe("C:\\tools\\a%PATH%\\gradle.bat", ("--version",))
    assert batch_arguments_safe("C:\\tools\\gradle.exe", ("x&y",))  # not a batch file
    host = _host(path="C:\\tools")
    host.add_exe("C:\\tools\\gradle.bat", "1:1", "Gradle 8.5")
    unsafe = spec_for("gradle").__class__(
        name="gradle", category=spec_for("gradle").category, executables=("gradle",),
        version_args=("--version", "a|b"))
    snapshot = HostToolDiscovery(host.probes(), specs=(unsafe,)).discover(previous=None, full=False)
    assert _names(snapshot)["gradle"].version_status is VersionStatus.NOT_PROBED
    assert host.runs == []
    # Control: the registry's own batch-safe args are probed.
    safe = HostToolDiscovery(host.probes(), specs=(spec_for("gradle"),)).discover(previous=None, full=False)
    assert _names(safe)["gradle"].version == "8.5"
    assert host.runs == [("C:\\tools\\gradle.bat", "--version")]


def test_project_local_vswhere_is_never_run():
    host = _vswhere_host()
    host.local.add(PF86)
    notes = []
    assert windows.discover_visual_studio(host.probes(), notes) == []
    assert host.runs == []
    assert any("project root" in note for note in notes)

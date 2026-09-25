"""Host-owned debug argv templates, lexical symbol paths and the approval digest."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.debugging.plan_digest import debug_command_digest
from sonder_runtime.domain.debugging.symbol_path import (
    MS_SYMBOL_SERVER, SymbolPathRejected, SymbolStoreRejected, build_cdb_symbol_path, lexical_store,
    lexical_symbol_dir,
)
from sonder_runtime.domain.debugging.templates import (
    CDB_SFLAGS, SYMOPT_LOAD_ANYTHING, TemplateError, cdb_template, dump_syms_template, eu_stack_template,
    gdb_template, heaptrack_print_template, lldb_template, materialize, perf_templates, posix_environment,
    stackwalk_template, symbolizer_template, tracy_csvexport_templates, windows_environment, with_netns,
    wpaexporter_template, xperf_template,
)


CDB = "C:\\Program Files (x86)\\Windows Kits\\10\\Debuggers\\x64\\cdb.exe"


# ------------------------------------------------------------------ exact argv snapshots

def test_cdb_argv_snapshot():
    template = cdb_template(CDB)
    assert template.argv == (
        CDB, "-z", "{input}", "-lines", "-noshell", "-sins", "-netsyms", "no", "-sflags", "0x022802B7",
        "-y", "{sympath}", "-i", "{imagepath}", "-c",
        ".echo SONDER_{nonce}_BEGIN;.ecxr;.echo SONDER_{nonce}_STACK;kn 64;.echo SONDER_{nonce}_ANALYZE;"
        "!analyze -v;.echo SONDER_{nonce}_THREADS;~*kn 16;.echo SONDER_{nonce}_MODULES;lm t n;"
        ".echo SONDER_{nonce}_END;q")
    assert cdb_template(CDB, network=True).argv[7] == "yes"


def test_cdb_sflags_never_load_anything_and_no_locals():
    assert CDB_SFLAGS == 0x022802B7 and not CDB_SFLAGS & SYMOPT_LOAD_ANYTHING
    joined = " ".join(cdb_template(CDB).argv)
    assert ".symopt" not in joined and "kP" not in joined and ".load" not in joined


def test_gdb_argv_snapshot():
    template = gdb_template("/usr/bin/gdb")
    assert template.argv == (
        "/usr/bin/gdb", "-nx", "-nh", "-batch", "-q",
        "-iex", "set auto-load off", "-iex", "set debuginfod enabled off",
        "-iex", "set libthread-db-search-path $sdir", "-iex", "set history save off",
        "-iex", "set solib-search-path {solibpath}",
        "-ex", "set pagination off", "-ex", "set width 0", "-ex", "set print elements 64",
        "-ex", "set max-value-size 65536",
        "-ex", "echo \\nSONDER_{nonce}_SIG\\n", "-ex", "p $_siginfo._sifields._sigfault.si_addr",
        "-ex", "echo \\nSONDER_{nonce}_BT\\n", "-ex", "bt 64",
        "-ex", "echo \\nSONDER_{nonce}_THREADS\\n", "-ex", "thread apply all bt 16",
        "-ex", "echo \\nSONDER_{nonce}_LIBS\\n", "-ex", "info sharedlibrary",
        "-ex", "echo \\nSONDER_{nonce}_END\\n", "{exe}", "{input}")
    assert "bt full" not in " ".join(template.argv)
    assert gdb_template("/usr/bin/gdb", network=True).argv[8] == "set debuginfod enabled on"
    assert template.needs_executable


def test_lldb_settings_use_dash_O_before_core():
    argv = lldb_template("/usr/bin/lldb").argv
    assert argv[:3] == ("/usr/bin/lldb", "--batch", "--no-lldbinit")
    core = argv.index("--core")
    o_positions = [i for i, item in enumerate(argv) if item == "-O"]
    assert len(o_positions) == 5 and max(o_positions) < core
    assert argv[o_positions[0] + 1] == "settings set prompt (SONDER_{nonce}) "
    assert "settings clear plugin.symbol-locator.debuginfod.server-urls" in argv
    assert argv[core:] == ("--core", "{input}", "{exe}", "-o", "thread backtrace all -c 32", "-o", "image list")
    assert not any("script" in item and "load-script" not in item for item in argv)


def test_other_crash_engine_snapshots():
    assert eu_stack_template("/usr/bin/eu-stack").argv == (
        "/usr/bin/eu-stack", "--core", "{input}", "-e", "{exe}", "-a", "-b", "-i", "-m", "-s", "-n", "64")
    assert stackwalk_template("/x/minidump-stackwalk").argv == (
        "/x/minidump-stackwalk", "--json", "--symbols-path", "{rundir}/syms", "{input}")
    assert "--symbols-url" not in " ".join(stackwalk_template("/x/msw").argv)
    assert stackwalk_template("/x/minidump_stackwalk", breakpad_cpp=True).argv == (
        "/x/minidump_stackwalk", "-m", "{input}", "{rundir}/syms")
    assert dump_syms_template("/x/dump_syms", "spark_game", "1B4E28BA2FA111D2883FB9A761BDE3FB3").argv == (
        "/x/dump_syms", "{rundir}/sym/spark_game/spark_game.pdb",
        "-o", "{rundir}/syms/spark_game.pdb/1B4E28BA2FA111D2883FB9A761BDE3FB3/spark_game.sym")


def test_llvm_symbolizer_always_no_debuginfod():
    pe = symbolizer_template("/usr/bin/llvm-symbolizer-18", "spark_tiny.exe", [0x1006, 0x1003])
    assert pe.argv == ("/usr/bin/llvm-symbolizer-18", "--obj", "{rundir}/sym/spark_tiny/spark_tiny.exe",
                       "--no-debuginfod", "--inlines", "--demangle", "--relative-address",
                       "--output-style=JSON", "0x1006", "0x1003")
    elf = symbolizer_template("/usr/bin/llvm-symbolizer", "crasher", [0x1266], elf=True)
    assert "--no-debuginfod" in elf.argv and "--debug-file-directory={rundir}/sym" in elf.argv
    with pytest.raises(TemplateError):
        symbolizer_template("/usr/bin/llvm-symbolizer", "a.exe", range(65))
    with pytest.raises(TemplateError):
        symbolizer_template("/usr/bin/llvm-symbolizer", "../a.exe", [1])


def test_profile_engine_snapshots():
    folded, flat = perf_templates("/usr/bin/perf")
    assert folded.argv[1:] == ("report", "-i", "{input}", "--stdio", "--no-children", "--percent-limit", "0.5",
                               "--max-stack", "32", "-g", "folded,0.5,caller,function,percent", "--sort", "dso,sym")
    assert flat.argv[1:] == ("report", "-i", "{input}", "--stdio", "--children", "-g", "none",
                             "--percent-limit", "0.3", "--sort", "dso,sym")
    assert dict(folded.env) == {"PERF_CONFIG": "/dev/null", "PERF_BUILDID_DIR": "{rundir}/buildid"}
    assert not any(word in folded.argv for word in ("script", "-s", "--tui", "--gtk"))
    assert heaptrack_print_template("/usr/bin/heaptrack_print").argv[1:] == (
        "{input}", "--print-peaks", "1", "--print-allocators", "1", "--print-leaks", "1",
        "--print-temporary", "1", "--peak-limit", "20")
    assert len(tracy_csvexport_templates("/x/tracy-csvexport")) == 1
    assert tracy_csvexport_templates("/x/t", frame_zone="Frame")[1].argv == ("/x/t", "-u", "{input}")
    assert xperf_template("C:\\wpt\\xperf.exe").argv[1:] == (
        "-i", "{input}", "-o", "{rundir}\\out\\cpu.txt", "-symbols", "-a", "profile", "-detail")
    assert wpaexporter_template("C:\\wpt\\wpaexporter.exe", "cpu_sampled.wpaProfile",
                                "C:\\sonder\\profiles\\cpu_sampled.wpaProfile").argv[3:5] == (
        "-profile", "C:\\sonder\\profiles\\cpu_sampled.wpaProfile")


def test_environment_builders():
    env = dict(posix_environment(network=False))
    assert env == {"PATH": "/usr/bin:/bin", "HOME": "{rundir}/home", "LANG": "C.UTF-8",
                   "TMPDIR": "{rundir}/tmp", "DEBUGINFOD_URLS": "", "PERF_CONFIG": "/dev/null"}
    assert dict(posix_environment(network=True, debuginfod_url="https://debuginfod.ubuntu.com"))[
        "DEBUGINFOD_URLS"] == "https://debuginfod.ubuntu.com"
    win = dict(windows_environment("C:\\Windows", "C:\\Kits\\Debuggers\\x64"))
    assert win["USERPROFILE"] == win["LOCALAPPDATA"] == win["APPDATA"] == "{rundir}\\home"
    assert win["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert not any(key.startswith("_NT_") for key in win)
    for forbidden in ("DBGHELP_LOG", "INIT", "_NT_ALT_SYMBOL_PATH", "_NT_DEBUGGER_EXTENSION_PATH"):
        assert forbidden not in win
    xperf_env = dict(windows_environment("C:\\Windows", "C:\\wpt", symbol_path="cache*x", with_symcache=True))
    assert xperf_env["_NT_SYMCACHE_PATH"] == "{rundir}\\symcache"


def test_materialize_and_netns():
    template = with_netns(gdb_template("/usr/bin/gdb"), "/usr/bin/unshare")
    assert template.argv[:5] == ("/usr/bin/unshare", "--user", "--map-current-user", "--net", "--")
    argv, env = materialize(template, {"nonce": "a" * 16, "rundir": "/r", "input": "/r/in/core",
                                       "exe": "/r/in/exe", "solibpath": ""})
    assert "echo \\nSONDER_aaaaaaaaaaaaaaaa_BT\\n" in argv and argv[-2:] == ("/r/in/exe", "/r/in/core")
    assert "set solib-search-path " in argv
    with pytest.raises(TemplateError):
        materialize(template, {"nonce": "a" * 16, "rundir": "/r", "input": "/x"})  # {exe} unbound
    with pytest.raises(TemplateError):
        materialize(template, {"nonce": "XYZ", "rundir": "/r", "input": "/x", "exe": "/e", "solibpath": ""})
    with pytest.raises(TemplateError):
        materialize(template, {"nonce": "a" * 16, "rundir": "/r\n", "input": "/x", "exe": "/e", "solibpath": ""})
    with pytest.raises(TemplateError):
        materialize(template, {"nonce": "a" * 16, "rundir": "{input}", "input": "/x", "exe": "/e",
                               "solibpath": ""})
    with pytest.raises(TemplateError):
        materialize(template, {"shell": "x"})
    env_template = perf_templates("/usr/bin/perf")[0]
    _, env = materialize(env_template, {"rundir": "/r", "input": "/r/in/perf.data"})
    assert dict(env)["PERF_BUILDID_DIR"] == "/r/buildid"


# ------------------------------------------------------------------ digest

def _digest(**overrides):
    params = dict(template_argvs=[gdb_template("/usr/bin/gdb").argv], tool_identities=["gdb:/usr/bin/gdb:15.1"],
                  input_sha256="a" * 64, engines=["pure", "gdb"], network=False, store_ids=[])
    params.update(overrides)
    return debug_command_digest(**params)


def test_digest_stable_across_nonces_and_rundirs_and_sensitive_to_inputs():
    first = _digest()
    assert first == _digest() and len(first) == 64
    # Nonce and rundir are bound later; they are not part of the digest at all.
    for nonce, rundir in (("1" * 16, "/r1"), ("2" * 16, "/r2")):
        materialize(gdb_template("/usr/bin/gdb"), {"nonce": nonce, "rundir": rundir, "input": "/i",
                                                  "exe": "/e", "solibpath": ""})
        assert _digest() == first
    assert _digest(input_sha256="b" * 64) != first
    assert _digest(engines=["pure", "lldb"]) != first
    assert _digest(network=True) != first
    assert _digest(store_ids=["\\\\buildserver\\symbols"]) != first
    assert _digest(template_argvs=[gdb_template("/usr/bin/gdb", network=True).argv]) != first


# ------------------------------------------------------------------ lexical symbol paths

@pytest.mark.parametrize("value,system", [
    ("C:\\syms*x", "Windows"), ("C:\\a;C:\\b", "Windows"), ("srv*C:\\cache*https://x", "Windows"),
    ("symsrv*x", "Windows"), ("cache*C:\\c", "Windows"), ("https://msdl.microsoft.com", "Windows"),
    ("https://host/x", "Linux"), ("file:///etc", "Linux"), ("\\\\server\\share\\syms", "Windows"),
    ("\\\\?\\C:\\syms", "Windows"), ("\\\\.\\PhysicalDrive0", "Windows"), ("//server/share", "Linux"),
    ("C:\\syms\\..\\..\\Windows", "Windows"), ("/usr/lib/../../etc", "Linux"), ("relative\\dir", "Windows"),
    ("relative/dir", "Linux"), ("C:relative", "Windows"), ("\\rooted-no-drive", "Windows"),
    ("C:\\syms\"x", "Windows"), ("/tmp/a'b", "Linux"), ("/tmp/a\nb", "Linux"), ("C:\\x:stream", "Windows"),
    ("C:\\syms", "Linux"), ("", "Linux"), (" /tmp/a", "Linux"),
])
def test_lexical_symbol_dir_rejections(value, system):
    with pytest.raises(SymbolPathRejected):
        lexical_symbol_dir(value, system=system)


def test_lexical_symbol_dir_accepts_plain_absolute_dirs():
    assert lexical_symbol_dir("C:\\src\\out\\build\\x64-RelWithDebInfo", system="Windows") == \
        "C:\\src\\out\\build\\x64-RelWithDebInfo"
    assert lexical_symbol_dir("D:/syms/engine", system="Windows") == "D:\\syms\\engine"
    assert lexical_symbol_dir("/home/dev/build/bin", system="Linux") == "/home/dev/build/bin"
    assert SymbolPathRejected.code == "SYMBOL_PATH_REJECTED"


def test_lexical_store_and_cdb_symbol_path():
    assert lexical_store("https://symbols.example.test/game", system="Windows") == \
        "https://symbols.example.test/game"
    assert lexical_store("\\\\buildserver\\symbols", system="Windows") == "\\\\buildserver\\symbols"
    for bad in ("http://x/y", "https://x:8443/y", "srv*c*https://x", "\\\\?\\C:\\x", "C:\\syms",
                "https://x/../y", "\\\\host", "https://x/a;b"):
        with pytest.raises(SymbolStoreRejected):
            lexical_store(bad, system="Windows")
    offline = build_cdb_symbol_path("C:\\run\\symcache", ["C:\\build\\out"], ["\\\\buildserver\\symbols"],
                                    network=False)
    assert offline == "cache*C:\\run\\symcache;C:\\build\\out"
    online = build_cdb_symbol_path("C:\\state\\symcache", ["C:\\build\\out"], ["\\\\buildserver\\symbols"],
                                   network=True)
    assert online == ("cache*C:\\state\\symcache;C:\\build\\out;srv*C:\\state\\symcache*%s;"
                      "srv*C:\\state\\symcache*\\\\buildserver\\symbols" % MS_SYMBOL_SERVER)
    with pytest.raises(SymbolPathRejected):
        build_cdb_symbol_path("C:\\c", ["C:\\d%d" % i for i in range(9)], network=False)
    with pytest.raises(SymbolStoreRejected):
        build_cdb_symbol_path("C:\\c", [], ["\\\\s\\a"] * 5, network=True)

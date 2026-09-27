"""Cause hints, crash signatures and report finalisation.

Hints are heuristics with an explicit confidence; they never claim a root
cause. ``crash_signature`` is a stable 16-hex bucket key over the exception
name and a *basis*:

- ``functions``: the top three in-project function names with template and
  argument lists stripped (when the crashing thread is symbolicated);
- ``module_offsets``: the top three in-project ``module!+0xoffset@debug_id``;
- ``exception_only``: the exception name alone.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import replace

from .exceptions import GPU_DEVICE_LOST_CODES
from .model import CauseHint, CrashReport, ModuleInfo, StackFrame, ThreadSummary


NULL_PAGE_LIMIT = 0x10000
_FILL_PATTERNS = {
    0xDDDDDDDD: "use_after_free",       # MSVC debug heap: freed
    0xFEEEFEEE: "use_after_free",       # HeapFree fill
    0xCDCDCDCD: "uninitialized_memory",  # MSVC debug heap: fresh allocation
    0xCCCCCCCC: "uninitialized_memory",  # MSVC /RTC: uninitialised stack
    0xBAADF00D: "uninitialized_memory",  # LocalAlloc(LMEM_FIXED)
}
_SYSTEM_MODULE_RE = re.compile(
    r"^(?:ntdll|kernel32|kernelbase|ucrtbased?|vcruntime\d*d?|msvcp\d*d?|msvcrt|user32|gdi32"
    r"|win32u|combase|rpcrt4|sechost|advapi32|ole32|oleaut32|shell32|ws2_32|bcrypt"
    r"|d3d\d+(?:core)?|dxgi|dxcore|d3dcompiler_\d+|nv\w*|amd\w*|ati\w*|ig[dcx]\w*|opengl32"
    r"|vulkan-1|xinput\w*|xaudio\w*|dbghelp|dbgcore|clr|coreclr|mscorwks|clrjit"
    r"|libc|libm|libdl|librt|libpthread|libstdc\+\+|libc\+\+(?:abi)?|libgcc_s|libunwind"
    r"|ld-linux[\w.-]*|ld64|linux-vdso|linux-gate|libasan|libubsan|libtsan|liblsan|libhwasan"
    r"|libclang_rt[\w.-]*|libsystem_\w+|libdyld|dyld|libobjc|libvulkan|libgl\w*|libegl\w*"
    r"|libx11|libxcb\w*|libwayland\w*|libz|libcrypto|libssl|libsdl2[\w-]*|valgrind\w*"
    r"|vgpreload\w*)(?:[.-].*)?$", re.IGNORECASE)
_SYSTEM_PATH_RE = re.compile(
    r"^(?:/usr/|/lib|/lib64/|/system/|/System/|/opt/homebrew/|[A-Za-z]:[\\/]windows[\\/]"
    r"|\\\\\?\\[A-Za-z]:[\\/]windows[\\/])", re.IGNORECASE)
_GPU_MODULE_RE = re.compile(r"^(?:nvwgf2um\w*|nvoglv\w*|nvd3dum\w*|nvlddmkm|amdxc(?:32|64)?"
                            r"|atidxx\w*|atiumd\w*|amdvlk\w*|igd\w*|igc\w*|igxelp\w*"
                            r"|d3d12core|d3d12|dxgi)(?:\..*)?$", re.IGNORECASE)
_WAIT_FUNCTION_RE = re.compile(
    r"(?:NtWaitFor\w+|ZwWaitFor\w+|WaitForSingleObject\w*|WaitForMultipleObjects\w*"
    r"|SleepConditionVariable\w+|RtlSleepConditionVariable\w+|NtDelayExecution|RtlpWaitOn\w+"
    r"|AcquireSRWLock\w+|EnterCriticalSection|RtlEnterCriticalSection|NtAlertThreadByThreadId\w*"
    r"|futex_wait\w*|__futex_abstimed_wait\w*|pthread_cond_(?:timed)?wait\w*|__lll_lock_wait\w*"
    r"|pthread_mutex_lock|___pthread_mutex_lock|pthread_join|__GI___poll|epoll_wait|nanosleep"
    r"|clock_nanosleep\w*|__psynch_cvwait|__psynch_mutexwait|semaphore_wait_trap|mach_msg_trap)")
# C runtime entry and thread-start glue: identical across every crash, and
# some debuggers stop the backtrace before it, so it never feeds a signature.
_STARTUP_RE = re.compile(
    r"^(?:_start|__libc_start_main\w*|__libc_start_call_main|mainCRTStartup|wmainCRTStartup"
    r"|WinMainCRTStartup|wWinMainCRTStartup|__scrt_common_main\w*|invoke_main|BaseThreadInitThunk"
    r"|RtlUserThreadStart|start_thread|clone3?|thread_start|_pthread_start)$")
_PURE_VIRTUAL_RE = re.compile(r"(?:_purecall|__cxa_pure_virtual|__cxa_deleted_virtual)")
_ASSERT_RE = re.compile(r"(?:_wassert|__assert_fail|__assert_rtn|_assert|abort_message|__GI_abort)")
_SANITIZER_KINDS = {
    "heap-buffer-overflow": "heap_buffer_overflow",
    "stack-buffer-overflow": "stack_buffer_overflow",
    "stack-buffer-underflow": "stack_buffer_overflow",
    "global-buffer-overflow": "global_buffer_overflow",
    "heap-use-after-free": "use_after_free",
    "use-after-free": "use_after_free",
    "stack-use-after-return": "stack_use_after_return",
    "stack-use-after-scope": "stack_use_after_scope",
    "double-free": "double_free",
    "attempting double-free": "double_free",
    "bad-free": "invalid_free",
    "attempting free on address which was not malloc()-ed": "invalid_free",
    "alloc-dealloc-mismatch": "alloc_dealloc_mismatch",
    "new-delete-type-mismatch": "alloc_dealloc_mismatch",
    "memory-leak": "memory_leak",
    "detected memory leaks": "memory_leak",
    "data-race": "data_race",
    "data race": "data_race",
    "lock-order-inversion": "deadlock",
    "thread-leak": "thread_leak",
    "heap-use-after-free-tag": "use_after_free",
    "tag-mismatch": "memory_tag_mismatch",
    "undefined-behavior": "undefined_behavior",
    "stack-overflow": "stack_overflow",
    "InvalidRead": "invalid_read",
    "InvalidWrite": "invalid_write",
    "InvalidFree": "invalid_free",
    "MismatchedFree": "alloc_dealloc_mismatch",
    "UninitCondition": "uninitialized_memory",
    "UninitValue": "uninitialized_memory",
    "Leak_DefinitelyLost": "memory_leak",
    "Leak_IndirectlyLost": "memory_leak",
    "Leak_PossiblyLost": "memory_leak",
}


def is_system_module(name: str, path: str = "") -> bool:
    """Heuristic: an OS, runtime, driver or sanitizer-runtime module."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if base and _SYSTEM_MODULE_RE.match(base):
        return True
    return bool(path and _SYSTEM_PATH_RE.match(str(path)))


_LIBC_RELATIVE_RE = re.compile(
    r"^(?:\.\./)+(?:csu|sysdeps|nptl|stdlib|string|elf|posix|misc|malloc|signal|libio|debug|assert"
    r"|src/libsanitizer|libsanitizer)/")
_CRT_SOURCE_RE = re.compile(r"[\\/](?:vctools[\\/]crt|minkernel[\\/]crts|onecore[\\/])", re.IGNORECASE)


def is_system_file(path: str) -> bool:
    """Heuristic: a libc/CRT/sanitizer-runtime or system-header source path."""
    text = str(path or "")
    return bool(_SYSTEM_PATH_RE.match(text) or _LIBC_RELATIVE_RE.match(text) or _CRT_SOURCE_RE.search(text)) \
        or "/sysdeps/" in text or "compiler-rt/" in text or "/libsanitizer/" in text


def strip_function_name(name: str) -> str:
    """``ns::Foo<int>::bar(int) const+0x12`` -> ``ns::Foo::bar``."""
    text = str(name or "").strip()
    text = re.sub(r"\s*\+\s*(?:0x[0-9a-fA-F]+|\d+)$", "", text)
    for op, token in (("operator<<=", "\x01a"), ("operator>>=", "\x01b"), ("operator<<", "\x01c"),
                      ("operator>>", "\x01d"), ("operator<=>", "\x01e"), ("operator<=", "\x01f"),
                      ("operator>=", "\x01g"), ("operator<", "\x01h"), ("operator>", "\x01i"),
                      ("operator()", "\x01j"), ("operator->", "\x01k")):
        text = text.replace(op, token)
    out = []
    depth_angle = 0
    depth_paren = 0
    for ch in text[:2048]:
        if ch == "<":
            depth_angle += 1
            continue
        if ch == ">" and depth_angle:
            depth_angle -= 1
            continue
        if ch == "(" and not depth_angle:
            # Keep "(anonymous namespace)" style scopes, drop argument lists.
            depth_paren += 1
            out.append("\x02")
            continue
        if ch == ")" and depth_paren and not depth_angle:
            depth_paren -= 1
            out.append("\x03")
            continue
        if depth_angle:
            continue
        out.append(ch)
    text = "".join(out)
    text = re.sub(r"\x02anonymous namespace\x03", "(anonymous namespace)", text)
    text = re.sub(r"\x02[^\x02\x03]*\x03", "", text)
    text = re.sub(r"[\x02\x03]", "", text)
    text = re.sub(r"\s+(?:const|volatile|&&|&|noexcept)\b.*$", "", text).strip()
    for op, token in (("operator<<=", "\x01a"), ("operator>>=", "\x01b"), ("operator<<", "\x01c"),
                      ("operator>>", "\x01d"), ("operator<=>", "\x01e"), ("operator<=", "\x01f"),
                      ("operator>=", "\x01g"), ("operator<", "\x01h"), ("operator>", "\x01i"),
                      ("operator()", "\x01j"), ("operator->", "\x01k")):
        text = text.replace(token, op)
    return text[:240]


def _crashing_frames(report: CrashReport) -> tuple[StackFrame, ...]:
    thread = report.crashing_thread()
    return thread.frames if thread is not None else ()


def _in_project_frames(report: CrashReport) -> list[StackFrame]:
    return [frame for frame in _crashing_frames(report)
            if frame.in_project and not _STARTUP_RE.match(strip_function_name(frame.function))]


def crash_signature(report: CrashReport) -> tuple[str, str]:
    """``(signature, basis)``; see the module docstring for the basis rules."""
    name = report.exception.name if report.exception is not None else "NO_EXCEPTION"
    project = _in_project_frames(report)
    parts: list[str] = []
    basis = "exception_only"
    if project and any(frame.function for frame in project):
        basis = "functions"
        for frame in project:
            if len(parts) == 3:
                break
            if frame.function:
                parts.append(strip_function_name(frame.function))
    elif project:
        debug_ids = {module.name.lower(): module.debug_id for module in report.modules}
        for frame in project:
            if len(parts) == 3:
                break
            if frame.module and frame.module_offset is not None:
                parts.append("%s!+0x%x@%s" % (frame.module.lower(), frame.module_offset,
                                              debug_ids.get(frame.module.lower(), "")))
        if parts:
            basis = "module_offsets"
    material = "%s|%s|%s" % (name, basis, "|".join(parts))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16], basis


def symbolication_level(report: CrashReport) -> str:
    frames = _in_project_frames(report) or list(_crashing_frames(report))
    frames = frames[:16]
    if not frames:
        return "none"
    named = sum(1 for frame in frames if frame.function)
    if named == len(frames):
        return "full"
    return "partial" if named else "none"


def _near(value: int, pattern: int) -> bool:
    for candidate in (pattern, pattern | (pattern << 32)):
        if abs(value - candidate) < 0x10000:
            return True
    return False


def derive_hints(report: CrashReport) -> tuple[CauseHint, ...]:
    hints: list[CauseHint] = []
    seen: set[str] = set()

    def add(kind: str, confidence: str, evidence: str) -> None:
        if kind not in seen:
            seen.add(kind)
            hints.append(CauseHint(kind, confidence, evidence))

    exc = report.exception
    frames = _crashing_frames(report)
    functions = [frame.function for frame in frames[:12] if frame.function]
    name = exc.name if exc is not None else ""
    code = exc.code.upper() if exc is not None else ""
    access_addr = exc.access_address if exc is not None else None

    if exc is not None:
        if name in _SANITIZER_KINDS or exc.detail in _SANITIZER_KINDS:
            add(_SANITIZER_KINDS.get(name) or _SANITIZER_KINDS[exc.detail], "high",
                "sanitizer reported %s" % name)
        faulting = name in ("EXCEPTION_ACCESS_VIOLATION", "EXCEPTION_IN_PAGE_ERROR") or \
            exc.signal in ("SIGSEGV", "SIGBUS") or name in ("SEGV", "EXC_BAD_ACCESS", "invalid_read",
                                                          "InvalidRead", "InvalidWrite")
        if faulting and access_addr is not None:
            if exc.access == "execute":
                if access_addr < NULL_PAGE_LIMIT:
                    add("null_function_pointer", "high", "execute at 0x%x" % access_addr)
                else:
                    add("execute_violation", "medium", "execute at 0x%x" % access_addr)
            elif access_addr < NULL_PAGE_LIMIT:
                add("null_deref", "high", "%s at 0x%x" % (exc.access or "access", access_addr))
            else:
                for pattern, kind in _FILL_PATTERNS.items():
                    if _near(access_addr, pattern):
                        add(kind, "medium", "address 0x%x near fill pattern 0x%08X" % (access_addr, pattern))
                        break
                if 0x0000800000000000 <= access_addr < 0xFFFF800000000000:
                    add("wild_pointer", "medium", "non-canonical address 0x%x" % access_addr)
        if "free'd" in exc.detail or "freed block" in exc.detail:
            add("use_after_free", "high", "access inside a freed block")
        if name == "EXCEPTION_STACK_OVERFLOW" or name == "stack-overflow":
            add("stack_overflow", "high", name)
        if name == "STATUS_STACK_BUFFER_OVERRUN" or name.startswith("FAST_FAIL"):
            detail = exc.detail
            if "STACK_COOKIE" in detail or "GS_VIOLATION" in detail:
                add("gs_cookie_overrun", "high", detail)
            elif "GUARD_ICALL" in detail or "GUARD_JUMPTABLE" in detail:
                add("cfg_violation", "high", detail)
            elif "FATAL_APP_EXIT" in detail:
                add("abort", "high", detail)
            elif "INVALID_ARG" in detail:
                add("invalid_crt_parameter", "medium", detail)
            elif "CORRUPT_LIST_ENTRY" in detail or "HEAP_METADATA" in detail:
                add("heap_corruption", "high", detail)
            else:
                add("fast_fail", "medium", detail or name)
        if name == "STATUS_HEAP_CORRUPTION":
            add("heap_corruption", "high", name)
        if name == "CPP_EH_EXCEPTION":
            add("unhandled_cpp_exception", "high", exc.detail or name)
        if name == "CLR_EXCEPTION":
            add("unhandled_managed_exception", "medium", name)
        if name in ("EXCEPTION_INT_DIVIDE_BY_ZERO", "EXCEPTION_FLT_DIVIDE_BY_ZERO") or \
                (exc.signal == "SIGFPE" and "DIV" in exc.detail):
            add("divide_by_zero", "high", name or exc.detail)
        if name in ("EXCEPTION_ILLEGAL_INSTRUCTION", "EXCEPTION_PRIV_INSTRUCTION") or exc.signal == "SIGILL":
            add("illegal_instruction", "medium", name or exc.signal)
        if exc.signal == "SIGABRT" or name in ("STATUS_FATAL_APP_EXIT", "STATUS_ASSERTION_FAILURE"):
            add("abort", "high", exc.signal or name)
        if name == "STATUS_NO_MEMORY":
            add("out_of_memory", "medium", name)
        try:
            numeric = int(code, 16) if code.startswith("0X") else None
        except ValueError:
            numeric = None
        gpu_code = numeric in GPU_DEVICE_LOST_CODES or name.startswith("DXGI_ERROR")
        gpu_frames = [frame.module for frame in frames[:5] if frame.module and _GPU_MODULE_RE.match(frame.module)]
        if gpu_frames and gpu_code:
            add("gpu_driver", "medium", "%s in %s" % (name, gpu_frames[0]))
        elif gpu_frames:
            add("gpu_driver", "low", "top frames in %s" % gpu_frames[0])
        elif gpu_code:
            add("gpu_driver", "low", name)

    if any(_PURE_VIRTUAL_RE.search(function) for function in functions):
        add("pure_virtual_call", "high", "pure virtual call handler on the stack")
    if any(_ASSERT_RE.search(function) for function in functions[:6]):
        add("assertion", "medium", "assert/abort handler on the stack")

    waits = [thread for thread in report.threads if thread.frames and any(
        _WAIT_FUNCTION_RE.search(frame.function or "") for frame in thread.frames[:4])]
    if exc is None:
        if report.source_kind in ("windows_minidump", "breakpad_minidump", "crashpad_minidump",
                                  "elf_core", "apple_ips"):
            if waits:
                add("hang_or_deadlock", "medium", "no exception; %d thread(s) waiting" % len(waits))
            else:
                add("hang_or_deadlock", "low", "capture has no exception record")
    return tuple(hints)


def finalize_report(report: CrashReport) -> CrashReport:
    """Recompute symbolication, hints and signature for ``report``."""
    report = replace(report, symbolication=symbolication_level(report))
    report = replace(report, hints=derive_hints(report))
    signature, basis = crash_signature(report)
    return replace(report, signature=signature, signature_basis=basis)


def order_modules(modules: list[ModuleInfo], crashing: ModuleInfo | None, limit: int) -> tuple[ModuleInfo, ...]:
    """Crashing module first, then project modules, then the rest; capped."""
    ordered: list[ModuleInfo] = []
    if crashing is not None:
        ordered.append(crashing)
    ordered += [module for module in modules if module.in_project and module is not crashing]
    ordered += [module for module in modules if not module.in_project and module is not crashing]
    return tuple(ordered[:limit])


def cap_threads(threads: list[ThreadSummary], crashing_id: int | None, *,
                max_crashing_frames: int, max_others: int, max_other_frames: int) -> tuple[ThreadSummary, ...]:
    """Crashing thread first (frame-capped), then at most ``max_others`` others."""
    crashing_index = next((i for i, t in enumerate(threads) if t.crashed), None)
    if crashing_index is None and crashing_id is not None:
        crashing_index = next((i for i, t in enumerate(threads) if t.thread_id == crashing_id), None)
    out: list[ThreadSummary] = []
    if crashing_index is not None:
        thread = threads[crashing_index]
        frames = thread.frames[:max_crashing_frames]
        out.append(replace(thread, crashed=True, frames=frames,
                           frames_truncated=thread.frames_truncated or len(thread.frames) > len(frames)))
    others = [t for i, t in enumerate(threads) if i != crashing_index]
    for thread in others[:max_others]:
        frames = thread.frames[:max_other_frames]
        out.append(replace(thread, crashed=False, frames=frames,
                           frames_truncated=thread.frames_truncated or len(thread.frames) > len(frames)))
    return tuple(out)


__all__ = [
    "cap_threads", "crash_signature", "derive_hints", "finalize_report", "is_system_file",
    "is_system_module", "order_modules", "strip_function_name", "symbolication_level",
]

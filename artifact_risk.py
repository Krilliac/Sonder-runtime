"""Bounded static risk inspection for documents, executables, and scripts."""
from __future__ import annotations

import hashlib
import math
import os
import re
import struct
import time
from pathlib import Path

import pdf_risk


DEFAULT_MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_SECONDS = 5.0
MAX_SECONDS = 15.0
_POLICY_ORDER = {"off": 0, "report": 1, "deny-high": 2, "deny-medium": 3, "deny-unknown": 4}

_PATTERNS = (
    ("encoded_powershell", rb"(?i)powershell(?:\.exe)?[^\r\n\x00]{0,160}(?:-enc|-encodedcommand|frombase64string)", "high"),
    ("download_and_execute", rb"(?i)(?:curl|wget|certutil|bitsadmin)[^\r\n\x00]{0,200}(?:\||&&|start|exec|sh\b|bash\b|powershell|\.exe)", "high"),
    ("process_injection_api", rb"(?i)(?:WriteProcessMemory|CreateRemoteThread|NtCreateThreadEx|QueueUserAPC)", "high"),
    ("executable_memory_api", rb"(?i)(?:VirtualAllocEx?|VirtualProtectEx?|NtAllocateVirtualMemory)", "medium"),
    ("credential_target", rb"(?i)(?:\.ssh[/\\]|\.aws[/\\]credentials|\.azure[/\\]|Login Data|Local State)", "medium"),
    ("persistence_target", rb"(?i)(?:CurrentVersion[/\\]Run|schtasks(?:\.exe)?|/etc/cron|\.config/autostart|systemd/user)", "medium"),
    ("shell_execution_api", rb"(?i)(?:WinExec|ShellExecute[AW]?|CreateProcess[AW]?|/bin/(?:ba)?sh)", "medium"),
)
_URL_RE = re.compile(rb"(?i)https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{3,512}")
_IP_RE = re.compile(rb"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")


class ArtifactRiskError(ValueError):
    """Raised when an artifact cannot be inspected safely."""


class ArtifactRiskDenied(PermissionError):
    """Execution-policy denial carrying the content-free inspection report."""

    def __init__(self, result):
        self.result = dict(result)
        super().__init__(
            "execution denied by artifact risk policy: %s (%s)"
            % (self.result.get("policy"), self.result.get("risk"))
        )


def _clamp_int(value, low, high):
    if type(value) is not int:
        raise ArtifactRiskError("numeric limits must be exact JSON integers")
    return max(low, min(value, high))


def _clamp_seconds(value):
    if type(value) not in (int, float):
        raise ArtifactRiskError("max_seconds must be an exact JSON number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ArtifactRiskError("max_seconds must be finite and positive")
    return min(number, MAX_SECONDS)


def _check(deadline):
    if time.monotonic() > deadline:
        raise TimeoutError("artifact inspection exceeded its deadline")


def _entropy(data):
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    total = len(data)
    return -sum((count / total) * math.log2(count / total) for count in counts if count)


def _add_indicator(target, name, severity, count=1, evidence="static"):
    row = target.setdefault(name, {"indicator": name, "severity": severity, "count": 0, "evidence": []})
    row["count"] += count
    if evidence not in row["evidence"] and len(row["evidence"]) < 8:
        row["evidence"].append(evidence)


def _scan_patterns(data, indicators):
    for name, pattern, severity in _PATTERNS:
        count = len(re.findall(pattern, data))
        if count:
            _add_indicator(indicators, name, severity, count)
    urls = len(_URL_RE.findall(data))
    ips = len(_IP_RE.findall(data))
    if urls:
        _add_indicator(indicators, "embedded_url", "low", urls)
    if ips:
        _add_indicator(indicators, "embedded_ipv4", "low", ips)


def _rva_to_offset(rva, sections):
    for section in sections:
        span = max(section["virtual_size"], section["raw_size"])
        if section["virtual_address"] <= rva < section["virtual_address"] + span:
            return section["raw_offset"] + (rva - section["virtual_address"])
    return None


def _c_string(data, offset, limit=512):
    if offset is None or offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\x00", offset, min(len(data), offset + limit))
    if end < 0:
        return ""
    return data[offset:end].decode("ascii", errors="replace")


def _parse_pe(data, source_size, indicators, deadline):
    if len(data) < 0x40:
        raise ArtifactRiskError("truncated DOS header")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ArtifactRiskError("invalid or truncated PE header")
    machine, section_count, timestamp, _, _, optional_size, characteristics = struct.unpack_from(
        "<HHIIIHH", data, pe_offset + 4,
    )
    if not 0 < section_count <= 96 or optional_size < 64:
        raise ArtifactRiskError("invalid PE section or optional-header count")
    optional = pe_offset + 24
    if optional + optional_size > len(data):
        raise ArtifactRiskError("truncated PE optional header")
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x10B:
        pointer_size, data_directory = 4, optional + 96
    elif magic == 0x20B:
        pointer_size, data_directory = 8, optional + 112
    else:
        raise ArtifactRiskError("unsupported PE optional-header magic")
    entry_rva = struct.unpack_from("<I", data, optional + 16)[0]
    sections = []
    table = optional + optional_size
    if table + section_count * 40 > len(data):
        raise ArtifactRiskError("truncated PE section table")
    max_raw_end = 0
    for index in range(section_count):
        _check(deadline)
        off = table + index * 40
        name = data[off:off + 8].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", data, off + 8)
        flags = struct.unpack_from("<I", data, off + 36)[0]
        sample = data[raw_offset:min(len(data), raw_offset + raw_size, raw_offset + 1024 * 1024)]
        entropy = round(_entropy(sample), 3)
        executable = bool(flags & 0x20000000)
        writable = bool(flags & 0x80000000)
        if executable and writable:
            _add_indicator(indicators, "writable_executable_section", "high", evidence=name or str(index))
        if executable and len(sample) >= 4096 and entropy >= 7.5:
            _add_indicator(indicators, "high_entropy_executable_section", "medium", evidence=name or str(index))
        sections.append({
            "name": name, "virtual_size": virtual_size, "virtual_address": virtual_address,
            "raw_size": raw_size, "raw_offset": raw_offset, "characteristics": flags,
            "entropy": entropy,
        })
        max_raw_end = max(max_raw_end, raw_offset + raw_size)

    imports = set()
    if data_directory + 16 <= optional + optional_size:
        import_rva, _ = struct.unpack_from("<II", data, data_directory + 8)
        descriptor = _rva_to_offset(import_rva, sections) if import_rva else None
        for _index in range(256):
            if descriptor is None or descriptor + 20 > len(data):
                break
            original, _, _, name_rva, first_thunk = struct.unpack_from("<IIIII", data, descriptor)
            if not any((original, name_rva, first_thunk)):
                break
            dll = _c_string(data, _rva_to_offset(name_rva, sections)).lower()
            if dll:
                imports.add("dll:" + dll)
            thunk = _rva_to_offset(original or first_thunk, sections)
            step = pointer_size
            ordinal_mask = 1 << (pointer_size * 8 - 1)
            for _item in range(512):
                if thunk is None or thunk + step > len(data):
                    break
                value = int.from_bytes(data[thunk:thunk + step], "little")
                if not value:
                    break
                if not value & ordinal_mask:
                    name_offset = _rva_to_offset(value, sections)
                    name = _c_string(data, None if name_offset is None else name_offset + 2)
                    if name:
                        imports.add(name)
                thunk += step
            descriptor += 20
    joined = "\n".join(sorted(imports)).encode("ascii", errors="ignore")
    _scan_patterns(joined, indicators)
    injection = {"WriteProcessMemory", "CreateRemoteThread"} <= imports and any(
        name in imports for name in ("VirtualAlloc", "VirtualAllocEx", "NtAllocateVirtualMemory")
    )
    if injection:
        _add_indicator(indicators, "process_injection_import_chain", "high")

    cert_present = False
    if data_directory + 8 * 5 <= optional + optional_size:
        cert_offset, cert_size = struct.unpack_from("<II", data, data_directory + 8 * 4)
        cert_present = bool(cert_offset and cert_size and cert_offset + cert_size <= source_size)
    overlay = max(0, source_size - max_raw_end) if max_raw_end else 0
    if overlay > 1024 * 1024:
        _add_indicator(indicators, "large_pe_overlay", "medium", evidence="bytes:%d" % overlay)
    return {
        "machine": machine, "sections": sections, "entry_rva": entry_rva,
        "characteristics": characteristics, "imports_count": len(imports),
        "certificate_table_present": cert_present, "overlay_bytes": overlay,
    }


def _parse_elf(data, indicators, deadline):
    if len(data) < 52 or data[:4] != b"\x7fELF":
        raise ArtifactRiskError("invalid or truncated ELF header")
    bits = data[4]
    endian = {1: "<", 2: ">"}.get(data[5])
    if bits not in (1, 2) or endian is None:
        raise ArtifactRiskError("unsupported ELF class or byte order")
    if bits == 2:
        if len(data) < 64:
            raise ArtifactRiskError("truncated ELF64 header")
        e_type, machine = struct.unpack_from(endian + "HH", data, 16)
        entry, phoff = struct.unpack_from(endian + "QQ", data, 24)
        phentsize, phnum = struct.unpack_from(endian + "HH", data, 54)
    else:
        e_type, machine = struct.unpack_from(endian + "HH", data, 16)
        entry, phoff = struct.unpack_from(endian + "II", data, 24)
        phentsize, phnum = struct.unpack_from(endian + "HH", data, 42)
    if phnum > 4096 or phentsize < (56 if bits == 2 else 32):
        raise ArtifactRiskError("invalid ELF program-header table")
    wx_segments = 0
    interpreter = False
    for index in range(phnum):
        _check(deadline)
        off = phoff + index * phentsize
        if off + phentsize > len(data):
            raise ArtifactRiskError("truncated ELF program-header table")
        if bits == 2:
            p_type, flags = struct.unpack_from(endian + "II", data, off)
        else:
            p_type = struct.unpack_from(endian + "I", data, off)[0]
            flags = struct.unpack_from(endian + "I", data, off + 24)[0]
        interpreter = interpreter or p_type == 3
        if p_type == 1 and flags & 0x1 and flags & 0x2:
            wx_segments += 1
    if wx_segments:
        _add_indicator(indicators, "writable_executable_segment", "high", wx_segments)
    return {"bits": 64 if bits == 2 else 32, "byte_order": "little" if endian == "<" else "big",
            "type": e_type, "machine": machine, "entry": entry,
            "program_headers": phnum, "interpreter_present": interpreter,
            "writable_executable_segments": wx_segments}


def _parse_macho(data, indicators, deadline):
    if len(data) < 28:
        raise ArtifactRiskError("truncated Mach-O header")
    magic_bytes = data[:4]
    mapping = {
        b"\xce\xfa\xed\xfe": ("<", 32), b"\xcf\xfa\xed\xfe": ("<", 64),
        b"\xfe\xed\xfa\xce": (">", 32), b"\xfe\xed\xfa\xcf": (">", 64),
    }
    if magic_bytes not in mapping:
        raise ArtifactRiskError("unsupported Mach-O magic")
    endian, bits = mapping[magic_bytes]
    cpu_type, cpu_subtype, file_type, ncmds, sizeofcmds, flags = struct.unpack_from(endian + "IIIIII", data, 4)
    header_size = 32 if bits == 64 else 28
    if ncmds > 4096 or header_size + sizeofcmds > len(data):
        raise ArtifactRiskError("truncated Mach-O load commands")
    offset = header_size
    dylibs = 0
    code_signature = False
    wx_segments = 0
    for _index in range(ncmds):
        _check(deadline)
        if offset + 8 > len(data):
            raise ArtifactRiskError("truncated Mach-O load command")
        command, command_size = struct.unpack_from(endian + "II", data, offset)
        if command_size < 8 or offset + command_size > len(data):
            raise ArtifactRiskError("invalid Mach-O load-command size")
        base_command = command & 0x7FFFFFFF
        if base_command == 0x1 and bits == 32 and command_size >= 56:
            initprot = struct.unpack_from(endian + "I", data, offset + 44)[0]
            wx_segments += int(bool(initprot & 0x2 and initprot & 0x4))
        elif base_command == 0x19 and bits == 64 and command_size >= 72:
            initprot = struct.unpack_from(endian + "I", data, offset + 60)[0]
            wx_segments += int(bool(initprot & 0x2 and initprot & 0x4))
        elif base_command in (0xC, 0x18, 0x1F, 0x20, 0x23):
            dylibs += 1
        elif base_command == 0x1D:
            code_signature = True
        offset += command_size
    if wx_segments:
        _add_indicator(indicators, "writable_executable_segment", "high", wx_segments)
    return {"bits": bits, "byte_order": "little" if endian == "<" else "big",
            "cpu_type": cpu_type, "cpu_subtype": cpu_subtype, "file_type": file_type,
            "load_commands": ncmds, "flags": flags, "dylib_commands": dylibs,
            "code_signature_present": code_signature,
            "writable_executable_segments": wx_segments}


def _kind(prefix, suffix):
    if prefix.startswith(b"%PDF-"):
        return "pdf"
    if prefix.startswith(b"MZ"):
        return "pe"
    if prefix.startswith(b"\x7fELF"):
        return "elf"
    if prefix[:4] in (b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf"):
        return "macho"
    if prefix.startswith(b"#!") or suffix.lower() in {".ps1", ".sh", ".bash", ".py", ".js", ".cmd", ".bat"}:
        return "script"
    return "binary"


def inspect_artifact(path, *, max_scan_bytes=DEFAULT_MAX_SCAN_BYTES,
                     max_seconds=DEFAULT_MAX_SECONDS, extra_roots=""):
    """Statically inspect an artifact. No content is rendered or executed."""
    scan_cap = _clamp_int(max_scan_bytes, 1024, MAX_SCAN_BYTES)
    deadline = time.monotonic() + _clamp_seconds(max_seconds)
    with pdf_risk._open_guarded(str(path), extra_roots) as (actual, handle, opened):
        size = opened.st_size
        if size <= 0:
            raise ArtifactRiskError("artifact is empty")
        if size > MAX_SOURCE_BYTES:
            raise ArtifactRiskError("artifact exceeds the 256 MiB source ceiling")
        prefix = handle.read(min(size, scan_cap // 2 if size > scan_cap else size))
        kind = _kind(prefix, actual.suffix)
        if kind == "pdf":
            return pdf_risk.inspect_pdf(
                actual, max_scan_bytes=scan_cap,
                max_seconds=max(0.001, deadline - time.monotonic()), extra_roots=extra_roots,
            ) | {"kind": "pdf"}
        complete = size <= scan_cap
        if complete:
            data = prefix + handle.read(size - len(prefix))
            ranges = [[0, size]]
        else:
            suffix_size = scan_cap - len(prefix)
            handle.seek(size - suffix_size)
            suffix_start = handle.tell()
            data = prefix + handle.read(suffix_size)
            ranges = [[0, len(prefix)], [suffix_start, size]]
        _check(deadline)
        digest = hashlib.sha256(data).hexdigest() if complete else None
    indicators = {}
    _scan_patterns(data, indicators)
    details = {}
    incomplete = [] if complete else ["file_exceeds_scan_budget"]
    try:
        # Structural offsets are meaningful only when the inspected bytes are
        # contiguous from start to EOF. Prefix+tail sampling remains useful for
        # fixed indicators, but parsing the join as one executable is unsafe.
        if not complete and kind in {"pe", "elf", "macho"}:
            incomplete.append("structural_parse_requires_complete_file")
            details = {"structural_parse_skipped": True}
        elif kind == "pe":
            details = _parse_pe(data, size, indicators, deadline)
        elif kind == "elf":
            details = _parse_elf(data, indicators, deadline)
        elif kind == "macho":
            details = _parse_macho(data, indicators, deadline)
        elif kind == "script":
            details = {"shebang_present": data.startswith(b"#!")}
        else:
            incomplete.append("unsupported_format")
    except (ArtifactRiskError, struct.error) as exc:
        incomplete.append("malformed_%s" % kind)
        details = {"parse_error": str(exc)}
    ordered = [indicators[name] for name in sorted(indicators)]
    severities = {row["severity"] for row in ordered}
    if "high" in severities:
        risk = "high"
    elif "medium" in severities:
        risk = "medium"
    elif "low" in severities:
        risk = "low"
    else:
        risk = "none_detected" if not incomplete else "unknown"
    return {
        "schema_version": 1, "path": str(actual), "kind": kind,
        "source_bytes": size, "bytes_scanned": len(data), "sha256": digest,
        "scan_complete": not incomplete, "risk": risk, "indicators": ordered,
        "incomplete_reasons": sorted(set(incomplete)), "ranges_scanned": ranges,
        "details": details, "execution": "none",
    }


def format_result(result):
    return pdf_risk.format_result(result)


def effective_policy(requested=""):
    configured = os.environ.get("SONDER_EXECUTION_RISK_POLICY", "report").strip().lower() or "report"
    requested = str(requested or "").strip().lower() or "off"
    if configured not in _POLICY_ORDER:
        raise ArtifactRiskError("invalid SONDER_EXECUTION_RISK_POLICY")
    if requested not in _POLICY_ORDER:
        raise ArtifactRiskError("invalid requested execution risk policy")
    return max((configured, requested), key=_POLICY_ORDER.__getitem__)


def policy_denies(policy, risk):
    if policy == "deny-high":
        return risk == "high"
    if policy == "deny-medium":
        return risk in {"high", "medium"}
    if policy == "deny-unknown":
        return risk in {"high", "medium", "unknown"}
    return False


def enforce_execution_policy(path, *, requested="", extra_roots=""):
    """Inspect an exact file and raise when effective policy denies its risk."""
    policy = effective_policy(requested)
    if policy == "off":
        return {"policy": policy, "risk": "not_inspected", "denied": False}
    result = inspect_artifact(path, extra_roots=extra_roots)
    denied = policy_denies(policy, result["risk"])
    result = dict(result)
    result.update({"policy": policy, "denied": denied})
    if denied:
        raise ArtifactRiskDenied(result)
    return result

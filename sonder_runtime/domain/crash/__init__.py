"""Pure crash-capture readers and the typed ``CrashReport`` (sonder.crash_report/1).

Readers: minidump (Windows/Breakpad/Crashpad), ELF core, sanitizer text,
valgrind memcheck XML, macOS ``.ips`` and debugger transcripts. All inputs
are hostile; see each module for its bounds. No I/O.
"""

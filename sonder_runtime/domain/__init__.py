"""Pure domain layer (SPEC-3).

Domain modules import only the standard library and other domain
modules. No I/O, no environment reads, no threads, no globals mutated at
import time — scripts/check_architecture.py enforces this in CI.
"""

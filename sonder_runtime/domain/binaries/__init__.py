"""Pure binary-identity readers (PE/RSDS, PDB MSF 7.0, ELF build-id).

Hostile-input readers over a range-checked ``ByteReader``; no I/O. Used by
the crash readers and by the debug planner's PE+PDB verification.
"""

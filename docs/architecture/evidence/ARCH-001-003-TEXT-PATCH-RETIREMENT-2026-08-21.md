# ARCH-001/002/003 text-patch retirement evidence

Date: 2026-08-21

Removed the root `text_patch.py` compatibility module. `server.py` and the
tests now import the canonical packaged filesystem adapter directly, and the
architecture checker permanently treats the retired root path as forbidden.

Verification: `python -m pytest -q tests/test_text_patch.py tests/test_text_patch_compatibility.py --basetemp .pytest-root-text-patch` — **22 passed, 2 skipped**; architecture and compile checks pass.

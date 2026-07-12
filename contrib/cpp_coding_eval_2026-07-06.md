# Sonder Runtime C++ coding eval — 2026-07-06

Handed the local coder 33 self-contained C++17 tasks (pure logic, known algorithms,
boilerplate). Every output was compiled with g++ and run against test vectors.

**Result: 25/33 clean pass (76%).**

## Strong (correct + reusable)
Known algorithms and straightforward logic: FNV-1a & CRC-32 hashes, Levenshtein
distance, Roman numeral <-> int (both directions), GCD (Euclid), next-power-of-two,
popcount, base64 helpers, string/path utilities (trim, split, starts/ends-with,
basename, rot13, snake->camel), input validation, clamp/lerp/smoothstep, median,
palindrome, IPv4 validation. Correct (one needed a one-char unsigned-char cast for a
locale-function signedness nit).

## Weak (recurring failure modes)
1. Edge-case completeness: missed zero-padding in a duration formatter; leading-dot and
   dot-before-slash cases in an extension parser; double-slash at a path junction;
   non-multiple-of-3 handling in base64 encode.
2. Arithmetic subtlety: integer division dropping the fraction before printing a decimal.
3. Inventing non-existent APIs that don't compile: a Python-style string_view.partition,
   and mutating a read-only string_view via std::transform.
4. Byte-layout math: packed-struct sizes computed wrong (padding miscounted).

## Operating rule
Good force-multiplier for pure logic / textbook algorithms / boilerplate, ALWAYS behind
a compile+test gate. Do not trust it for wire layout / packing, hot paths, or crypto.
The learning loop was fed all 33 outcomes (tests_passed / rejected / failed).

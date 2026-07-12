import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import import_autofix  # noqa: E402
import grounding  # noqa: E402  (read-only: used to ground the fix in a real run)


# --- detect_missing_import ---------------------------------------------------

def test_detect_direct_module_random():
    tb = ("Traceback (most recent call last):\n"
          '  File "t.py", line 1, in <module>\n'
          "    print(random.choice([1, 2, 3]))\n"
          "NameError: name 'random' is not defined")
    assert import_autofix.detect_missing_import(tb) == "random"


def test_detect_direct_module_os():
    tb = "NameError: name 'os' is not defined"
    assert import_autofix.detect_missing_import(tb) == "os"


def test_detect_typing_symbol_maps_to_typing_module():
    tb = "NameError: name 'List' is not defined"
    assert import_autofix.detect_missing_import(tb) == "typing"


def test_detect_collections_symbol_maps_to_collections_module():
    tb = "NameError: name 'OrderedDict' is not defined"
    assert import_autofix.detect_missing_import(tb) == "collections"


def test_detect_itertools_symbol_maps_to_itertools_module():
    tb = "NameError: name 'chain' is not defined"
    assert import_autofix.detect_missing_import(tb) == "itertools"


def test_detect_returns_none_when_no_name_error():
    assert import_autofix.detect_missing_import("ValueError: bad thing") is None
    assert import_autofix.detect_missing_import("") is None
    assert import_autofix.detect_missing_import(None) is None


def test_detect_returns_none_for_unsupported_name():
    tb = "NameError: name 'numpy' is not defined"
    assert import_autofix.detect_missing_import(tb) is None


def test_detect_uses_last_name_error_when_several():
    tb = ("NameError: name 'random' is not defined\n"
          "...later retry...\n"
          "NameError: name 'json' is not defined")
    assert import_autofix.detect_missing_import(tb) == "json"


# --- fix_missing_imports ------------------------------------------------------

def test_fix_prepends_direct_module_import():
    code = "print(random.choice([1, 2, 3]))"
    tb = "NameError: name 'random' is not defined"
    fixed = import_autofix.fix_missing_imports(code, tb)
    assert fixed.splitlines()[0] == "import random"
    assert "print(random.choice" in fixed


def test_fix_prepends_from_import_for_symbol():
    code = "d = OrderedDict()\nprint(d)"
    tb = "NameError: name 'OrderedDict' is not defined"
    fixed = import_autofix.fix_missing_imports(code, tb)
    assert fixed.splitlines()[0] == "from collections import OrderedDict"


def test_fix_is_noop_when_already_imported():
    code = "import random\nprint(random.choice([1]))"
    tb = "NameError: name 'random' is not defined"
    assert import_autofix.fix_missing_imports(code, tb) == code


def test_fix_is_noop_when_from_import_already_present():
    code = "from collections import OrderedDict\nd = OrderedDict()"
    tb = "NameError: name 'OrderedDict' is not defined"
    assert import_autofix.fix_missing_imports(code, tb) == code


def test_fix_returns_code_unchanged_when_no_name_error():
    code = "print('hello')"
    assert import_autofix.fix_missing_imports(code, "SyntaxError: oops") == code


def test_fix_returns_code_unchanged_for_unsupported_name():
    code = "print(numpy.array([1]))"
    tb = "NameError: name 'numpy' is not defined"
    assert import_autofix.fix_missing_imports(code, tb) == code


def test_fix_wrong_pygame_math_attrs_adds_math_import():
    code = "import pygame\nx = pygame.cos(pygame.radians(90))"
    tb = "AttributeError: module 'pygame' has no attribute 'cos'"

    fixed = import_autofix.fix_wrong_module_attrs(code, tb)

    assert fixed.startswith("import math\n")
    assert "math.cos(math.radians(90))" in fixed


def test_fix_common_generation_errors_handles_wrong_pygame_math_module():
    code = "import pygame\nprint(round(pygame.cos(0)))"
    tb = "AttributeError: module 'pygame' has no attribute 'cos'"

    fixed = import_autofix.fix_common_generation_errors(code, tb)

    assert "import math" in fixed
    assert "math.cos" in fixed


def test_fix_common_generation_errors_handles_pygame_math_namespace():
    code = "import pygame\nprint(round(pygame.math.cos(0)))"
    tb = "AttributeError: module 'pygame.math' has no attribute 'cos'"

    fixed = import_autofix.fix_common_generation_errors(code, tb)

    assert "import math" in fixed
    assert "math.cos" in fixed
    assert "pygame.math.cos" not in fixed


# --- fix_cpp_missing_headers ---------------------------------------------------

def test_cpp_fix_maps_real_gcc_assert_error_to_cassert():
    code = "int main() { assert(1); return 0; }"
    err = ("game.cpp: In function 'int main()':\n"
           "game.cpp:1:14: error: 'assert' was not declared in this scope")
    fixed = import_autofix.fix_cpp_missing_headers(code, err)
    assert fixed.splitlines()[0] == "#include <cassert>"
    assert "int main()" in fixed


def test_cpp_fix_maps_msvc_identifier_not_found_to_cmath():
    code = "int main() { double d = sqrt(2.0); return (int)d; }"
    err = "game.cpp(1): error C3861: 'sqrt': identifier not found"
    fixed = import_autofix.fix_cpp_missing_headers(code, err)
    assert fixed.splitlines()[0] == "#include <cmath>"


def test_cpp_fix_maps_not_a_member_of_std_to_algorithm():
    code = "int main() { int v[3]{3,1,2}; std::sort(v, v+3); return v[0]; }"
    err = "game.cpp:1:31: error: 'sort' is not a member of 'std'"
    fixed = import_autofix.fix_cpp_missing_headers(code, err)
    assert fixed.splitlines()[0] == "#include <algorithm>"


def test_cpp_fix_handles_multiple_symbols_without_duplicates():
    code = "int main() { assert(1); double d = std::sqrt(2.0); return (int)d; }"
    err = ("game.cpp:1:14: error: 'assert' was not declared in this scope\n"
           "game.cpp:1:30: error: 'sqrt' is not a member of 'std'\n"
           "game.cpp:1:30: error: 'sqrt' is not a member of 'std'")
    fixed = import_autofix.fix_cpp_missing_headers(code, err)
    lines = fixed.splitlines()
    assert lines.count("#include <cassert>") == 1
    assert lines.count("#include <cmath>") == 1


def test_cpp_fix_is_noop_when_header_already_included():
    code = "#include <cassert>\nint main() { assert(1); return 0; }"
    err = "game.cpp:2:14: error: 'assert' was not declared in this scope"
    assert import_autofix.fix_cpp_missing_headers(code, err) == code


def test_cpp_fix_is_noop_for_unknown_symbols_and_clean_output():
    code = "int main() { return 0; }"
    assert import_autofix.fix_cpp_missing_headers(code, "") == code
    err = "game.cpp:1:1: error: 'nlohmann' has not been declared"
    assert import_autofix.fix_cpp_missing_headers(code, err) == code


# --- distill_cpp_errors ----------------------------------------------------------

def test_distill_keeps_only_error_lines():
    raw = ("game.cpp: In function 'int main()':\n"
           "game.cpp:2:1: warning: unused variable 'x'\n"
           "game.cpp:9:3: error: 'assert' was not declared in this scope\n"
           "game.cpp(4): error C3861: 'sqrt': identifier not found\n"
           "note: suggested alternative: 'short'\n")
    distilled = import_autofix.distill_cpp_errors(raw)
    assert "error: 'assert'" in distilled
    assert "error C3861" in distilled
    assert "warning" not in distilled
    assert "note:" not in distilled


def test_distill_falls_back_to_truncated_original_without_error_lines():
    raw = "linker exploded mysteriously\n" * 100
    distilled = import_autofix.distill_cpp_errors(raw, limit=50)
    assert distilled == raw[:50]


# --- grounded end-to-end: the exact breakout-class failure --------------------

def test_grounded_random_choice_breakout_case():
    """The motivating failure: model forgets `import random`. Run the buggy
    code for real, feed the real traceback through the autofix, then run the
    fixed code for real and confirm it now passes."""
    buggy = "x = random.choice([1, 2, 3])\nprint(x)"
    ok, out = grounding.run_code(buggy)
    assert ok is False
    assert "NameError" in out

    fixed = import_autofix.fix_missing_imports(buggy, out)
    assert fixed.startswith("import random")

    ok2, out2 = grounding.run_code(fixed)
    assert ok2 is True, out2


def test_grounded_typing_annotation_breakout_case():
    buggy = "def f(x: List[int]) -> int:\n    return sum(x)\nprint(f([1, 2, 3]))"
    ok, out = grounding.run_code(buggy)
    assert ok is False
    assert "NameError" in out

    fixed = import_autofix.fix_missing_imports(buggy, out)
    assert fixed.startswith("from typing import List")

    ok2, out2 = grounding.run_code(fixed)
    assert ok2 is True, out2


def test_grounded_pygame_math_attr_case():
    pytest.importorskip("pygame")
    buggy = "import pygame\nprint(round(pygame.cos(0)))"
    ok, out = grounding.run_code(buggy)
    assert ok is False
    assert "AttributeError" in out

    fixed = import_autofix.fix_common_generation_errors(buggy, out)
    assert "math.cos" in fixed

    ok2, out2 = grounding.run_code(fixed)
    assert ok2 is True, out2

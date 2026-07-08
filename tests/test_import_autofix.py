import os
import sys

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
    buggy = "import pygame\nprint(round(pygame.cos(0)))"
    ok, out = grounding.run_code(buggy)
    assert ok is False
    assert "AttributeError" in out

    fixed = import_autofix.fix_common_generation_errors(buggy, out)
    assert "math.cos" in fixed

    ok2, out2 = grounding.run_code(fixed)
    assert ok2 is True, out2

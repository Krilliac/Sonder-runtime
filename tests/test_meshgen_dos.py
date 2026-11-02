"""Resource-exhaustion regressions: every reviewer probe must end in a ScriptError within two seconds.

The probes (d01..d17, e01..e05) each make one allow-listed operation do more work than its element count
suggests (aliased containers, streaming str/print/f-string/assert, default-argument copies, sorts, shuffles,
sums, joins).  The recorder charges work in proportion to the elements touched, so each one must hit a cap.
"""
from __future__ import annotations

import time

import pytest

from sonder_runtime.domain.meshgen.dsl.errors import ScriptError
from sonder_runtime.domain.meshgen.dsl.recorder import compile_script

PROBES = {
    'd01_shuffle': 'import random\ndef build():\n    l = list(range(200000))\n    for i in range(1000000):\n        random.shuffle(l)\n',
    'd02_sort_strs': 'def build():\n    l = [str(i) for i in range(200000)]\n    for i in range(1000000):\n        l.reverse()\n        l.sort()\n        l = sorted(l, reverse=True)\n',
    'd03_alias_eq': 'def build():\n    row = [0] * 200000\n    row2 = [0] * 200000\n    g1 = [row] * 200000\n    g2 = [row2] * 200000\n    x = g1 == g2\n',
    'd04_alias_str': 'def build():\n    a = [0] * 200\n    b = [a] * 200\n    c = [b] * 200\n    d = c\n    s = str(d)\n',
    'd05_default_copy': 'def f(x=[[0] * 3000] * 3000):\n    return 0\ndef build():\n    for i in range(100000):\n        f()\n',
    'd06_count_alias': 'def build():\n    row = [0] * 200000\n    g = [row] * 200000\n    n = g.count([0] * 200000)\n',
    'd07_in_alias': 'def build():\n    row = [0] * 200000\n    g = [row] * 200000\n    n = [1] in g\n    m = ([0]*199999 + [1]) in g\n',
    'd08_sum_loop': 'def build():\n    l = list(range(200000))\n    for i in range(1000000):\n        s = sum(l)\n',
    'd09_percent': 'def build():\n    t = "%999d" * 20000\n    for i in range(100000):\n        s = t % tuple(range(20000))\n',
    'd10_while': 'def build():\n    while True:\n        pass\n',
    'd11_pow': 'def build():\n    x = 2 ** 10 ** 9\n',
    'd12_strmul': 'def build():\n    x = "a" * 10 ** 9\n',
    'd13_print_alias': 'def build():\n    a = [0] * 200\n    b = [a] * 200\n    c = [b] * 200\n    d = c\n    print(d)\n',
    'd14_join': 'def build():\n    l = ["a" * 1000] * 99\n    for i in range(1000000):\n        s = "".join(l)\n',
    'd15_minmax': 'def build():\n    row = [0] * 200000\n    g = [row] * 200000\n    g2 = [[0] * 200000] * 200000\n    m = max(g + g2)\n',
    'd16_assert_alias': 'def build():\n    a = [0] * 200\n    b = [a] * 200\n    c = [b] * 200\n    d = c\n    assert False, d\n',
    'd17_fstring_alias': 'def build():\n    a = [0] * 200\n    b = [a] * 200\n    c = [b] * 200\n    d = c\n    s = f"{d}"\n',
    'e01_shuffle': 'import random\ndef build():\n    l = list(range(200000))\n    for i in range(1000):\n        for j in range(1000):\n            random.shuffle(l)\n',
    'e02_sort': 'import random\ndef build():\n    l = [str(i) for i in range(100000)]\n    for i in range(1000):\n        for j in range(1000):\n            random.shuffle(l)\n            l.sort()\n',
    'e03_print4': 'def build():\n    a = [0] * 200\n    b = [a] * 200\n    c = [b] * 200\n    d = [c] * 200\n    print(d)\n',
    'e04_sum': 'def build():\n    l = list(range(200000))\n    for i in range(1000):\n        for j in range(1000):\n            s = sum(l)\n',
    'e05_eq_small': 'def build():\n    row = [0] * 200000\n    row2 = [0] * 200000\n    g1 = [row] * 2000\n    g2 = [row2] * 2000\n    x = g1 == g2\n',
}

CAP_WORDS = ("too large", "too long", "too much work", "too many", "at most", "too big", "limit")


@pytest.mark.parametrize("name", sorted(PROBES))
def test_reviewer_probe_hits_a_cap_within_two_seconds(name):
    started = time.monotonic()
    with pytest.raises(ScriptError) as info:
        compile_script(PROBES[name])
    elapsed = time.monotonic() - started
    message = str(info.value)
    assert elapsed < 2.0, "%s took %.2fs" % (name, elapsed)
    assert "internal error" not in message and any(word in message for word in CAP_WORDS), message

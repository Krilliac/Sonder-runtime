"""Usability: ordinary model-style scripts must record well inside the work budget."""
from __future__ import annotations

import time

import pytest

from sonder_runtime.domain.meshgen.dsl.recorder import compile_script

SCRIPTS = {
    'cart': 'import math\n\nWOOD = "#73502e"\nIRON = "#404047"\nWHEEL_R = 0.45\nSPOKES = 12\n\n\ndef ring_points(radius, count, y, z0):\n    pts = []\n    for i in range(count):\n        a = 2 * math.pi * i / count\n        pts.append((radius * math.cos(a), y, z0 + radius * math.sin(a)))\n    return pts\n\n\ndef wheel(name, x, y):\n    parts = []\n    rim = primitive("torus", name=name + "_rim", major_radius=WHEEL_R, minor_radius=0.04,\n                    segments=24, rings=6, loc=(x, y, WHEEL_R), rot=(0, 90, 0))\n    paint(rim, IRON)\n    parts.append(rim)\n    hub = primitive("cylinder", name=name + "_hub", radius=0.08, depth=0.16, segments=10,\n                    loc=(x, y, WHEEL_R), rot=(0, 90, 0))\n    paint(hub, IRON)\n    parts.append(hub)\n    for i in range(SPOKES):\n        ang = 360.0 * i / SPOKES\n        rad = math.radians(ang)\n        cy = y + 0.5 * WHEEL_R * math.cos(rad)\n        cz = WHEEL_R + 0.5 * WHEEL_R * math.sin(rad)\n        spoke = primitive("cylinder", name="%s_spoke%02d" % (name, i), radius=0.018, depth=WHEEL_R * 0.95,\n                          segments=6, loc=(x, cy, cz), rot=(ang, 0, 0))\n        paint(spoke, WOOD)\n        parts.append(spoke)\n    return join(parts, name)\n\n\ndef plank(name, size, loc):\n    p = primitive("cube", name=name, size=1.0, loc=loc, scale=size)\n    paint(p, WOOD)\n    return p\n\n\ndef build():\n    parts = []\n    for side, x in (("L", -0.7), ("R", 0.7)):\n        for end, y in (("F", -0.6), ("B", 0.6)):\n            parts.append(wheel("wheel_%s%s" % (side, end), x, y))\n    axle = primitive("cylinder", name="axle", radius=0.03, depth=1.5, segments=8, loc=(0, -0.6, WHEEL_R), rot=(0, 90, 0))\n    parts.append(axle)\n    for i in range(9):\n        y = -0.9 + i * 0.225\n        parts.append(plank("deck%d" % i, (1.3, 0.2, 0.05), (0, y, 0.8)))\n    for side in (-1, 1):\n        for i in range(3):\n            parts.append(plank("side%d_%d" % (side, i), (0.05, 2.0, 0.12), (side * 0.65, 0, 0.9 + i * 0.14)))\n    pts = ring_points(0.1, 8, 1.2, 0.8)\n    verts = [(0, 0.9, 0.8)] + pts\n    faces = [(0, 1 + i, 1 + (i + 1) % 8) for i in range(8)]\n    handle = mesh("handle_cap", verts, faces)\n    parts.append(handle)\n    body = join(parts, "cart")\n    paint_faces(body, lambda p: p.center.z > 0.75 and p.normal.z > 0.5, "#8c6138")\n    transform(body, loc=(0, 0, 0))\n',
    'fence': 'import math\nimport random\n\nPOSTS = 40\nSPACING = 0.5\nPOST_H = 1.1\nWOOD = ["#664729", "#755230", "#5c4024"]\n\n\ndef lerp(a, b, t):\n    return a + (b - a) * t\n\n\ndef post_height(i):\n    wave = 0.04 * math.sin(i * 0.7)\n    return POST_H + wave + random.uniform(-0.03, 0.03)\n\n\ndef post(i, x, y, angle):\n    h = post_height(i)\n    p = primitive("cube", name="post%02d" % i, size=1.0, loc=(x, y, h / 2), scale=(0.09, 0.09, h),\n                  rot=(0, 0, angle))\n    cap = primitive("cone", name="cap%02d" % i, radius=0.07, depth=0.08, segments=4,\n                    loc=(x, y, h + 0.04), rot=(0, 0, 45 + angle))\n    colour = random.choice(WOOD)\n    paint(p, colour)\n    paint(cap, colour)\n    return [p, cap]\n\n\ndef rail(i, a, b, z):\n    dx, dy = b[0] - a[0], b[1] - a[1]\n    length = math.hypot(dx, dy)\n    ang = math.degrees(math.atan2(dy, dx))\n    r = primitive("cube", name="rail%02d_%d" % (i, int(z * 100)), size=1.0,\n                  loc=(lerp(a[0], b[0], 0.5), lerp(a[1], b[1], 0.5), z), scale=(length, 0.04, 0.08),\n                  rot=(0, 0, ang))\n    paint(r, WOOD[1])\n    return r\n\n\ndef build():\n    random.seed(7)\n    spots = []\n    for i in range(POSTS):\n        t = i / (POSTS - 1)\n        x = i * SPACING\n        y = 0.6 * math.sin(t * math.pi * 2)\n        spots.append((x, y))\n    parts = []\n    for i, (x, y) in enumerate(spots):\n        nxt = spots[min(i + 1, POSTS - 1)]\n        prv = spots[max(i - 1, 0)]\n        angle = math.degrees(math.atan2(nxt[1] - prv[1], nxt[0] - prv[0]))\n        parts.extend(post(i, x, y, angle))\n    for i in range(POSTS - 1):\n        for z in (0.35, 0.8):\n            parts.append(rail(i, spots[i], spots[i + 1], z))\n    fence = join(parts, "fence")\n    shade_smooth(fence, smooth=False)\n    paint_faces(fence, lambda p: p.normal.z > 0.9 and p.center.z > 1.0, "#4d331a")\n',
    'grid_append34': 'import math\ndef build():\n    verts = []\n    faces = []\n    N = 34\n    for i in range(N + 1):\n        for j in range(N + 1):\n            verts.append((i * 0.1, j * 0.1, 0.05 * math.sin(i * 0.5) * math.cos(j * 0.5)))\n    for i in range(N):\n        for j in range(N):\n            a = i * (N + 1) + j\n            faces.append((a, a + 1, a + N + 2, a + N + 1))\n    mesh("terrain", verts, faces)\n',
    'grid_pluseq': 'import math\ndef build():\n    verts = []\n    faces = []\n    N = 24\n    for i in range(N + 1):\n        for j in range(N + 1):\n            verts += [(i * 0.1, j * 0.1, 0.05 * math.sin(i * 0.5) * math.cos(j * 0.5))]\n    for i in range(N):\n        for j in range(N):\n            a = i * (N + 1) + j\n            faces += [(a, a + 1, a + N + 2, a + N + 1)]\n    mesh("terrain", verts, faces)\n',
    'grid_pluseq34': 'import math\ndef build():\n    verts = []\n    faces = []\n    N = 34\n    for i in range(N + 1):\n        for j in range(N + 1):\n            verts += [(i * 0.1, j * 0.1, 0.05 * math.sin(i * 0.5) * math.cos(j * 0.5))]\n    for i in range(N):\n        for j in range(N):\n            a = i * (N + 1) + j\n            faces += [(a, a + 1, a + N + 2, a + N + 1)]\n    mesh("terrain", verts, faces)\n',
}

GRID = chr(10).join([
    "import math",
    "def build():",
    "    verts = []",
    "    faces = []",
    "    N = 40",
    "    for i in range(N + 1):",
    "        for j in range(N + 1):",
    "            verts += [(i * 0.1, j * 0.1, 0.05 * math.sin(i * 0.5) * math.cos(j * 0.5))]",
    "    for i in range(N):",
    "        for j in range(N):",
    "            a = i * (N + 1) + j",
    "            faces += [(a, a + 1, a + N + 2, a + N + 1)]",
    "    mesh('terrain', verts, faces)",
    "",
])


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_reviewer_usability_script_records(name):
    started = time.monotonic()
    recording = compile_script(SCRIPTS[name])
    assert recording.ops and time.monotonic() - started < 3.0


def test_a_40_by_40_grid_built_with_plus_equals_records():
    recording = compile_script(GRID)
    assert recording.ops[0]["op"] == "core.mesh"
    assert len(recording.ops[0]["args"][1]) == 41 * 41 and len(recording.ops[0]["args"][2]) == 1600


def test_plus_equals_is_charged_like_extend_not_by_the_whole_list():
    """1,000 single-element += on a growing list must cost about 1,000 units, not 1,000 x its length."""
    source = chr(10).join(["def build():", "    l = []", "    for i in range(150000):", "        l += [i]", ""])
    compile_script(source)


def test_a_one_element_tuple_is_shown_with_its_comma():
    source = chr(10).join(["def build():", "    print((1,))", "    print([(2,)])", "    print((1, 2))", ""])
    assert compile_script(source).log == ["(1,)", "[(2,)]", "(1, 2)"]

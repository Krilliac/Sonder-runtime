"""Usability: ordinary model-style scripts must record well inside the work budget."""
from __future__ import annotations

import time

import pytest

from sonder_runtime.domain.meshgen.dsl.recorder import compile_script

SCRIPTS = {
    'lantern_post': 'import math\n\nPALETTE = {\n    "iron": (0.12, 0.12, 0.13, 1.0),\n    "brass": (0.72, 0.55, 0.22, 1.0),\n    "glass": (1.0, 0.85, 0.45, 0.6),\n    "stone": (0.45, 0.43, 0.40, 1.0),\n    "trim": (0.30, 0.20, 0.10, 1.0),\n}\nRING_COUNT = 24\nPOST_HEIGHT = 2.6\n\n\ndef colour(name):\n    if name in PALETTE:\n        return PALETTE[name]\n    return PALETTE["iron"]\n\n\ndef ring(index, z, radius):\n    r = primitive("torus", name=f"ring_{index:02d}", major_radius=radius, minor_radius=0.012,\n                  segments=16, rings=6, loc=(0.0, 0.0, z))\n    tone = "brass" if index % 3 == 0 else "iron"\n    paint(r, colour(tone))\n    return r\n\n\ndef base():\n    plinth = primitive("cube", name="plinth", size=0.4, loc=(0.0, 0.0, 0.1), scale=(1.0, 1.0, 0.5))\n    paint(plinth, colour("stone"))\n    paint_faces(plinth, lambda p: p.center.z > 0.15 and abs(p.normal.z) > 0.9, colour("trim"))\n    return plinth\n\n\ndef lamp(top):\n    cage = primitive("cylinder", name="cage", radius=0.14, depth=0.32, segments=8, loc=(0.0, 0.0, top + 0.16))\n    paint(cage, colour("iron"))\n    limit = top + 0.05\n    paint_faces(cage, lambda p: p.center.z > limit and abs(p.normal.z) < 0.2, colour("glass"))\n    cap = primitive("cone", name="cap", radius=0.18, depth=0.14, segments=8, loc=(0.0, 0.0, top + 0.39))\n    paint(cap, colour("brass"))\n    return [cage, cap]\n\n\ndef build():\n    parts = [base()]\n    post = primitive("cylinder", name="post", radius=0.05, depth=POST_HEIGHT, segments=12,\n                     loc=(0.0, 0.0, 0.2 + POST_HEIGHT / 2))\n    paint(post, colour("iron"))\n    parts.append(post)\n    rings = []\n    for i in range(RING_COUNT):\n        t = i / (RING_COUNT - 1)\n        z = 0.3 + t * (POST_HEIGHT - 0.3)\n        radius = 0.065 + 0.01 * math.sin(t * math.pi * 4)\n        rings.append(ring(i, z, radius))\n    parts += rings\n    parts += lamp(0.2 + POST_HEIGHT)\n    for k in range(4):\n        angle = k * math.pi / 2\n        arm = primitive("cube", name=f"arm_{k}", size=0.03, scale=(4.0, 1.0, 1.0),\n                        loc=(0.12 * math.cos(angle), 0.12 * math.sin(angle), POST_HEIGHT),\n                        rot=(0.0, 0.0, angle))\n        paint(arm, colour("brass"))\n        parts.append(arm)\n    lookups = 0\n    for step in range(2000):\n        for key in ("iron", "brass", "glass", "stone", "trim", "copper"):\n            if key in PALETTE:\n                lookups += 1\n    print("parts", len(parts), "lookups", lookups)\n    join(parts, "lantern_post")\n',
    'lantern_post_hex': 'import math\n\nPALETTE = {\n    "iron": "#1F1F21",\n    "brass": "#B88C38",\n    "glass": "#FFD973",\n    "stone": "#736E66",\n    "trim": "#4D331A",\n}\nRING_COUNT = 24\nPOST_HEIGHT = 2.6\n\n\ndef colour(name):\n    if name in PALETTE:\n        return PALETTE[name]\n    return PALETTE["iron"]\n\n\ndef ring(index, z, radius):\n    r = primitive("torus", name=f"ring_{index:02d}", major_radius=radius, minor_radius=0.012,\n                  segments=16, rings=6, loc=(0.0, 0.0, z))\n    tone = "brass" if index % 3 == 0 else "iron"\n    paint(r, colour(tone))\n    return r\n\n\ndef base():\n    plinth = primitive("cube", name="plinth", size=0.4, loc=(0.0, 0.0, 0.1), scale=(1.0, 1.0, 0.5))\n    paint(plinth, colour("stone"))\n    paint_faces(plinth, lambda p: p.center.z > 0.15 and abs(p.normal.z) > 0.9, colour("trim"))\n    return plinth\n\n\ndef lamp(top):\n    cage = primitive("cylinder", name="cage", radius=0.14, depth=0.32, segments=8, loc=(0.0, 0.0, top + 0.16))\n    paint(cage, colour("iron"))\n    limit = top + 0.05\n    paint_faces(cage, lambda p: p.center.z > limit and abs(p.normal.z) < 0.2, colour("glass"))\n    cap = primitive("cone", name="cap", radius=0.18, depth=0.14, segments=8, loc=(0.0, 0.0, top + 0.39))\n    paint(cap, colour("brass"))\n    return [cage, cap]\n\n\ndef build():\n    parts = [base()]\n    post = primitive("cylinder", name="post", radius=0.05, depth=POST_HEIGHT, segments=12,\n                     loc=(0.0, 0.0, 0.2 + POST_HEIGHT / 2))\n    paint(post, colour("iron"))\n    parts.append(post)\n    rings = []\n    for i in range(RING_COUNT):\n        t = i / (RING_COUNT - 1)\n        z = 0.3 + t * (POST_HEIGHT - 0.3)\n        radius = 0.065 + 0.01 * math.sin(t * math.pi * 4)\n        rings.append(ring(i, z, radius))\n    parts += rings\n    parts += lamp(0.2 + POST_HEIGHT)\n    for k in range(4):\n        angle = k * math.pi / 2\n        arm = primitive("cube", name=f"arm_{k}", size=0.03, scale=(4.0, 1.0, 1.0),\n                        loc=(0.12 * math.cos(angle), 0.12 * math.sin(angle), POST_HEIGHT),\n                        rot=(0.0, 0.0, angle))\n        paint(arm, colour("brass"))\n        parts.append(arm)\n    lookups = 0\n    for step in range(2000):\n        for key in ("iron", "brass", "glass", "stone", "trim", "copper"):\n            if key in PALETTE:\n                lookups += 1\n    print("parts", len(parts), "lookups", lookups)\n    join(parts, "lantern_post")\n',
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


@pytest.mark.parametrize("name", sorted(set(SCRIPTS) - {"lantern_post"}))
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


def test_a_40_by_40_grid_with_a_40_entry_palette_lookup_per_cell_records():
    names = ", ".join("'k%d': '#%06x'" % (i, i * 997 % 0xFFFFFF) for i in range(40))
    source = chr(10).join([
        "PALETTE = {" + names + "}",
        "def build():",
        "    verts = []",
        "    faces = []",
        "    hits = 0",
        "    for i in range(41):",
        "        for j in range(41):",
        "            verts += [(i * 0.1, j * 0.1, 0.0)]",
        "            if 'k%d' % (j % 50) in PALETTE:",
        "                hits += 1",
        "    mesh('terrain', verts, faces)",
        ""])
    assert compile_script(source).ops


def test_the_lantern_post_with_tuple_colours_fails_at_record_time_and_the_hex_version_records():
    from sonder_runtime.domain.meshgen.dsl.errors import ScriptError
    with pytest.raises(ScriptError) as info:
        compile_script(SCRIPTS["lantern_post"])
    assert str(info.value).startswith("line ") and "colour" in str(info.value)
    assert compile_script(SCRIPTS["lantern_post_hex"]).ops

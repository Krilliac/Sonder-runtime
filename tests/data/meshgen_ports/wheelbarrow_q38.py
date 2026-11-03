import math
import lf_kit


def build():
    parts = []
    P = lf_kit.PALETTE

    # --- Wheel (front, -Y end, axle along X) ---
    rim = lf_kit.torus("wheel_rim", 0.42, 0.07, segs=14, rsegs=5, hexcol="wood", mat="LF_Toon",
                       rot=(0, 90, 0), loc=(0, -0.75, 0.42))
    lf_kit.wobble(rim, 0.008, 1.5, seed=1)
    parts.append(rim)

    # Wooden spokes (4)
    for i in range(4):
        ang = i * 45.0
        rad = math.radians(ang)
        y0, z0 = -0.75, 0.42
        x1 = 0.38 * math.cos(rad)
        z1 = 0.42 + 0.38 * math.sin(rad)
        spoke = lf_kit.rounded_cyl("spoke%d" % i, 0.035, 0.38, r_top=0.025, round=0.02, segs=6,
                                   bevel_steps=1, hexcol="wood_light", loc=(x1 * 0.5, y0, z0 + (z1 - z0) * 0.5),
                                   rot=(0, 0, ang + 90))
        parts.append(spoke)

    # Brass hub
    hub = lf_kit.rounded_cyl("hub", 0.09, 0.12, r_top=0.08, round=0.03, segs=8, bevel_steps=1,
                             hexcol="brass", mat="LF_ToonMetal", rot=(0, 90, 0), loc=(0, -0.75, 0.42))
    parts.append(hub)

    # --- Tray (deep tapered wooden box, wider at top) ---
    tray_pts = [
        (-0.35, -0.5),
        (0.35, -0.5),
        (0.45, 0.9),
        (-0.45, 0.9),
    ]
    tray = lf_kit.prism("tray", tray_pts, 0.55, bevel=0.04, bevel_segs=2, hexcol="wood",
                        loc=(0, 0, 0.35))
    lf_kit.wobble(tray, 0.015, 1.2, seed=2)
    lf_kit.paint_gradient(tray, P["wood_dark"], P["wood_light"], 0.35, 0.9)
    lf_kit.jitter_colors(tray, 0.04, seed=3)
    parts.append(tray)

    # Tray inner lip
    inner_pts = [
        (-0.30, -0.45),
        (0.30, -0.45),
        (0.40, 0.85),
        (-0.40, 0.85),
    ]
    inner = lf_kit.prism("tray_inner", inner_pts, 0.50, bevel=0.02, bevel_segs=1, hexcol="wood_dark",
                         loc=(0, 0, 0.38))
    parts.append(inner)

    # --- Handles ---
    for side in [-1, 1]:
        x = side * 0.38
        path = [(x, 0.85, 0.55), (x, 1.35, 0.62), (x, 1.75, 0.68)]
        handle = lf_kit.tube("handle_%d" % side, path, radii=0.04, sides=6, round_ends=True,
                             hexcol="wood", mat="LF_Toon")
        lf_kit.wobble(handle, 0.008, 1.5, seed=4 + side)
        parts.append(handle)

    # --- Legs ---
    for side in [-1, 1]:
        x = side * 0.35
        leg = lf_kit.rounded_cyl("leg_%d" % side, 0.04, 0.35, r_top=0.035, round=0.02, segs=6,
                                 bevel_steps=1, hexcol="wood_dark", loc=(x, 0.75, 0.175))
        parts.append(leg)

    # --- Cabbages ---
    cab_positions = [
        (-0.15, -0.1, 0.85),
        (0.12, 0.05, 0.88),
        (-0.05, 0.25, 0.82),
        (0.18, 0.35, 0.86),
        (-0.22, 0.15, 0.84),
    ]
    for i, (cx, cy, cz) in enumerate(cab_positions):
        r = 0.12 + 0.02 * (i % 2)
        cab = lf_kit.sphere("cabbage_%d" % i, r, squash=(1.0, 1.0, 0.85), segs=8, rings=4,
                            hexcol="cabbage", mat="LF_ToonFoliage", loc=(cx, cy, cz))
        lf_kit.paint_gradient(cab, P["cabbage_dark"], P["cabbage"], cz - r, cz + r * 0.5)
        lf_kit.jitter_colors(cab, 0.05, seed=10 + i)
        parts.append(cab)

    lf_kit.fit_height(1.1, parts)

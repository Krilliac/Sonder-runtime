import lf_kit


def build():
    parts = []

    # --- Stump ---
    stump = lf_kit.rounded_cyl("stump", 0.42, 0.30, r_top=0.40, round=0.07, segs=8, bevel_steps=1,
                               hexcol="wood_dark", loc=(0, 0, 0))
    lf_kit.wobble(stump, 0.012, 1.1, seed=5)
    lf_kit.paint_gradient(stump, lf_kit.PALETTE["wood_deep"], lf_kit.PALETTE["wood_dark"], 0.0, 0.30)
    parts.append(stump)

    # cut top (lighter)
    top = lf_kit.rounded_cyl("stump_top", 0.40, 0.05, r_top=0.38, round=0.02, segs=8, bevel_steps=1,
                             hexcol="wood_light", loc=(0, 0, 0.28))
    lf_kit.wobble(top, 0.008, 1.0, seed=6)
    parts.append(top)

    # a couple of flaring roots at the base
    for i, (rx, ry) in enumerate([(0.42, 0.10), (-0.36, -0.18), (0.05, -0.42)]):
        root = lf_kit.rounded_cyl("root%d" % i, 0.10, 0.10, r_top=0.05, round=0.05, segs=6, bevel_steps=1,
                                  hexcol="wood_dark", loc=(rx, ry, 0.02))
        parts.append(root)

    # --- Skep: stacked bulging straw coils ---
    coils = [
        (0.34, 0.14, "hay"),
        (0.33, 0.14, "hay_dark"),
        (0.30, 0.14, "hay"),
        (0.26, 0.14, "hay_dark"),
        (0.21, 0.14, "hay"),
        (0.15, 0.14, "hay_dark"),
    ]
    z = 0.30
    for i, (r, h, col) in enumerate(coils):
        coil = lf_kit.torus("coil%d" % i, r, 0.075, segs=8, rsegs=3, hexcol=col, mat="LF_Toon",
                            loc=(0, 0, z + h / 2))
        lf_kit.wobble(coil, 0.01, 1.3, seed=10 + i)
        parts.append(coil)
        z += h

    # small rounded cap on top
    cap = lf_kit.dome("skep_cap", 0.16, 0.12, segs=8, rings=3, hexcol="hay", loc=(0, 0, z))
    lf_kit.wobble(cap, 0.008, 1.2, seed=20)
    parts.append(cap)

    # entrance hole (flattened dark sphere) at front (-Y) bottom of skep
    hole = lf_kit.sphere("entrance", 0.09, squash=(1.0, 0.5, 1.0), segs=6, rings=3,
                         hexcol="navy", loc=(0, -0.30, 0.42))
    parts.append(hole)

    # --- Bees ---
    for i, (bx, by, bz) in enumerate([(0.12, -0.42, 0.55), (-0.10, -0.46, 0.62), (0.02, -0.50, 0.48)]):
        bee = lf_kit.sphere("bee%d" % i, 0.035, segs=5, rings=2, hexcol="sun", loc=(bx, by, bz))
        parts.append(bee)

    lf_kit.fit_height(1.3, parts)

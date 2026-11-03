import lf_kit


def build():
    parts = []

    # --- Stump Base ---
    stump_body = lf_kit.rounded_cyl("stump_body", 0.45, 0.35, r_top=0.42, round=0.1, segs=8, bevel_steps=2,
                                    hexcol="wood_dark", mat="LF_Toon")
    parts.append(stump_body)

    # Cut top surface: lighter wood color to simulate the cut face
    stump_top = lf_kit.box("stump_top", (0.84, 0.84, 0.02), radius=0.05, hexcol="wood_light", mat="LF_Toon",
                           loc=(0, 0, 0.35))
    parts.append(stump_top)

    # Roots flaring out at the base (the old script fetched each tube with lf.last_obj(); a recorded
    # script just keeps the returned handle)
    root1 = lf_kit.tube("root1", [(0, 0, 0), (0.2, -0.1, -0.05), (0.4, -0.2, -0.1)], radii=0.06, sides=6,
                        round_ends=True, hexcol="wood_dark", mat="LF_Toon", loc=(0, 0, 0))
    root2 = lf_kit.tube("root2", [(0, 0, 0), (-0.15, -0.2, -0.06), (-0.35, -0.3, -0.12)], radii=0.05, sides=6,
                        round_ends=True, hexcol="wood_dark", mat="LF_Toon", loc=(0, 0, 0))
    root3 = lf_kit.tube("root3", [(0, 0, 0), (0.1, 0.25, -0.04), (0.25, 0.4, -0.08)], radii=0.04, sides=6,
                        round_ends=True, hexcol="wood_dark", mat="LF_Toon", loc=(0, 0, 0))
    parts.extend([root1, root2, root3])

    # --- Skep (Beehive): stacked straw coils ---
    coils = [
        {"r": 0.42, "z": 0.35 + 0.1, "h": 0.18, "col": "hay"},
        {"r": 0.39, "z": 0.35 + 0.1 + 0.18, "h": 0.18, "col": "hay_dark"},
        {"r": 0.36, "z": 0.35 + 0.1 + 0.18 * 2, "h": 0.18, "col": "hay"},
        {"r": 0.33, "z": 0.35 + 0.1 + 0.18 * 3, "h": 0.18, "col": "hay_dark"},
        {"r": 0.30, "z": 0.35 + 0.1 + 0.18 * 4, "h": 0.18, "col": "hay"},
    ]

    for i, coil in enumerate(coils):
        center_z = coil["z"] + coil["h"] / 2
        tube_obj = lf_kit.torus("skep_coil_%d" % i, R=coil["r"], r=coil["h"] / 2.5, segs=14, rsegs=6,
                                hexcol=coil["col"], mat="LF_Toon", loc=(0, 0, center_z))
        parts.append(tube_obj)
        lf_kit.wobble(tube_obj, amount=0.015, freq=2.0, seed=i * 10 + 1)

    # Top cap
    cap_z = 1.35 + 0.05
    skep_cap = lf_kit.dome("skep_cap", r=0.28, h=0.15, segs=10, rings=4, hexcol="hay_dark", mat="LF_Toon",
                           loc=(0, 0, cap_z))
    parts.append(skep_cap)

    # Entrance hole at the front (-Y) bottom of the skep
    entrance_z = 0.55
    entrance_y = -0.42
    hole_obj = lf_kit.sphere("entrance_hole", r=0.12, squash=(0.8, 0.5, 0.8), segs=6, rings=4,
                             hexcol="navy", mat="LF_Toon", loc=(0, entrance_y - 0.05, entrance_z))
    parts.append(hole_obj)
    hole_back = lf_kit.box("entrance_back", (0.24, 0.15, 0.05), radius=0.02, hexcol="wood_deep", mat="LF_Toon",
                           loc=(0, entrance_y - 0.1, entrance_z))
    parts.append(hole_back)

    # --- Bees ---
    bee_color = "sun"
    bee1 = lf_kit.sphere("bee1", r=0.03, segs=5, rings=3, hexcol=bee_color, mat="LF_Toon", loc=(0.2, -0.6, 0.8))
    bee2 = lf_kit.sphere("bee2", r=0.025, segs=5, rings=3, hexcol=bee_color, mat="LF_Toon", loc=(-0.15, -0.7, 0.9))
    bee3 = lf_kit.sphere("bee3", r=0.028, segs=5, rings=3, hexcol=bee_color, mat="LF_Toon", loc=(0.1, -0.5, 1.1))
    parts.extend([bee1, bee2, bee3])

    lf_kit.fit_height(1.3, parts)

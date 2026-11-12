import lf_kit


def build():
    parts = []
    P = lf_kit.PALETTE

    # Wheelbarrow wheel (front)
    wheel_radius = 0.35
    wheel = lf_kit.rounded_cyl("wheel", wheel_radius, 0.12, r_top=wheel_radius, round=0.06, segs=12, bevel_steps=2,
                               hexcol="wood", loc=(0, -0.8, 0.1))
    lf_kit.paint_gradient(wheel, P["wood_dark"], P["wood_light"], 0.0, 0.12)
    parts.append(wheel)

    # Wheel hub (brass)
    hub = lf_kit.torus("hub", wheel_radius - 0.03, 0.04, segs=16, rsegs=4, hexcol="brass", mat="LF_ToonMetal",
                       loc=(0, -0.8, 0.1))
    parts.append(hub)

    # Tray (wooden)
    tray_points = [(0.35, 0), (0.45, 0.1), (0.45, 0.6), (0.35, 0.7)]
    tray = lf_kit.prism("tray", tray_points, 0.25, bevel=0.02, hexcol="wood", loc=(0, -0.3, 0.1))
    lf_kit.paint_gradient(tray, P["wood_dark"], P["wood_light"], 0.0, 0.25)
    parts.append(tray)

    # Legs (short wooden legs under tray)
    leg_height = 0.2
    leg_width = 0.06
    leg1 = lf_kit.rounded_cyl("leg1", leg_width, leg_height, r_top=leg_width, round=0.02, segs=8, bevel_steps=1,
                              hexcol="wood", loc=(0.15, -0.6, 0.0))
    leg2 = lf_kit.rounded_cyl("leg2", leg_width, leg_height, r_top=leg_width, round=0.02, segs=8, bevel_steps=1,
                              hexcol="wood", loc=(-0.15, -0.6, 0.0))
    lf_kit.paint_gradient(leg1, P["wood_dark"], P["wood_light"], 0.0, leg_height)
    lf_kit.paint_gradient(leg2, P["wood_dark"], P["wood_light"], 0.0, leg_height)
    parts.append(leg1)
    parts.append(leg2)

    # Handles (long wooden handles)
    handle_length = 0.8
    handle_radius = 0.03
    handle1 = lf_kit.rounded_cyl("handle1", handle_radius, handle_length, r_top=handle_radius, round=0.01, segs=8,
                                 bevel_steps=1, hexcol="wood", loc=(0, 0.6, 0.25))
    handle2 = lf_kit.rounded_cyl("handle2", handle_radius, handle_length, r_top=handle_radius, round=0.01, segs=8,
                                 bevel_steps=1, hexcol="wood", loc=(0, 0.6, 0.25), rot=(0, 0, 180))
    lf_kit.paint_gradient(handle1, P["wood_dark"], P["wood_light"], 0.0, handle_length)
    lf_kit.paint_gradient(handle2, P["wood_dark"], P["wood_light"], 0.0, handle_length)
    parts.append(handle1)
    parts.append(handle2)

    # Cabbages in tray
    cabbage1 = lf_kit.sphere("cabbage1", 0.08, segs=6, rings=4, hexcol="cabbage", mat="LF_ToonFoliage",
                             loc=(0.1, -0.4, 0.25))
    cabbage2 = lf_kit.sphere("cabbage2", 0.09, segs=6, rings=4, hexcol="cabbage_dark", mat="LF_ToonFoliage",
                             loc=(-0.1, -0.3, 0.28))
    cabbage3 = lf_kit.sphere("cabbage3", 0.07, segs=6, rings=4, hexcol="cabbage", mat="LF_ToonFoliage",
                             loc=(0.05, -0.5, 0.3))
    parts.append(cabbage1)
    parts.append(cabbage2)
    parts.append(cabbage3)

    # Lean the whole thing slightly (every part but the hub; the old script read part.name, which a
    # recorded script cannot, so the leaning parts are listed explicitly)
    leaning = [wheel, tray, leg1, leg2, handle1, handle2, cabbage1, cabbage2, cabbage3]
    for part in leaning:
        lf_kit.lean(part, 0.02, "x", 0.0, 1.5)

    # Fit total height to 1.1m
    lf_kit.fit_height(1.1, parts)

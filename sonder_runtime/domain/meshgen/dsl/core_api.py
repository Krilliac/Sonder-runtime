"""The core API digest shown to the model: what plain-name functions exist and the dialect rules.  Pure."""
from __future__ import annotations

from .opschema import CORE_SIGS

CORE_DIGEST = """\
primitive(kind, name="", size=1, radius=1, depth=2, segments=32, rings=16, subdivisions=2, major_radius=1, minor_radius=0.25, loc=(0,0,0), rot=(0,0,0), scale=1)  # kind: cube, uv_sphere, ico_sphere, cylinder, cone, torus, plane, circle, grid; segments<=256, rings<=128, subdivisions<=5
mesh(name, verts, faces, loc=(0,0,0), rot=(0,0,0), scale=1)  # verts [(x,y,z),...], faces [(i,j,k[,l...]),...] (3 to 8 indices each)
transform(obj, loc=None, rot=None, scale=None)  # sets absolute location / rotation (DEGREES) / scale
shade_smooth(obj, smooth=True, angle=None)
modifier(obj, type, **props)  # BEVEL(width, segments<=16, limit_method, angle_limit, profile), SUBSURF(levels<=3), SOLIDIFY(thickness, offset), MIRROR(use_axis, use_clip), ARRAY(count<=64, relative_offset_displace), BOOLEAN(operation, object, solver), DECIMATE(ratio), SCREW(angle, steps<=128, axis), SIMPLE_DEFORM(deform_method, angle, factor), WIREFRAME(thickness), TRIANGULATE, WEIGHTED_NORMAL, EDGE_SPLIT
paint(obj, colour)  # "#RRGGBB" (or a kit palette key)
paint_faces(obj, predicate, colour)  # predicate: lambda p: p.center.z > 0.5  (p.center/p.normal .x .y .z in OBJECT-LOCAL coordinates, p.index, p.area; numbers captured by value)
join(objs, name)  # objs: a list of results
delete(obj)
"""

DIALECT_RULES = """\
- Write ONE `def build():` with no parameters. Only `import math`, `import random` and the kit module may be imported (no bpy, bmesh or mathutils).
- Plain Python: assignments, if/for/while, helper `def`s, list comprehensions, f-strings, `%` formatting, list/str/dict methods, math.*, random.* and the builtins range len min max abs round int float str bool list tuple dict sum enumerate zip reversed sorted any all print.
- No classes, try/except, with, yield, lambda (except the predicate of paint_faces), global, *args/**kwargs, decorators, sets or bit operators.
- Rotations are in DEGREES everywhere, for example rot=(0, 90, 0).
- Kit and core functions only RECORD what to build. Each returns a handle: pass it to other kit/core functions (paint, transform, join, a list of parts) but never inspect it (no indexing, arithmetic, attributes, unpacking or comparisons). There are no measurements: compute positions from the sizes you pass.
- The script is recorded, not run: there is no scene, no bpy.data, no file or network access, and nothing can be read back from Blender.
"""


def core_api_digest() -> str:
    """The core function list (kept in step with ``CORE_SIGS``)."""
    names = {line.split("(", 1)[0] for line in CORE_DIGEST.splitlines() if "(" in line}
    if names != set(CORE_SIGS):
        raise RuntimeError("core API digest is out of step with the op table")
    return CORE_DIGEST.strip()


__all__ = ["CORE_DIGEST", "DIALECT_RULES", "core_api_digest"]

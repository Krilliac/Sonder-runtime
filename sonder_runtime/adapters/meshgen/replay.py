"""Trusted replay of a validated ``ops.json`` inside Blender.  Runs ONLY in Blender, never imported by the host.

The op list was recorded on the host from model-written text (see ``domain/meshgen/dsl/recorder.py``)
and re-validated by the harness with ``opschema`` before this runs.  Nothing here executes model code:
each op names a kit function (from the allow-list the job carries) or one of the core ops below, and
its arguments are plain JSON data (numbers, strings, arrays, references to earlier results and
declarative predicates).  Hostile *data* is bounded by the caps in this file and by the AppContainer.
Rotations are in degrees.
"""
import importlib
import math

bpy = importlib.import_module("bpy")
opschema = importlib.import_module("opschema")

MAX_COORD = 100000.0
MAX_VERTS = 200000
MAX_FACES = 400000
PRIMITIVES = ("cube", "uv_sphere", "ico_sphere", "cylinder", "cone", "torus", "plane", "circle", "grid")
# modifier type -> {property: (kind, low, high)}; kinds: float int angle(degrees) enum bool bools3 vec3 object
MODIFIERS = {
    "BEVEL": {"width": ("float", 0.0, 10.0), "segments": ("int", 1, 16), "limit_method": ("enum",),
              "angle_limit": ("angle", 0.0, 180.0), "profile": ("float", 0.0, 1.0)},
    "SUBSURF": {"levels": ("int", 0, 3)},
    "SOLIDIFY": {"thickness": ("float", -10.0, 10.0), "offset": ("float", -1.0, 1.0)},
    "MIRROR": {"use_axis": ("bools3",), "use_clip": ("bool",)},
    "ARRAY": {"count": ("int", 1, 64), "relative_offset_displace": ("vec3", -100.0, 100.0)},
    "BOOLEAN": {"operation": ("enum",), "object": ("object",), "solver": ("enum",)},
    "DECIMATE": {"ratio": ("float", 0.0, 1.0)},
    "SCREW": {"angle": ("angle", -3600.0, 3600.0), "steps": ("int", 1, 128), "axis": ("enum",)},
    "SIMPLE_DEFORM": {"deform_method": ("enum",), "angle": ("angle", -3600.0, 3600.0), "factor": ("float", -100.0, 100.0)},
    "WIREFRAME": {"thickness": ("float", 0.0, 10.0)},
    "TRIANGULATE": {},
    "WEIGHTED_NORMAL": {},
    "EDGE_SPLIT": {},
}


class ReplayError(Exception):
    """A recorded op failed in Blender (the message names the script line)."""


# ---------------------------------------------------------------------------------------------- values
def _number(value, name, low=-MAX_COORD, high=MAX_COORD):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a number" % name)
    if not low <= value <= high:
        raise ValueError("%s must be between %s and %s" % (name, low, high))
    return float(value)


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("%s must be a whole number between %d and %d" % (name, low, high))
    return value


def _vec3(value, name, default=None, low=-MAX_COORD, high=MAX_COORD):
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value, value, value]
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("%s must be three numbers" % name)
    return tuple(_number(item, name, low, high) for item in value)


def _text(value, name, default=""):
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % name)
    return value[:63]


def _object(value, name):
    if not isinstance(value, bpy.types.Object) or value.type != "MESH":
        raise ValueError("%s must be a mesh object made by an earlier kit/core call" % name)
    return value


def _radians3(vec):
    return tuple(math.radians(item) for item in vec)


# ---------------------------------------------------------------------------------------------- colour
def _hex_rgb(text, kit):
    text = str(text).strip()
    palette = getattr(kit, "PALETTE", None) if kit is not None else None
    if isinstance(palette, dict) and text in palette:
        text = str(palette[text])
    text = text.lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError("colour must be #RRGGBB or a kit palette name")
    return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _colour_attribute(obj):
    mesh = obj.data
    attr = mesh.color_attributes.get("Col")
    if attr is None:
        attr = mesh.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
        attr.data.foreach_set("color_srgb", [0.69, 0.69, 0.69, 1.0] * len(attr.data))
    mesh.color_attributes.active_color = attr
    mesh.color_attributes.render_color_index = mesh.color_attributes.find("Col")
    return attr


def _write_corners(obj, corners):
    attr = _colour_attribute(obj)
    flat = []
    for r, g, b in corners:
        flat.extend((r, g, b, 1.0))
    attr.data.foreach_set("color_srgb", flat)
    obj.data.update()


# ---------------------------------------------------------------------------------------------- predicates
def eval_pred(node, poly):
    """Evaluate a validated predicate tree for one polygon (no eval; a small recursive interpreter)."""
    if isinstance(node, (bool, int, float)):
        return node
    head = node[0]
    if head == "s":
        return node[1]
    if head == "f":
        if node[1] == "center":
            return getattr(poly.center, node[2])
        if node[1] == "normal":
            return getattr(poly.normal, node[2])
        return {"index": poly.index, "area": poly.area, "material_index": poly.material_index}[node[1]]
    rest = node[1:]
    if head == "and":
        value = True
        for item in rest:
            value = eval_pred(item, poly)
            if not value:
                return value
        return value
    if head == "or":
        value = False
        for item in rest:
            value = eval_pred(item, poly)
            if value:
                return value
        return value
    if head == "if":
        return eval_pred(rest[1] if eval_pred(rest[0], poly) else rest[2], poly)
    if head == "call":
        values = [eval_pred(item, poly) for item in rest[1:]]
        name = rest[0]
        if name == "abs":
            return abs(*values)
        if name == "min":
            return min(*values)
        if name == "max":
            return max(*values)
        if name == "round":
            return round(*values)
        return getattr(math, name[5:])(*values)
    values = [eval_pred(item, poly) for item in rest]
    if head == "neg":
        return -values[0]
    if head == "pos":
        return +values[0]
    if head == "not":
        return not values[0]
    a, b = values
    return {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b, "/": lambda: a / b, "//": lambda: a // b,
            "%": lambda: a % b, "**": lambda: a ** b, "<": lambda: a < b, "<=": lambda: a <= b, ">": lambda: a > b,
            ">=": lambda: a >= b, "==": lambda: a == b, "!=": lambda: a != b}[head]()


def _predicate(tree):
    return lambda poly: bool(eval_pred(tree, poly))


# ---------------------------------------------------------------------------------------------- core ops
def _place(obj, name, loc, rot, scale):
    if name:
        obj.name = name
        obj.data.name = name
    obj.location = loc
    obj.rotation_euler = _radians3(rot)
    obj.scale = scale
    return obj


def core_primitive(kit, kind, name="", size=1, radius=1, depth=2, segments=32, rings=16, subdivisions=2,
                   major_radius=1, minor_radius=0.25, loc=None, rot=None, scale=None):
    if kind not in PRIMITIVES:
        raise ValueError("kind must be one of: %s" % ", ".join(PRIMITIVES))
    size = _number(size, "size", 0.0001, MAX_COORD)
    radius = _number(radius, "radius", 0.0001, MAX_COORD)
    depth = _number(depth, "depth", 0.0001, MAX_COORD)
    segments = _integer(segments, "segments", 3, 256)
    rings = _integer(rings, "rings", 2, 128)
    subdivisions = _integer(subdivisions, "subdivisions", 1, 5)
    major_radius = _number(major_radius, "major_radius", 0.0001, MAX_COORD)
    minor_radius = _number(minor_radius, "minor_radius", 0.0001, MAX_COORD)
    loc = _vec3(loc, "loc", (0.0, 0.0, 0.0))
    rot = _vec3(rot, "rot", (0.0, 0.0, 0.0), -3600.0, 3600.0)
    scale = _vec3(scale, "scale", (1.0, 1.0, 1.0))
    ops = bpy.ops.mesh
    if kind == "cube":
        ops.primitive_cube_add(size=size)
    elif kind == "uv_sphere":
        ops.primitive_uv_sphere_add(segments=segments, ring_count=rings, radius=radius)
    elif kind == "ico_sphere":
        ops.primitive_ico_sphere_add(subdivisions=subdivisions, radius=radius)
    elif kind == "cylinder":
        ops.primitive_cylinder_add(vertices=segments, radius=radius, depth=depth)
    elif kind == "cone":
        ops.primitive_cone_add(vertices=segments, radius1=radius, radius2=0.0, depth=depth)
    elif kind == "torus":
        ops.primitive_torus_add(major_segments=segments, minor_segments=max(3, min(rings, 64)),
                                major_radius=major_radius, minor_radius=minor_radius)
    elif kind == "plane":
        ops.primitive_plane_add(size=size)
    elif kind == "circle":
        ops.primitive_circle_add(vertices=segments, radius=radius, fill_type="NGON")
    else:
        ops.primitive_grid_add(x_subdivisions=max(1, min(segments, 64)), y_subdivisions=max(1, min(rings, 64)), size=size)
    return _place(bpy.context.active_object, _text(name, "name"), loc, rot, scale)


def core_mesh(kit, name, verts, faces, loc=None, rot=None, scale=None):
    if not isinstance(verts, (list, tuple)) or not 1 <= len(verts) <= MAX_VERTS:
        raise ValueError("verts must be a list of 1 to %d points" % MAX_VERTS)
    if not isinstance(faces, (list, tuple)) or not 1 <= len(faces) <= MAX_FACES:
        raise ValueError("faces must be a list of 1 to %d faces" % MAX_FACES)
    points = [_vec3(point, "vertex") for point in verts]
    count = len(points)
    polygons = []
    for face in faces:
        if not isinstance(face, (list, tuple)) or not 3 <= len(face) <= 8:
            raise ValueError("each face needs 3 to 8 vertex indices")
        polygons.append(tuple(_integer(index, "face index", 0, count - 1) for index in face))
    name = _text(name, "name", "mesh")
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(points, [], polygons)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = _vec3(loc, "loc", (0.0, 0.0, 0.0))
    obj.rotation_euler = _radians3(_vec3(rot, "rot", (0.0, 0.0, 0.0), -3600.0, 3600.0))
    obj.scale = _vec3(scale, "scale", (1.0, 1.0, 1.0))
    return obj


def core_transform(kit, obj, loc=None, rot=None, scale=None):
    obj = _object(obj, "obj")
    if loc is not None:
        obj.location = _vec3(loc, "loc")
    if rot is not None:
        obj.rotation_euler = _radians3(_vec3(rot, "rot", None, -3600.0, 3600.0))
    if scale is not None:
        obj.scale = _vec3(scale, "scale")
    return obj


def _override(obj):
    return bpy.context.temp_override(active_object=obj, object=obj, selected_objects=[obj],
                                     selected_editable_objects=[obj])


def core_shade_smooth(kit, obj, smooth=True, angle=None):
    obj = _object(obj, "obj")
    mesh = obj.data
    if angle is not None:
        with _override(obj):
            bpy.ops.object.shade_smooth_by_angle(angle=math.radians(_number(angle, "angle", 0.0, 180.0)))
        return obj
    mesh.polygons.foreach_set("use_smooth", [bool(smooth)] * len(mesh.polygons))
    mesh.update()
    return obj


def core_modifier(kit, obj, type, **props):
    obj = _object(obj, "obj")
    if type not in MODIFIERS:
        raise ValueError("unsupported modifier type %r; supported: %s" % (str(type)[:30], ", ".join(sorted(MODIFIERS))))
    table = MODIFIERS[type]
    modifier = obj.modifiers.new(name=type.title(), type=type)
    for key, value in props.items():
        if key not in table:
            raise ValueError("%s has no property %r; allowed: %s" % (type, key[:30], ", ".join(sorted(table)) or "none"))
        kind = table[key]
        if kind[0] == "float":
            setattr(modifier, key, _number(value, key, kind[1], kind[2]))
        elif kind[0] == "int":
            setattr(modifier, key, _integer(value, key, kind[1], kind[2]))
        elif kind[0] == "angle":
            setattr(modifier, key, math.radians(_number(value, key, kind[1], kind[2])))
        elif kind[0] == "bool":
            setattr(modifier, key, bool(value))
        elif kind[0] == "bools3":
            if not isinstance(value, (list, tuple)) or len(value) != 3:
                raise ValueError("%s must be three true/false values" % key)
            setattr(modifier, key, tuple(bool(item) for item in value))
        elif kind[0] == "vec3":
            setattr(modifier, key, _vec3(value, key, None, kind[1], kind[2]))
        elif kind[0] == "object":
            setattr(modifier, key, _object(value, key))
        else:
            items = {item.identifier for item in modifier.bl_rna.properties[key].enum_items}
            if value not in items:
                raise ValueError("%s must be one of: %s" % (key, ", ".join(sorted(items))))
            setattr(modifier, key, value)
    if type == "SUBSURF" and "levels" in props:
        modifier.render_levels = modifier.levels
    return obj


def core_paint(kit, obj, colour):
    obj = _object(obj, "obj")
    rgb = _hex_rgb(colour, kit)
    _write_corners(obj, [rgb] * len(obj.data.loops))
    return obj


def core_paint_faces(kit, obj, predicate, colour):
    obj = _object(obj, "obj")
    if not callable(predicate):
        raise ValueError("predicate must be a lambda")
    rgb = _hex_rgb(colour, kit)
    attr = _colour_attribute(obj)
    flat = [0.0] * (len(attr.data) * 4)
    attr.data.foreach_get("color_srgb", flat)
    corners = [(flat[i * 4], flat[i * 4 + 1], flat[i * 4 + 2]) for i in range(len(attr.data))]
    for poly in obj.data.polygons:
        if predicate(poly):
            for loop in poly.loop_indices:
                corners[loop] = rgb
    _write_corners(obj, corners)
    return obj


def _apply(obj):
    """Bake modifiers and the object transform into the mesh (keeps colour data)."""
    if obj.modifiers:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        baked = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), preserve_all_data_layers=True,
                                                depsgraph=depsgraph)
        obj.modifiers.clear()
        obj.data = baked
    obj.data.transform(obj.matrix_basis)
    obj.matrix_basis.identity()
    obj.data.update()
    _colour_attribute(obj)


def core_join(kit, objs, name):
    if not isinstance(objs, (list, tuple)) or not 1 <= len(objs) <= opschema.MAX_OPS:
        raise ValueError("objs must be a list of results from earlier kit/core calls")
    objs = [_object(item, "objs[%d]" % index) for index, item in enumerate(objs)]
    for obj in objs:
        _apply(obj)
    first = objs[0]
    if len(objs) > 1:
        bpy.context.view_layer.update()
        with bpy.context.temp_override(active_object=first, object=first, selected_objects=objs,
                                       selected_editable_objects=objs):
            bpy.ops.object.join()
    name = _text(name, "name", "joined")
    first.name = name
    first.data.name = name
    return first


def core_delete(kit, obj):
    obj = _object(obj, "obj")
    bpy.data.objects.remove(obj, do_unlink=True)
    return None


CORE = {
    "primitive": core_primitive, "mesh": core_mesh, "transform": core_transform, "shade_smooth": core_shade_smooth,
    "modifier": core_modifier, "paint": core_paint, "paint_faces": core_paint_faces, "join": core_join,
    "delete": core_delete,
}


# ---------------------------------------------------------------------------------------------- driver
def _resolve(value, store):
    if isinstance(value, list):
        return [_resolve(item, store) for item in value]
    if isinstance(value, dict):
        if "$ref" in value:
            return store[value["$ref"]]
        return _predicate(value["$pred"])
    return value


def run(doc, kit_name, kit_allowed):
    """Replay every op in order; raises ``ReplayError`` naming the script line on the first failure."""
    kit = importlib.import_module(kit_name) if kit_name else None
    allowed = frozenset(kit_allowed)
    store = {}
    for op in doc["ops"]:
        kind, _, short = op["op"].partition(".")
        try:
            args = [_resolve(item, store) for item in op["args"]]
            kwargs = {key: _resolve(item, store) for key, item in op["kwargs"].items()}
            if kind == "core":
                result = CORE[short](kit, *args, **kwargs)
            elif kind == "kit" and kit is not None and short in allowed:
                result = getattr(kit, short)(*args, **kwargs)
            else:
                raise ValueError("op not permitted")
        except Exception as exc:  # noqa: BLE001 - every failure becomes a line-numbered error for the repair loop
            label = ("%s.%s" % (kit_name, short)) if kind == "kit" else short
            raise ReplayError("line %d: %s(...) failed in Blender: %s: %s"
                              % (op["line"], label, exc.__class__.__name__, str(exc)[:300])) from None
        store[op["id"]] = result
    return len(store)

"""Blender-side harness for mesh generation.  Runs INSIDE Blender, never imported.

Invoked as ``blender -b --factory-startup --disable-autoexec --python harness.py -- job.json``.
The job file is JSON: code_path, out_dir, name, kit_path, max_tris, height_m, export,
render.  The harness builds the candidate script with restricted builtins, joins the
parts into one mesh, normalises size/placement, exports GLB or FBX, renders a 2x2 sheet
and prints one ``MESHGEN_RESULT {json}`` line.

This is defence in depth alongside the AST gate, not a sandbox.
"""
import json
import math
import os
import sys
import traceback

import importlib

# Resolved by name: these modules exist only inside Blender, and the layering gate
# parses ``import`` statements, which would otherwise flag this Blender-only file.
bpy = importlib.import_module("bpy")
mathutils = importlib.import_module("mathutils")

ALLOWED_ROOTS = {"bpy", "bmesh", "mathutils", "math", "random"}
RENDER_PX = 512
DEFAULT_GREY = (0.69, 0.69, 0.69)


RESULT_PATH = ""
PARTS = []


def _emit(payload):
    """Write the result where the runner told us to; stdout is never trusted by the runner."""
    if not RESULT_PATH:
        return
    tmp = RESULT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, RESULT_PATH)


def _neuter_script_execution():
    """Disable the two Python entry points that run text blocks (belt and braces).

    The AST gate already refuses these names; this keeps a script that slipped past it from
    executing a Text datablock with unrestricted builtins.
    """
    def refuse(*_args, **_kwargs):
        raise RuntimeError("script execution is disabled by the mesh harness")

    bpy.utils.execfile = refuse
    bpy.types.Text.as_module = refuse


def _job():
    argv = sys.argv
    if "--" not in argv:
        raise RuntimeError("job file argument missing")
    with open(argv[argv.index("--") + 1], "r", encoding="utf-8") as handle:
        return json.load(handle)


# ``paint`` is handed to untrusted scripts, and a function's ``__globals__`` is reachable by
# reflection.  It is therefore built by exec into a fresh namespace holding nothing but a
# handful of harmless builtins: no os, sys, json, bpy or this module.
_PAINT_SOURCE = """
def paint(obj, hex_colour):
    "Give a mesh object a flat vertex colour (byte colour corner attribute Col)."
    if obj.type != "MESH":
        raise ValueError("paint() needs a mesh object")
    text = str(hex_colour).strip().lstrip("#")
    if len(text) == 3:
        text = "".join([ch * 2 for ch in text])
    if len(text) != 6:
        raise ValueError("colour must be #RRGGBB")
    linear = []
    for i in range(0, 6, 2):
        c = int(text[i:i + 2], 16) / 255.0
        linear.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    mesh = obj.data
    attr = mesh.color_attributes.get("Col")
    if attr is None:
        attr = mesh.color_attributes.new(name="Col", type="BYTE_COLOR", domain="CORNER")
    attr.data.foreach_set("color", [linear[0], linear[1], linear[2], 1.0] * len(attr.data))
    mesh.color_attributes.active_color = attr
    mesh.color_attributes.render_color_index = mesh.color_attributes.find("Col")
    mesh.update()
"""


def _make_paint():
    import builtins

    namespace = {"__builtins__": {name: getattr(builtins, name)
                                  for name in ("str", "len", "int", "range", "ValueError")}}
    exec(compile(_PAINT_SOURCE, "meshgen_paint", "exec"), namespace)
    return namespace["paint"]


paint = _make_paint()


def _safe_builtins(allowed):
    import builtins

    names = (
        "abs all any bool dict enumerate float int isinstance len list map max min pow print "
        "range reversed round set sorted str sum tuple zip ValueError Exception __build_class__"
    ).split()
    safe = {name: getattr(builtins, name) for name in names}
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or name.split(".")[0] not in allowed:
            raise ImportError("import of %r is not allowed" % (name,))
        return real_import(name, globals, locals, fromlist, level)

    safe["__import__"] = guarded_import
    return safe


def _stub_excluded_kit_names(module_name, excluded):
    """Defence in depth: replace kit *functions* the policy excludes (file/export/render helpers).

    The AST gate already refuses them; if a script reached one anyway (a gate bug, a reflection
    trick) it hits a stub instead of code that writes files.  Excluded names are, by
    construction, not used by the names that stay exposed.
    """
    module = importlib.import_module(module_name)

    def blocked(*_args, **_kwargs):
        raise RuntimeError("this kit function is not available in mesh_generate; the harness exports and renders")

    for attr in excluded:
        if not hasattr(module, attr):
            continue
        if callable(getattr(module, attr)):  # constants stay: kit primitives read them (e.g. a colour table)
            setattr(module, attr, blocked)


def _clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mesh in list(bpy.data.meshes):
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def _bake_and_join(name):
    view_layer = bpy.context.view_layer
    view_layer.update()
    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objs:
        raise RuntimeError("build() created no mesh objects")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in objs:
        matrix = obj.matrix_world.copy()
        evaluated = obj.evaluated_get(depsgraph)
        baked = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
        obj.modifiers.clear()
        obj.data = baked
        obj.parent = None
        obj.matrix_basis = mathutils.Matrix.Identity(4)
        baked.transform(matrix)
        if matrix.determinant() < 0:
            baked.flip_normals()
        if baked.color_attributes.get("Col") is None:
            attr = baked.color_attributes.new(name="Col", type="BYTE_COLOR", domain="CORNER")
            attr.data.foreach_set("color", [*DEFAULT_GREY, 1.0] * len(attr.data))
        baked.color_attributes.active_color = baked.color_attributes.get("Col")
        baked.update()
        PARTS.append({"name": str(obj.name)[:40], "tris": sum(len(poly.vertices) - 2 for poly in baked.polygons)})
    view_layer.update()
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objs:
        obj.select_set(True)
    view_layer.objects.active = objs[0]
    if len(objs) > 1:
        with bpy.context.temp_override(active_object=objs[0], object=objs[0], selected_objects=objs,
                                       selected_editable_objects=objs):
            bpy.ops.object.join()
    joined = view_layer.objects.active
    joined.name = name
    joined.data.name = name
    return joined


def _vertices(mesh):
    flat = [0.0] * (len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", flat)
    return flat


def _extent(flat):
    xs, ys, zs = flat[0::3], flat[1::3], flat[2::3]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _normalise(obj, height_m):
    mesh = obj.data
    if not mesh.vertices:
        raise RuntimeError("the joined mesh has no vertices")
    lo, hi = _extent(_vertices(mesh))
    scale = 1.0
    if height_m and height_m > 0 and hi[2] - lo[2] > 1e-9:
        scale = height_m / (hi[2] - lo[2])
    cx, cy = (lo[0] + hi[0]) / 2.0 * scale, (lo[1] + hi[1]) / 2.0 * scale
    matrix = mathutils.Matrix.Translation((-cx, -cy, -lo[2] * scale)) @ mathutils.Matrix.Scale(scale, 4)
    mesh.transform(matrix)
    mesh.update()
    bpy.context.view_layer.update()
    lo, hi = _extent(_vertices(mesh))
    return [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]], lo, hi


def _export(obj, path, kind):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    if kind == "fbx":
        bpy.ops.export_scene.fbx(filepath=path, use_selection=True, add_leaf_bones=False)
        return
    props = bpy.ops.export_scene.gltf.get_rna_type().properties
    kwargs = {"filepath": path, "export_format": "GLB", "use_selection": True, "export_yup": True,
              "export_apply": False}
    if "export_vertex_color" in props:
        items = {item.identifier for item in props["export_vertex_color"].enum_items}
        kwargs["export_vertex_color"] = "ACTIVE" if "ACTIVE" in items else "MATERIAL"
    if "export_active_vertex_color_when_no_material" in props:
        kwargs["export_active_vertex_color_when_no_material"] = True
    bpy.ops.export_scene.gltf(**kwargs)


def _setup_render(scene):
    try:
        scene.render.engine = "BLENDER_WORKBENCH"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = RENDER_PX
    scene.render.resolution_y = RENDER_PX
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.view_settings.view_transform = "Standard"
    shading = scene.display.shading
    shading.light = "FLAT"
    shading.color_type = "VERTEX"
    shading.show_object_outline = True
    shading.object_outline_color = (0.05, 0.05, 0.08)
    world = bpy.data.worlds.new("meshgen_world")
    world.color = (0.86, 0.88, 0.92)
    scene.world = world
    cam_data = bpy.data.cameras.new("meshgen_cam")
    cam_data.type = "ORTHO"
    cam = bpy.data.objects.new("meshgen_cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    return cam, cam_data


def _render_views(obj, out_dir, name, size, lo, hi):
    scene = bpy.context.scene
    cam, cam_data = _setup_render(scene)
    centre = mathutils.Vector(((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2))
    span = max(size) or 1.0
    dist = span * 4.0 + 2.0
    diag = math.sqrt(sum(s * s for s in size)) or span
    views = [
        ("front", centre + mathutils.Vector((0, -dist, 0)), mathutils.Euler((math.pi / 2, 0, 0)), None, span),
        ("right", centre + mathutils.Vector((dist, 0, 0)), mathutils.Euler((math.pi / 2, 0, math.pi / 2)), None, span),
        ("quarter", None, None, mathutils.Vector((1.0, -1.0, 0.8)), diag),
        ("top", centre + mathutils.Vector((0, 0, dist)), mathutils.Euler((0, 0, 0)), None, span),
    ]
    paths = {}
    for key, position, euler, direction, extent in views:
        cam_data.ortho_scale = extent * 1.25
        if direction is not None:
            direction = direction.normalized()
            cam.location = centre + direction * dist
            cam.rotation_euler = direction.to_track_quat("Z", "Y").to_euler()
        else:
            cam.location = position
            cam.rotation_euler = euler
        bpy.context.view_layer.update()
        path = os.path.join(out_dir, "%s_view_%s.png" % (name, key))
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        paths[key] = path
    return paths


def _compose_sheet(paths, sheet_path, single_path, keep_dir_name):
    side = RENDER_PX
    sheet = bpy.data.images.new("meshgen_sheet", 2 * side, 2 * side, alpha=False)
    sheet.colorspace_settings.name = "sRGB"
    buffer = [1.0] * (4 * 2 * side * 2 * side)
    # Image rows run bottom-up: the top row of cells starts at y = side.
    layout = {"front": (0, side), "right": (side, side), "quarter": (0, 0), "top": (side, 0)}
    for key, (cx, cy) in layout.items():
        img = bpy.data.images.load(paths[key])
        img.colorspace_settings.name = "sRGB"
        if img.size[0] != side or img.size[1] != side:
            raise RuntimeError("unexpected render size %r" % (tuple(img.size),))
        pixels = list(img.pixels)
        for row in range(side):
            src = pixels[row * side * 4:(row + 1) * side * 4]
            dst = ((cy + row) * 2 * side + cx) * 4
            buffer[dst:dst + side * 4] = src
        if key == "quarter":
            img.filepath_raw = single_path
            img.file_format = "PNG"
            img.save()
        bpy.data.images.remove(img)
    sheet.pixels.foreach_set(buffer)
    sheet.filepath_raw = sheet_path
    sheet.file_format = "PNG"
    sheet.save()
    for path in paths.values():
        try:
            os.remove(path)
        except OSError:
            pass


def main():
    global RESULT_PATH
    job = _job()
    RESULT_PATH = job["result_path"]
    _neuter_script_execution()
    out_dir = job["out_dir"]
    name = job["name"]
    os.makedirs(out_dir, exist_ok=True)
    kit_path = job.get("kit_path") or ""
    allowed = set(ALLOWED_ROOTS)
    # Test hook: the job file is written by the host, never by the model.  Tests use it to hand a
    # script a module (winreg) the gate would refuse, to prove the secret guard still holds.
    allowed.update(str(root) for root in (job.get("extra_import_roots") or []))
    if kit_path:
        sys.path.insert(0, os.path.dirname(os.path.abspath(kit_path)))
        allowed.add(os.path.splitext(os.path.basename(kit_path))[0])
    if kit_path:
        _stub_excluded_kit_names(os.path.splitext(os.path.basename(kit_path))[0], job.get("kit_excluded") or [])
    with open(job["code_path"], "r", encoding="utf-8") as handle:
        source = handle.read()
    _clear_scene()
    env = {"__builtins__": _safe_builtins(allowed), "__name__": "meshgen_candidate", "paint": paint}
    exec(compile(source, job["code_path"], "exec"), env)
    build = env.get("build")
    if not callable(build):
        raise RuntimeError("the script does not define build()")
    build()
    obj = _bake_and_join(name)
    size, lo, hi = _normalise(obj, float(job.get("height_m") or 0.0))
    mesh = obj.data
    tris = sum(len(poly.vertices) - 2 for poly in mesh.polygons)
    kind = "fbx" if str(job.get("export", "glb")).lower() == "fbx" else "glb"
    model_path = os.path.join(out_dir, "%s.%s" % (name, kind))
    _export(obj, model_path, kind)
    files = {"model": model_path}
    result = {"ok": True, "tris": tris, "within_budget": tris <= int(job.get("max_tris") or 0),
              "size": [round(v, 4) for v in size], "files": files,
              "parts": sorted(PARTS, key=lambda part: -part["tris"])[:8]}
    if job.get("render", True):
        try:
            paths = _render_views(obj, out_dir, name, size, lo, hi)
            sheet_path = os.path.join(out_dir, "%s_sheet.png" % name)
            single_path = os.path.join(out_dir, "%s_view.png" % name)
            _compose_sheet(paths, sheet_path, single_path, name)
            files["sheet"] = sheet_path
            files["view"] = single_path
        except Exception as exc:  # the model exported; a render problem must not discard it
            result["render_error"] = "%s: %s" % (type(exc).__name__, exc)
    _emit(result)


try:
    main()
except BaseException as exc:  # noqa: BLE001 - every failure must come back as a result line
    tail = traceback.format_exc().strip().splitlines()[-6:]
    _emit({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "traceback": tail})

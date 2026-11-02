"""Blender-side harness for mesh generation.  Runs INSIDE Blender, never imported.

Invoked as ``blender -b --factory-startup --disable-autoexec --python harness.py -- job.json``.
The job file is JSON: ops_path, kit_allowed, out_dir, name, kit_path, max_tris, height_m, export, render,
result_path.  No model-written code runs here: the host recorded the script into ``ops.json`` (a
JSON op list); this harness validates that file against ``opschema`` (fail closed: anything outside
the grammar, an unknown op or a bad reference refuses the whole build), has ``replay.py`` replay the
ops with trusted code, then bakes and joins the parts, normalises size/placement, exports GLB or FBX,
renders a 2x2 sheet and writes the result file.
"""
import json
import math
import os
import sys
import time
import traceback

import importlib

# Resolved by name: these modules exist only inside Blender, and the layering gate
# parses ``import`` statements, which would otherwise flag this Blender-only file.
bpy = importlib.import_module("bpy")
mathutils = importlib.import_module("mathutils")
importlib.import_module("mathutils.bvhtree")
connectivity = None  # set by main(): the sibling module, importable once sys.path includes this folder

RENDER_PX = 512
DEFAULT_GREY = (0.69, 0.69, 0.69)


RESULT_PATH = ""
PARTS = []
GEOM_BUDGET_S = 5.0   # wall time the floating-part check may use before it is skipped
GEOM_PARTS = []   # per baked part: name, creating script line, detached flag, world-space vertices and triangles


def _emit(payload):
    """Write the result where the runner told us to; stdout is never trusted by the runner."""
    if not RESULT_PATH:
        return
    tmp = RESULT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, RESULT_PATH)


def _job():
    argv = sys.argv
    if "--" not in argv:
        raise RuntimeError("job file argument missing")
    with open(argv[argv.index("--") + 1], "r", encoding="utf-8") as handle:
        return json.load(handle)


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
        GEOM_PARTS.append(_capture_part(obj, baked))
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


def _capture_part(obj, baked):
    """Copy what the floating-part check needs from one baked part (its vertices are already in world space)."""
    flat = _vertices(baked)
    baked.calc_loop_triangles()
    triangles = baked.loop_triangles
    indices = [0] * (len(triangles) * 3)
    triangles.foreach_get("vertices", indices)
    line = obj.get("meshgen_line", 0)
    return {"name": str(obj.name), "line": line if isinstance(line, int) else 0,
            "detached": bool(obj.get("meshgen_detached", 0)), "verts": flat, "tris": indices}


def _vertices(mesh):
    flat = [0.0] * (len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", flat)
    return flat


def _extent(flat):
    xs, ys, zs = flat[0::3], flat[1::3], flat[2::3]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _normal_transform(lo, hi, height_m):
    """Uniform scale and translation that size the build to ``height_m``, centre it on X/Y and stand it on Z=0."""
    scale = 1.0
    if height_m and height_m > 0 and hi[2] - lo[2] > 1e-9:
        scale = height_m / (hi[2] - lo[2])
    cx, cy = (lo[0] + hi[0]) / 2.0 * scale, (lo[1] + hi[1]) / 2.0 * scale
    return scale, (-cx, -cy, -lo[2] * scale)


def _normalise(obj, height_m):
    mesh = obj.data
    if not mesh.vertices:
        raise RuntimeError("the joined mesh has no vertices")
    lo, hi = _extent(_vertices(mesh))
    scale, shift = _normal_transform(lo, hi, height_m)
    matrix = mathutils.Matrix.Translation(shift) @ mathutils.Matrix.Scale(scale, 4)
    mesh.transform(matrix)
    mesh.update()
    bpy.context.view_layer.update()
    lo, hi = _extent(_vertices(mesh))
    return [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]], lo, hi


def _part_view(part, scale, shift):
    """One part in final (normalised) space: points, bounding box and, built on demand, a BVH tree."""
    flat = part["verts"]
    points = [(flat[i] * scale + shift[0], flat[i + 1] * scale + shift[1], flat[i + 2] * scale + shift[2])
              for i in range(0, len(flat), 3)]
    triangles = [tuple(part["tris"][i:i + 3]) for i in range(0, len(part["tris"]), 3)]
    box = None
    if points:
        xs, ys, zs = [p[0] for p in points], [p[1] for p in points], [p[2] for p in points]
        box = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
    return {"points": points, "triangles": triangles, "bvh": None, "tri_boxes": None, "name": part["name"],
            "line": part["line"], "detached": part["detached"], "box": box}


def _bvh(view):
    if view["bvh"] is None:
        view["bvh"] = mathutils.bvhtree.BVHTree.FromPolygons(view["points"], view["triangles"], all_triangles=True)
    return view["bvh"]


class _Skipped(Exception):
    """The floating-part check was skipped on purpose (too many parts, out of time); the message says why."""


def _nearest(source, target):
    """Smallest distance from a bounded sample of ``source``'s vertices to ``target``'s surface.  Returns
    ``(distance, point_on_source, point_on_target)`` or ``None``."""
    bvh = _bvh(target)
    best = None
    for index in connectivity.sample_indices(len(source["points"])):
        point = source["points"][index]
        hit = bvh.find_nearest(point)
        if hit[0] is None:
            continue
        if best is None or hit[3] < best[0]:
            best = (hit[3], point, tuple(hit[0]))
    if best is None:
        return None
    # the vertices only bound the distance: anything closer lies in the other part's box grown by that bound, so
    # sample the source surface there too (a flush or edge contact has no vertex at the closest point)
    if source["tri_boxes"] is None:
        source["tri_boxes"] = connectivity.triangle_boxes(source["points"], source["triangles"])
    for point in connectivity.contact_samples(source["points"], source["triangles"], target["box"], best[0],
                                              tri_boxes=source["tri_boxes"]):
        hit = bvh.find_nearest(point)
        if hit[0] is not None and hit[3] < best[0]:
            best = (hit[3], point, tuple(hit[0]))
    return best


def _inside(bvh, point):
    """True when ``point`` is inside the closed mesh of ``bvh`` (odd number of ray crossings)."""
    direction = mathutils.Vector((0.5773, 0.3711, 0.7273)).normalized()
    origin, crossings = mathutils.Vector(point), 0
    for _ in range(64):
        hit = bvh.ray_cast(origin, direction)
        if hit[0] is None:
            break
        crossings += 1
        origin = hit[0] + direction * 1e-5
    return crossings % 2 == 1


def _touch(a, b, tol, deadline):
    """True when the two parts overlap, one lies inside the other, or a surface sample of either is within ``tol`` of
    the other.  The samples (``connectivity.contact_samples``) are clipped to the other part's box, so a flush face
    or a line contact is found even when neither part has a vertex there."""
    if _bvh(a).overlap(_bvh(b)):
        return True
    for source, target in ((a, b), (b, a)):
        if source["tri_boxes"] is None:
            source["tri_boxes"] = connectivity.triangle_boxes(source["points"], source["triangles"])
        bvh = _bvh(target)
        for point in connectivity.contact_samples(source["points"], source["triangles"], target["box"], tol,
                                                  tri_boxes=source["tri_boxes"]):
            hit = bvh.find_nearest(point)
            if hit[0] is not None and hit[3] <= tol:
                return True
        if time.monotonic() > deadline:
            raise _Skipped("skipped: over the time budget")
    for inner, outer in ((a, b), (b, a)):
        if connectivity.box_contains(outer["box"], inner["box"]) and _inside(_bvh(outer), inner["points"][0]):
            return True
    return False


def _closest(members, others, deadline):
    """The nearest approach of any member to any other part: ``(distance, vector, other)``.  ``vector`` points from
    the other part's closest point to the member's.  Pairs are tried nearest-box first and the search stops
    once a box is farther than the best distance found."""
    best = None
    ordered = sorted((connectivity.box_distance(m["box"], o["box"]), mi, oi)
                     for mi, m in enumerate(members) for oi, o in enumerate(others))
    for lower, mi, oi in ordered:
        if best is not None and lower >= best[0]:
            break
        if time.monotonic() > deadline:
            raise _Skipped("skipped: over the time budget")
        member, other = members[mi], others[oi]
        found = _nearest(member, other)
        if found is not None and (best is None or found[0] < best[0]):
            best = (found[0], tuple(found[1][k] - found[2][k] for k in range(3)), other)
        found = _nearest(other, member)
        if found is not None and (best is None or found[0] < best[0]):
            best = (found[0], tuple(found[2][k] - found[1][k] for k in range(3)), other)
    return best


def _geometry_report(height_m):
    """The ``geometry`` block: which parts float free of the ground-standing ones (see ``connectivity.py``)."""
    flat = [value for part in GEOM_PARTS for value in part["verts"]]
    if not flat:
        return None
    lo, hi = _extent(flat)
    scale, shift = _normal_transform(lo, hi, height_m)
    views = [_part_view(part, scale, shift) for part in GEOM_PARTS]
    dims = [(hi[k] - lo[k]) * scale for k in range(3)]
    tol = connectivity.tolerance(dims[2])
    eligible = [v["box"] is not None and bool(v["triangles"]) and not v["detached"] for v in views]
    boxes = [v["box"] if eligible[i] else None for i, v in enumerate(views)]
    deadline = time.monotonic() + GEOM_BUDGET_S
    try:
        pairs = connectivity.candidate_pairs(boxes, tol, connectivity.MAX_PAIRS)
    except connectivity.TooManyPairs:
        raise _Skipped("skipped: too many parts") from None
    touching = []
    for i, j in pairs:
        if _touch(views[i], views[j], tol, deadline):
            touching.append((i, j))
    grounded = [boxes[i] is not None and connectivity.is_grounded(boxes[i][2], tol) for i in range(len(views))]
    groups = connectivity.floating_components(len(views), touching, grounded, [not e for e in eligible])
    entries = []
    floating_parts = {i for group in groups for i in group}
    for group in groups[:connectivity.MAX_FLOATING]:
        members = [views[i] for i in group]
        # the target is a part that stands on the ground (not another floater), else the ground itself
        others = [views[i] for i in range(len(views)) if eligible[i] and i not in floating_parts]
        box = [min(m["box"][k] for m in members) if k < 3 else max(m["box"][k] for m in members) for k in range(6)]
        centre = [(box[k] + box[k + 3]) / 2.0 for k in range(3)]
        best = _closest(members, others, deadline) if others else None
        if best is None:
            gap, vector, nearest = box[2], (0.0, 0.0, box[2]), None
        else:
            gap, vector, nearest = best[0], best[1], best[2]["name"]
        entries.append(connectivity.floating_entry([m["name"] for m in members], [m["line"] for m in members],
                                                   centre, gap, vector, nearest))
    detached = [(v["name"], v["line"]) for v in views if v["detached"]]
    return connectivity.geometry_block(len(views), entries, dims[2], dims, detached)


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


def _load_support():
    """Import the sibling support modules (opschema, connectivity, replay) that sit next to this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    return (importlib.import_module("opschema"), importlib.import_module("replay"),
            importlib.import_module("connectivity"))


def main():
    global RESULT_PATH, connectivity
    job = _job()
    RESULT_PATH = job["result_path"]
    out_dir = job["out_dir"]
    name = job["name"]
    os.makedirs(out_dir, exist_ok=True)
    opschema, replay, connectivity = _load_support()
    kit_path = job.get("kit_path") or ""
    kit_name = os.path.splitext(os.path.basename(kit_path))[0] if kit_path else ""
    kit_allowed = [str(item) for item in (job.get("kit_allowed") or [])] if kit_name else []
    with open(job["ops_path"], "r", encoding="utf-8") as handle:
        if os.fstat(handle.fileno()).st_size > opschema.MAX_JSON_BYTES:
            raise RuntimeError("ops.json is larger than %d bytes" % opschema.MAX_JSON_BYTES)
        doc = json.load(handle)
    opschema.validate_ops(doc, frozenset(kit_allowed))
    if kit_path:
        sys.path.insert(0, os.path.dirname(os.path.abspath(kit_path)))
    _clear_scene()
    replay.run(doc, kit_name, kit_allowed)
    obj = _bake_and_join(name)
    size, lo, hi = _normalise(obj, float(job.get("height_m") or 0.0))
    mesh = obj.data
    tris = sum(len(poly.vertices) - 2 for poly in mesh.polygons)
    geometry, geometry_error = None, ""
    if tris <= int(job.get("max_tris") or 0):  # an over-budget build is discarded, so it is not worth measuring
        try:
            geometry = _geometry_report(float(job.get("height_m") or 0.0))
        except _Skipped as exc:
            geometry_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - the model is fine; a failed measurement only drops the geometry block
            geometry_error = "%s: %s" % (type(exc).__name__, exc)
    kind = "fbx" if str(job.get("export", "glb")).lower() == "fbx" else "glb"
    model_path = os.path.join(out_dir, "%s.%s" % (name, kind))
    _export(obj, model_path, kind)
    files = {"model": model_path}
    result = {"ok": True, "tris": tris, "within_budget": tris <= int(job.get("max_tris") or 0),
              "size": [round(v, 4) for v in size], "files": files,
              "parts": sorted(PARTS, key=lambda part: -part["tris"])[:8]}
    if geometry is not None:
        result["geometry"] = geometry
    if geometry_error:
        result["geometry_error"] = geometry_error[:300]
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

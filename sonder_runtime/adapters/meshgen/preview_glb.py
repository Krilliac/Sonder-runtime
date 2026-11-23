"""Headless Blender script: import a GLB/OBJ 3D model, measure geometry, and render a 2x2 studio sheet.

Runs ONLY inside Blender, never imported by host code:
    blender -b --factory-startup --disable-autoexec --python preview_glb.py -- job.json

Job JSON keys:
    model_path: absolute path to the .glb / .obj file
    sheet_path: destination path for the 2x2 studio turnaround PNG
    result_path: destination path for the JSON summary
    tile_px: optional per-view tile resolution (default 384 -> 768x768 sheet)
"""
from __future__ import annotations

import importlib
import json
import math
import os
import sys
import traceback

bpy = importlib.import_module("bpy")
mathutils = importlib.import_module("mathutils")


def _emit(result_path: str, payload: dict) -> None:
    if not result_path:
        return
    tmp = result_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, result_path)


def _load_job() -> dict:
    argv = sys.argv
    if "--" not in argv:
        raise RuntimeError("job file argument missing")
    with open(argv[argv.index("--") + 1], "r", encoding="utf-8") as handle:
        return json.load(handle)


def _clear_scene() -> None:
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mesh in list(bpy.data.meshes):
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def _import_model(model_path: str) -> list:
    ext = os.path.splitext(model_path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=model_path)
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=model_path)
        else:
            bpy.ops.import_scene.obj(filepath=model_path)
    else:
        raise RuntimeError(f"unsupported 3D format: {ext}")
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def _measure_scene(meshes: list) -> tuple[list[float], list[float], int, int, int]:
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    lo = [float("inf"), float("inf"), float("inf")]
    hi = [float("-inf"), float("-inf"), float("-inf")]
    total_tris = 0
    total_verts = 0
    materials: set[str] = set()
    for obj in meshes:
        ev = obj.evaluated_get(depsgraph)
        me = ev.to_mesh()
        try:
            me.calc_loop_triangles()
            total_tris += len(me.loop_triangles)
            total_verts += len(me.vertices)
            for mat in me.materials:
                if mat is not None:
                    materials.add(mat.name)
            mat_w = obj.matrix_world
            for corner in obj.bound_box:
                p = mat_w @ mathutils.Vector(corner)
                for idx in range(3):
                    lo[idx] = min(lo[idx], float(p[idx]))
                    hi[idx] = max(hi[idx], float(p[idx]))
        finally:
            ev.to_mesh_clear()
    if not all(math.isfinite(v) for v in lo + hi):
        lo = [-0.5, -0.5, -0.5]
        hi = [0.5, 0.5, 0.5]
    return lo, hi, total_tris, total_verts, len(materials)


def _setup_studio(scene, tile_px: int, has_materials: bool):
    # Workbench STUDIO + TEXTURE is fast, reliable headlessly across Blender 4.x/5.x,
    # displays embedded glTF base-color PBR textures crisply, and avoids GPU shader stalls.
    engine_used = "BLENDER_WORKBENCH"
    try:
        scene.render.engine = "BLENDER_WORKBENCH"
    except TypeError:
        for candidate in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "CYCLES"):
            try:
                scene.render.engine = candidate
                engine_used = candidate
                break
            except TypeError:
                continue

    scene.render.resolution_x = tile_px
    scene.render.resolution_y = tile_px
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.view_settings.view_transform = "Standard"

    if engine_used == "BLENDER_WORKBENCH":
        shading = scene.display.shading
        shading.light = "STUDIO"
        shading.color_type = "TEXTURE" if has_materials else "MATERIAL"
        shading.show_specular_highlight = True
        shading.show_cavity = True
        shading.cavity_type = "BOTH"
        shading.show_object_outline = False
    else:
        for name, loc, energy in (
            ("key", (3.0, -3.5, 4.0), 400.0),
            ("fill", (-3.5, -2.0, 2.5), 180.0),
            ("rim", (0.0, 4.0, 3.0), 220.0),
        ):
            ldata = bpy.data.lights.new(name, type="AREA")
            ldata.energy = energy
            ldata.size = 3.0
            lobj = bpy.data.objects.new(name, ldata)
            lobj.location = loc
            scene.collection.objects.link(lobj)

    world = bpy.data.worlds.new("preview_world")
    world.color = (0.14, 0.16, 0.20)
    scene.world = world

    cam_data = bpy.data.cameras.new("preview_cam")
    cam_data.type = "PERSP"
    cam_data.lens = 50.0
    cam = bpy.data.objects.new("preview_cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    return cam, cam_data, engine_used


def _render_turnaround(lo: list[float], hi: list[float], out_dir: str, sheet_path: str, tile_px: int, has_materials: bool) -> str:
    scene = bpy.context.scene
    cam, _cam_data, engine_used = _setup_studio(scene, tile_px, has_materials)
    centre = mathutils.Vector(((lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5, (lo[2] + hi[2]) * 0.5))
    size = [max(hi[i] - lo[i], 1e-4) for i in range(3)]
    radius = max(math.sqrt(sum(s * s for s in size)) * 0.5, 0.25)
    dist = radius * 3.1

    angles = [
        ("hero", mathutils.Vector((0.85, -1.0, 0.45))),
        ("front", mathutils.Vector((0.0, -1.0, 0.12))),
        ("side", mathutils.Vector((1.0, 0.0, 0.12))),
        ("back", mathutils.Vector((-0.7, 1.0, 0.35))),
    ]
    tile_paths: dict[str, str] = {}
    for key, direction in angles:
        d = direction.normalized()
        cam.location = centre + d * dist
        look = (centre - cam.location).normalized()
        cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
        bpy.context.view_layer.update()
        path = os.path.join(out_dir, f"_tile_{key}.png")
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        tile_paths[key] = path

    # Compose 2x2 sheet (Blender image rows run bottom-up).
    sheet = bpy.data.images.new("preview_sheet", 2 * tile_px, 2 * tile_px, alpha=False)
    sheet.colorspace_settings.name = "sRGB"
    buf = [0.14, 0.16, 0.20, 1.0] * (2 * tile_px * 2 * tile_px)
    layout = {"hero": (0, tile_px), "front": (tile_px, tile_px), "side": (0, 0), "back": (tile_px, 0)}
    for key, (cx, cy) in layout.items():
        img = bpy.data.images.load(tile_paths[key])
        img.colorspace_settings.name = "sRGB"
        pixels = list(img.pixels)
        for row in range(tile_px):
            src = pixels[row * tile_px * 4:(row + 1) * tile_px * 4]
            dst = ((cy + row) * 2 * tile_px + cx) * 4
            buf[dst:dst + tile_px * 4] = src
        bpy.data.images.remove(img)
        try:
            os.remove(tile_paths[key])
        except OSError:
            pass
    sheet.pixels.foreach_set(buf)
    sheet.filepath_raw = sheet_path
    sheet.file_format = "PNG"
    sheet.save()
    return engine_used


def main() -> None:
    result_path = ""
    try:
        job = _load_job()
        result_path = str(job.get("result_path") or "")
        model_path = str(job["model_path"])
        sheet_path = str(job["sheet_path"])
        tile_px = max(128, min(int(job.get("tile_px", 384)), 768))
        out_dir = os.path.dirname(os.path.abspath(sheet_path))
        os.makedirs(out_dir, exist_ok=True)

        _clear_scene()
        meshes = _import_model(model_path)
        if not meshes:
            raise RuntimeError("imported 3D file contained no mesh objects")
        lo, hi, tris, verts, materials = _measure_scene(meshes)
        dims = [round(hi[i] - lo[i], 4) for i in range(3)]
        engine = _render_turnaround(lo, hi, out_dir, sheet_path, tile_px, has_materials=materials > 0)
        _emit(result_path, {
            "ok": True,
            "preview_path": sheet_path,
            "tris": tris,
            "verts": verts,
            "materials": materials,
            "dims": dims,
            "engine": engine,
        })
    except Exception as exc:
        _emit(result_path, {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc()[-1500:],
        })
        sys.exit(1)


if __name__ == "__main__":
    main()

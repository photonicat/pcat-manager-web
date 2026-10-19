#!/usr/bin/env python3
"""Convert a 3D model of the device enclosure into a web-ready .glb.

Run under Blender's bundled Python, not the system one:

    blender --background --python tools/model_to_glb.py -- \
        --in ~/Downloads/photonicat2.dae \
        --out app/static/photonicat2.glb \
        --tris 4000 --normalize

Accepts dae / 3ds / stl / obj / ply / fbx / gltf / glb / blend as input. STEP
and other boundary-representation CAD formats are *not* readable by Blender --
tessellate them with FreeCAD first (see tools/step_to_mesh.py).

The output targets the orientation widget on /dev, which only ever rotates the
model, so the pipeline is deliberately lossy in the ways that don't matter:
geometry is decimated to a triangle budget, and --normalize recentres the mesh
on its bounding-box centre and scales the longest axis to 1.0 so the three.js
side needs no knowledge of the source units (mm vs m vs inches).
"""

import argparse
import math
import os
import sys

import bpy  # provided by Blender; this script cannot run under system python


# Blender's importers are spread across operators that get renamed between
# releases, and some live in add-ons that are not enabled by default. Each entry
# is tried in order until one exists.
IMPORTERS = {
    ".dae": [("wm", "collada_import")],
    ".3ds": [("import_scene", "max3ds")],
    ".stl": [("wm", "stl_import"), ("import_mesh", "stl")],
    ".obj": [("wm", "obj_import"), ("import_scene", "obj")],
    ".ply": [("wm", "ply_import"), ("import_mesh", "ply")],
    ".fbx": [("import_scene", "fbx")],
    ".gltf": [("import_scene", "gltf")],
    ".glb": [("import_scene", "gltf")],
}

# Add-ons backing the importers above; enabling an already-enabled one is a no-op.
ADDONS = {
    ".3ds": "io_scene_3ds",
    ".fbx": "io_scene_fbx",
    ".dae": "io_scene_collada",
}


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(prog="model_to_glb")
    p.add_argument("--in", dest="src", required=True, help="source model file")
    p.add_argument("--out", dest="dst", required=True, help="destination .glb")
    p.add_argument("--tris", type=int, default=4000,
                   help="triangle budget; 0 disables decimation (default 4000)")
    p.add_argument("--normalize", action="store_true",
                   help="recentre on bounding-box centre, scale longest axis to 1.0")
    p.add_argument("--no-join", dest="join", action="store_false", default=True,
                   help="keep source objects separate instead of joining into one mesh")
    p.add_argument("--draco", type=int, default=6,
                   help="Draco compression level 0-10, 0 disables (default 6)")
    return p.parse_args(argv)


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_model(path):
    ext = os.path.splitext(path)[1].lower()

    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return

    if ext not in IMPORTERS:
        sys.exit(f"model_to_glb: unsupported input extension {ext!r}. "
                 f"Supported: {', '.join(sorted(IMPORTERS))}, .blend")

    if ext in ADDONS:
        try:
            bpy.ops.preferences.addon_enable(module=ADDONS[ext])
        except Exception as exc:                     # already enabled, or built in
            print(f"model_to_glb: addon_enable({ADDONS[ext]}) -> {exc}")

    for module, op in IMPORTERS[ext]:
        # bpy.ops.<module>.<op> resolves to a stub even for operators that do not
        # exist in this build; calling it is the only reliable existence check.
        fn = getattr(getattr(bpy.ops, module, None), op, None)
        if fn is None:
            continue
        try:
            fn(filepath=path)
            print(f"model_to_glb: imported via bpy.ops.{module}.{op}")
            return
        except (AttributeError, RuntimeError) as exc:
            print(f"model_to_glb: bpy.ops.{module}.{op} failed -> {exc}")

    sys.exit(f"model_to_glb: no working importer for {ext!r} in this Blender build")


def mesh_objects():
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def drop_non_geometry():
    """Cameras, lights and empties ride along in dae/fbx exports and would bloat
    the glb with nodes the widget never uses."""
    for obj in list(bpy.context.scene.objects):
        if obj.type not in ("MESH",):
            bpy.data.objects.remove(obj, do_unlink=True)


def join_meshes():
    """Collapse every mesh object into one.

    CAD-derived glTF exports routinely carry one primitive per analytic surface
    -- the photonicat2 render export has 18541 primitives over 99 meshes, and
    that per-primitive accessor/bufferView overhead is the bulk of its 37 MB.
    Joining merges material slots, so the exporter emits a single primitive per
    material (7 here) instead of thousands, which also collapses the draw call
    count the browser has to issue every frame.
    """
    objs = mesh_objects()
    if len(objs) < 2:
        return

    if bpy.context.object is None or bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objs:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()

    joined = mesh_objects()
    slots = len(joined[0].material_slots) if joined else 0
    print(f"model_to_glb: joined {len(objs)} objects -> "
          f"{len(joined)} mesh, {slots} material slot(s)")


def triangle_count():
    total = 0
    for obj in mesh_objects():
        for poly in obj.data.polygons:
            total += max(0, len(poly.vertices) - 2)
    return total


def decimate(budget):
    """Collapse-decimate every mesh so the scene total lands near `budget`.

    The ratio is applied per-object rather than globally: an even proportional
    cut keeps small detail parts (ports, buttons) from being erased entirely
    while the big shell absorbs the whole reduction.
    """
    before = triangle_count()
    if before <= budget:
        print(f"model_to_glb: {before} tris already within budget {budget}, no decimation")
        return before, before

    ratio = budget / before
    for obj in mesh_objects():
        mod = obj.modifiers.new(name="pcat_decimate", type="DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = ratio
    # Modifiers are baked by the exporter's export_apply=True; evaluate here only
    # to report the real post-decimation figure.
    depsgraph = bpy.context.evaluated_depsgraph_get()
    after = 0
    for obj in mesh_objects():
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        for poly in mesh.polygons:
            after += max(0, len(poly.vertices) - 2)
        evaluated.to_mesh_clear()
    print(f"model_to_glb: decimated {before} -> {after} tris (ratio {ratio:.4f})")
    return before, after


def world_bounds():
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for obj in mesh_objects():
        for corner in obj.bound_box:
            world = obj.matrix_world @ type(obj.location)(corner)
            for axis in range(3):
                lo[axis] = min(lo[axis], world[axis])
                hi[axis] = max(hi[axis], world[axis])
    return lo, hi


def normalize():
    """Centre the model on the origin and scale its longest axis to 1.0.

    Without this the widget's camera framing depends on the source file's units,
    which differ per CAD tool (STEP is usually mm, Blender is m).
    """
    objs = mesh_objects()
    if not objs:
        return

    lo, hi = world_bounds()
    size = [hi[i] - lo[i] for i in range(3)]
    longest = max(size)
    if longest <= 0 or not math.isfinite(longest):
        print("model_to_glb: degenerate bounds, skipping normalize")
        return
    centre = [(hi[i] + lo[i]) / 2 for i in range(3)]
    scale = 1.0 / longest

    # Parent everything to one empty, transform that, then bake into the meshes.
    pivot = bpy.data.objects.new("pcat_pivot", None)
    bpy.context.scene.collection.objects.link(pivot)
    for obj in objs:
        if obj.parent is None:
            obj.parent = pivot
            obj.matrix_parent_inverse = pivot.matrix_world.inverted()

    pivot.location = [-c * scale for c in centre]
    pivot.scale = (scale, scale, scale)

    bpy.context.view_layer.update()
    print(f"model_to_glb: normalized -- source size {tuple(round(s, 3) for s in size)}, "
          f"scale {scale:.6g}")


def export_glb(path, draco_level):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    kwargs = dict(
        filepath=path,
        export_format="GLB",
        export_apply=True,          # bake the decimate modifiers
        export_yup=True,            # glTF is Y-up, Blender is Z-up
        export_cameras=False,
        export_lights=False,
        export_animations=False,    # the widget rotates the model itself
        export_skins=False,
        export_morph=False,
    )
    if draco_level > 0:
        kwargs["export_draco_mesh_compression_enable"] = True
        kwargs["export_draco_mesh_compression_level"] = draco_level

    try:
        bpy.ops.export_scene.gltf(**kwargs)
    except TypeError as exc:
        # Exporter argument names drift between Blender releases; retry bare.
        print(f"model_to_glb: exporter rejected an argument ({exc}); retrying minimal")
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", export_apply=True)


def main():
    args = parse_args()
    src = os.path.abspath(os.path.expanduser(args.src))
    dst = os.path.abspath(os.path.expanduser(args.dst))

    if not os.path.isfile(src):
        sys.exit(f"model_to_glb: no such file: {src}")

    clear_scene()
    import_model(src)
    drop_non_geometry()

    if not mesh_objects():
        sys.exit("model_to_glb: import produced no mesh objects")

    if args.join:
        join_meshes()
    if args.tris > 0:
        decimate(args.tris)
    if args.normalize:
        normalize()

    export_glb(dst, args.draco)

    size = os.path.getsize(dst)
    print(f"model_to_glb: wrote {dst} ({size / 1024:.1f} KiB, "
          f"{len(mesh_objects())} mesh object(s))")


if __name__ == "__main__":
    main()

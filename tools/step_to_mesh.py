#!/usr/bin/env python3
"""Tessellate a STEP/IGES/BREP CAD file into a mesh Blender can read.

Run under FreeCAD's bundled Python, passing paths via the environment because
freecadcmd swallows the command line (see parse_args):

    STEP_IN=enclosure.STEP STEP_OUT=/tmp/enclosure.stl \
        /Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd tools/step_to_mesh.py

Or just use tools/cad_to_glb.sh, which chains this with model_to_glb.py.

Mechanical CAD stores boundary representations (analytic surfaces), not
triangles, and Blender cannot read them. This converts B-rep to triangles so
tools/model_to_glb.py can take over.

Feed the result straight into model_to_glb.py, which does the decimation --
tessellate finely here (small --deflection) and let the triangle budget there
decide the final density. Tessellating coarsely produces faceted curves that no
amount of downstream processing can recover.
"""

import argparse
import os
import sys

import FreeCAD  # noqa: F401  -- provided by freecadcmd
import Mesh
import MeshPart
import Part


def parse_args():
    """Configuration comes from the environment, not argv.

    freecadcmd's own option parser consumes the command line before the script
    sees it: bare trailing arguments are treated as documents to open, and
    --pass rejects any value starting with a dash (so `--pass --in x` just prints
    FreeCAD's help). Environment variables are the one channel that survives.
    Argv is still honoured when this runs under a plain interpreter.
    """
    env = os.environ
    p = argparse.ArgumentParser(prog="step_to_mesh")
    p.add_argument("--in", dest="src", default=env.get("STEP_IN"),
                   help="source STEP/IGES/BREP file [$STEP_IN]")
    p.add_argument("--out", dest="dst", default=env.get("STEP_OUT"),
                   help="destination .stl or .obj [$STEP_OUT]")
    p.add_argument("--deflection", type=float, default=float(env.get("STEP_DEFLECTION", 0.1)),
                   help="linear deflection in source units, smaller is finer [$STEP_DEFLECTION]")
    p.add_argument("--angular", type=float, default=float(env.get("STEP_ANGULAR", 0.5)),
                   help="angular deflection in radians [$STEP_ANGULAR]")

    argv = [a for a in sys.argv[1:] if not a.endswith(".py")]
    args, _unknown = p.parse_known_args(argv)

    if not args.src or not args.dst:
        sys.exit("step_to_mesh: set STEP_IN and STEP_OUT (see the module docstring)")
    return args


def main():
    args = parse_args()
    src = os.path.abspath(os.path.expanduser(args.src))
    dst = os.path.abspath(os.path.expanduser(args.dst))

    if not os.path.isfile(src):
        sys.exit(f"step_to_mesh: no such file: {src}")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)

    doc = FreeCAD.newDocument("step_to_mesh")
    Part.insert(src, doc.Name)
    doc.recompute()

    combined = Mesh.Mesh()
    solids = 0
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull():
            continue
        mesh = MeshPart.meshFromShape(
            Shape=shape,
            LinearDeflection=args.deflection,
            AngularDeflection=args.angular,
            Relative=False,
        )
        combined.addMesh(mesh)
        solids += 1
        print(f"step_to_mesh: {obj.Name}: {mesh.CountFacets} facets")

    if solids == 0:
        sys.exit("step_to_mesh: file contained no non-null shapes")

    combined.write(dst)
    size = os.path.getsize(dst)
    print(f"step_to_mesh: wrote {dst} -- {solids} shape(s), "
          f"{combined.CountFacets} facets, {size / 1024:.1f} KiB")


# freecadcmd execs the script with __name__ set to the module basename rather
# than "__main__", so a bare __main__ guard would silently do nothing (exit 0,
# no output, no file). Accept both.
if __name__ in ("__main__", "step_to_mesh"):
    main()

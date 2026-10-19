#!/bin/bash
# Convert any CAD/mesh model of the device into a web-ready .glb for /dev.
#
#   tools/cad_to_glb.sh enclosure.STEP app/static/photonicat2.glb
#   tools/cad_to_glb.sh model.dae app/static/photonicat2.glb 6000
#
# Args: <input> <output.glb> [triangle-budget, default 4000]
#
# STEP/IGES/BREP inputs are boundary representations that Blender cannot read,
# so they are tessellated with FreeCAD first. Mesh formats (dae/3ds/stl/obj/ply/
# fbx/gltf/glb/blend) go straight to Blender.
#
# Override tool locations with $BLENDER / $FREECADCMD if they live elsewhere.

set -euo pipefail

BLENDER="${BLENDER:-$(command -v blender || true)}"
FREECADCMD="${FREECADCMD:-/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd}"
DEFLECTION="${DEFLECTION:-0.1}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -lt 2 ]; then
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
fi

src="$1"
dst="$2"
tris="${3:-4000}"

[ -f "$src" ] || { echo "cad_to_glb: no such file: $src" >&2; exit 1; }
[ -n "$BLENDER" ] && [ -x "$BLENDER" ] || {
  echo "cad_to_glb: blender not found; set \$BLENDER" >&2; exit 1; }

ext="$(printf '%s' "${src##*.}" | tr '[:upper:]' '[:lower:]')"

# Scratch dir for the intermediate tessellation; always cleaned up.
tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/cad_to_glb.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT

mesh="$src"
case "$ext" in
  step|stp|iges|igs|brep|brp)
    [ -x "$FREECADCMD" ] || {
      echo "cad_to_glb: $ext input needs FreeCAD; set \$FREECADCMD" >&2; exit 1; }
    echo "==> tessellating $ext with FreeCAD (deflection $DEFLECTION)"
    mesh="$tmpdir/tessellated.stl"
    # Paths go through the environment: freecadcmd's own parser eats argv.
    STEP_IN="$src" STEP_OUT="$mesh" STEP_DEFLECTION="$DEFLECTION" \
      "$FREECADCMD" "$here/step_to_mesh.py" 2>&1 \
      | grep -E '^step_to_mesh:' || true
    [ -f "$mesh" ] || { echo "cad_to_glb: tessellation produced no mesh" >&2; exit 1; }
    ;;
esac

echo "==> converting to glb with Blender (budget ${tris} tris)"
"$BLENDER" --background --python "$here/model_to_glb.py" -- \
  --in "$mesh" --out "$dst" --tris "$tris" --normalize 2>&1 \
  | grep -E '^model_to_glb:|^Error|Traceback' || true

[ -f "$dst" ] || { echo "cad_to_glb: no glb was written" >&2; exit 1; }
echo "==> done: $dst ($(du -h "$dst" | cut -f1))"

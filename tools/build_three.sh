#!/bin/bash
# Build app/static/pcat3d.min.js (three.js + glTF/Draco loaders + the /dev
# orientation widget) and vendor the Draco decoder next to it.
#
#   tools/build_three.sh
#
# Only needs re-running when src/three/pcat_model.js changes or three.js is
# bumped -- the outputs are checked in, so a normal misc/make.py build does not
# need node installed.
#
# Outputs (all flat in app/static/, no new directories):
#   app/static/pcat3d.min.js          bundled, minified, tree-shaken ESM
#   app/static/draco_decoder.wasm     glTF-flavoured Draco decoder
#   app/static/draco_wasm_wrapper.js  its loader shim

set -euo pipefail

THREE_VERSION="${THREE_VERSION:-0.185.1}"
ESBUILD_VERSION="${ESBUILD_VERSION:-0.25.0}"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
entry="$repo/src/pcat_model.js"
out_js="$repo/app/static/pcat3d.min.js"
out_draco="$repo/app/static"

[ -f "$entry" ] || { echo "build_three: missing $entry" >&2; exit 1; }
command -v npx >/dev/null || {
  echo "build_three: npx not found. node@20 lives outside the default PATH on this" >&2
  echo "             machine: export PATH=/opt/homebrew/opt/node@20/bin:\$PATH" >&2
  exit 1; }

work="$(mktemp -d "${TMPDIR:-/tmp}/build_three.XXXXXX")"
trap 'rm -rf "$work"' EXIT

echo "==> fetching three@$THREE_VERSION"
mkdir -p "$work/node_modules"
( cd "$work" && npm pack "three@$THREE_VERSION" >/dev/null 2>&1 )
tar xzf "$work/three-$THREE_VERSION.tgz" -C "$work"
mv "$work/package" "$work/node_modules/three"

echo "==> bundling $(basename "$entry")"
cp "$entry" "$work/entry.js"
( cd "$work" && npx --yes "esbuild@$ESBUILD_VERSION" entry.js \
    --bundle --minify --format=esm --target=es2020 \
    --outfile=bundle.js --log-level=warning )

cp "$work/bundle.js" "$out_js"

# The glTF-flavoured decoder is the one DRACOLoader defaults to and is ~90 KiB
# smaller than the general-purpose build. The pure-JS draco_decoder.js fallback
# (512 KiB) is deliberately not shipped: every browser that supports WebGL 2
# also supports WebAssembly.
echo "==> vendoring draco decoder"
src_draco="$work/node_modules/three/examples/jsm/libs/draco/gltf"
cp "$src_draco/draco_decoder.wasm" "$src_draco/draco_wasm_wrapper.js" "$out_draco/"

echo "==> done"
for f in "$out_js" "$out_draco/draco_decoder.wasm" "$out_draco/draco_wasm_wrapper.js"; do
  printf '    %-34s %8.1f KiB\n' "app/static/$(basename "$f")" \
    "$(echo "$(stat -f%z "$f")/1024" | bc -l)"
done

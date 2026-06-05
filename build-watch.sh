#!/bin/bash
cd "$(dirname "$0")"
echo "Current dir:`pwd`"
pnpm install
# force chokidar (the watcher PostCSS uses) into polling mode
export CHOKIDAR_USEPOLLING=true
export CHOKIDAR_INTERVAL=500   # check every 500ms
#
pnpm build:css --watch

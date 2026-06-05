#!/bin/bash
cd "$(dirname "$0")"
echo "Current dir:`pwd`"
pnpm install
pnpm build:css
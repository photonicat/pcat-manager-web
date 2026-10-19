#!/bin/bash
# Script to run pcat-manager-web in dummy mode for x86/x64 testing

echo "Starting pcat-manager-web in DUMMY MODE for x86/x64 testing..."
echo "This mode simulates hardware components without actual Phonicat hardware."
echo ""
echo "Features in dummy mode:"
echo "  - Simulated Unix socket communication (no pcat-manager required)"
echo "  - Mock battery and power management data"
echo "  - Fake modem and SMS functionality"
echo "  - Virtual hardware paths and system files"
echo "  - Dummy network interfaces and statistics"
echo ""

# Change to app directory
cd "$(dirname "$0")/app"

# Run with dummy mode and common development flags
python3 pcat-manager-web-main.py \
    --dummy-mode \
    --skip-auth \
    --port 8080 \
    "$@"
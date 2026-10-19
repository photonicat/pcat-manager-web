#!/bin/sh
# Openwrt luci service bind to tcp port 8080

# Check if running on x86/x64 architecture and exit with warning
ARCH=$(uname -m)
case "$ARCH" in
    x86_64|i386|i686)
        printf "\033[31m❌ ERROR: Cannot start development server on x86/x64 architecture\033[0m\n"
        printf "\033[31m   This application requires ARM-based OpenWrt devices with modem hardware\033[0m\n"
        printf "\033[31m   Current architecture: $ARCH\033[0m\n"
        printf "\033[33m   Use CSS build commands only: ./build.sh or pnpm build:css\033[0m\n"
        exit 1
        ;;
esac

# Check for existing system process and kill if found
SYSTEM_PROC=$(ps | grep "/usr/share/pcat-manager-web/pcat-manager-web-main.pyc" | grep -v grep)
if [ -n "$SYSTEM_PROC" ]; then
    PID=$(echo "$SYSTEM_PROC" | awk '{print $1}')
    printf "\033[33m⚠️  Killing system pcat-manager-web process (PID: $PID)\033[0m\n"
    printf "\033[33m   Process: $SYSTEM_PROC\033[0m\n"
    sleep 1
    kill -9 $PID 2>/dev/null
    printf "\033[33m✓ System process terminated\033[0m\n\n"
fi

# Parse arguments
STDOUT_LOGGING="1"  # Default: enable stdout logging
if [ "$1" = "--no-stdout" ]; then
    STDOUT_LOGGING="0"
    printf "\033[33m📝 Stdout logging disabled - logs only to file\033[0m\n"
elif [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    printf "Usage: $0 [--no-stdout]\n"
    printf "  --no-stdout  Disable stdout logging (logs only to file)\n"
    printf "  Default: stdout logging enabled with colors\n"
    exit 0
else
    printf "\033[32m📝 Stdout logging enabled with colors\033[0m\n"
fi

export FLASK_TEMPLATE_RELOAD=1
export PCAT_LOG_STDOUT="$STDOUT_LOGGING"
export PCAT_LOG_LEVEL="${PCAT_LOG_LEVEL:-DEBUG}"
./pcat-manager-web-main.py -p 80 --skip-auth 

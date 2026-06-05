# Photonicat Manager Web

Web management interface for Photonicat router devices running OpenWrt. Provides a simplified, user-friendly web UI to replace the complex native Luci configuration interface.

## Features

- Network configuration (WiFi, LAN, WAN)
- Mobile network / APN settings
- Modem management
- SMS messaging
- Data usage statistics
- Firmware & MCU update
- Screen / display customization
- System management (reboot, reset, password)

## Setup Development Environment

Python 3.9.5 (matches target OpenWrt device).

```bash
sudo apt install python3-pip
pip install -r requirements.txt
```

### Build

```bash
./build.sh                    # Install dependencies and build CSS
pnpm install                  # Install Node.js dependencies
pnpm build:css                # Build Tailwind CSS only
```

### Run Development Server

```bash
cd app && ./dev-start.sh      # Start dev server on port 80 with skip-auth
python3 app/pcat-manager-web-main.py -p 8001 --skip-auth  # Custom port
```

> **Note**: The modem hardware and drivers are only available on the target ARM-based OpenWrt devices. Do not test modem-related features on x86/x64 machines.

### Code Formatting

```bash
pnpm prettier                 # Format all files with Prettier
```

## Technology Stack

- **Backend**: Python Flask, Flask-SocketIO, passlib, pyserial
- **Frontend**: Tailwind CSS, Alpine.js, Jinja2 templates
- **Real-time**: WebSocket for system updates and progress
- **Config**: OpenWrt UCI (Unified Configuration Interface)
- **Build**: PostCSS, Prettier, pnpm

## Production Deployment

```bash
python3 ./misc/make.py        # Build encrypted deployment package
```

The built package is placed in the `build/` directory. Install on device:

```bash
opkg install pcat-manager-web_*.ipk --force-reinstall
```

The web application deploys to `/usr/share/pcat-manager-web/` on the device.

## Update Servers

### Firmware
- **Production**: `https://dl.photonicat.com/images/photonicat2/openwrt/`
- **Latest check**: `https://dl.photonicat.com/images/photonicat2/openwrt/latest_image.txt`

### MCU Firmware
- **Production**: `https://dl.photonicat.com/firmware/pcat2_mcu/`
- **Latest check**: `https://dl.photonicat.com/firmware/pcat2_mcu/latest_img.json`

## Documentation

- [API Documentation](docs/api.md) - REST API and WebSocket reference

## Debugging

Logs are written to `/tmp/pcat-manager-web.log` (max 10MB, 2 backup files).

```bash
tail -f /tmp/pcat-manager-web.log          # Real-time monitoring
grep -i "ERROR" /tmp/pcat-manager-web.log  # Search for errors
```

## License

Copyright Photonicat. All rights reserved.

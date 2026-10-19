# Dummy Mode for x86/x64 Development

## Overview

The dummy mode allows you to run and debug the pcat-manager-web application on x86/x64 machines without requiring actual Phonicat hardware. This is useful for development, testing, and debugging purposes.

## Features

When running in dummy mode, the application:

- **Simulates hardware components**: Battery, power management, temperature sensors
- **Mocks Unix socket communication**: No need for pcat-manager daemon
- **Provides fake modem/SMS functionality**: Test SMS features without hardware
- **Creates virtual system paths**: All hardware paths point to temporary files
- **Generates dummy network statistics**: Simulates network interface data

## Usage

### Quick Start

```bash
./run_dummy_mode.sh
```

This will start the application on port 8080 with authentication disabled.

### Manual Start

```bash
cd app
python3 pcat-manager-web-main.py --dummy-mode --skip-auth -p 8080
```

### Command Line Options

- `--dummy-mode`: Enable dummy mode for x86/x64 testing
- `--skip-auth`: Skip authentication (useful for development)
- `-p PORT`: Specify the port (default: 80, recommended: 8080+ for testing)
- `--skip-pin`: Skip PIN protection

## Simulated Components

### 1. Socket Client (`pc_socket_client_dummy.py`)
- Simulates battery status (85% charge by default)
- Provides fake STM32 firmware version
- Simulates charging/discharging cycles
- Mock temperature readings (40-50°C range)

### 2. Modem Client (`pc_modem_client_dummy.py`)
- Simulates 4G/5G modem
- Provides fake signal strength (varies 20-100%)
- Mock SMS send/receive functionality
- Dummy IMEI/IMSI/ICCID numbers

### 3. SMS Client (`pc_sms_client_dummy.py`)
- Pre-populated with sample SMS messages
- Supports sending/deleting SMS (stored in memory)
- Simulates read/unread status

### 4. Hardware Paths (`dummy_hardware.py`)
- Creates temporary directory structure
- Simulates `/sys/class/` hardware paths
- Provides fake battery/network statistics
- Mock device serial numbers

## Limitations

In dummy mode, certain features are limited or non-functional:

- System commands (uci, ubus, opkg) return errors or empty results
- Real network configuration changes are not possible
- Actual reboot/reset operations are disabled
- WiFi configuration is simulated only

## Development Notes

The dummy mode is automatically detected by checking for the `--dummy-mode` flag early in the startup process. This ensures all configuration paths are redirected to temporary directories before any file operations occur.

All dummy files are created in temporary directories (e.g., `/tmp/pcat_dummy_*`) and are automatically cleaned up when the application exits.

## Testing

To verify dummy mode is working:

1. Start the application with `--dummy-mode`
2. Check console output for "[DUMMY]" prefixed messages
3. Access the web interface at `http://localhost:8080`
4. Verify simulated data appears (battery %, signal strength, etc.)

## Troubleshooting

If you encounter permission errors:
- Ensure you're not using privileged ports (< 1024) without sudo
- Check that temporary directories can be created in `/tmp/`

If the application fails to start:
- Check that Python 3.9+ is installed
- Verify all Python dependencies are installed
- Look for error messages prefixed with "[DUMMY]" in the console
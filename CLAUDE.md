# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Python Flask-based web management interface for Phonicat router devices running OpenWrt. It provides a simplified web interface to replace the complex native Luci configuration interface, focusing on essential network configuration functions for end users.

## Development Commands

### Build and Development

```bash
# Initial setup and CSS build
./build.sh                    # Installs dependencies and builds CSS
./build.sh --update-pysms     # Also update pysms_tool from GitHub
pnpm install                  # Install Node.js dependencies
pnpm build:css               # Build Tailwind CSS only

# Update pysms_tool from GitHub
python3 update_pysms_tool.py              # Update pysms_tool to latest version
python3 update_pysms_tool.py --force      # Force update even if directory exists
python3 update_pysms_tool.py --dry-run    # Show what would be updated
./update_pysms_tool.sh                    # Shell wrapper for easier usage

# Development server (choose one)
cd app && ./dev-start.sh     # Start development server on port 80 with skip-auth
python3 app/pcat-manager-web-main.py -p 8001 --skip-auth  # Alternative with custom port

# Code formatting
pnpm prettier               # Format all files with prettier

# CSS development with watch mode
./build-watch.sh            # Auto-rebuild CSS on file changes
```

### Production Deployment

```bash
python3 ./misc/make.py      # Build encrypted deployment package for OpenWrt
```

## Architecture

The application follows a multi-threaded architecture with these key components:

### Core Entry Point

- `app/pcat-manager-web-main.py` - Main entry point that initializes and starts all threads:
  - FlaskAppThread (Web services)
  - PcSocketClient (Unix socket communication)
  - PcModemClient (Modem communication)
  - App (Main application thread)

### Key Modules

- `app/pcat_manager_web.py` - Flask web server with REST APIs and WebSocket support
- `app/app.py` - Core application logic, system monitoring, and configuration management
- `app/pcat_util/command_parser.py` - Command output parsing utilities, especially for wireless configuration
- `app/pc_socket_client.py` - Unix domain socket client for pcat-manager communication
- `app/pc_modem_client.py` - Modem communication handling
- `app/pc_sms_client.py` - SMS message handling (uses pysms_tool for native Python SMS operations)
- `app/pc_screen_client.py` - Screen/display management
- `app/pcat_config.py` - Application configuration management
- `app/pcat_util/user_auth.py` - User authentication utilities
- `app/pysms_tool/` - Native Python SMS tool library (downloaded from GitHub)

### Communication Architecture

```
[Browser] <-> [Flask App] <-> [UCI System]
                ↑
    [Unix Socket Bridge to pcat-manager]
```

## Key API Endpoints

### System Management

- `GET /api/v1/system/board.json` - Device information
- `GET /api/v1/system/info.json` - System information
- `POST /system/reboot` - System reboot
- `POST /system/reset` - System reset

### Network Configuration

- `GET /api/v2/wireless.json` - Get wireless configuration
- `POST /api/v1/wireless.json` - Save wireless configuration
- `GET /api/v1/network_interfaces.json` - Get network interface config
- `POST /api/v1/network_interfaces.json` - Save network interface config

### Mobile Network

- `GET /api/v1/apn_settings.json` - Get APN settings
- `POST /api/v1/apn_settings.json` - Save APN settings
- `GET /api/v1/modem/basic.json` - Get modem basic info
- `POST /api/v1/modem/config.json` - Save modem configuration

### Data Statistics

- `GET /api/v1/data_stats.json` - Get data statistics
- `POST /stats/reset` - Reset statistics

### SMS Management

- `GET /api/v1/sms/list.json` - Get SMS messages
- `POST /api/v1/sms/send.json` - Send SMS message
- `POST /api/v1/sms/delete.json` - Delete SMS message

## Configuration Management

The system uses OpenWrt's UCI (Unified Configuration Interface) for configuration management. Configuration changes are applied through command-line tools and UCI bindings.

## Inter-Process Communication

The web application communicates with the main pcat-manager daemon via Unix domain socket at `/tmp/pcat-manager.sock`. Commands are sent as JSON messages terminated with null bytes.

## Frontend Technology

- **CSS Framework**: Tailwind CSS for responsive design
- **JavaScript**: AlpineJS for lightweight interactivity
- **WebSocket**: Real-time communication for system updates and package installation progress

## File Structure Notes

- `app/templates/` - Jinja2 HTML templates
- `app/static/` - Static assets (CSS, JS, images, fonts)
- `src/styles/` - Source Tailwind CSS files
- `docs/` - Technical documentation including API specs and design documents
- `misc/` - Deployment and development utilities

## Development Environment

- **Python Version**: 3.9.5 (matches target OpenWrt device)
- **Target Platform**: OpenWrt on embedded ARM devices
- **Package Manager**: pnpm (for Node.js dependencies)
- **Main Dependencies**:
  - Backend: Flask 3.x, SocketIO, passlib, pyserial
  - Frontend: Tailwind CSS, AlpineJS
  - Build Tools: PostCSS, Prettier

### Development Platform Notes

- **IMPORTANT**: Do not start the development server using `./dev-start.sh` or test modem-related components when developing on x86 or x64 machines, as the modem hardware and drivers are only available on the target ARM-based OpenWrt devices. The application is designed to run exclusively on embedded ARM devices with OpenWrt.

## Custom Styling

The project uses custom Tailwind CSS configuration with Phonicat branding colors:

- `pcatyellow: #FDE021`
- `pcatgray: #00274D`
- `pcatblue: #008AD3`
- Font families: Titillium Web (sans), Orbitron (titles)

## Testing and Quality

No specific test framework is configured. Manual testing should focus on:

- Web interface functionality across different device screen sizes
- API endpoint responses
- Real-time WebSocket communication
- System configuration changes via UCI
- Mock testing utilities available in `app/test/` directory

### Template Validation

**IMPORTANT**: Always validate templates before production deployment to prevent Jinja2 syntax errors.

```bash
# Validate compiled templates for production deployment
./tools/fix_template3.sh
```

This script:
- Compiles templates using the production build process (`misc/make.py`)
- Validates all templates for Jinja2 syntax errors
- Checks critical templates (firmware update, MCU update, screen controls)
- Reports any template issues that could break production

### CI/CD Pipeline

The project includes automated GitLab CI/CD pipeline (`.gitlab-ci.yml`) that runs:

**Validation Stage:**
- Template validation using `./tools/fix_template3.sh`
- Code quality checks with Prettier
- Template security scanning

**Build Stage:**
- Production package creation with `misc/make.py`
- CSS compilation and optimization
- Artifact generation for deployment

**Test Stage:**
- Deployment simulation and verification
- Security analysis of templates

The pipeline automatically runs on:
- Merge requests (validation and testing)
- Master branch commits (full build and deployment)
- Git tags (release builds)

Pipeline status: [![Pipeline Status](https://git.g77k.com/qichunren/pcat-manager-web/badges/master/pipeline.svg)](https://git.g77k.com/qichunren/pcat-manager-web/-/pipelines)

**Template Development Workflow**:
1. **Detect Issues**: `./tools/fix_templates.sh --detect-only`
2. **Fix Complex Expressions**: Replace problematic Alpine.js expressions with JavaScript helper functions
3. **Validate Build**: `./tools/fix_template3.sh` 
4. **Deploy**: Use validated templates from `build/templates/`

**Common Template Issues**:
- Complex Alpine.js expressions: `x-text="condition ? '{{...}}' : '{{...}}"` → `x-text="helperFunction()"`
- Bracket notation conflicts: `{{locales["key"]}}` → `{{locales.key}}`
- HTML entity issues after minification: `&quot;` breaking Jinja2 parsing

## Security Considerations

- User authentication via Flask sessions
- Password management using passlib
- Optional `--skip-auth` flag for development
- Configuration changes require proper UCI permissions
- No sensitive information should be logged or committed

## UI Text and Translations

When adding new UI text to the web interface, ALWAYS remember to add translations in both English and Chinese to the Python translation files. This ensures proper internationalization support for all user-facing text.

## Git Commit Guidelines

When creating git commits using Claude Code, follow these preferences:

- **Commit Messages**: Use clear, descriptive commit messages following conventional commit format
- **Author Attribution**: Commit with the repository owner's name only - do NOT include "Co-Authored-By: Claude" lines or generated tags

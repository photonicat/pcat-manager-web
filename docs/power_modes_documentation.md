# Power Modes Documentation

This document describes the power management modes available for Photonicat devices through the pcat-manager system.

## Overview

Photonicat supports multiple power management modes accessible through the web interface at `/timer` and configurable via the pcat-manager socket API.

## Available Power Modes

### 1. Manual Mode (手动模式)

**Mode Value**: `0`
**Description**: Traditional manual power control. The device only powers on/off via the physical power button or software commands.

**Use Cases**:
- Standard desktop/server usage
- Maximum user control
- When you want explicit control over power states

**Web Interface**: Select "Manual mode" (手动模式) tab

### 2. Car/UPS Mode (车载/UPS模式)

**Mode Value**: `1`
**Description**: Automatic power-on when charger connects, delayed graceful shutdown after charger disconnects.

**Features**:
- Auto power-on when external power (charger) is connected
- Configurable delay (0-600 minutes) before automatic shutdown
- Graceful shutdown to protect data integrity

**Use Cases**:
- Car installations (powers on with ignition)
- UPS applications
- Temporary power scenarios

**Configuration**:
```json
{
  "command": "charger-on-auto-start-set",
  "state": 1,
  "timeout": 300
}
```

**Web Interface**: Select "Car/UPS mode" (车载/UPS模式) tab, configure timeout

### 3. Timer Mode (定时模式)

**Description**: Schedule automatic power on/off events based on time and day of week.

**Features**:
- Up to 6 scheduled events
- Power on or power off actions
- Day-of-week selection
- Precise time control (HH:MM format)

**Use Cases**:
- Automated daily operations
- Power saving schedules
- Periodic tasks (backups, monitoring)

**Configuration**:
```json
{
  "command": "schedule-power-event-set",
  "event-list": [
    {
      "action": "startup",
      "enabled": 1,
      "enable-bits": 1,
      "hour": 8,
      "minute": 0,
      "dow-bits": 62
    }
  ]
}
```

**Web Interface**: Select "Timer mode" (定时模式) tab, add/remove timer events

### 4. Direct Power Mode (直连电源模式) - NEW

**Mode Value**: `2`
**Description**: Instant power control for battery-free or quick-response applications.

**Features**:
- **Auto power-on**: Instantly powers on when external power connects
- **Instant power-off**: Immediately powers off when external power disconnects
- **Battery handling**:
  - With battery: Performs graceful shutdown
  - Without battery: Instant power-off
- **Ignores battery level checks**: Powers on even if battery is too low

**Use Cases**:
- **Battery-free installations**: Ideal for devices without battery
- **Quick-response applications**: Immediate power state changes
- **Industrial automation**: Synchronized with external power control
- **Test environments**: Fast power cycling

**Advantages**:
- No delay in power state changes
- Works reliably without battery
- Simplifies power management in controlled environments

**Limitations**:
- Without battery: No graceful shutdown (instant power cut)
- Requires stable external power supply
- Not suitable for mobile/portable use

**Configuration**:
```json
{
  "command": "power-on-mode-set",
  "mode": 2
}
```

**Web Interface**: Select "Direct Power mode" (直连电源模式) tab

## API Reference

### power-on-mode-set Command

Sets the power-on mode for the device.

**Request**:
```json
{
  "command": "power-on-mode-set",
  "mode": 2
}
```

**Parameters**:
- `mode` (int): Power mode value
  - `0`: Manual mode - Do nothing when device is powered
  - `1`: Auto power-on - Auto power-on when device is powered
  - `2`: Direct Power mode - Instant power control (ignores battery checks)

**Response**:
```json
{
  "command": "power-on-mode-set",
  "code": 0
}
```

### power-on-mode-get Command

Retrieves the current power-on mode.

**Request**:
```json
{
  "command": "power-on-mode-get"
}
```

**Response**:
```json
{
  "command": "power-on-mode-get",
  "code": 0,
  "mode": 2
}
```

## Mode Comparison Table

| Feature | Manual Mode | Car/UPS Mode | Timer Mode | Direct Power Mode |
|---------|-------------|--------------|------------|-------------------|
| Auto power-on | ❌ | ✅ (with charger) | ✅ (scheduled) | ✅ (instant) |
| Auto power-off | ❌ | ✅ (delayed) | ✅ (scheduled) | ✅ (instant) |
| Battery required | ⚠️ Optional | ✅ Recommended | ⚠️ Optional | ❌ Not required |
| Graceful shutdown | ✅ Manual | ✅ Always | ✅ Always | ⚠️ Only with battery |
| Response time | Manual | ~Seconds | Scheduled | Instant |
| Battery checks | ✅ | ✅ | ✅ | ❌ Ignored |
| Use case | General | Car/UPS | Automation | Battery-free |

## Implementation Details

### Web Interface (timer.html)

The power mode selection is implemented in `/app/templates/timer.html` with four mode tabs:
- `unknown_mode_page`: Manual mode
- `car_mode_page`: Car/UPS mode
- `timer_mode_page`: Timer mode
- `force_power_mode_page`: Direct Power mode (NEW)

### Backend API (pcat_manager_web.py)

New API endpoint: `/api/v1/forcepowermode.json`

```python
@flask_app.route("/api/v1/forcepowermode.json", methods=['POST'])
def save_force_power_mode_action():
    json_data = request.get_json()
    state = json_data.get("forcemode") == 'true'

    # Set power-on-mode to 2 (force power mode)
    pc_app.socket_client.set_power_on_mode(2 if state else 0)

    # Clear any timer-based power events and charger auto-start
    pc_app.socket_client.set_schedule_power_events([])
    pc_app.socket_client.set_charger_on_auto_start(False, 0)

    return {"status": "ok"}
```

### Socket Client (pc_socket_client.py)

New method: `set_power_on_mode(mode)`

```python
def set_power_on_mode(self, mode):
    """
    Set power-on mode for the device.

    Args:
        mode (int): Power mode value
            0 - Manual mode (default)
            1 - Auto power-on mode
            2 - Direct power mode (ignores battery checks)
    """
    cmd = { "command": "power-on-mode-set", "mode": mode }
    # Send to pcat-manager via Unix socket
```

## Localization

The new mode includes translations in both English and Chinese:

**English**:
- `force_power_mode`: "Direct Power mode"
- `force_power_mode_tip_1`: "Direct Power Mode: Auto power-on when external power connects, instant power-off when disconnected"
- `force_power_mode_tip_2`: "With battery: graceful shutdown. Without battery: instant power-off. Ignores battery level checks. Ideal for battery-free setups or quick-response applications"

**Chinese**:
- `force_power_mode`: "直连电源模式"
- `force_power_mode_tip_1`: "直连电源模式：外接电源接通时自动开机，断开时立即关机"
- `force_power_mode_tip_2`: "有电池时正常安全关机；无电池时立即断电。忽略电池电量检查，适用于无电池场景或需要快速响应的应用"

## Migration Guide

### From Car/UPS Mode to Direct Power Mode

If you currently use Car/UPS mode but need faster response times or have no battery:

1. Navigate to `/timer` in the web interface
2. Select "Direct Power mode" (直连电源模式) tab
3. Click Save

**Note**: This will disable the delay timeout feature. Power will cut immediately when external power is removed.

### From Manual Mode to Direct Power Mode

For automation without scheduling:

1. Navigate to `/timer` in the web interface
2. Select "Direct Power mode" (直连电源模式) tab instead of "Manual mode"
3. Click Save

The device will now automatically respond to external power state changes.

## Troubleshooting

### Direct Power Mode Issues

**Problem**: Device doesn't power on when external power connects
- **Solution**: Verify that pcat-manager is running and mode is set to `2`
- **Check**: Run `power-on-mode-get` via socket API to confirm mode

**Problem**: Device shuts down immediately instead of gracefully
- **Cause**: No battery installed or battery critically low
- **Expected behavior**: This is normal for Direct Power Mode without battery

**Problem**: Settings don't persist after reboot
- **Solution**: Ensure pcat-manager saves configuration to persistent storage
- **Check**: Configuration should be stored in pcat-manager's config files

## Related Documentation

- [PCat Manager Socket API](../README.md)
- [Timer API Documentation](./api.md)
- [Power Management Guide](./ctrl_cmd.md)

## Version History

- **v1.0.0** (2026-03-10): Added Direct Power Mode (mode `2`) for battery-free and quick-response applications

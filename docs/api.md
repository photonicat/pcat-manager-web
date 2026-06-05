# Phonicat Manager Web API Documentation

This document provides comprehensive documentation for all API endpoints available in the Phonicat Manager Web application.

## Base URL
All API endpoints are relative to the base URL of your Phonicat device (typically `http://192.168.1.1/`)

## Authentication
Most API endpoints require authentication. You must first log in at `/login` to establish a session.

## Content Type
All POST requests should use `application/json` content type and include a valid CSRF token.

---

## System Management APIs

### Get Device Board Information
**GET** `/api/v1/system/board.json`

Returns OpenWrt board information including model, release details, and hardware specifications.

**Response:**
```json
{
  "kernel": "5.15.x",
  "hostname": "OpenWrt",
  "system": "ARMv8 Processor",
  "model": "Phonicat-N5",
  "board_name": "phonicat-n5",
  "release": {
    "distribution": "OpenWrt",
    "version": "23.05.0",
    "revision": "r23497-6637af95aa"
  }
}
```

### Get System Information
**GET** `/api/v1/system/info.json`

Returns system runtime information including uptime, load averages, memory usage, and local time.

**Response:**
```json
{
  "uptime": 86400,
  "load": [0.12, 0.15, 0.10],
  "memory": {
    "total": 1073741824,
    "free": 536870912,
    "shared": 10485760,
    "buffered": 52428800
  },
  "localtime": 1640995200
}
```

### System Control

#### Reboot System
**POST** `/system/reboot`

Initiates a system reboot. Requires authentication.

**Request:**
```json
{}
```

**Response:**
```json
{
  "status": "success",
  "message": "System will reboot in a few seconds"
}
```

#### Factory Reset
**POST** `/system/reset`

Performs a factory reset of the system. Requires authentication.

**Response:**
```json
{
  "status": "success",
  "message": "System will be reset to factory defaults"
}
```

#### Shutdown System
**POST** `/system/shutdown`

Safely shuts down the system. Requires authentication.

**Response:**
```json
{
  "status": "success",
  "message": "System will shutdown in a few seconds"
}
```

---

## Network Configuration APIs

### Wireless Configuration

#### Get Wireless Settings
**GET** `/api/v2/wireless.json`

Returns current wireless network configuration including all radio interfaces.

**Query Parameters:**
- `mock=true` (optional): Returns mock data for testing

**Response:**
```json
{
  "radio0": {
    "enabled": true,
    "ssid": "Phonicat-WiFi",
    "encryption": "psk2",
    "key": "password123",
    "hidden": false,
    "channel": "auto",
    "htmode": "HE20",
    "country": "US"
  }
}
```

#### Save Wireless Settings
**POST** `/api/v1/wireless.json`

Updates wireless network configuration. Requires authentication.

**Request:**
```json
{
  "radio0": {
    "enabled": true,
    "ssid": "MyNetwork",
    "encryption": "psk2",
    "key": "mypassword",
    "hidden": false,
    "channel": "auto",
    "htmode": "HE20",
    "country": "US"
  }
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Wireless settings saved successfully"
}
```

### Network Interfaces

#### Get Network Interfaces
**GET** `/api/v1/network_interfaces.json`

Returns current network interface configuration including WAN settings.

**Response:**
```json
{
  "multi_wan_enabled": false,
  "interfaces": {
    "wan": {
      "enabled": true,
      "proto": "dhcp"
    },
    "wan2": {
      "enabled": false,
      "proto": "none"
    }
  }
}
```

#### Save Network Interfaces
**POST** `/api/v1/network_interfaces.json`

Updates network interface configuration. Requires authentication.

**Request:**
```json
{
  "multi_wan": true,
  "wan_enabled": true,
  "wan2_enabled": true
}
```

### Connected Devices

#### Get WiFi Clients
**GET** `/api/v1/wifi_clients.json`

Returns list of currently connected WiFi devices.

**Response:**
```json
{
  "clients": [
    {
      "mac": "aa:bb:cc:dd:ee:ff",
      "hostname": "MyDevice",
      "ip": "192.168.1.100",
      "connected_time": 3600,
      "signal_strength": -45,
      "rx_bytes": 1048576,
      "tx_bytes": 524288
    }
  ]
}
```

---

## Mobile Network APIs

### APN Settings

#### Get APN Settings
**GET** `/api/v1/apn_settings.json`

Returns current APN (Access Point Name) configuration for mobile data.

**Response:**
```json
{
  "apn": "internet",
  "username": "",
  "password": "",
  "auth_type": "none",
  "pdp_type": "ipv4"
}
```

#### Save APN Settings
**POST** `/api/v1/apn_settings.json`

Updates APN configuration. Requires authentication.

**Request:**
```json
{
  "apn": "internet",
  "username": "user",
  "password": "pass",
  "auth_type": "pap",
  "pdp_type": "ipv4v6"
}
```

### Modem Information

#### Get Basic Modem Information
**GET** `/api/v1/modem/basic.json`

Returns basic modem status and information.

**Response:**
```json
{
  "signal_strength": 75,
  "network_type": "LTE",
  "operator": "Verizon",
  "phone_number": "+1234567890",
  "imei": "123456789012345",
  "sim_status": "ready",
  "registration_status": "registered_roaming",
  "data_usage": {
    "rx_bytes": 1073741824,
    "tx_bytes": 536870912
  }
}
```

#### Refresh Modem Information
**POST** `/api/v1/modem/basic.json`

Triggers a refresh of modem information.

**Request:**
```json
{
  "refresh": true
}
```

### Modem Configuration

#### Get Modem Configuration
**GET** `/api/v1/modem/config.json`

Returns current modem configuration settings.

**Response:**
```json
{
  "band_selection": "auto",
  "network_mode": "auto",
  "roaming_enabled": true
}
```

#### Save Modem Configuration
**POST** `/api/v1/modem/config.json`

Updates modem configuration settings.

**Request:**
```json
{
  "band_selection": "lte_only",
  "network_mode": "lte_only",
  "roaming_enabled": false
}
```

#### Reset Modem
**POST** `/api/v2/modem/reset.json`

Performs a factory reset of the modem including SMS clear.

**Response:**
```json
{
  "status": "success",
  "message": "Modem reset initiated"
}
```

#### Update SIM Number
**POST** `/api/v1/modem/sim_number.json`

Updates the displayed SIM card phone number.

**Request:**
```json
{
  "sim_number": "+1234567890"
}
```

#### Detect Phone Number
**GET** `/api/v1/modem/detect_my_number.json`

Attempts to auto-detect the phone number from the SIM card.

**Response:**
```json
{
  "phone_number": "+1234567890",
  "detection_method": "sim_card",
  "confidence": "high"
}
```

---

## SMS Management APIs

### List SMS Messages
**GET** `/api/v2/sms/list.json`

Returns list of SMS messages with pagination support.

**Query Parameters:**
- `page=1` (optional): Page number for pagination
- `limit=20` (optional): Number of messages per page

**Response:**
```json
{
  "messages": [
    {
      "id": "msg_123",
      "hash_id": "abc123",
      "from": "+1234567890",
      "content": "Hello world",
      "timestamp": "2024-01-01T12:00:00Z",
      "read": false,
      "direction": "incoming"
    }
  ],
  "total_count": 50,
  "page": 1,
  "limit": 20,
  "has_more": true
}
```

### Send SMS
**POST** `/api/v1/modem/sms/send.json`

Sends an SMS message.

**Request:**
```json
{
  "send_to": "+1234567890",
  "msg": "Hello from Phonicat"
}
```

**Response:**
```json
{
  "status": "success",
  "message": "SMS sent successfully",
  "message_id": "sent_456"
}
```

### Send SMS (Version 2)
**POST** `/api/v2/modem/sms/send.json`

Enhanced SMS sending with additional options.

**Request:**
```json
{
  "send_to": "+1234567890",
  "msg": "Hello world",
  "options": {
    "delivery_report": true
  }
}
```

### Mark SMS as Read
**POST** `/api/v1/modem/sms/read.json`

Marks an SMS message as read.

**Request:**
```json
{
  "hash_id": "abc123"
}
```

### Delete SMS
**POST** `/api/v1/sms/delete.json`

Deletes one or more SMS messages.

**Request:**
```json
{
  "hash_ids": ["abc123", "def456"]
}
```

### Clear All SMS
**POST** `/api/v2/sms/clear_all.json`

Deletes all SMS messages from the device.

**Response:**
```json
{
  "status": "success",
  "message": "All SMS messages cleared",
  "deleted_count": 25
}
```

---

## Voice Call APIs

### Make Voice Call
**POST** `/api/v1/voice_call/make`

Initiates an outgoing voice call.

**Request:**
```json
{
  "number": "+1234567890"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Call initiated successfully",
  "call_id": "call_123"
}
```

### Answer Voice Call
**POST** `/api/v1/voice_call/answer`

Answers an incoming voice call.

**Response:**
```json
{
  "success": true,
  "message": "Call answered successfully"
}
```

### Hangup Voice Call
**POST** `/api/v1/voice_call/hangup`

Ends the current voice call.

**Response:**
```json
{
  "success": true,
  "message": "Call ended successfully"
}
```

### Get Call Status
**GET** `/api/v1/voice_call/status`

Returns current voice call status.

**Response:**
```json
{
  "active_call": true,
  "call_state": "connected",
  "call_duration": 120,
  "remote_number": "+1234567890",
  "call_direction": "outgoing"
}
```

### Set Audio Path
**POST** `/api/v1/voice_call/audio_path`

Sets the audio path for voice calls (speaker/earpiece).

**Request:**
```json
{
  "path": 1
}
```
*Note: 1 = speaker, 0 = earpiece*

### Get Call History
**GET** `/api/v1/voice_call/history`

Returns voice call history.

**Response:**
```json
{
  "calls": [
    {
      "id": "call_123",
      "number": "+1234567890",
      "direction": "outgoing",
      "start_time": "2024-01-01T12:00:00Z",
      "duration": 120,
      "status": "completed"
    }
  ]
}
```

### Clear Call History
**DELETE** `/api/v1/voice_call/history`

Clears all call history records.

**Response:**
```json
{
  "success": true,
  "message": "Call history cleared successfully"
}
```

### Delete Specific Call Record
**DELETE** `/api/v1/voice_call/history/<call_id>`

Deletes a specific call history record.

**Response:**
```json
{
  "success": true,
  "message": "Call record deleted successfully"
}
```

### Check Incoming Call
**GET** `/api/v1/voice_call/incoming`

Checks for incoming call status.

**Response:**
```json
{
  "success": true,
  "incoming_call": {
    "active": false,
    "number": null,
    "ring_count": 0,
    "duration": 0
  }
}
```

---

## Device Control APIs

### Power LED Settings

#### Get Power LED Settings
**GET** `/api/v1/power_led_settings.json`

Returns current power LED configuration.

**Response:**
```json
{
  "power_led_enabled": true,
  "brightness": 80
}
```

#### Save Power LED Settings
**POST** `/api/v1/power_led_settings.json`

Updates power LED configuration.

**Request:**
```json
{
  "power_led_enabled": true,
  "brightness": 90
}
```

### Beeper Settings

#### Get Beeper Settings
**GET** `/api/v1/beeper_settings.json`

Returns current beeper configuration.

**Response:**
```json
{
  "beeper_enabled": true,
  "volume": 75
}
```

#### Save Beeper Settings
**POST** `/api/v1/beeper_settings.json`

Updates beeper configuration.

**Request:**
```json
{
  "beeper_enabled": false,
  "volume": 50
}
```

### Device Settings

#### Save General Settings
**POST** `/api/v1/settings.json`

Updates general device settings.

**Request:**
```json
{
  "energy_saving_mode": true
}
```

---

## Timer and Scheduling APIs

### Get Time Information
**GET** `/api/v1/time.json`

Returns current system time and timezone information.

**Response:**
```json
{
  "current_time": "2024-01-01T12:00:00Z",
  "timezone": "UTC",
  "uptime": 86400
}
```

### Timer Management

#### Get Scheduled Timers
**GET** `/api/v1/timers.json`

Returns list of configured power scheduling timers.

**Response:**
```json
{
  "timers": [
    {
      "id": 1,
      "hour": 22,
      "minute": 0,
      "action": "shutdown",
      "days": [1, 2, 3, 4, 5],
      "enabled": true
    },
    {
      "id": 2,
      "hour": 6,
      "minute": 0,
      "action": "startup",
      "days": [1, 2, 3, 4, 5, 6, 7],
      "enabled": true
    }
  ]
}
```

#### Save Scheduled Timers
**POST** `/api/v1/timers.json`

Updates power scheduling timer configuration.

**Request:**
```json
{
  "timers": [
    {
      "hour": 23,
      "minute": 30,
      "action": "shutdown",
      "days": [1, 2, 3, 4, 5],
      "enabled": true
    }
  ]
}
```

### Car Boot Mode
**POST** `/api/v1/carbootmode.json`

Configures car boot mode settings.

**Request:**
```json
{
  "carmode": "true",
  "timeout": 45
}
```

---

## Statistics and Monitoring APIs

### Dashboard Data
**GET** `/api/v1/dashboard.json`

Returns dashboard overview data including system status and quick stats.

**Response:**
```json
{
  "signal_strength": 75,
  "data_usage": {
    "rx_bytes": 1073741824,
    "tx_bytes": 536870912
  },
  "connected_devices": 3,
  "uptime": 86400,
  "cpu_usage": 15.5,
  "memory_usage": 45.2,
  "temperature": 42
}
```

### Status Information
**GET** `/api/v1/status2.json` ⚠️ **DEPRECATED**

Returns detailed system status information.

> **Note:** This endpoint is deprecated. Use `/api/v1/dashboard.json` instead for current system status information.

**Response:**
```json
{
  "system": {
    "uptime": 86400,
    "load": [0.12, 0.15, 0.10],
    "memory_usage": 45.2,
    "cpu_usage": 15.5
  },
  "network": {
    "wan_status": "connected",
    "wan_ip": "203.0.113.1",
    "dns_servers": ["8.8.8.8", "8.8.4.4"]
  },
  "modem": {
    "status": "connected",
    "signal_strength": 75,
    "operator": "Verizon"
  }
}
```

### Data Statistics
**GET** `/api/v1/data_stats.json`

Returns detailed data usage statistics.

**Query Parameters:**
- `network_type` (optional): Filter by network type (e.g., "lte", "wifi")

**Response:**
```json
{
  "interfaces": {
    "wwan0": {
      "rx_bytes": 1073741824,
      "tx_bytes": 536870912,
      "rx_packets": 1000000,
      "tx_packets": 800000
    },
    "wlan0": {
      "rx_bytes": 2147483648,
      "tx_bytes": 1073741824,
      "rx_packets": 2000000,
      "tx_packets": 1500000
    }
  },
  "total": {
    "rx_bytes": 3221225472,
    "tx_bytes": 1610612736
  }
}
```

#### Reset Statistics
**POST** `/statistics/reset`

Resets network usage statistics counters.

**Response:**
```json
{
  "status": "success",
  "message": "Statistics reset successfully"
}
```

---

## Screen Management APIs

### Screen Configuration

#### Get User Screen Configuration
**GET** `/api/v1/screen/user_config.json`

Returns user-configured screen settings.

**Response:**
```json
{
  "brightness": 80,
  "auto_sleep": true,
  "sleep_timeout": 300,
  "display_mode": "info"
}
```

#### Save User Screen Configuration
**POST** `/api/v1/screen/user_config.json`

Updates user screen configuration.

**Request:**
```json
{
  "brightness": 90,
  "auto_sleep": false,
  "sleep_timeout": 600,
  "display_mode": "clock"
}
```

#### Get Default Screen Configuration
**GET** `/api/v1/screen/default_config.json`

Returns default screen configuration settings.

### Screen Control

#### Get Screen Frame
**GET** `/api/v1/screen/frame.png`

Returns current screen frame as PNG image.

#### Restart Screen
**GET** `/api/v1/screen/restart`

Restarts the screen display service.

**Response:**
```json
{
  "status": "success",
  "message": "Screen restarted successfully"
}
```

#### Screen Short Click
**GET** `/api/v1/screen/short_click`

Simulates a short button press on the screen.

**Response:**
```json
{
  "status": "success",
  "message": "Short click executed"
}
```

---

## System Configuration APIs

### CPU Scheduling
**GET/POST** `/api/v1/cpu_scheduling.json`

Get or set CPU scheduling configuration for power management.

**GET Response:**
```json
{
  "cpu_scheduling_enabled": true
}
```

**POST Request:**
```json
{
  "cpu_scheduling_enabled": false
}
```

### Fan Speed Control

#### Get Fan Speed Settings
**GET** `/api/v1/fan_speed.json`

Returns current fan speed configuration.

**Response:**
```json
{
  "fan_speed": 5
}
```

#### Set Fan Speed
**POST** `/api/v1/fan_speed.json`

Updates fan speed setting (1-10 scale).

**Request:**
```json
{
  "fan_speed": 8
}
```

#### Get Actual Fan Speed
**GET** `/api/v1/fan_speed_actual.json`

Returns actual measured fan speed in RPM.

**Response:**
```json
{
  "actual_fan_speed": 2500,
  "target_speed": 5
}
```

### SMS Screen Limit
**GET/POST** `/api/v1/sms_limit_for_screen.json`

Configure how many SMS messages to show on the screen display.

**GET Response:**
```json
{
  "sms_limit_for_screen": 10
}
```

**POST Request:**
```json
{
  "sms_limit_for_screen": 15
}
```

---

## Development and Maintenance APIs

### Development Server Check
**GET** `/api/v1/dev_server_check.json`

Checks if running in development server mode.

**Response:**
```json
{
  "is_dev_server": false,
  "skip_auth": false
}
```

### Firmware Management

#### Check Firmware Update
**GET** `/api/v1/firmware_update_check.json`

Checks for available firmware updates.

**Response:**
```json
{
  "update_available": true,
  "current_version": "1.2.3",
  "latest_version": "1.3.0",
  "download_url": "https://example.com/firmware.bin",
  "release_notes": "Bug fixes and improvements"
}
```

#### Get Firmware Upgrade Status
**GET** `/api/v1/firmware_upgrade_status.json`

Returns current firmware upgrade progress.

**Response:**
```json
{
  "status": "downloading",
  "progress": 45,
  "message": "Downloading firmware (45%)",
  "can_cancel": true
}
```

### Package Management

#### Get Package Versions
**GET** `/pkg_versions`

Returns version information for installed packages.

**Response:**
```json
{
  "versions": {
    "pcat-manager": "2.1.0",
    "pcat-manager-web": "1.5.2"
  }
}
```

#### Upload Package
**POST** `/pkg_upload`

Uploads a package file for installation. Requires multipart/form-data.

#### Update Package
**GET** `/pkg_update`

Initiates package update process.

### SMS Hook Management

#### Get SMS Hook Script
**GET** `/api/v1/modem/hook_script.json`

Returns current SMS hook script configuration.

**Response:**
```json
{
  "script": "#!/bin/bash\necho 'SMS received'",
  "enabled": true
}
```

#### Save SMS Hook Script
**POST** `/api/v1/modem/hook_script.json`

Updates SMS hook script configuration.

**Request:**
```json
{
  "script": "#!/bin/bash\necho 'New SMS hook'",
  "enabled": true
}
```

#### Get Hook Execution History
**GET** `/api/v1/modem/hook_history.json`

Returns SMS hook execution history with pagination.

**Query Parameters:**
- `page=1` (optional): Page number
- `limit=20` (optional): Records per page
- `success_only=true` (optional): Show only successful executions

**Response:**
```json
{
  "history": [
    {
      "id": 1,
      "execution_time": "2024-01-01T12:00:00Z",
      "success": true,
      "execution_time_ms": 150,
      "sms_from": "+1234567890",
      "sms_content": "Test message",
      "stdout": "Hook executed successfully",
      "stderr": "",
      "exit_code": 0
    }
  ],
  "total_count": 50,
  "page": 1,
  "has_more": true
}
```

#### Get Hook History Details
**GET** `/api/v1/modem/hook_history/<history_id>.json`

Returns detailed information for a specific hook execution.

#### Cleanup Hook History
**DELETE** `/api/v1/modem/hook_history.json`

Removes old hook execution history records.

**Query Parameters:**
- `older_than_days=30` (optional): Delete records older than specified days

#### Test Hook History Insert
**POST** `/api/v1/modem/hook_history_insert_test.json`

Inserts a test hook history record for testing purposes.

#### Create Hook History Table
**POST** `/api/v1/modem/hook_history_create_table.json`

Manually creates the hook history database table.

#### Test Hook History
**GET** `/api/v1/modem/hook_history_test.json`

Tests hook history database functionality.

---

## WebSocket Events

The application uses SocketIO for real-time communication. Connect to the WebSocket at the root URL.

### Client Events (Send to Server)

#### client_connected
Sent when client establishes connection.

```json
{
  "message": "Client connected"
}
```

#### request_install
Request local package installation.

```json
{
  "filename": "package.ipk"
}
```

#### request_install_package
Request package installation from repository.

```json
{
  "package": "package-name"
}
```

#### request_firmware_upgrade
Request automatic firmware upgrade.

```json
{
  "upgrade_url": "https://example.com/firmware.bin"
}
```

#### get_firmware_upgrade_status_socket
Request firmware upgrade status update.

#### cancel_firmware_upgrade
Cancel ongoing firmware upgrade (during download only).

### Server Events (Receive from Server)

#### connected_response
Server confirmation of connection.

```json
{
  "data": "Welcome\n"
}
```

#### log_response
Real-time log messages during operations.

```json
{
  "data": "Installation progress...",
  "ts": "12:00:00"
}
```

#### firmware_upgrade_status
Firmware upgrade progress updates.

```json
{
  "status": "downloading",
  "progress": 45,
  "message": "Downloading firmware (45%)"
}
```

---

## Error Responses

All API endpoints return consistent error responses:

```json
{
  "status": "error",
  "message": "Descriptive error message",
  "error_code": "SPECIFIC_ERROR_CODE"
}
```

Common HTTP status codes:
- `200`: Success
- `400`: Bad Request (invalid parameters)
- `401`: Unauthorized (login required)
- `403`: Forbidden (insufficient permissions)
- `404`: Not Found
- `500`: Internal Server Error

## Rate Limiting

Some endpoints may have rate limiting applied, particularly:
- SMS sending: Limited to prevent spam
- System control operations: Limited for safety
- File uploads: Limited by size and frequency

## CSRF Protection

All state-changing requests (POST, PUT, DELETE) require CSRF token validation. Include the token in:
- Form field: `csrf_token`
- HTTP header: `X-CSRF-Token`
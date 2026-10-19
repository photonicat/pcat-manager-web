#!/usr/bin/env python3
"""
Platform detection and mock data system for cross-platform development.
Provides fake data patterns when running on non-OpenWrt systems.
"""

import platform
import os
import time
import random
import json
import tempfile
import subprocess
import math
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import threading

# Platform detection
def is_openwrt() -> bool:
    """Detect if running on OpenWrt ARM device"""
    try:
        # Primary check: OpenWrt-specific device tree files
        if os.path.exists("/proc/device-tree/compatible"):
            with open("/proc/device-tree/compatible", "r") as f:
                content = f.read()
                if "photonicat" in content.lower():
                    return True
        
        # Secondary check: Look for OpenWrt-specific files/directories
        openwrt_indicators = [
            "/etc/openwrt_release",
            "/etc/openwrt_version", 
            "/usr/lib/opkg/info",
            "/etc/config/system"
        ]
        
        for indicator in openwrt_indicators:
            if os.path.exists(indicator):
                return True
        
        # Check if we're on ARM architecture (OpenWrt target)
        if platform.machine().startswith(('arm', 'aarch64')):
            # Additional OpenWrt-specific checks for ARM
            if os.path.exists("/usr/bin/opkg") or os.path.exists("/sbin/uci"):
                return True
            
        return False
    except:
        return False

IS_OPENWRT = is_openwrt()
IS_MOCK_MODE = not IS_OPENWRT

print(f"Platform detection: OpenWrt={IS_OPENWRT}, Mock mode={IS_MOCK_MODE}")

class MockDataGenerator:
    """Generate realistic fake data patterns for development"""
    
    def __init__(self):
        self.start_time = datetime.now()
        self.lock = threading.Lock()
        
        # Network interface patterns
        self.network_interfaces = {
            "eth0": {"mac": "aa:bb:cc:dd:ee:f0", "status": "up"},
            "wlan0": {"mac": "aa:bb:cc:dd:ee:f1", "status": "up"}, 
            "wwan0": {"mac": "aa:bb:cc:dd:ee:f2", "status": "up"}
        }
        
        # SMS message patterns
        self.sms_messages = self._generate_fake_sms()
        
        # System stats baseline
        self.base_rx_bytes = 1024 * 1024 * random.randint(100, 1000)
        self.base_tx_bytes = 1024 * 1024 * random.randint(50, 500)
        
    def _generate_fake_sms(self) -> List[Dict]:
        """Generate realistic SMS messages"""
        messages = []
        operators = ["Verizon", "AT&T", "T-Mobile", "Cricket"]
        message_types = [
            "Your account balance is $25.43. Text STOP to opt out.",
            "Service update: Your plan now includes 5G access.",
            "Data usage alert: You've used 80% of your monthly allowance.",
            "Welcome to our network! Your service is now active.",
            "Bill payment received. Thank you for your payment of $45.00"
        ]
        
        for i in range(random.randint(3, 15)):
            timestamp = self.start_time - timedelta(hours=random.randint(1, 720))
            messages.append({
                "index": i + 1,
                "status": random.choice(["REC READ", "REC UNREAD"]),
                "sender": f"+1{random.randint(1000000000, 9999999999)}",
                "timestamp": timestamp.strftime("%y/%m/%d,%H:%M:%S"),
                "content": random.choice(message_types),
                "storage": "ME"
            })
        
        return sorted(messages, key=lambda x: x["timestamp"], reverse=True)
    
    def get_device_tree_compatible(self) -> str:
        """Mock device tree compatible string"""
        devices = [
            "phytiumpi,d2000-devboard-ddr4\0phytiumpi,d2000\0",
            "photonicat,v2\0mediatek,mt7981\0",
            "photonicat2,router\0mediatek,mt7986a\0"
        ]
        return random.choice(devices)
    
    def get_network_interface_mac(self, interface: str) -> str:
        """Get MAC address for network interface"""
        if interface in self.network_interfaces:
            return self.network_interfaces[interface]["mac"]
        # Generate consistent fake MAC
        mac_int = hash(interface) % (256**6)
        mac_bytes = [(mac_int >> (8*i)) & 0xff for i in range(6)]
        return ":".join(f"{b:02x}" for b in mac_bytes)
    
    def get_emmc_serial(self) -> str:
        """Generate consistent eMMC serial"""
        return f"0x{random.randint(0x10000000, 0xffffffff):08x}"
    
    def get_system_stats(self) -> Dict[str, Any]:
        """Generate realistic system statistics"""
        uptime_seconds = (datetime.now() - self.start_time).total_seconds()
        
        # Add some growth patterns to network stats
        growth_factor = 1 + (uptime_seconds / 3600) * 0.1  # 10% per hour
        
        return {
            "uptime": int(uptime_seconds),
            "load_avg": [
                round(0.1 + random.random() * 0.8, 2),
                round(0.15 + random.random() * 0.7, 2), 
                round(0.2 + random.random() * 0.6, 2)
            ],
            "memory": {
                "total": 1024 * 1024 * 1024,  # 1GB
                "available": int(1024 * 1024 * 1024 * (0.3 + random.random() * 0.4)),
                "used": int(1024 * 1024 * 1024 * (0.2 + random.random() * 0.5))
            },
            "network": {
                "rx_bytes": int(self.base_rx_bytes * growth_factor),
                "tx_bytes": int(self.base_tx_bytes * growth_factor),
                "rx_packets": int(self.base_rx_bytes * growth_factor / 1024),
                "tx_packets": int(self.base_tx_bytes * growth_factor / 1024)
            }
        }
    
    def get_modem_status(self) -> Dict[str, Any]:
        """Generate realistic modem status"""
        operators = ["Verizon", "AT&T", "T-Mobile", "Cricket Wireless"]
        technologies = ["LTE", "5G", "UMTS", "GSM"]
        
        # Simulate connection quality variations
        signal_quality = 15 + random.randint(-5, 20)  # Range: 10-35
        
        return {
            "sim_status": "READY",
            "network_status": "REGISTERED_HOME",
            "operator": random.choice(operators),
            "technology": random.choice(technologies),
            "signal_quality": max(0, min(31, signal_quality)),
            "sim_ccid": f"89014103271234567890{random.randint(10, 99)}",
            "sim_cimi": f"31041012345678{random.randint(100, 999)}",
            "phone_number": f"+1{random.randint(2000000000, 9999999999)}",
            "apn_name": "internet",
            "ip_address": f"10.{random.randint(100, 254)}.{random.randint(1, 254)}.{random.randint(2, 254)}",
            "data_session": "ACTIVE" if random.random() > 0.1 else "INACTIVE"
        }
    
    def get_battery_status(self) -> Dict[str, Any]:
        """Generate realistic battery status with patterns"""
        # Simulate battery discharge/charge cycles
        minutes = (datetime.now() - self.start_time).total_seconds() / 60
        base_level = 50 + 30 * abs(math.sin(minutes / 30))  # 30 min cycle
        
        is_charging = random.random() > 0.7
        if is_charging:
            base_level = min(100, base_level + 20)
        
        return {
            "on_charging": is_charging,
            "charge_percent": int(base_level * 100),  # 0-10000 scale
            "battery_voltage": int(3700 + random.randint(-200, 500)),  # mV
            "charger_voltage": 5035 if is_charging else 0,
            "battery_current": random.randint(-500, 2000),  # mA
            "board_temperature": 25.0 + random.random() * 20.0  # °C
        }
    
    def get_wireless_config(self) -> str:
        """Generate UCI wireless configuration output"""
        return f"""wireless.radio0=wifi-device
wireless.radio0.type='mac80211'
wireless.radio0.path='pci0000:00/0000:00:1e.0'
wireless.radio0.band='2g'
wireless.radio0.channel='auto'
wireless.radio0.htmode='HT20'
wireless.radio0.disabled='0'

wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
wireless.default_radio0.network='lan'
wireless.default_radio0.mode='ap'
wireless.default_radio0.ssid='Photonicat-{random.randint(1000, 9999)}'
wireless.default_radio0.encryption='psk2'
wireless.default_radio0.key='testpassword'

wireless.radio1=wifi-device
wireless.radio1.type='mac80211'
wireless.radio1.path='platform/soc/18000000.wifi'
wireless.radio1.band='5g'
wireless.radio1.channel='auto'
wireless.radio1.htmode='VHT80'
wireless.radio1.disabled='0'

wireless.default_radio1=wifi-iface
wireless.default_radio1.device='radio1'
wireless.default_radio1.network='lan'
wireless.default_radio1.mode='ap'
wireless.default_radio1.ssid='Photonicat-5G-{random.randint(1000, 9999)}'
wireless.default_radio1.encryption='psk2'
wireless.default_radio1.key='testpassword'
"""

# Global mock data generator
mock_data = MockDataGenerator()

class MockFileSystem:
    """Mock filesystem paths for cross-platform development"""
    
    @staticmethod
    def create_mock_files():
        """Create mock system files in temp directory"""
        if IS_OPENWRT:
            return  # Don't create mocks on real OpenWrt
            
        temp_dir = tempfile.gettempdir()
        mock_root = os.path.join(temp_dir, "openwrt_mock")
        
        # Create directory structure
        dirs = [
            "proc/device-tree",
            "sys/class/net/eth0",
            "sys/class/net/wlan0", 
            "sys/class/net/wwan0",
            "sys/class/block/mmcblk0/device",
            "sys/devices/system/cpu/cpu0/cpufreq",
            "sys/class/hwmon/hwmon0",
            "sys/class/thermal/cooling_device0",
            "tmp",
            "etc"
        ]
        
        for d in dirs:
            os.makedirs(os.path.join(mock_root, d), exist_ok=True)
        
        # Create mock files
        mock_files = {
            "proc/device-tree/compatible": mock_data.get_device_tree_compatible(),
            "sys/class/net/eth0/address": mock_data.get_network_interface_mac("eth0"),
            "sys/class/net/wlan0/address": mock_data.get_network_interface_mac("wlan0"),
            "sys/class/net/wwan0/address": mock_data.get_network_interface_mac("wwan0"),
            "sys/class/block/mmcblk0/device/serial": mock_data.get_emmc_serial(),
            "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor": "schedutil",
            "sys/class/hwmon/hwmon0/fan1_input": str(random.randint(1000, 3000)),
            "sys/class/thermal/cooling_device0/type": "pcat-pm-fan",
            "sys/class/thermal/cooling_device0/cur_state": "5"
        }
        
        for file_path, content in mock_files.items():
            full_path = os.path.join(mock_root, file_path)
            with open(full_path, 'w') as f:
                f.write(content)
        
        return mock_root

def get_mock_root() -> Optional[str]:
    """Get mock filesystem root directory"""
    if IS_OPENWRT:
        return "/"
    
    temp_dir = tempfile.gettempdir()
    mock_root = os.path.join(temp_dir, "openwrt_mock")
    
    if not os.path.exists(mock_root):
        return MockFileSystem.create_mock_files()
    
    return mock_root

# Mock command execution
def mock_subprocess_run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Mock subprocess.run for UCI and other OpenWrt commands"""
    if IS_OPENWRT:
        return subprocess.run(cmd, **kwargs)  # Use real commands on OpenWrt
    
    # Mock UCI commands
    if cmd[0] == "uci":
        if "show" in cmd and "wireless" in cmd:
            output = mock_data.get_wireless_config().encode()
            return subprocess.CompletedProcess(cmd, 0, stdout=output)
        elif "show" in cmd:
            # Generic UCI show command
            output = b"mock.config='value'"
            return subprocess.CompletedProcess(cmd, 0, stdout=output)
        elif "set" in cmd or "commit" in cmd:
            # Mock set/commit operations
            return subprocess.CompletedProcess(cmd, 0)
    
    # Mock other system commands
    if cmd[0] in ["iwinfo", "ip", "iptables"]:
        return subprocess.CompletedProcess(cmd, 0, stdout=b"mock output")
    
    # Default mock response
    return subprocess.CompletedProcess(cmd, 1, stderr=b"Command not found in mock mode")

# Export mock utilities
__all__ = [
    'IS_OPENWRT', 'IS_MOCK_MODE', 'mock_data', 'get_mock_root', 
    'mock_subprocess_run', 'MockDataGenerator', 'MockFileSystem'
]
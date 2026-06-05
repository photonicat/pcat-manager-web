# -*- coding: utf-8 -*-
# Dummy hardware interface for testing on x86/x64 machines

import os
import random
import json
import tempfile
import shutil
from pathlib import Path

class DummyHardware:
    """
    Creates and manages dummy hardware files for testing on x86/x64 machines.
    """
    
    def __init__(self):
        # Create a temporary directory for dummy hardware files
        self.temp_dir = tempfile.mkdtemp(prefix="pcat_dummy_")
        self.setup_dummy_files()
        print(f"[DUMMY] Created dummy hardware directory: {self.temp_dir}")
    
    def setup_dummy_files(self):
        """Create all necessary dummy hardware files"""
        
        # Battery power supply files
        battery_dir = Path(self.temp_dir) / "sys/class/power_supply/battery"
        battery_dir.mkdir(parents=True, exist_ok=True)
        
        # Battery files with dummy values
        (battery_dir / "current_now").write_text("500000")  # microamps
        (battery_dir / "energy_full_design").write_text("10000000")  # microjoules
        (battery_dir / "capacity").write_text("85")  # percentage
        (battery_dir / "status").write_text("Charging")
        (battery_dir / "power_now").write_text("1850000")  # microwatts
        
        # Network interface files
        net_dir = Path(self.temp_dir) / "sys/class/net/eth0"
        net_dir.mkdir(parents=True, exist_ok=True)
        (net_dir / "address").write_text("00:11:22:33:44:55")
        (net_dir / "speed").write_text("1000")  # Mbps
        (net_dir / "carrier").write_text("1")  # Link up
        
        # Network statistics
        stats_dir = net_dir / "statistics"
        stats_dir.mkdir(exist_ok=True)
        (stats_dir / "rx_bytes").write_text(str(random.randint(1000000, 10000000)))
        (stats_dir / "tx_bytes").write_text(str(random.randint(1000000, 10000000)))
        
        # Block device (SD card) serial
        block_dir = Path(self.temp_dir) / "sys/class/block/mmcblk0/device"
        block_dir.mkdir(parents=True, exist_ok=True)
        (block_dir / "serial").write_text("0x12345678")
        
        # Proc files
        proc_dir = Path(self.temp_dir) / "proc"
        proc_dir.mkdir(exist_ok=True)
        
        # Uptime
        (proc_dir / "uptime").write_text("12345.67 98765.43")
        
        # Device tree compatible (for board detection)
        device_tree_dir = proc_dir / "device-tree"
        device_tree_dir.mkdir(exist_ok=True)
        (device_tree_dir / "compatible").write_text("rockchip,rk3568-photonicat\0")
        
        # DHCP leases
        tmp_dir = Path(self.temp_dir) / "tmp"
        tmp_dir.mkdir(exist_ok=True)
        
        # Create dummy DHCP leases file
        dhcp_leases = [
            "1234567890 00:11:22:33:44:55 192.168.1.100 device1 *",
            "1234567891 00:11:22:33:44:56 192.168.1.101 device2 *",
        ]
        (tmp_dir / "dhcp.leases").write_text("\n".join(dhcp_leases))
        
        # Create pcat-manager-web-bootup marker
        (tmp_dir / "pcat-manager-web-bootup").touch()
        
        # Device files
        dev_dir = Path(self.temp_dir) / "dev"
        dev_dir.mkdir(exist_ok=True)
        
        # Machine serial number file (boot1 JSON format)
        import json as _json
        boot1_data = _json.dumps({
            "machine_sn": "DUMMY-SN-123456",
            "emmc_sn": "0x12345678",
            "signature": "",
            "oem": {
                "name": "Dev Router",
                "id": "dev",
                "company_url": "",
                "forum_url": "",
                "wiki_url": "",
                "tips_url": "",
                "ip_check_urls": ["https://api.ipify.org"],
                "images": {}
            }
        })
        (dev_dir / "mmcblk0boot1").write_bytes(boot1_data.encode("utf-8") + b"\x00" + b"\xff" * 100)
        
        # SD card device (simulate presence)
        (dev_dir / "mmcblk1").touch()
        
        # Thermal/cooling devices for fan control
        thermal_dir = Path(self.temp_dir) / "sys/class/thermal"
        thermal_dir.mkdir(exist_ok=True)
        
        cooling_device = thermal_dir / "cooling_device0"
        cooling_device.mkdir(exist_ok=True)
        (cooling_device / "type").write_text("pcat-pm-fan")
        (cooling_device / "cur_state").write_text("5")  # Fan speed 5/9
        
        # Hwmon for fan RPM
        hwmon_dir = Path(self.temp_dir) / "sys/class/hwmon/hwmon0"
        hwmon_dir.mkdir(parents=True, exist_ok=True)
        (hwmon_dir / "fan1_input").write_text("2500")  # 2500 RPM
        
        # CPU frequency governor
        cpu_dir = Path(self.temp_dir) / "sys/devices/system/cpu/cpu0/cpufreq"
        cpu_dir.mkdir(parents=True, exist_ok=True)
        (cpu_dir / "scaling_governor").write_text("schedutil")
        
        # Create additional CPU cores
        for i in range(1, 4):
            cpu_dir = Path(self.temp_dir) / f"sys/devices/system/cpu/cpu{i}/cpufreq"
            cpu_dir.mkdir(parents=True, exist_ok=True)
            (cpu_dir / "scaling_governor").write_text("schedutil")
    
    def get_path(self, original_path):
        """
        Convert an original hardware path to the dummy path.
        """
        if original_path.startswith("/sys/"):
            return str(Path(self.temp_dir) / original_path[1:])
        elif original_path.startswith("/proc/"):
            return str(Path(self.temp_dir) / original_path[1:])
        elif original_path.startswith("/dev/"):
            return str(Path(self.temp_dir) / original_path[1:])
        elif original_path.startswith("/tmp/"):
            return str(Path(self.temp_dir) / original_path[1:])
        return original_path
    
    def update_dynamic_values(self):
        """
        Update dynamic values to simulate changing hardware state.
        """
        # Update battery level
        battery_dir = Path(self.temp_dir) / "sys/class/power_supply/battery"
        current_capacity = int((battery_dir / "capacity").read_text())
        
        # Simulate charging/discharging
        status = (battery_dir / "status").read_text().strip()
        if status == "Charging":
            new_capacity = min(100, current_capacity + random.randint(0, 2))
        else:
            new_capacity = max(10, current_capacity - random.randint(0, 1))
        
        (battery_dir / "capacity").write_text(str(new_capacity))
        
        # Update network statistics
        stats_dir = Path(self.temp_dir) / "sys/class/net/eth0/statistics"
        rx_bytes = int((stats_dir / "rx_bytes").read_text())
        tx_bytes = int((stats_dir / "tx_bytes").read_text())
        
        (stats_dir / "rx_bytes").write_text(str(rx_bytes + random.randint(100, 10000)))
        (stats_dir / "tx_bytes").write_text(str(tx_bytes + random.randint(100, 10000)))
        
        # Update uptime
        proc_dir = Path(self.temp_dir) / "proc"
        uptime_parts = (proc_dir / "uptime").read_text().split()
        new_uptime = float(uptime_parts[0]) + 1.0
        new_idle = float(uptime_parts[1]) + 0.8
        (proc_dir / "uptime").write_text(f"{new_uptime:.2f} {new_idle:.2f}")
        
        # Update fan RPM based on cur_state
        cooling_device = Path(self.temp_dir) / "sys/class/thermal/cooling_device0"
        fan_state = int((cooling_device / "cur_state").read_text())
        fan_rpm = 1000 + (fan_state * 300)  # Scale RPM based on state
        
        hwmon_dir = Path(self.temp_dir) / "sys/class/hwmon/hwmon0"
        (hwmon_dir / "fan1_input").write_text(str(fan_rpm))
    
    def cleanup(self):
        """Clean up temporary directory"""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            print(f"[DUMMY] Cleaned up dummy hardware directory: {self.temp_dir}")

# Global instance
dummy_hardware = None

def init_dummy_hardware():
    """Initialize the dummy hardware singleton"""
    global dummy_hardware
    if dummy_hardware is None:
        dummy_hardware = DummyHardware()
    return dummy_hardware

def get_dummy_path(original_path):
    """Get the dummy version of a hardware path"""
    if dummy_hardware:
        return dummy_hardware.get_path(original_path)
    return original_path

def cleanup_dummy_hardware():
    """Clean up dummy hardware resources"""
    global dummy_hardware
    if dummy_hardware:
        dummy_hardware.cleanup()
        dummy_hardware = None
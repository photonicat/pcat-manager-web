#!/usr/bin/env python3
"""
Mock hardware implementations for cross-platform development.
Replaces hardware-specific functionality with realistic fake data.
"""

import threading
import time
import queue
import random
import json
import socket
import tempfile
import os
from datetime import datetime
from typing import Dict, List, Any, Optional

from platform_mock import IS_MOCK_MODE, mock_data


class MockSerial:
    """Mock serial port for modem communication"""
    
    def __init__(self, port, baudrate=115200, timeout=1):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = False
        self._responses = queue.Queue()
        self._setup_responses()
    
    def _setup_responses(self):
        """Setup realistic AT command responses"""
        self.at_responses = {
            "AT": "OK",
            "ATI": "Quectel\nEG25-G\nRevision: EG25GGBR07A08M2G\nOK",
            "AT+CPIN?": "+CPIN: READY\nOK",
            "AT+CREG?": "+CREG: 0,1\nOK",  # Registered home network
            "AT+CSQ": f"+CSQ: {random.randint(10, 31)},99\nOK",  # Signal quality
            "AT+COPS?": '+COPS: 0,0,"Verizon",7\nOK',
            "AT+CGDCONT?": '+CGDCONT: 1,"IP","internet","0.0.0.0",0,0,0,0\nOK',
            "AT+QCCID": f'+QCCID: 89014103271234567890{random.randint(10,99)}\nOK',
            "AT+CIMI": f'31041012345678{random.randint(100,999)}\nOK',
            "AT+CNUM": f'+CNUM: "","+1{random.randint(2000000000,9999999999)}",145\nOK',
            "AT+CMGL=4": self._get_sms_list(),
            "AT+CGPADDR=1": f'+CGPADDR: 1,"10.{random.randint(100,254)}.{random.randint(1,254)}.{random.randint(2,254)}"\nOK'
        }
    
    def _get_sms_list(self) -> str:
        """Generate SMS list response"""
        sms_list = []
        for msg in mock_data.sms_messages[:5]:  # Limit to 5 messages
            sms_list.append(f'+CMGL: {msg["index"]},"{msg["status"]}","{msg["sender"]}","","{msg["timestamp"]}"\n{msg["content"]}')
        
        return "\n\n".join(sms_list) + "\n\nOK"
    
    def open(self):
        self.is_open = True
        
    def close(self):
        self.is_open = False
        
    def write(self, data: bytes):
        if not self.is_open:
            raise Exception("Serial port not open")
        
        cmd = data.decode().strip()
        response = self.at_responses.get(cmd, "ERROR")
        
        # Add some realistic delay
        threading.Timer(0.1, lambda: self._responses.put(response.encode() + b'\r\n')).start()
    
    def read(self, size=1024) -> bytes:
        try:
            return self._responses.get(timeout=self.timeout)
        except queue.Empty:
            return b""
    
    def in_waiting(self) -> int:
        return 0 if self._responses.empty() else 1


class MockUnixSocket:
    """Mock Unix domain socket for pcat-manager communication"""
    
    def __init__(self):
        self.connected = False
        self.responses = queue.Queue()
        self._setup_responses()
    
    def _setup_responses(self) -> None:
        """Setup realistic socket responses"""
        battery = mock_data.get_battery_status()
        
        self.socket_responses = {
            "pmu_io_get": json.dumps({
                "type": "pmu_io_status",
                "on_charging": battery["on_charging"],
                "charge_percent": battery["charge_percent"],
                "battery_voltage": battery["battery_voltage"],
                "battery_current": battery["battery_current"],
                "board_temperature": battery["board_temperature"]
            }),
            "system_info": json.dumps({
                "type": "system_info",
                "uptime": mock_data.get_system_stats()["uptime"],
                "load_avg": mock_data.get_system_stats()["load_avg"]
            })
        }
    
    def connect(self, address):
        self.connected = True
        print(f"Mock socket connected to {address}")
    
    def send(self, data: bytes):
        if not self.connected:
            raise ConnectionError("Socket not connected")
        
        try:
            msg = json.loads(data.decode())
            cmd_type = msg.get("type", "unknown")
            
            if cmd_type in self.socket_responses:
                response = self.socket_responses[cmd_type]
                threading.Timer(0.05, lambda: self.responses.put(response.encode() + b'\0')).start()
        except json.JSONDecodeError:
            pass
    
    def recv(self, size=1024) -> bytes:
        try:
            return self.responses.get(timeout=1.0)
        except queue.Empty:
            return b""
    
    def close(self):
        self.connected = False


class MockModemClient(threading.Thread):
    """Mock modem client that generates realistic data"""
    
    def __init__(self, modem_dev, modem_db):
        super().__init__()
        self.modem_dev = modem_dev
        self.modem_db = modem_db
        self.daemon = True
        self.running = True
        
        # Initialize threading components
        import threading
        self.mutex = threading.Lock()
        
        # Initialize realistic modem data
        self.basic = mock_data.get_modem_status()
        self.modem_valid = True
        self.querying = False
        
        print(f"MockModemClient initialized for {modem_dev}")
    
    def run(self):
        while self.running:
            # Update modem status periodically
            time.sleep(5)
            self.basic.update(mock_data.get_modem_status())
    
    def stop(self):
        self.running = False
    
    def get_basic_info(self) -> Dict[str, Any]:
        return self.basic

    def is_em05(self) -> bool:
        """Mock: not an EM05 modem. Missing this 500'd /device_control."""
        return False

    def start_query(self, query_type: str):
        """Mock start query method"""
        print(f"Mock modem query started: {query_type}")
    
    def send_sms(self, phone_number: str, message: str) -> bool:
        """Mock SMS sending"""
        print(f"Mock SMS sent to {phone_number}: {message}")
        return True
    
    def delete_sms(self, index: int) -> bool:
        """Mock SMS deletion"""
        print(f"Mock SMS deleted at index {index}")
        return True


class MockSocketClient(threading.Thread):
    """Mock socket client for pcat-manager communication"""
    
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.running_flag = True
        self.sock_connected = False
        
        # Mock socket connection (don't actually create a real socket)
        
        # Mock hardware status
        self.on_charging = False
        self.charge_percent = 7500  # 75%
        self.battery_voltage = 3850
        self.charger_voltage = 5035
        self.battery_current = 150
        self.board_temperature = 32.5
        
        # Mock firmware versions
        self.stm32_firmware_version = "v1.2.3-production"
        self.esp32_firmware_version = "v2.1.0-stable"
        self.pmu_hw_version = "PCAT2-REV2"
        
        # Mock additional attributes needed by the app
        self.modem_apn = "internet"
        self.device_type = "photonicat"
        self.sim_state = "READY"
        self.isp_name = "Mock Carrier"

        # Battery health / info (mock), so /battery renders real-looking cards.
        self.battery_info_supported = True
        self.battery_full_capacity = 5000      # mAh
        self.battery_nominal_capacity = 5200   # mAh
        self.battery_cycle_count = 37
        self.battery_capacity_learned = True
        self.battery_learning_active = False
        self.battery_internal_resistance = 85  # mOhm (whole loop)
        # Resistance split (fw >= RA2E1260721001, proto v8): fixed board
        # parasitic and cell-only (total - parasitic). Missing these crashed
        # /api/v1/battery_info.json in mock mode with an AttributeError.
        self.battery_internal_resistance_parasitic = 35  # mOhm, board constant
        self.battery_internal_resistance_battery = 50    # mOhm, cells only
        self.battery_runtime_seconds = 432000  # 120 h
        self.battery_mid_voltage = 3712        # 2P2S center-tap, mV
        # SOC display-factor diagnostics (fw >= RA2E1260719001)
        self.battery_soc_raw_x10 = 723         # raw SoC 72.3%
        self.battery_soc_empty_pct = 18        # learned empty point
        self.battery_soc_disp_a_x1000 = 1312   # display a
        self.battery_soc_disp_b_x100 = -2694   # display b
        self.battery_chg_gain_x1000 = 1042     # charge gain autolearn
        self.battery_cur_trim_a_x1000 = 1000   # current trim a (identity)
        self.battery_cur_trim_b_ma = 0         # current trim b
        # Remaining capacity / energy counters (fw >= RA2E1260719002)
        self.battery_remain_mah = 3615         # mAh
        self.battery_consumed_mwh = 10480      # mWh since anchor
        self.battery_charged_mwh = 21315       # mWh since anchor
        # Protocol version + MCU health + time estimates (fw >= RA2E1260719003)
        self.battery_proto_ver = 7
        self.battery_health_pct_mcu = 96       # %
        self.battery_minutes_to_empty = 213    # steady discharge in mock mode
        self.battery_full_charge_seen = None   # proto 7: not reported
        self.battery_minutes_to_full = None    # not charging
        # Temperature-compensation diagnostics (fw >= RA2E1260719009, proto v7)
        self.battery_gauge_temp_c = 31           # C
        self.battery_temp_empty_extra_x10 = 0    # warm: no cold uplift
        self.battery_r_temp_factor_x1000 = 964   # ~31C -> slightly below 1.0
        # MCU uptime diagnostics (fw >= RA2E1260721006, proto v9)
        self.mcu_uptime_seconds = 1123200        # 13 days since MCU reset
        self.mcu_since_sleep_seconds = 273600    # 3.17 days since last standby wake
        self.mcu_sleep_count = 42                # standby entries since reset
        # Battery charge limit % (80-100); the real value lives in the MCU.
        self.charge_threshold = 90

        print("MockSocketClient initialized")
    
    def run(self):
        """Simulate periodic hardware updates"""
        while self.running_flag:
            time.sleep(2)
            
            # Simulate battery changes
            battery_data = mock_data.get_battery_status()
            self.on_charging = battery_data["on_charging"]
            self.charge_percent = battery_data["charge_percent"]
            self.battery_voltage = battery_data["battery_voltage"]
            self.battery_current = battery_data["battery_current"]
            self.board_temperature = battery_data["board_temperature"]
    
    def stm32_firmware_exist(self) -> bool:
        return True
    
    def pmu_io_get(self):
        """Mock PMU IO get command"""
        print("Mock PMU IO get called")
    
    def pmu_io_set(self, **kwargs):
        """Mock PMU IO set command"""
        print(f"Mock PMU IO set called with args: {kwargs}")

    def charge_threshold_get(self):
        """Mock: value is already local; a real client re-queries the MCU."""
        pass

    def charge_threshold_set(self, value):
        """Mock: store the charge limit like the MCU would"""
        try:
            self.charge_threshold = max(80, min(100, int(value)))
        except (TypeError, ValueError):
            pass
        print(f"Mock charge threshold set to: {self.charge_threshold}")
    
    def set_power_led(self, state):
        """Mock power LED control"""
        print(f"Mock power LED set to: {state}")
    
    def set_beeper(self, state):
        """Mock beeper control"""
        print(f"Mock beeper set to: {state}")
    
    def set_auto_wan(self, enabled):
        """Mock auto WAN setting"""
        print(f"Mock auto WAN set to: {enabled}")
    
    def stop(self):
        self.running_flag = False


class MockScreenClient(threading.Thread):
    """Mock screen client"""
    
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.running = True
        
        print("MockScreenClient initialized")
    
    def run(self):
        while self.running:
            time.sleep(10)  # Periodic screen updates
    
    def update_display(self, content: str):
        print(f"Mock screen display: {content}")
    
    def stop(self):
        self.running = False


class MockSmsClient(threading.Thread):
    """Mock SMS client with realistic message handling"""
    
    def __init__(self, modem_dev, modem_client, modem_db):
        super().__init__()
        self.modem_dev = modem_dev
        self.modem_client = modem_client
        self.modem_db = modem_db
        self.daemon = True
        self.running = True
        
        # Use mock SMS data
        self.messages = mock_data.sms_messages.copy()
        
        print("MockSmsClient initialized")
    
    def run(self):
        """Simulate periodic SMS checking"""
        while self.running:
            time.sleep(30)  # Check for new SMS every 30 seconds
            
            # Occasionally add a new mock message
            if random.random() < 0.1:  # 10% chance
                self.add_mock_message()
    
    def add_mock_message(self):
        """Add a new mock SMS message"""
        new_msg = {
            "index": len(self.messages) + 1,
            "status": "REC UNREAD",
            "sender": f"+1{random.randint(1000000000, 9999999999)}",
            "timestamp": datetime.now().strftime("%y/%m/%d,%H:%M:%S"),
            "content": "New mock SMS message received",
            "storage": "ME"
        }
        self.messages.insert(0, new_msg)
        print(f"Mock SMS received from {new_msg['sender']}")
    
    def get_messages(self) -> List[Dict[str, Any]]:
        return self.messages
    
    def send_message(self, phone_number: str, content: str) -> bool:
        print(f"Mock SMS sent to {phone_number}: {content}")
        return True
    
    def delete_message(self, index: int) -> bool:
        self.messages = [msg for msg in self.messages if msg["index"] != index]
        print(f"Mock SMS deleted at index {index}")
        return True
    
    def stop(self):
        self.running = False


# Conditional imports and replacements
if IS_MOCK_MODE:
    # Replace real hardware modules with mocks
    print("Using mock hardware implementations")
    
    # Mock serial import
    import sys
    sys.modules['serial'] = type('MockSerialModule', (), {'Serial': MockSerial})()
    
    # Don't mock socket globally as it breaks SSL - we'll handle Unix sockets specifically
    # in the socket client code instead

else:
    print("Using real hardware implementations")


# Export mock classes
__all__ = [
    'MockModemClient', 'MockSocketClient', 'MockScreenClient', 'MockSmsClient',
    'MockSerial', 'MockUnixSocket'
]
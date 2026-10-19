# -*- coding: utf-8 -*-
# Dummy modem client for testing on x86/x64 machines

import threading
import time
import random
from pcat_util import pcat_logger

class PcModemClientDummy(threading.Thread):
    """
    Dummy modem client that simulates modem responses without actual hardware.
    """
    
    def __init__(self, modem_dev, modem_db):
        threading.Thread.__init__(self)
        self.running = False
        self.server_address = modem_dev  # Store but don't use
        self.modem_db = modem_db
        
        # Dummy modem state
        self.signal_strength = 75
        self.network_mode = "4G"
        self.operator = "Test Carrier"
        self.imei = "123456789012345"
        self.imsi = "460011234567890"
        self.iccid = "89860123456789012345"
        self.phone_number = "+1234567890"
        self.data_connected = True
        self.roaming = False
        
        # SMS storage
        self.sms_messages = []
        self._generate_dummy_sms()
    
    def _generate_dummy_sms(self):
        """Generate some dummy SMS messages for testing"""
        self.sms_messages = [
            {
                "index": 1,
                "status": "REC READ",
                "sender": "+1234567890",
                "timestamp": "2024/01/20,10:30:00+00",
                "content": "Test message 1"
            },
            {
                "index": 2,
                "status": "REC UNREAD",
                "sender": "+0987654321",
                "timestamp": "2024/01/20,11:00:00+00",
                "content": "Test message 2 - unread"
            }
        ]
    
    def send_at_command(self, command, timeout=5):
        """Simulate AT command responses"""
        pcat_logger.logger.debug(f"[DUMMY] Modem AT command: {command}")
        
        # Simulate common AT commands
        if command == "AT":
            return "OK"
        elif command == "AT+CSQ":
            # Signal quality
            return f"+CSQ: {self.signal_strength},99\r\nOK"
        elif command == "AT+CREG?":
            # Network registration
            return "+CREG: 0,1\r\nOK"
        elif command == "AT+COPS?":
            # Operator
            return f'+COPS: 0,0,"{self.operator}",7\r\nOK'
        elif command == "AT+CGSN":
            # IMEI
            return f"{self.imei}\r\nOK"
        elif command == "AT+CIMI":
            # IMSI
            return f"{self.imsi}\r\nOK"
        elif command == "AT+CCID":
            # ICCID
            return f"+CCID: {self.iccid}\r\nOK"
        elif command == "AT+CNUM":
            # Phone number
            return f'+CNUM: "","{self.phone_number}",145\r\nOK'
        elif command.startswith("AT+CMGS"):
            # Send SMS
            return "OK"
        elif command == "AT+CMGL=\"ALL\"":
            # List SMS messages
            result = ""
            for msg in self.sms_messages:
                result += f'+CMGL: {msg["index"]},"{msg["status"]}","{msg["sender"]}","","{msg["timestamp"]}"\r\n'
                result += f'{msg["content"]}\r\n'
            result += "OK"
            return result
        else:
            return "OK"
    
    def get_signal_strength(self):
        """Return dummy signal strength"""
        return self.signal_strength
    
    def get_network_info(self):
        """Return dummy network info"""
        return {
            "mode": self.network_mode,
            "operator": self.operator,
            "connected": self.data_connected,
            "roaming": self.roaming
        }
    
    def get_device_info(self):
        """Return dummy device info"""
        return {
            "imei": self.imei,
            "imsi": self.imsi,
            "iccid": self.iccid,
            "phone_number": self.phone_number
        }
    
    def send_sms(self, number, message):
        """Simulate sending SMS"""
        pcat_logger.logger.info(f"[DUMMY] Sending SMS to {number}: {message}")
        # Add to sent messages
        self.sms_messages.append({
            "index": len(self.sms_messages) + 1,
            "status": "STO SENT",
            "sender": number,
            "timestamp": time.strftime("%Y/%m/%d,%H:%M:%S+00"),
            "content": message
        })
        return True
    
    def delete_sms(self, index):
        """Simulate deleting SMS"""
        pcat_logger.logger.info(f"[DUMMY] Deleting SMS at index {index}")
        self.sms_messages = [msg for msg in self.sms_messages if msg["index"] != index]
        return True
    
    def list_sms(self):
        """Return dummy SMS list"""
        return self.sms_messages
    
    def run(self):
        """Main thread loop"""
        pcat_logger.logger.info("[DUMMY] PcModemClient thread running in dummy mode")
        self.running = True
        
        while self.running:
            # Simulate periodic updates
            self.signal_strength = max(20, min(100, self.signal_strength + random.randint(-5, 5)))
            time.sleep(5)
    
    def start_query(self, query_type=""):
        """Start a query (dummy implementation)"""
        pcat_logger.logger.debug(f"[DUMMY] Modem start_query: {query_type}")
        # Simulate query response by updating internal state
        if query_type == "all":
            # Update all modem parameters
            self.signal_strength = max(20, min(100, self.signal_strength + random.randint(-5, 5)))
        return True
    
    def stop(self):
        """Stop the thread"""
        self.running = False
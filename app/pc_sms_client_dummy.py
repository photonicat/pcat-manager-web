# -*- coding: utf-8 -*-
# Dummy SMS client for testing on x86/x64 machines

import threading
import time
import json
from datetime import datetime
from pcat_util import pcat_logger

class PcSmsClientDummy(threading.Thread):
    """
    Dummy SMS client that simulates SMS functionality without actual hardware.
    """
    
    def __init__(self, modem_dev, modem_client, modem_db):
        threading.Thread.__init__(self)
        self.running = False
        self.modem_dev = modem_dev
        self.modem_client = modem_client
        self.modem_db = modem_db
        
        # Dummy SMS storage
        self.sms_list = [
            {
                "id": 1,
                "phone": "+1234567890",
                "content": "Welcome to Photonicat dummy mode!",
                "time": "2024-01-20 10:00:00",
                "type": "received",
                "read": True
            },
            {
                "id": 2,
                "phone": "+0987654321",
                "content": "This is a test SMS message for development.",
                "time": "2024-01-20 11:00:00",
                "type": "received",
                "read": False
            },
            {
                "id": 3,
                "phone": "+1111111111",
                "content": "Sent message example",
                "time": "2024-01-20 12:00:00",
                "type": "sent",
                "read": True
            }
        ]
        self.next_id = 4
    
    def get_sms_list(self):
        """Return the list of SMS messages"""
        return self.sms_list
    
    def send_sms(self, phone_number, message):
        """Simulate sending an SMS"""
        pcat_logger.logger.info(f"[DUMMY] Sending SMS to {phone_number}: {message}")
        
        new_sms = {
            "id": self.next_id,
            "phone": phone_number,
            "content": message,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "sent",
            "read": True
        }
        
        self.sms_list.append(new_sms)
        self.next_id += 1
        
        return {"success": True, "message": "SMS sent successfully (dummy)"}
    
    def delete_sms(self, sms_id):
        """Simulate deleting an SMS"""
        pcat_logger.logger.info(f"[DUMMY] Deleting SMS with ID {sms_id}")
        
        self.sms_list = [sms for sms in self.sms_list if sms["id"] != sms_id]
        return {"success": True, "message": "SMS deleted successfully (dummy)"}
    
    def mark_as_read(self, sms_id):
        """Mark an SMS as read"""
        for sms in self.sms_list:
            if sms["id"] == sms_id:
                sms["read"] = True
                return True
        return False
    
    def get_unread_count(self):
        """Get count of unread messages"""
        return sum(1 for sms in self.sms_list if not sms["read"] and sms["type"] == "received")
    
    def simulate_receive_sms(self, phone, content):
        """Simulate receiving a new SMS (for testing)"""
        new_sms = {
            "id": self.next_id,
            "phone": phone,
            "content": content,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "received",
            "read": False
        }
        
        self.sms_list.append(new_sms)
        self.next_id += 1
        
        pcat_logger.logger.info(f"[DUMMY] Received SMS from {phone}: {content}")
    
    def run(self):
        """Main thread loop"""
        pcat_logger.logger.info("[DUMMY] PcSmsClient thread running in dummy mode")
        self.running = True
        
        # Simulate occasional incoming SMS
        incoming_counter = 0
        
        while self.running:
            time.sleep(10)
            
            # Simulate an incoming SMS every 60 seconds
            incoming_counter += 1
            if incoming_counter >= 6:  # Every 60 seconds
                incoming_counter = 0
                # Uncomment to simulate incoming messages
                # self.simulate_receive_sms(
                #     "+5555555555",
                #     f"Auto-generated test message at {datetime.now().strftime('%H:%M:%S')}"
                # )
    
    def stop(self):
        """Stop the thread"""
        self.running = False
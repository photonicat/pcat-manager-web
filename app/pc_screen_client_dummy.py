# -*- coding: utf-8 -*-
# Dummy screen client for testing on x86/x64 machines

import threading
import time
from pcat_util import pcat_logger

class PcScreenClientDummy(threading.Thread):
    """
    Dummy screen client that simulates screen operations without actual hardware.
    """
    
    def __init__(self):
        threading.Thread.__init__(self)
        self.running = False
        
        # Dummy screen state
        self.screen_on = True
        self.brightness = 80
        self.current_page = "home"
        self.display_content = "Photonicat Dummy Mode"
    
    def display_text(self, text, line=0):
        """Simulate displaying text on screen"""
        pcat_logger.logger.debug(f"[DUMMY] Screen display line {line}: {text}")
        self.display_content = text
        return True
    
    def clear_screen(self):
        """Simulate clearing the screen"""
        pcat_logger.logger.debug("[DUMMY] Screen cleared")
        self.display_content = ""
        return True
    
    def set_brightness(self, level):
        """Simulate setting screen brightness"""
        self.brightness = max(0, min(100, level))
        pcat_logger.logger.debug(f"[DUMMY] Screen brightness set to {self.brightness}%")
        return True
    
    def turn_on(self):
        """Simulate turning on the screen"""
        self.screen_on = True
        pcat_logger.logger.debug("[DUMMY] Screen turned on")
        return True
    
    def turn_off(self):
        """Simulate turning off the screen"""
        self.screen_on = False
        pcat_logger.logger.debug("[DUMMY] Screen turned off")
        return True
    
    def show_page(self, page_name):
        """Simulate showing a specific page"""
        self.current_page = page_name
        pcat_logger.logger.debug(f"[DUMMY] Screen showing page: {page_name}")
        return True
    
    def get_status(self):
        """Get dummy screen status"""
        return {
            "on": self.screen_on,
            "brightness": self.brightness,
            "page": self.current_page,
            "content": self.display_content
        }
    
    def run(self):
        """Main thread loop"""
        pcat_logger.logger.info("[DUMMY] PcScreenClient thread running in dummy mode")
        self.running = True
        
        while self.running:
            # Just sleep, no actual screen operations needed
            time.sleep(5)
    
    def stop(self):
        """Stop the thread"""
        self.running = False
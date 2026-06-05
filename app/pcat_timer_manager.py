# -*- coding: utf-8 -*-
"""
Timer manager for handling scheduled power LED and beeper control
"""

import threading
import time
import json
import os
from datetime import datetime, time as datetime_time
from pcat_util import pcat_logger
import pcat_config


class PcatTimerManager(threading.Thread):
    """Manages timed settings for power LED and beeper"""
    
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.running = False
        self.settings_file = '/etc/pcat_timer_settings.json'
        
        # Default settings - using 3-state system
        self.settings = {
            'power_led': {
                'mode': 'on',  # off, on, timed
                'start_time': '08:00',
                'end_time': '22:00'
            },
            'beeper': {
                'mode': 'on',  # off, on, timed
                'start_time': '08:00',
                'end_time': '22:00'
            }
        }
        
        self.socket_client = None
        self.load_settings()
    
    def set_socket_client(self, socket_client):
        """Set the socket client for PMU control"""
        self.socket_client = socket_client
    
    def load_settings(self):
        """Load settings from file"""
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    loaded_settings = json.load(f)
                self._migrate_old_format(loaded_settings)
                self.settings = loaded_settings
                pcat_logger.logger.info(f"Loaded timer settings from {self.settings_file}")
        except Exception as e:
            pcat_logger.logger.warning(f"Failed to load timer settings: {e}")
    
    def _migrate_old_format(self, settings):
        """Migrate old enabled/all_day format to new mode format"""
        for device in ['power_led', 'beeper']:
            if device in settings:
                device_settings = settings[device]
                if 'mode' not in device_settings:
                    # Migrate from old format
                    enabled = device_settings.get('enabled', True)
                    all_day = device_settings.get('all_day', True)
                    
                    if not enabled:
                        device_settings['mode'] = 'off'
                    elif all_day:
                        device_settings['mode'] = 'on'
                    else:
                        device_settings['mode'] = 'timed'
                    
                    # Remove old keys
                    device_settings.pop('enabled', None)
                    device_settings.pop('all_day', None)
                    pcat_logger.logger.info(f"Migrated {device} settings from old format to mode: {device_settings['mode']}")
    
    def save_settings(self):
        """Save settings to file"""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
            pcat_logger.logger.info(f"Saved timer settings to {self.settings_file}")
        except Exception as e:
            pcat_logger.logger.error(f"Failed to save timer settings: {e}")
    
    def set_power_led_settings(self, mode, start_time, end_time):
        """Set power LED settings with 3-state mode: off, on, timed"""
        self.settings['power_led'] = {
            'mode': mode,
            'start_time': start_time,
            'end_time': end_time
        }
        self.save_settings()
        pcat_logger.logger.info(f"Power LED settings updated: mode={mode}, start={start_time}, end={end_time}")
    
    def set_beeper_settings(self, mode, start_time, end_time):
        """Set beeper settings with 3-state mode: off, on, timed"""
        self.settings['beeper'] = {
            'mode': mode,
            'start_time': start_time,
            'end_time': end_time
        }
        self.save_settings()
        pcat_logger.logger.info(f"Beeper settings updated: mode={mode}, start={start_time}, end={end_time}")
    
    def get_power_led_mode(self):
        return self.settings['power_led'].get('mode', 'on')
    
    def get_power_led_start_time(self):
        return self.settings['power_led'].get('start_time', '08:00')
    
    def get_power_led_end_time(self):
        return self.settings['power_led'].get('end_time', '22:00')
    
    def get_beeper_mode(self):
        return self.settings['beeper'].get('mode', 'on')
    
    def get_beeper_start_time(self):
        return self.settings['beeper'].get('start_time', '08:00')
    
    def get_beeper_end_time(self):
        return self.settings['beeper'].get('end_time', '22:00')
    
    def is_time_in_range(self, start_time_str, end_time_str):
        """Check if current time is within the specified range"""
        try:
            current_time = datetime.now().time()
            start_time = datetime_time.fromisoformat(start_time_str)
            end_time = datetime_time.fromisoformat(end_time_str)
            
            pcat_logger.logger.debug(f"Time range check: {start_time_str}-{end_time_str}, current: {current_time.strftime('%H:%M:%S')}")
            
            if start_time <= end_time:
                # Same day range (e.g., 09:00 to 17:00)
                result = start_time <= current_time <= end_time
                pcat_logger.logger.debug(f"Same day range: {start_time} <= {current_time} <= {end_time} = {result}")
                return result
            else:
                # Overnight range (e.g., 22:00 to 08:00)
                result = current_time >= start_time or current_time <= end_time
                pcat_logger.logger.debug(f"Overnight range: {current_time} >= {start_time} OR {current_time} <= {end_time} = {result}")
                return result
        except Exception as e:
            pcat_logger.logger.error(f"Error checking time range: {e}")
            return False
    
    def apply_power_led_setting(self):
        """Apply power LED setting based on current configuration"""
        if not self.socket_client:
            pcat_logger.logger.warning("apply_power_led_setting: socket_client not available")
            return
            
        led_settings = self.settings['power_led']
        mode = led_settings.get('mode', 'on')
        current_time = datetime.now().time()
        
        pcat_logger.logger.info(f"Applying power LED setting: mode={mode}, current_time={current_time.strftime('%H:%M:%S')}")
        
        if mode == 'off':
            # Power LED always off
            pcat_logger.logger.info("Setting power LED OFF (mode: off)")
            self.socket_client.pmu_io_set(status_led_v2_enabled=False)
        elif mode == 'on':
            # Power LED always on
            pcat_logger.logger.info("Setting power LED ON (mode: on)")
            self.socket_client.pmu_io_set(status_led_v2_enabled=True)
        elif mode == 'timed':
            # Power LED based on time schedule (ON from start_time to end_time, OFF otherwise)
            start_time = led_settings['start_time']
            end_time = led_settings['end_time']
            in_range = self.is_time_in_range(start_time, end_time)
            
            pcat_logger.logger.info(f"Timed mode: {start_time}-{end_time}, current: {current_time.strftime('%H:%M')}, in_range: {in_range}")
            
            if in_range:
                pcat_logger.logger.info("Setting power LED ON (timed: within schedule)")
                self.socket_client.pmu_io_set(status_led_v2_enabled=True)   # ON during scheduled hours
            else:
                pcat_logger.logger.info("Setting power LED OFF (timed: outside schedule)")
                self.socket_client.pmu_io_set(status_led_v2_enabled=False)  # OFF outside scheduled hours
        
        # Log the command that was sent
        pcat_logger.logger.debug(f"Power LED setting applied for mode '{mode}'")
    
    
    def apply_beeper_setting(self):
        """Apply beeper setting based on current configuration"""
        if not self.socket_client:
            return
            
        beeper_settings = self.settings['beeper']
        mode = beeper_settings.get('mode', 'on')
        
        if mode == 'off':
            # Beeper always off
            self.socket_client.pmu_io_set(beeper_enabled=False)
        elif mode == 'on':
            # Beeper always on
            self.socket_client.pmu_io_set(beeper_enabled=True)
        elif mode == 'timed':
            # Beeper based on time schedule (ON from start_time to end_time, OFF otherwise)
            if self.is_time_in_range(beeper_settings['start_time'], beeper_settings['end_time']):
                self.socket_client.pmu_io_set(beeper_enabled=True)   # ON during scheduled hours
            else:
                self.socket_client.pmu_io_set(beeper_enabled=False)  # OFF outside scheduled hours

    def apply_smart_fan_control(self):
        """Apply smart fan control based on temperature and charging status"""
        try:
            # Get current fan mode
            fan_mode = pcat_config.get_fan_mode()

            # If manual mode, apply saved manual fan speed
            if fan_mode == "manual":
                user_speed = pcat_config.get_fan_speed()
                pcat_config.set_fan_speed_direct(user_speed)
                pcat_logger.logger.debug(f"[Smart Fan] Manual mode - applied saved speed {user_speed}")
                return

            # Get current user max fan speed setting
            user_max_speed = pcat_config.get_fan_speed()

            # Get current temperature
            temperature = self.socket_client.board_temperature if self.socket_client and self.socket_client.board_temperature is not None else None

            # Apply smart algorithm with current mode (smart_quiet or smart_cool)
            # Note: set_fan_speed_smart now calls is_device_charging() internally
            actual_speed = pcat_config.set_fan_speed_smart(temperature, user_max_speed, fan_mode)

            # Get charging status for logging only
            is_charging = pcat_config.is_device_charging()

            # Sanitize values for logging to prevent None issues
            temp_display = f"{temperature}°C" if temperature is not None else "N/A"
            charging_display = is_charging if is_charging is not None else "N/A"
            user_max_display = user_max_speed if user_max_speed is not None else "N/A"
            actual_display = actual_speed if actual_speed is not None else "N/A"

            # Log for dev panel (always log smart fan control activity)
            pcat_logger.logger.warning(f"[Smart Fan] mode={fan_mode}, temp={temp_display}, charging={charging_display}, user_max={user_max_display}, actual_speed={actual_display}")

        except Exception as e:
            # Log errors but don't let them crash the timer loop
            pcat_logger.logger.warning(f"[Smart Fan] Error in smart fan control: {e}")
    
    def apply_startup_settings(self):
        """Apply settings immediately on startup"""
        if self.socket_client:
            pcat_logger.logger.info("Applying startup settings for power LED, beeper, and smart fan control")
            self.apply_power_led_setting()
            self.apply_beeper_setting()
            self.apply_smart_fan_control()
            # Re-apply saved power mode to pcat-manager daemon
            saved_power_mode = pcat_config.get_power_mode()
            if saved_power_mode == "direct_power":
                self.socket_client.set_power_on_mode(2)
                pcat_logger.logger.info("Restored direct power mode (power-on-mode=2) from saved config")
        else:
            pcat_logger.logger.warning("Cannot apply startup settings: socket client not available")
    
    def force_apply_settings(self):
        """Force immediate application of all settings (for debugging)"""
        pcat_logger.logger.info("Force applying timer settings")
        self.apply_power_led_setting()
        self.apply_beeper_setting()
        self.apply_smart_fan_control()
    
    def start(self):
        """Start the timer manager thread"""
        self.running = True
        super().start()
        # Apply settings immediately on startup
        self.apply_startup_settings()
        pcat_logger.logger.info("PcatTimerManager started")
    
    def stop(self):
        """Stop the timer manager thread"""
        self.running = False
        pcat_logger.logger.info("PcatTimerManager stopped")
    
    def run(self):
        """Main timer loop"""
        pcat_logger.logger.info("PcatTimerManager thread running")

        # Run timer early on startup - check after 10 seconds
        early_startup_done = False

        while self.running:
            try:
                # Apply settings every minute (or 10s on first run)
                self.apply_power_led_setting()
                self.apply_beeper_setting()
                self.apply_smart_fan_control()

                # Early startup: sleep for 10 seconds on first run, then 60 seconds
                sleep_duration = 10 if not early_startup_done else 60
                early_startup_done = True

                # Sleep for specified duration before next check
                for _ in range(sleep_duration):
                    if not self.running:
                        break
                    time.sleep(1)

            except Exception as e:
                pcat_logger.logger.error(f"Error in timer manager loop: {e}")
                time.sleep(10)  # Wait a bit before retrying
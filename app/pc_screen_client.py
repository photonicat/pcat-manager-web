#@Author    : c2h2

import threading
import os
import json
import datetime
import time
import requests

import pcat_config
from pcat_util import isp_dict, pcat_logger


class PcScreenClient (threading.Thread):
    CACHE_TTL = 60  # seconds

    # The live preview on /screen polls frame.png about once a second, and the
    # display renders a new frame roughly that often too, so anything shorter
    # than this only buys duplicate bytes. It exists to collapse the burst that
    # one page load produces (two <img> and two CSS background slices all point
    # at the same URL) and to keep N open tabs from multiplying upstream hits.
    FRAME_CACHE_TTL = 0.3   # seconds
    # A wedged display service must not sit on a granian blocking thread (there
    # are only 4 on the device): fail fast and let the browser retry.
    FRAME_TIMEOUT = 2.0     # seconds

    def __init__(self):
        threading.Thread.__init__(self)
        self.server_address = 'http://127.0.0.1:8081'
        self.running_flag = False
        # cache state:
        self._config_cache         = None
        self._config_cache_time    = 0.0
        # live-frame state: keep-alive session + the short coalescing cache
        self._frame_session        = requests.Session()
        self._frame_lock           = threading.Lock()
        self._frame_cache          = None
        self._frame_cache_time     = 0.0

    def run(self):
        while True:
            time.sleep(1)


    def get_screen_info(self):
        try:
            url = self.server_address + "/api/v1/info.json"
            response = requests.get(url)
            return response.json()
        except requests.RequestException as e:
            pcat_logger.logger.error(f"Failed to fetch screen info: {e}")
            return None

    def get_frame(self):
        """Current LCD framebuffer as PNG bytes, or None if the display is down.

        Served from a sub-second cache and over a keep-alive session: the
        preview polls this at ~1Hz per viewer, and a fresh TCP connect per
        request was a measurable share of the ~35ms round trip.
        """
        now = time.time()
        if self._frame_cache is not None and now - self._frame_cache_time < self.FRAME_CACHE_TTL:
            return self._frame_cache

        # Only one thread goes upstream; the rest take whatever it just cached.
        with self._frame_lock:
            now = time.time()
            if self._frame_cache is not None and now - self._frame_cache_time < self.FRAME_CACHE_TTL:
                return self._frame_cache
            try:
                url = f'{self.server_address}/api/v1/go_frame.png'
                resp = self._frame_session.get(url, timeout=self.FRAME_TIMEOUT)
                resp.raise_for_status()
                self._frame_cache = resp.content
                self._frame_cache_time = time.time()
                return self._frame_cache
            except requests.RequestException as e:
                pcat_logger.logger.error(f"Failed to fetch screen: {e}")
                # Drop the cache rather than serve a frozen frame: the caller
                # turns None into a 503 so the page can show "reconnecting"
                # instead of a screenshot that silently stopped moving.
                self._frame_cache = None
                self._frame_cache_time = 0.0
                return None
    
    def get_default_config(self):
        try:
            url = self.server_address + "/api/v1/go_get_default_config.json"
            response = requests.get(url)
            return response.json()
        except requests.RequestException as e:
            pcat_logger.logger.error(f"Failed to fetch default screen: {e}")
            return None

    def get_user_config(self):
        try:
            url = self.server_address + "/api/v1/go_get_user_config.json"
            response = requests.get(url)
            return response.json()
        except requests.RequestException as e:
            pcat_logger.logger.error(f"Failed to fetch screen: {e}")
            return None

    def save_user_config(self, p_data):
        try:
            url = self.server_address + "/api/v1/go_save_user_config.json"
            response = requests.post(url, json=p_data)
            return response.json()
        except requests.RequestException as e:
            pcat_logger.logger.error(f"Failed to save screen: {e}")
            return None

    def get_custom_metrics_status(self):
        """Per-source status (last value/error, success count) for custom_metrics."""
        try:
            url = self.server_address + "/api/v1/custom_metrics/status"
            response = requests.get(url, timeout=5)
            return response.json()
        except requests.RequestException as e:
            pcat_logger.logger.error(f"Failed to fetch custom metrics status: {e}")
            return None

    def execute_custom_metrics_source(self, source_name):
        """Trigger an immediate fetch/execute of one custom_metrics source."""
        try:
            url = f"{self.server_address}/api/v1/custom_metrics/{source_name}/execute"
            response = requests.post(url, timeout=5)
            return response.json()
        except requests.RequestException as e:
            pcat_logger.logger.error(f"Failed to execute custom metrics source: {e}")
            return None

    def get_data(self):
        """Live globalData (all data_key -> current value pairs) from the display."""
        try:
            url = self.server_address + "/api/v1/go_data.json"
            response = requests.get(url, timeout=5)
            return response.json()
        except requests.RequestException as e:
            pcat_logger.logger.error(f"Failed to fetch screen data: {e}")
            return None
    
    def get_config(self, force_refresh: bool = False):
        """
        Fetch /api/v1/get_config.json, but reuse the cached copy
        if it's younger than CACHE_TTL seconds (unless force_refresh=True).
        """
        now = time.time()
        age = now - self._config_cache_time

        if not force_refresh and self._config_cache is not None and age < self.CACHE_TTL:
            # cache still fresh
            return self._config_cache

        # otherwise do HTTP fetch
        try:
            url = self.server_address + "/api/v1/go_get_config.json"
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            cfg = resp.json()

            # update cache
            self._config_cache      = cfg
            self._config_cache_time = now
            return cfg

        except requests.RequestException as e:
            pcat_logger.logger.error(f"Failed to fetch config: {e}")
            # on error, you might still want to return stale cache:
            return self._config_cache

    def reset_config_cache(self):
        """
        Drop the saved config so that next get_config() will re-fetch.
        """
        self._config_cache      = None
        self._config_cache_time = 0.0

    def reset_config(self):
        """
        Wipe the display's saved user_config.json so the base config takes over.
        Clears the local cache too so the next get_config() re-fetches.
        """
        try:
            url = self.server_address + "/api/v1/go_reset_config"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            self.reset_config_cache()
            return response.json()
        except requests.RequestException as e:
            pcat_logger.logger.error(f"Failed to reset screen config: {e}")
            return {"status": "error", "message": f"Reset failed: {str(e)}"}

    def restart_screen(self): #exec command /etc/init.d/photonicat2_display restart
        try:
            pcat_logger.logger.info("Attempting to restart screen display service")
            result = os.system("/etc/init.d/pcat2-display-mini stop && sleep 3 && /etc/init.d/pcat2-display-mini start")
            
            if result == 0:
                pcat_logger.logger.info("Screen display service restarted successfully")
                return {"status": "success", "message": "Screen restarted successfully"}
            else:
                pcat_logger.logger.error(f"Screen restart failed with exit code: {result}")
                return {"status": "error", "message": f"Screen restart failed with exit code: {result}"}
                
        except Exception as e:
            pcat_logger.logger.error(f"Exception during screen restart: {e}")
            return {"status": "error", "message": f"Exception during screen restart: {str(e)}"}

    def short_click(self): #exec command to trigger page change
        try:
            pcat_logger.logger.info("Sending short click command to display")
            api_url = self.server_address + "/api/v1/go_changePage"
            
            response = requests.get(api_url, timeout=1)
            response.raise_for_status()
            
            pcat_logger.logger.info("Short click command sent successfully")
            return {"status": "success", "message": "Short click sent successfully", "data": response.json()}
            
        except requests.Timeout as e:
            pcat_logger.logger.error(f"Timeout sending short click: {e}")
            return {"status": "error", "message": "Request timeout - display service may be unavailable"}
            
        except requests.ConnectionError as e:
            pcat_logger.logger.error(f"Connection error sending short click: {e}")
            return {"status": "error", "message": "Cannot connect to display service"}
            
        except requests.HTTPError as e:
            pcat_logger.logger.error(f"HTTP error sending short click: {e}")
            return {"status": "error", "message": f"HTTP error: {e.response.status_code}"}
            
        except requests.RequestException as e:
            pcat_logger.logger.error(f"Request error sending short click: {e}")
            return {"status": "error", "message": f"Request failed: {str(e)}"}
            
        except Exception as e:
            pcat_logger.logger.error(f"Unexpected error during short click: {e}")
            return {"status": "error", "message": f"Unexpected error: {str(e)}"}



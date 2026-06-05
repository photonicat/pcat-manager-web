#@Author    : qichunren

import threading
# import pyprctl # patch:set real thread name
import serial
import select
import time
from datetime import datetime
import queue
import json
import random
import re
import os
import requests
import asyncio

from cellular_constants import *
from pcat_util import hardware_util
from database import init_db, connect_db, get_rows
from pcat_util import pcat_logger

# Permanent cache for EM05 detection - never expires once set
_em05_cache = {'result': None, 'queried': False, 'lock': threading.Lock()}
_model_cache = {'model': None, 'queried': False, 'lock': threading.Lock()}
import pcat_config
import app

MAX_QUEUE = 20
MAX_MODEM_RETRY_COUNT = 1000
global_query_counter=0
global_routine_query_counter=0
sms_sent_flag = False   


class PcModemClient (threading.Thread):
    def __init__(self, modem_dev, modem_db):
        print("PcModemClient init")
        threading.Thread.__init__(self)
        self.mutex = threading.Lock()
        self.storage = "ME" # ME & SM
        self.modem_tty_path = modem_dev # '/dev/ttyUSB3'

        self.recv_buffer = b''
        self.serial_obj = None
        self.modem_uart_connected = False
        self.modem_tty_failed_count = 0
        with self.mutex:
            self.modem_valid = False
        self.cmd_queue = []
        self.to_send_content = ""

        self.basic = {}
        self.basic["sim_ccid"] = ""
        self.basic["sim_cimi"] = ""
        self.basic["sim_number"] = ""
        self.querying = False

        self.roaming = pcat_config.get_user_config_key("roaming", 1)
        self.lte_bands_user_desired = pcat_config.get_user_config_key("lte_bands_desired", "all")
        self.nr5g_bands_user_desired = pcat_config.get_user_config_key("nr5g_bands_desired", "all")
        self.roaming_pref = pcat_config.get_user_config_key("roaming_pref", "any")

        self.api_lookup_lock = threading.Lock()
        self.serial_lock = threading.Lock()

        # AT command failure tracking for battery saving
        # Must be initialized before any cmd_queue_append call, since
        # cmd_queue_append -> cmd_should_skip reads cmd_failure_lock.
        self.cmd_failure_counts = {}
        self.cmd_failure_lock = threading.Lock()
        self.MAX_CMD_FAILURES = 5

        #if we have settings with sim pin, check and set it
        self.cpin = pcat_config.get_cpin()
        if len(self.cpin) >= 3:
            self.cmd_queue_append(f'AT+CPIN="{self.cpin}"')

        # Initialize from permanent cache if available
        with _model_cache['lock']:
            if _model_cache['model'] is not None:
                self.basic["model"] = _model_cache['model']

    def cleanup_serial_connection(self):
        """
        Clean up serial connection on error and reset state.
        Should be called with serial_lock held.
        """
        try:
            if self.serial_obj is not None:
                if hasattr(self.serial_obj, 'is_open') and self.serial_obj.is_open:
                    self.serial_obj.close()
                self.serial_obj = None
                print("Serial connection cleaned up")
        except Exception as e:
            print(f"Error during serial cleanup: {e}")
            self.serial_obj = None

    def cmd_should_skip(self, cmd):
        """Check if a command should be skipped due to too many failures."""
        with self.cmd_failure_lock:
            failures = self.cmd_failure_counts.get(cmd, 0)
            if failures >= self.MAX_CMD_FAILURES:
                return True
        return False

    def cmd_mark_failure(self, cmd):
        """Increment failure count for a command."""
        with self.cmd_failure_lock:
            current = self.cmd_failure_counts.get(cmd, 0)
            self.cmd_failure_counts[cmd] = current + 1
            new_count = self.cmd_failure_counts[cmd]

            if new_count >= self.MAX_CMD_FAILURES:
                print(f"⚠️ Command '{cmd}' has failed {new_count} times - will be skipped to save battery")
            else:
                print(f"Command '{cmd}' failed ({new_count}/{self.MAX_CMD_FAILURES})")

    def cmd_mark_success(self, cmd):
        """Reset failure count for a command after successful parsing."""
        with self.cmd_failure_lock:
            if cmd in self.cmd_failure_counts:
                del self.cmd_failure_counts[cmd]

    def cmd_queue_append(self, cmd):
        # Check if command should be skipped due to failures
        if self.cmd_should_skip(cmd):
            return

        self.mutex.acquire()
        try:
            # 1) avoid duplicates
            if cmd in self.cmd_queue:
                print(f"Warning: {cmd} is already in the queue")
                return

            # 2) if we're already at capacity, drop the oldest
            if len(self.cmd_queue) >= MAX_QUEUE:
                self.cmd_queue.pop(0)

            # 3) add the new command
            self.cmd_queue.append(cmd)
        finally:
            self.mutex.release()

    def start_query(self, refresh_type):
        global global_query_counter, global_routine_query_counter
        global_query_counter += 1

        if not app.is_wwan_powered():
            return
        
        if refresh_type == "all":
            return
            self.cmd_queue_append("AT+CMGF=1")
            self.cmd_queue_append("AT+CSCS=\"GSM\"")
            self.cmd_queue_append("ATI")
            self.cmd_queue_append("AT+CGSN") #IMEI

        elif refresh_type == "modem_basic":
            return
            self.cmd_queue_append("AT+CMGF=1")
            self.cmd_queue_append("AT+CSCS=\"GSM\"")
            self.cmd_queue_append("ATI")
            self.cmd_queue_append("AT+CGSN")
        else:
            global_routine_query_counter += 1
            print("Routine Query sequence: ", global_routine_query_counter)
                       
            if self.basic["sim_number"] == "":
                self.cmd_queue_append("AT+CNUM") # SIM number
                self.cmd_queue_append("AT+CCID") # SIM number
                self.cmd_queue_append("AT+CIMI") # SIM number

            # Only query model once - check permanent cache first
            with _model_cache['lock']:
                model_already_queried = _model_cache['queried']

            if not model_already_queried and (self.basic.get("model") is None or self.basic.get("model") == ""):
                self.cmd_queue_append("ATI") # modem model/version/etc

            # Query IMEI if not yet available (separate from model cache)
            if not self.basic.get("imei_num"):
                self.cmd_queue_append("AT+CGSN") # IMEI


            if self.is_fm350():
                # FM350-GL (MediaTek) specific routine queries
                # Query COPS in numeric format (MCC+MNC) for ISP lookup
                # Note: can't send multiple AT+COPS? due to queue dedup
                self.cmd_queue_append("AT+COPS=3,2") # Set to numeric format
                self.cmd_queue_append("AT+COPS?") # Query operator (MCC+MNC + AcT)
                self.cmd_queue_append("AT+CESQ") # Extended signal quality (RSRP, RSRQ, SINR)
                self.cmd_queue_append("AT+CSQ") # Basic signal strength
                self.cmd_queue_append("AT+GTCCINFO?") # Current cell information (serving cell)
                self.cmd_queue_append("AT+GTCAINFO?") # Carrier aggregation info
                self.cmd_queue_append('AT+GTSENRDTEMP=1') # Temperature
            else:
                # Qualcomm/EM05 modem queries
                self.cmd_queue_append("AT+QNWINFO") # Network info
                if not self.is_em05():
                    self.cmd_queue_append("AT+QSPN") # ISP
                    self.cmd_queue_append("AT+QCAINFO") # Carrier aggregation
                self.cmd_queue_append('AT+QCFG="usbnet"') # usbnet mode
                if not self.is_500u() and not self.is_em05():
                    self.cmd_queue_append("AT+QRSRP") # 5G power
                    self.cmd_queue_append("AT+QRSRQ") # 5G quality
                    self.cmd_queue_append("AT+QSINR") # 5G signal strength
                self.cmd_queue_append("AT+CSQ") # Basic signal strength
                self.cmd_queue_append('AT+QENG="servingcell"') # Serving cell info
                if not self.is_500u():
                    self.cmd_queue_append('AT+QCFG="usbspeed"') # USB speed
                self.cmd_queue_append('AT+QTEMP') # Temperature

            if global_routine_query_counter % 10 == 1:
                if self.is_fm350():
                    # FM350-GL (MediaTek) specific queries
                    self.cmd_queue_append('AT+GTACT?') # network mode preference (4G/5G)

                    # Set default available bands for FM350-GL (based on modem AT+GTACT? response)
                    # FM350-GL supports: LTE B1-B5/B7/B8/B12-B14/B17-B20/B25/B26/B28-B30/B34/B38-B43/B46/B48/B66/B71
                    #                    NR N1-N3/N5/N7/N8/N20/N25/N28/N30/N38/N40/N41/N48/N66/N71/N77-N79
                    if "policy_lte_bands" not in self.basic or not self.basic["policy_lte_bands"]:
                        self.basic["policy_lte_bands"] = "1:2:3:4:5:7:8:12:13:14:17:18:19:20:25:26:28:29:30:34:38:39:40:41:42:43:46:48:66:71"
                        print(f"FM350: Set default policy_lte_bands => {self.basic['policy_lte_bands']}")
                    if "policy_nr5g_bands" not in self.basic or not self.basic["policy_nr5g_bands"]:
                        self.basic["policy_nr5g_bands"] = "1:2:3:5:7:8:20:25:28:30:38:40:41:48:66:71:77:78:79"
                        print(f"FM350: Set default policy_nr5g_bands => {self.basic['policy_nr5g_bands']}")

                    # FM350 doesn't support AT+QNWPREFCFG="roam_pref", so load from config
                    if "roam_pref" not in self.basic or not self.basic["roam_pref"]:
                        stored_roam_pref = pcat_config.get_user_config_key("roaming_pref", "any")
                        if stored_roam_pref == "all":
                            stored_roam_pref = "any"
                        self.basic["roam_pref"] = stored_roam_pref
                        print(f"FM350: Loaded roaming preference from config: {stored_roam_pref}")

                    # Ensure automatic operator selection (allows roaming) on FM350.
                    # FM350-GL has no proprietary roam toggle; COPS=0 is the standard
                    # 3GPP way to permit roaming via auto network registration.
                    if not self.basic.get("fm350_cops_auto_set"):
                        self.cmd_queue_append('AT+COPS=0')
                        self.basic["fm350_cops_auto_set"] = True
                elif self.is_520() or self.is_500q():
                    self.cmd_queue_append('AT+QNWPREFCFG="policy_band"') # policy band, carrier/module capable
                    # Add small delay to ensure modem processes policy_band before capability query
                    time.sleep(0.1)
                    self.cmd_queue.append('AT+QNWPREFCFG="ue_capability_band"') # UE capability band, user's desired bands
                elif self.is_500u():
                    self.cmd_queue_append('AT+QNWPREFCFG="nr5g_band"')
                    self.cmd_queue_append('AT+QNWPREFCFG="lte_band"')

                if not self.is_em05() and not self.is_fm350():
                    # Qualcomm-specific queries (not supported by EM05 or FM350)
                    self.cmd_queue_append('AT+QNWPREFCFG="ue_usage_setting"') # UE usage setting
                    self.cmd_queue_append('AT+QNWPREFCFG="roam_pref"') # roaming preference (Qualcomm only)
                    self.cmd_queue_append('AT+QNWPREFCFG="mode_pref"') # 4G/5G tech preference

                if self.is_em05():
                    self.cmd_queue_append('AT+QCFG="band"')  #4G bands
                    self.cmd_queue_append('AT+QCFG="roamservice"') #em05 roam pref

                if not self.is_fm350():
                    self.cmd_queue_append('AT+COPS?') # carrier info (FM350 queries COPS in routine block)

    # Thread entrance, invoked by start method
    def run(self):
        sms_check_max_count = 10
        self.modem_tty_failed_count = 0
        pcat_logger.logger.info("SIM module thread started.")
        while True:
            if not app.is_wwan_powered():
                # WWAN is rfkill blocked, clear queue and wait
                if len(self.cmd_queue) > 0:
                    self.cmd_queue.clear()
                self.querying = False
                time.sleep(3)
                continue

            if len(self.cmd_queue) == 0:
                self.querying = False
                time.sleep(1)  # Wait for 1 second before checking the queue again
                continue
            else:
                #TODO fix when no modem is installed. (home/mobile version)
                if not self.open_modem_uart():
                    self.modem_tty_failed_count += 1
                    if self.modem_tty_failed_count < MAX_MODEM_RETRY_COUNT:
                        # Progressive retry delays based on system uptime
                        retry_delay = self._get_retry_delay()
                        print(f"Failed to connect to modem, retry {self.modem_tty_failed_count}/{MAX_MODEM_RETRY_COUNT}, waiting {retry_delay}s...")
                        time.sleep(retry_delay)
                        continue
                    else:
                        print("Failed to connect to modem tty device for " + str(self.modem_tty_failed_count) + " times, give up!")
                        break

                # Reset failed count on successful connection
                if self.modem_tty_failed_count > 0:
                    print(f"Modem connection restored after {self.modem_tty_failed_count} failed attempts")
                    self.modem_tty_failed_count = 0

                self.querying = True
                cmd_str = self.cmd_queue.pop(0) + "\r"

                try:
                    with self.serial_lock:
                        try:
                            if self.serial_obj is None:
                                print("Serial object is None, skipping command")
                                self.querying = False
                                continue
                            
                            # Clear any leftover data in input buffer before sending command
                            try:
                                self.serial_obj.reset_input_buffer()
                                self.serial_obj.reset_output_buffer()
                            except:
                                pass  # Some serial implementations don't support buffer reset
                            
                            # Ensure proper line ending and send command
                            self.serial_obj.write(cmd_str.encode())
                            self.serial_obj.flush()  # Force send immediately
                            print("[MODEM SEND]:" + cmd_str)
                            
                            
                            response_lines = []
                            recv_data = b''
                            start_time = time.time()
                            
                            # First, wait for the echo of our command to be received
                            command_echoed = False
                            expected_echo = cmd_str.strip()
                            
                            while True:
                                # Check for overall timeout (10 seconds max)
                                if time.time() - start_time > 10.0:
                                    print(f"Command timeout exceeded 10 seconds for: {expected_echo}")
                                    break
                                    
                                try:
                                    line = self.serial_obj.readline()
                                    if not line:  # Timeout occurred on readline
                                        if not command_echoed:
                                            print(f"No echo received for command: {expected_echo}")
                                        print("Readline timeout - no more data")
                                        break
                                except Exception as e:
                                    print(f"Serial readline error: {e}")
                                    break
                                    
                                recv_data += line
                                line_str = line.decode('utf-8', errors='ignore').strip()
                                
                                # Skip completely empty lines
                                if not line_str:
                                    continue
                                
                                response_lines.append(line_str)
                                
                                # Check if this is the command echo
                                if not command_echoed and line_str == expected_echo:
                                    command_echoed = True
                                    continue
                                
                                # Only check for completion after we've seen the command echo
                                if command_echoed:
                                    # Check for AT command completion
                                    if line_str in ('OK', 'ERROR'):
                                        #print(f"AT command completed with: {line_str}")
                                        break
                                    elif line_str.startswith('+CMS ERROR:') or line_str.startswith('+CME ERROR:'):
                                        print(f"AT command failed with: {line_str}")
                                        break
                                    elif line_str.startswith('NO CARRIER') or line_str.startswith('BUSY') or line_str.startswith('NO ANSWER'):
                                        print(f"AT command terminated with: {line_str}")
                                        break
                                else:
                                    # If we get a completion response without seeing echo, something is wrong
                                    if line_str in ('OK', 'ERROR') or line_str.startswith(('+CMS ERROR:', '+CME ERROR:', 'NO CARRIER', 'BUSY', 'NO ANSWER')):
                                        print(f"\033[31mWARNING: Got completion '{line_str}' without command echo for '{expected_echo}' - possible sync issue\033[0m") # Treat as completion anyway to avoid hanging
                                        break
                                    
                        except Exception as e:
                            print(f"Serial communication error: {e}")
                            pcat_logger.logger.error(f"Serial communication error: {e}")
                            self.cleanup_serial_connection()
                            self.querying = False
                            continue
                            
                except Exception as e:
                    print(f"Serial lock error: {e}")
                    pcat_logger.logger.error(f"Serial lock error: {e}")
                    self.querying = False
                    continue
                
                s = recv_data.decode("utf-8", "replace").replace("\r", "\\r").replace("\n", "\\n")
                print(f"[MODEM RECV]: {s}", flush=True)
                
                if recv_data.startswith(b'AT+QNWINFO\r\r\n') or recv_data.startswith(b'AT+QNWINFO\r\n'): #5G network info, #500U not working
                    self.handle_qnwinfo(recv_data)
                elif recv_data.startswith(b'AT+QCAINFO\r\r\n') or recv_data.startswith(b'AT+QCAINFO\r\n'): #5G network info
                    self.handle_qcainfo(recv_data)
                elif recv_data.startswith(b'AT+QENG="servingcell"\r\r\n') or recv_data.startswith(b'AT+QENG="servingcell"\r\n'): #serving cell
                    self.handle_qeng_servingcell(recv_data)
                elif recv_data.startswith(b'AT+QCFG="usbnet"\r\r\n') or recv_data.startswith(b'AT+QCFG="usbnet"\r\n'): #usbnet mode
                    self.handle_qcfg_usbnet(recv_data)
                elif recv_data.startswith(b'AT+QCFG="usbspeed"\r\r\n') or recv_data.startswith(b'AT+QCFG="usbspeed"\r\n'): #usb speed
                    self.handle_usbspeed(recv_data)
                elif recv_data.startswith(b'AT+QRSRP\r\r\n') or recv_data.startswith(b'AT+QRSRP\r\n'): #5G power
                    self.handle_qrsrp(recv_data)
                elif recv_data.startswith(b'AT+QRSRQ\r\r\n') or recv_data.startswith(b'AT+QRSRQ\r\n'): #5G quality
                    self.handle_qrsrq(recv_data)
                elif recv_data.startswith(b'AT+QSINR\r\r\n') or recv_data.startswith(b'AT+QSINR\r\n'): #5G SNR
                    self.handle_qsinr(recv_data)
                elif recv_data.startswith(b'AT+QSPN\r\r\n') or recv_data.startswith(b'AT+QSPN\r\n'): #ISP
                    self.handle_qspn(recv_data)
                elif recv_data.startswith(b'AT+CSQ\r\r\n') or recv_data.startswith(b'AT+CSQ\r\n'): #Signal strength
                    self.handle_csq(recv_data)
                elif recv_data.startswith(b'AT+CESQ\r\r\n') or recv_data.startswith(b'AT+CESQ\r\n'): #Extended signal quality (FM350)
                    self.handle_cesq(recv_data)
                elif recv_data.startswith(b'AT+CGSN\r\r\n') or recv_data.startswith(b'AT+CGSN\r\n'): #IMEI
                    self.handle_cgsn(recv_data)
                elif recv_data.startswith(b'AT+CNUM\r\r\n') or recv_data.startswith(b'AT+CNUM\r\n'): #SIM number
                    self.handle_cnum(recv_data)
                elif recv_data.startswith(b'AT+CCID\r\r\n') or recv_data.startswith(b'AT+CCID\r\n'): #SIM CCID
                    self.handle_ccid(recv_data)
                elif recv_data.startswith(b'AT+CIMI\r\r\n') or recv_data.startswith(b'AT+CIMI\r\n'): #SIM CIMI, 500U not working
                    self.handle_cimi(recv_data)
                elif recv_data.startswith(b'AT+QTEMP\r\r\n') or recv_data.startswith(b'AT+QTEMP\r\n'): #Temperature
                    self.handle_qtemp(recv_data)
                elif recv_data.startswith(b'AT+GTSENRDTEMP') or recv_data.startswith(b'AT+GTSENRDTEMP\r\n'): #FM350 Temperature
                    self.handle_gtsenrdtemp(recv_data)
                elif recv_data.startswith(b'AT+QNWPREFCFG') or recv_data.startswith(b'AT+QNWPREFCFG\r\n'): # 4G/5G band
                    self.handle_bands_and_roam_pref(recv_data)
                elif recv_data.startswith(b'AT+GTACT') or recv_data.startswith(b'AT+GTACT\r\n'): # FM350 network mode
                    self.handle_gtact(recv_data)
                elif recv_data.startswith(b'AT+GTCCINFO?') or recv_data.startswith(b'AT+GTCCINFO?\r\n'): # FM350 cell info
                    self.handle_gtccinfo(recv_data)
                elif recv_data.startswith(b'AT+GTCAINFO?') or recv_data.startswith(b'AT+GTCAINFO?\r\n'): # FM350 CA info
                    self.handle_gtcainfo(recv_data)
                elif recv_data.startswith(b'ATI\r\r\n') or recv_data.startswith(b'ATI\r\n'): #Modem model/version/etc
                    self.handle_ati(recv_data)
                elif recv_data.startswith(b'AT+COPS=3'): # COPS format setting, just OK
                    pass
                elif recv_data.startswith(b'AT+COPS?') or recv_data.startswith(b'AT+COPS?\r\n'): #Carrier info
                    self.handle_cops(recv_data)
                elif recv_data.startswith(b'AT+CPIN=') or recv_data.startswith(b'AT+CPIN?\r\n'): #CPIN
                    self.handle_cpin(recv_data)
                elif recv_data.startswith(b'AT+QCFG="band"\r\r\n') or recv_data.startswith(b'AT+QCFG="band"\r\n'): #4G bands
                    self.handle_qcfg_band(recv_data)
                elif recv_data.startswith(b'AT+QCFG="roamservice"\r\r\n') or recv_data.startswith(b'AT+QCFG="roamservice"\r\n'): #roam pref
                    self.handle_qcfg_roamservice(recv_data)
                elif recv_data.find(b'+CPMS: ') > -1 and recv_data.find(b'OK') > -1: #Modem storage
                    if recv_data.find(b'SM') > -1:
                        self.storage = "SM" #SIM
                    else:
                        self.storage = "ME" #Mobile
                else:
                    print("Warning: Unexpected modem response:", recv_data)
                

                if len(self.cmd_queue) == 0: #No more commands to send
                    pass #dont have to do anything
                
                # Add small delay between commands to prevent sync issues
                self.querying = False
                time.sleep(0.05)  # 50ms delay between commands for better synchronization

    def handle_cpin(self, response: bytes):
        """
        Handles the response to the AT+CPIN command.
        It checks if the PIN is correct, incorrect, or if any other status is returned.
        """
        decoded = response.decode("utf-8", errors="ignore").strip()
        print(f"CPIN Response: {decoded}")

        # Check if the response indicates that the PIN was correct or if it's incorrect
        if "+CPIN: READY" in decoded:
            print("SIM card PIN is correct, and the SIM is ready.")
            return True
        elif "+CPIN: SIM PIN" in decoded:
            print("SIM card requires PIN.")
            return False
        elif "+CPIN: SIM PUK" in decoded:
            print("SIM card is locked (PUK required).")
            return False
        elif "+CPIN: ERROR" in decoded or "ERROR" in decoded:
            print("Error with SIM card or PIN entry.")
            return False
        else:
            print("Unrecognized CPIN response:", decoded)
            return False

    def factory_reset_modem(self):
        self.mutex.acquire()
        if self.is_520():
            self.cmd_queue.append('AT+QCFG="ResetFactory"') #maybe only for rm520
        elif self.is_500u():
            self.cmd_queue.append('AT&F') #only for 500u
        self.mutex.release()

    def get_sim_number(self, sim_ccid: str, sim_cimi: str) -> str | None:
        """
        Look up a SIM number in /etc/pcat-sim_number.json by matching sim_ccid and sim_cimi.
        Returns the sim_number if found, otherwise None.
        """
        db_path = "/etc/pcat-sim_number.json"
        if not os.path.isfile(db_path):
            return None

        try:
            with open(db_path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

        sim_entries = data.get("sim_numbers", [])
        for entry in sim_entries:
            if entry.get("sim_ccid") == sim_ccid and entry.get("sim_cimi") == sim_cimi:
                return entry.get("sim_number")
        return None

    def save_sim_success(self, sim_ccid: str, sim_cimi: str, sim_number: str) -> None:
            """
            Append (sim_ccid, sim_cimi, sim_number) to /etc/pcat-sim_number.json.
            If the file or the "sim_numbers" array does not exist, create them.
            """
            db_path = "/etc/pcat-sim_number.json"
            # Prepare initial structure if file is missing or invalid
            if not os.path.isfile(db_path):
                data = {"sim_numbers": []}
            else:
                try:
                    with open(db_path, "r") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    data = {"sim_numbers": []}

            sim_entries = data.get("sim_numbers", [])
            # Append new record
            sim_entries.append({
                "sim_ccid": sim_ccid,
                "sim_cimi": sim_cimi,
                "sim_number": sim_number
            })
            data["sim_numbers"] = sim_entries

            # Write back to disk
            try:
                with open(db_path, "w") as f:
                    json.dump(data, f, indent=2)
            except OSError as e:
                print(f"Error saving SIM info to {db_path}: {e}")


    def check_my_number_with_api(self):
        """
        Blocking method that POSTs to the lookup endpoint.
        It will immediately return if another invocation is still running.
        """
        global sms_sent_flag

        if sms_sent_flag:
            print("Check_my_number_with_api: SMS already sent, skipping API lookup")
            return

        if self.api_lookup_lock.locked():
            print("Check_my_number_with_api: API lookup already in progress, skipping this call")
            return

        return

    def start_check_my_number(self):
        """
        Spawns a background thread to run check_my_number_with_api().
        If a lookup is already in progress, it will immediately skip.
        """
        #t = threading.Thread(target=self.check_my_number_with_api, daemon=True)
        #t.start()
        self.check_my_number_with_api()

    def handle_qtemp(self, response: bytes):
        """
        Parse QTEMP response for temperature measurements.
        Example response:
            b'AT+QTEMP\r\r\n+QTEMP:"modem-lte-sub6-pa1","42"\r\n...OK\r\n'
        Each line starting with +QTEMP: is of the form:
            +QTEMP:"<sensor-name>","<value>"
        We collect them in a dict and store under self.basic["qtemp_sensors"].
        """

        if b"+QTEMP:" not in response:
            print("Warning: Unexpected QTEMP format:", response)
            self.cmd_mark_failure("AT+QTEMP")
            return

        temps = {}
        lines = response.split(b"\r\n")
        for line in lines:
            if line.startswith(b"+QTEMP:"):
                try:
                    payload = line[len(b"+QTEMP:"):].strip()
                    parts = payload.split(b",")
                    sensor = parts[0].strip().strip(b'"').decode("ascii")
                    value = int(parts[1].strip().strip(b'"'))
                    temps[sensor] = value
                except Exception as e:
                    print(f"Failed to parse line {line!r}: {e}")

        if temps:
            self.basic["qtemp_sensors"] = temps
            self.cmd_mark_success("AT+QTEMP")
            print(f"Parsed QTEMP → {temps}")
        else:
            self.cmd_mark_failure("AT+QTEMP")
            print("Warning: No QTEMP data parsed.")

    def handle_gtsenrdtemp(self, response: bytes):
        """
        Parse AT+GTSENRDTEMP response for FM350-GL temperature measurements.

        Example response for AT+GTSENRDTEMP=1:
            AT+GTSENRDTEMP=1
            +GTSENRDTEMP: 1,50300
            OK

        Format: +GTSENRDTEMP: <sensor_id>,<temperature_in_thousandths>

        Sensor ID 1 returns the main temperature in thousandths of degrees Celsius
        (e.g., 50300 = 50.3°C)
        """
        if b"+GTSENRDTEMP:" not in response:
            print("Warning: Unexpected GTSENRDTEMP format:", response)
            self.cmd_mark_failure("AT+GTSENRDTEMP=1")
            return

        lines = response.split(b"\r\n")
        for line in lines:
            if line.startswith(b"+GTSENRDTEMP:"):
                try:
                    payload = line[len(b"+GTSENRDTEMP:"):].strip()
                    parts = payload.split(b",")
                    sensor_id = int(parts[0].strip())
                    temperature_thousandths = int(parts[1].strip())

                    # Convert from thousandths to degrees Celsius
                    temperature = temperature_thousandths / 1000.0

                    # Round to 1 decimal place for display
                    display_temp = round(temperature, 1)

                    print(f"Parsed GTSENRDTEMP sensor {sensor_id}: {temperature_thousandths}/1000 = {display_temp}°C")

                    # Store the temperature as simple number for FM350
                    self.basic["modem_temperature"] = display_temp
                    # Also store in qtemp_sensors dict format for API compatibility with Qualcomm modems
                    # Use int() to match Qualcomm's integer format expected by Go consumer
                    self.basic["qtemp_sensors"] = {"modem-ambient-usr": int(temperature)}
                    # Store decimal temperature separately for web UI precision
                    self.basic["modem_temperature_decimal"] = display_temp

                    self.cmd_mark_success("AT+GTSENRDTEMP=1")
                    return  # Only process first temperature line

                except Exception as e:
                    print(f"Failed to parse GTSENRDTEMP line {line!r}: {e}")

        self.cmd_mark_failure("AT+GTSENRDTEMP=1")
        print("Warning: No GTSENRDTEMP data parsed.")

    def set_tech_pref(self, tech_pref: str, force_update: bool = False):
        """
        Set the mode preference.
        """
        print(f"set_tech_pref called with tech_pref={tech_pref}, force_update={force_update}")
        print(f"is_em05={self.is_em05()}, is_520={self.is_520()}")

        if self.is_em05():
            print("set_tech_pref: EM05 detected, returning early")
            return

        if str(tech_pref) in ["AUTO", "LTE", "NR5G"]:
            print(f"set_tech_pref: Valid tech_pref, checking if update needed")
            current_tech_pref = self.basic.get("modem_tech_pref", "not_set")
            print(f"set_tech_pref: Current modem_tech_pref={current_tech_pref}")
            if tech_pref != self.basic["modem_tech_pref"] or force_update:
                if self.is_fm350():
                    # FM350-GL (MediaTek) uses AT+GTACT command
                    # Per spec: 2=LTE only, 14=NR-RAN only, 20=NR-RAN/WCDMA/LTE (full auto)
                    gtact_value = {"AUTO": "20", "LTE": "2", "NR5G": "14"}[tech_pref]
                    print(f"set_tech_pref: FM350 detected, adding AT command: AT+GTACT={gtact_value}")
                    self.cmd_queue_append(f'AT+GTACT={gtact_value}')
                    self.cmd_queue_append('AT+GTACT?') # check modem settings
                else:
                    # Qualcomm modems use AT+QNWPREFCFG
                    print(f"set_tech_pref: Adding AT command to queue: AT+QNWPREFCFG=\"mode_pref\",{tech_pref}")
                    self.cmd_queue_append(f'AT+QNWPREFCFG="mode_pref",{tech_pref}')
                    self.cmd_queue_append('AT+QNWPREFCFG="mode_pref"') # check modem settings
                self.basic["modem_tech_pref"] = tech_pref
            else:
                print(f"set_tech_pref: Tech pref is the same ({tech_pref}), force_update={force_update}, skip")
        else:
            print(f"Warning: Invalid mode preference: {tech_pref}")

    def handle_qcfg_roamservice(self, response: bytes): #em05 specific
        """
        AT+QCFG="roamservice"[,<roammode>[,<effect>]]
        <roammode> Number format. The mode of roam service.
        1 Disable roam service
        2 Enable roam service
        
        Expected response format:
        +QCFG: "roamservice",<roammode>
        """
        try:
            decoded = response.decode("utf-8", errors="ignore")
            lines = decoded.split("\r\n")
            
            for line in lines:
                if '+QCFG: "roamservice",' in line:
                    # Extract the roam mode value
                    roam_mode = line.split(",")[-1].strip()
                    
                    # Convert EM05 roam mode numbers to standard format
                    if roam_mode == "1":  # Disable roam service
                        self.basic["roam_pref"] = "no"
                    elif roam_mode == "2":  # Enable roam service  
                        self.basic["roam_pref"] = "any"
                    else:
                        # Unknown mode, set default
                        self.basic["roam_pref"] = "any"
                        print(f"Warning: Unknown EM05 roam mode: {roam_mode}")
                    
                    self.basic["roam_pref_num"] = roam_mode
                    print(f"EM05 roam_pref => {self.basic['roam_pref']} (mode: {roam_mode})")
                    return
                    
            print("Warning: No roamservice data found in response:", decoded)
            
        except Exception as e:
            print(f"Error parsing roamservice response: {e}")
            print(f"Response was: {response}")


    def set_roam_pref(self, roam_pref: str):
        """
        Set the roaming preference.

        Note: FM350-GL (MediaTek) modems don't have a direct roaming preference command
        like Qualcomm modems. Roaming is typically controlled by the network operator
        and SIM card settings. We store the preference for UI consistency but don't
        send AT commands for FM350.
        """
        print(f"set_roam_pref called with roam_pref={roam_pref}")
        if str(roam_pref) == "all":
            roam_pref = "any"
        if str(roam_pref) in ["any", "affiliated", "no"]:
            if self.is_fm350():
                # FM350-GL has no proprietary roam toggle. Use standard 3GPP
                # AT+COPS to control network registration:
                #   any        -> COPS=0 (automatic, allows roaming)
                #   affiliated -> COPS=0 (same as any; SIM/carrier policy decides)
                #   no         -> not reliably enforceable via AT on FM350;
                #                 fall back to storing preference only.
                print(f"set_roam_pref: FM350 detected, roam_pref='{roam_pref}'")
                self.basic["roam_pref"] = roam_pref
                pcat_config.set_user_config_key("roaming_pref", roam_pref)
                if roam_pref in ("any", "affiliated"):
                    self.cmd_queue_append('AT+COPS=0')
                    self.basic["fm350_cops_auto_set"] = True
            elif self.is_em05():
                if roam_pref == "any":
                    roam_pref_num = "2"
                elif roam_pref == "affiliated":
                    roam_pref_num = "2"
                elif roam_pref == "no":
                    roam_pref_num= "1"

                if roam_pref_num != self.basic.get("roam_pref_num"):
                    self.cmd_queue_append(f'AT+QCFG="roamservice",{roam_pref_num}')
                    self.cmd_queue_append('AT+QCFG="roamservice"') # check again modem settings
                    self.basic["roam_pref"] = roam_pref
                else:
                    print("set_roam_pref: EM05 roam pref is the same, skip")
            else:
                # Qualcomm modems (RM500U, RM520, etc.)
                if roam_pref == "any":
                    roam_pref_num = "255"
                elif roam_pref == "affiliated":
                    roam_pref_num = "3"
                elif roam_pref == "no":
                    roam_pref_num= "1"

                if roam_pref_num != self.basic.get("roam_pref_num"):
                    self.cmd_queue_append(f'AT+QNWPREFCFG="roam_pref",{roam_pref_num}')
                    self.cmd_queue_append('AT+QNWPREFCFG="roam_pref"') # check again modem settings
                    self.basic["roam_pref"] = roam_pref
                else:
                    print("set_roam_pref: Qualcomm roam pref is the same, skip")
        else:
            print(f"Warning: Invalid roam preference: {roam_pref}")

    def set_lte_bands(self, desired_lte_bands: str):
        print("\n\n\n\n setting_lte_bands", desired_lte_bands)
        if self.is_520() or self.is_500q():
            self._apply_band_pref(label="lte_band", policy_key="policy_lte_bands", setting_key="setting_lte_bands", user_want=desired_lte_bands)
        elif self.is_500u():
            cmd = f'AT+QNWPREFCFG="lte_band",{desired_lte_bands}'
            self.cmd_queue_append(cmd)
            self.cmd_queue_append('AT+QNWPREFCFG="lte_band"') # check modem settings
        elif self.is_em05():
            self.basic["setting_lte_bands"] = desired_lte_bands
            self.set_em05_lte_bands(desired_lte_bands)
        elif self.is_fm350():
            # FM350-GL: just store filtered LTE bands here.
            # The combined AT+GTACT command is sent by set_nr5g_bands (called next).
            policy_lte = self.basic.get("policy_lte_bands", "")
            allowed_lte = set(int(b) for b in policy_lte.split(":") if b.isdigit()) if policy_lte else set()
            lte_band_list = [int(b) for b in desired_lte_bands.split(":") if b.isdigit()]
            if allowed_lte:
                lte_band_list = [b for b in lte_band_list if b in allowed_lte]
            filtered_lte = ":".join(str(b) for b in sorted(lte_band_list))
            print(f"set_lte_bands: FM350 filtered bands: {filtered_lte}")
            self.basic["setting_lte_bands"] = filtered_lte

    def set_nr5g_bands(self, desired_nr5g_bands: str):
        print("\n\n\n\n setting_nr5g_bands", desired_nr5g_bands)
        if self.is_520() or self.is_500q() or self.is_em05():
            self._apply_band_pref(label="nr5g_band", policy_key="policy_nr5g_bands", setting_key="setting_nr5g_bands", user_want=desired_nr5g_bands)
        elif self.is_500u():
            cmd = f'AT+QNWPREFCFG="nr5g_band",{desired_nr5g_bands}'
            self.cmd_queue_append(cmd)
            self.cmd_queue_append('AT+QNWPREFCFG="nr5g_band"') # check modem settings
        elif self.is_fm350():
            # FM350-GL: filter NR bands against policy, then send combined AT+GTACT
            policy_nr = self.basic.get("policy_nr5g_bands", "")
            allowed_nr = set(int(b) for b in policy_nr.split(":") if b.isdigit()) if policy_nr else set()
            nr5g_band_list = [int(b) for b in desired_nr5g_bands.split(":") if b.isdigit()]
            if allowed_nr:
                nr5g_band_list = [b for b in nr5g_band_list if b in allowed_nr]
            fm350_nr5g_bands = [int(f"50{b}") for b in nr5g_band_list]

            # Get LTE bands (already filtered and stored by set_lte_bands)
            current_lte = self.basic.get("setting_lte_bands", "")
            lte_band_list = [int(b) for b in current_lte.split(":") if b.isdigit()] if current_lte else []
            fm350_lte_bands = [100 + b for b in lte_band_list]

            # Get current tech_pref to preserve RAT setting
            current_tech = self.basic.get("tech_pref", "AUTO")
            gtact_rat = {"AUTO": "20", "LTE": "2", "NR5G": "14"}.get(current_tech, "20")

            # Get PreferredAct and UMTS bands from last GTACT query
            preferred_acts = self.basic.get("gtact_preferred_acts", [6, 3])
            umts_bands = self.basic.get("gtact_umts_bands", [1, 2, 4, 5, 8])

            # Build AT+GTACT command: AT+GTACT=<rat>,<PreferredAct1>,<PreferredAct2>,<bands...>
            # PreferredAct: 2=WCDMA, 3=LTE, 6=NR-RAN
            pref_params = ",".join(str(p) for p in preferred_acts) if preferred_acts else "6,3"
            all_bands = umts_bands + fm350_lte_bands + fm350_nr5g_bands
            band_params = ",".join(str(b) for b in all_bands)
            cmd = f'AT+GTACT={gtact_rat},{pref_params},{band_params}'
            print(f"set_nr5g_bands: FM350 detected, setting bands: {cmd}")
            self.cmd_queue_append(cmd)
            self.cmd_queue_append('AT+GTACT?')
            self.basic["setting_nr5g_bands"] = ":".join(str(b) for b in sorted(nr5g_band_list))

    def _normalize_bands(self, bands_str: str) -> str:
        nums = [int(tok) for tok in bands_str.split(":") if tok.isdigit()]
        nums = sorted(set(nums))
        return ":".join(str(n) for n in nums)

       
    def handle_cops(self, response: bytes):
        """
        Parse +COPS? response and store:
        - operator_mode        (int)
        - operator_format      (int)
        - operator             (str)
        - operator_act         (int)
        - operator_rat         (str, e.g. 'GSM', 'LTE', '5G')
        Example response:
        b'+COPS: 0,2,"46000",11'
        """
        # look for the +COPS: line anywhere in the payload
        m = re.search(
            br'^\+COPS:\s*(\d+),\s*(\d+),\s*"([^"]+)",\s*(\d+)',
            response,
            flags=re.MULTILINE
        )
        if not m:
            print("Warning: Unexpected modem response:", response)
            return

        mode_str, fmt_str, oper_bytes, act_str = m.groups()
        mode = int(mode_str)
        fmt  = int(fmt_str)
        oper_raw = oper_bytes.decode('utf-8', errors='ignore')

        # Decode hex-encoded operator name (UCS2/UTF-16BE format)
        # FM350 returns names like "4E2D56FD8054901A" which is hex-encoded Chinese
        def decode_operator_name(name_str):
            try:
                # Check if it's all hex characters and even length
                if all(c in "0123456789ABCDEFabcdef" for c in name_str) and len(name_str) % 4 == 0:
                    # Try to decode as UCS2/UTF-16BE
                    decoded = bytes.fromhex(name_str).decode("utf-16-be", errors="ignore")
                    # Return decoded string if it's non-empty and contains valid characters
                    # Accept both ASCII printable and Unicode characters (for Chinese, etc.)
                    if decoded and not all(ord(c) < 32 for c in decoded):
                        return decoded
            except Exception:
                pass
            return name_str

        oper = decode_operator_name(oper_raw)
        act  = int(act_str)

        # map AcT codes to radio-access technologies
        act_map = {
            0: "GSM",      # 2G
            1: "GSM Compact",
            2: "UTRAN",    # 3G
            3: "GSM w/EGPRS",
            4: "UTRAN HSDPA",
            5: "UTRAN HSUPA",
            6: "UTRAN HSDPA+HSUPA",
            7: "E-UTRAN",  # 4G LTE
            8: "E-UTRAN (64QAM)",
            9: "E-UTRAN (256QAM)",
            10: "E-UTRAN (CA)",
            11: "NR5G",    # 5G
        }
        rat = act_map.get(act, f"UNKNOWN({act})")

        # store into your basic dict
        self.basic["operator_mode"]   = mode
        self.basic["operator_format"] = fmt
        self.basic["operator"]        = oper
        self.basic["operator_act"]    = act
        self.basic["operator_rat"]    = rat

        # For FM350, populate ISP and network fields from COPS numeric format
        # FM350 doesn't support AT+QNWINFO, so we derive cell_tech and cell_band from COPS
        if self.is_fm350():
            # Map AcT to cell_tech used by dashboard
            act_to_tech = {
                0: "2G", 1: "2G", 2: "3G", 3: "2G",
                4: "3G", 5: "3G", 6: "3G",
                7: "4G", 8: "4G", 9: "4G", 10: "4G",
                11: "5G",
            }
            tech = act_to_tech.get(act, "")
            if tech:
                self.basic["cell_tech"] = tech
            self.basic["network_info"] = rat

            if fmt == 2 and len(oper) >= 5:
                # Numeric format (MCC+MNC) — primary query format for FM350
                self.basic["cell_mccmnc"] = oper
                self.basic["enhanced_isp_mcc"] = oper[:3]
                self.basic["enhanced_isp_mnc"] = oper[3:]
                isp_name = lookup_isp_details(oper)
                self.basic["isp_details"] = isp_name
                if isp_name:
                    self.basic["cell_carrier_info"] = f"{isp_name}, {rat}"
                print(f"Parsed COPS (numeric) → '{oper}', AcT={act} ({rat}), ISP={isp_name}")
            elif fmt == 0:
                self.basic["cell_isp_long_name"] = oper
                print(f"Parsed COPS (long name) → '{oper}'")

        print(
            f"Parsed COPS → mode={mode}, format={fmt}, "
            f"operator='{oper}', AcT={act} ({rat})"
        )


    def _apply_band_pref(self, label, policy_key, setting_key, user_want):
        """
        label:       the AT command label, e.g. "lte_band" or "nr5g_band"
        policy_key:  e.g. "policy_lte_bands"
        setting_key: e.g. "setting_lte_bands"
        user_want:    colon-string of bands user specifically requested
        """
        # split into lists, dropping any empty strings
        policy_list  = self._normalize_bands(self.basic.get(policy_key, ""))
        setting_list = self._normalize_bands(self.basic.get(setting_key, ""))
        desired = self._normalize_bands(user_want)

        # compare as sets (order doesn’t matter)
        if set(desired) != set(setting_list):
            cmd = f'AT+QNWPREFCFG="{label}",{desired}'
            self.cmd_queue_append(cmd) # update desired setting
            self.cmd_queue_append('AT+QNWPREFCFG="ue_capability_band"') # check modem settings
            self.cmd_queue_append('AT+QNWINFO') # check modem settings
            self.cmd_queue_append('AT+QCAINFO') # check carrier info
           

    def handle_usbspeed(self, response: bytes):
        # Check for ERROR response (modem doesn't support this command)
        if b'ERROR' in response:
            self.cmd_mark_failure('AT+QCFG="usbspeed"')
            return

        decoded = response.decode("utf-8", errors="ignore")
        lines = decoded.split("\r\n")
        for line in lines:
            if '+QCFG: "usbspeed",' in line:
                self.basic["modem_usb_speed"] = line.split(",")[-1].strip().strip('"')
                print("modem_usb_speed =>", self.basic["modem_usb_speed"])

    def set_usb_speed(self, usb_speed: str):
        print("\n\n\n\n setting_usb_speed", usb_speed)
        if usb_speed in ["20", "311", "312"]:
            self.cmd_queue_append(f'AT+QCFG="usbspeed","{usb_speed}"')
            self.cmd_queue_append('AT+QCFG="usbspeed"') # check modem settings
            self.basic["modem_usb_speed"] = usb_speed
            print("modem_usb_speed =>", self.basic["modem_usb_speed"])
        else:
            print(f"Warning: Invalid USB speed: {usb_speed}")

    def handle_qcfg_band(self, response: bytes): #em-05 specific
        """AT+QCFG="band" Band Configuration
            Write Command
            AT+QCFG="band"[,<bandval>,<lteban
            dval>,<tdsbandval>[,<effect>]]
            Response
            If configuration parameters are omitted (that is, only execute
            AT+QCFG="band"), return the current configuration:
            +QCFG: "band",<bandval>,<ltebandval>,<tdsbandval>
            LTE Module Series
            EM05 AT Commands Manual
            EM05_AT_Commands_Manual 40 / 189
            Parameter
            OK
            If configuration parameters are all entered, configure the
            preferred frequency bands to be searched:
            OK
            ERROR
            If there is any error related to ME functionality:
            +CME ERROR: <err>
            Maximum Response Time 300ms
            <bandval> A hexadecimal value that specifies the GSM and WCDMA frequency bands. If it
            is set to 0, it means not to change GSM and WCDMA frequency bands. (e.g.:
            00000013=00000001(GSM900)+00000002(GSM1800)+00000010(WCDMA
            2100))
            00000000 No change
            00000001 GSM 900
            00000002 GSM 1800
            00000004 GSM 850
            00000008 GSM 1900
            00000010 WCDMA 2100
            00000020 WCDMA 1900
            00000040 WCDMA 850
            00000080 WCDMA 900
            00000100 WCDMA 800
            00000200 WCDMA 1700
            0000FFFF Any frequency band
            <ltebandval> A hexadecimal value that specifies the LTE frequency band. If it is set to 0 or
            0x40000000, it means not to change the LTE frequency band. (e.g.:
            0x15=0x1(LTE B1)+0x4(LTE B3)+0x10(LTE B5))
            0x1 (CM_BAND_PREF_LTE_EUTRAN_BAND1) LTE B1
            0x4 (CM_BAND_PREF_LTE_EUTRAN_BAND3) LTE B3
            0x10 (CM_BAND_PREF_LTE_EUTRAN_BAND5) LTE B5
            0x40 (CM_BAND_PREF_LTE_EUTRAN_BAND7) LTE B7
            0x80 (CM_BAND_PREF_LTE_EUTRAN_BAND8) LTE B8
            0x80000(CM_BAND_PREF_LTE_EUTRAN_BAND20) LTE B20
            0x7FFFFFFFFFFFFFFF(CM_BAND_PREF_ANY) Any frequency band
            <tdsbandval> A hexadecimal value that specifies the TD-SCDMA frequency band. If it is set to 0
            or 0x40000000, it means not to change the TD-SCDMA frequency band. (e.g.:
            0x21=0x1(TDS BCA) +0x20(TDS BCF))
            LTE Module Series
            EM05 AT Commands Manual
            EM05_AT_Commands_Manual 41 / 189
            4.3.6. AT+QCFG="hsdpacat" HSDPA Category Configuration
            The command specifies the HSDPA category. This configuration is valid only after the module is restarted.
            Parameter
            0x1 (CM_BAND_PREF_TDS_BANDA) TDS BCA
            0x2 (CM_BAND_PREF_TDS_BANDB) TDS BCB
            0x4 (CM_BAND_PREF_TDS_BANDC) TDS BCC
            0x8 (CM_BAND_PREF_TDS_BANDD) TDS BCD
            0x10 (CM_BAND_PREF_TDS_BANDE) TDS BCE
            0x20 (CM_BAND_PREF_TDS_BANDF) TDS BCF
            <effect> When to take effect
            0 Take effect after UE reboots
            1 Take effect immediately"""
            #sample return: b'AT+QCFG="band"\r\r\n+QCFG: "band",0x4d0,0x1e200000095,0x0\r\n\r\nOK\r\n'
        
        try:
            decoded = response.decode("utf-8", errors="ignore")
            lines = decoded.split("\r\n")
            
            for line in lines:
                if line.startswith('+QCFG: "band"'):
                    # Parse the response: +QCFG: "band",<bandval>,<ltebandval>,<tdsbandval>
                    parts = line.split(",")
                    if len(parts) >= 4:
                        # Extract hex values, removing quotes and whitespace
                        bandval = parts[1].strip()
                        ltebandval = parts[2].strip()  
                        tdsbandval = parts[3].strip()
                        
                        # Store raw hex values
                        self.basic["qcfg_band_gsm_wcdma"] = bandval
                        self.basic["qcfg_band_lte"] = ltebandval
                        self.basic["qcfg_band_tds"] = tdsbandval
                        
                        # Parse GSM/WCDMA bands
                        gsm_wcdma_bands = self._parse_gsm_wcdma_bands(bandval)
                        self.basic["qcfg_band_gsm_wcdma_list"] = gsm_wcdma_bands
                        
                        # Parse LTE bands
                        lte_bands = self._parse_lte_bands(ltebandval)
                        self.basic["qcfg_band_lte_list"] = lte_bands
                        
                        # Parse TDS bands
                        tds_bands = self._parse_tds_bands(tdsbandval)
                        self.basic["qcfg_band_tds_list"] = tds_bands

                        # Convert LTE bands to colon-separated format
                        self.basic["setting_lte_bands"] = self._lte_bands_to_colon_format(lte_bands)
                        self.basic["policy_lte_bands"] = "1:3:5:8:34:38:39:40:41"

                        self.basic["setting_nr5g_bands"] = "-"
                        self.basic["policy_nr5g_bands"] = "-"

                        
                        print(f"QCFG Band - GSM/WCDMA: {bandval} ({gsm_wcdma_bands})")
                        print(f"QCFG Band - LTE: {ltebandval} ({lte_bands})")
                        print(f"QCFG Band - TDS: {tdsbandval} ({tds_bands})")
                        
                        return
            
            print(f"Could not parse QCFG band response: {decoded}")
            
        except Exception as e:
            print(f"Error parsing QCFG band response: {e}")
            print(f"Response was: {response}")

    def _parse_gsm_wcdma_bands(self, hex_str: str) -> list:
        """Parse GSM and WCDMA band hex value into list of band names"""
        try:
            if hex_str.startswith('0x'):
                value = int(hex_str, 16)
            else:
                value = int(hex_str, 16)
            
            bands = []
            band_map = {
                0x0001: "GSM 900",
                0x0002: "GSM 1800", 
                0x0004: "GSM 850",
                0x0008: "GSM 1900",
                0x0010: "WCDMA 2100",
                0x0020: "WCDMA 1900",
                0x0040: "WCDMA 850",
                0x0080: "WCDMA 900",
                0x0100: "WCDMA 800",
                0x0200: "WCDMA 1700"
            }
            
            if value == 0:
                return ["No change"]
            elif value == 0xFFFF:
                return ["Any frequency band"]
            
            for mask, band_name in band_map.items():
                if value & mask:
                    bands.append(band_name)
                    
            return bands if bands else ["Unknown"]
            
        except Exception:
            return ["Parse error"]

    def _parse_lte_bands(self, hex_str: str) -> list:
        """Parse LTE band hex value into list of band numbers"""
        try:
            if hex_str.startswith('0x'):
                value = int(hex_str, 16)
            else:
                value = int(hex_str, 16)
            
            bands = []
            
            if value == 0 or value == 0x40000000:
                return ["No change"]
            elif value == 0x7FFFFFFFFFFFFFFF:
                return ["Any frequency band"]
            
            # LTE band mapping (bit positions to band numbers)
            lte_band_map = {
                0: 1,   # 0x1 -> LTE B1
                2: 3,   # 0x4 -> LTE B3  
                4: 5,   # 0x10 -> LTE B5
                6: 7,   # 0x40 -> LTE B7
                7: 8,   # 0x80 -> LTE B8
                19: 20  # 0x80000 -> LTE B20
            }
            
            # Check each bit position
            for bit_pos in range(64):  # 64-bit value
                if value & (1 << bit_pos):
                    if bit_pos in lte_band_map:
                        bands.append(f"B{lte_band_map[bit_pos]}")
                    else:
                        # For other bands, the band number is typically bit_pos + 1
                        bands.append(f"B{bit_pos + 1}")
                        
            return bands if bands else ["Unknown"]
            
        except Exception:
            return ["Parse error"]

    def _parse_tds_bands(self, hex_str: str) -> list:
        """Parse TD-SCDMA band hex value into list of band names"""
        try:
            if hex_str.startswith('0x'):
                value = int(hex_str, 16)
            else:
                value = int(hex_str, 16)
            
            bands = []
            band_map = {
                0x01: "TDS BCA",
                0x02: "TDS BCB", 
                0x04: "TDS BCC",
                0x08: "TDS BCD",
                0x10: "TDS BCE",
                0x20: "TDS BCF"
            }
            
            if value == 0 or value == 0x40000000:
                return ["No change"]
            
            for mask, band_name in band_map.items():
                if value & mask:
                    bands.append(band_name)
                    
            return bands if bands else ["Unknown"]
            
        except Exception:
            return ["Parse error"]

    def _lte_bands_to_colon_format(self, lte_bands: list) -> str:
        """Convert LTE band list to colon-separated format
        Example: ['B1', 'B3', 'B5', 'B8', 'B34', 'B38', 'B39', 'B40', 'B41'] -> "1:3:5:8:34:38:39:40:41"
        """
        try:
            # Handle special cases
            if not lte_bands or lte_bands == ["Unknown"] or lte_bands == ["Parse error"]:
                return ""
            if lte_bands == ["No change"]:
                return ""
            if lte_bands == ["Any frequency band"]:
                return "1:3:5:8:34:38:39:40:41"  # Default all bands
            
            # Extract band numbers from format like "B1", "B3", etc.
            band_numbers = []
            for band in lte_bands:
                if band.startswith('B') and band[1:].isdigit():
                    band_numbers.append(int(band[1:]))
            
            # Sort and convert to colon-separated string
            if band_numbers:
                band_numbers.sort()
                return ":".join(str(num) for num in band_numbers)
            else:
                return ""
                
        except Exception as e:
            print(f"Error converting LTE bands to colon format: {e}")
            return ""

    def _colon_format_to_lte_hex(self, colon_bands: str) -> str:
        """Convert colon-separated LTE bands to hex value for AT+QCFG="band" command
        Example: "1:3:5:8:34:38:39:40:41" -> "0x1e200000095"
        """
        try:
            if not colon_bands or colon_bands.strip() == "":
                return "0x0"  # No change
            
            # Parse band numbers from colon-separated string
            band_numbers = []
            for band_str in colon_bands.split(":"):
                band_str = band_str.strip()
                if band_str.isdigit():
                    band_numbers.append(int(band_str))
            
            if not band_numbers:
                return "0x0"
            
            # Convert band numbers to hex bitmask
            hex_value = 0
            
            # LTE band to bit position mapping (based on Quectel documentation)
            # Some common mappings - this may need adjustment based on specific modem model
            lte_band_bit_map = {
                1: 0,    # B1 -> bit 0 (0x1)
                3: 2,    # B3 -> bit 2 (0x4)  
                5: 4,    # B5 -> bit 4 (0x10)
                7: 6,    # B7 -> bit 6 (0x40)
                8: 7,    # B8 -> bit 7 (0x80)
                20: 19,  # B20 -> bit 19 (0x80000)
                # Extended bands for higher numbers
                34: 33,  # B34 -> bit 33
                38: 37,  # B38 -> bit 37
                39: 38,  # B39 -> bit 38
                40: 39,  # B40 -> bit 39
                41: 40   # B41 -> bit 40
            }
            
            for band_num in band_numbers:
                if band_num in lte_band_bit_map:
                    bit_pos = lte_band_bit_map[band_num]
                    hex_value |= (1 << bit_pos)
                else:
                    # For unmapped bands, use band_num - 1 as bit position
                    bit_pos = band_num - 1
                    if 0 <= bit_pos < 64:  # Ensure within 64-bit range
                        hex_value |= (1 << bit_pos)
            
            return f"0x{hex_value:x}"
            
        except Exception as e:
            print(f"Error converting colon format to LTE hex: {e}")
            return "0x0"

    def set_em05_lte_bands(self, desired_lte_bands: str):
        """Set LTE bands for EM05 modem using AT+QCFG="band" command
        
        Args:
            desired_lte_bands: Colon-separated band list (e.g., "1:3:5:8:34:38:39:40:41")
        """
        try:
            print(f"Setting EM05 LTE bands: {desired_lte_bands}")
            
            # Convert colon format to hex value for LTE bands
            lte_hex = self._colon_format_to_lte_hex(desired_lte_bands)
            lte_hex_hexpart = lte_hex.split("x")[-1]
            
            # Get current GSM/WCDMA and TDS band settings to preserve them
            # Use current values or defaults if not available
            gsm_wcdma_hex = self.basic.get("qcfg_band_gsm_wcdma", "0x0")  # No change to GSM/WCDMA
            tds_hex = self.basic.get("qcfg_band_tds", "0x0")  # No change to TDS
            
            # Construct AT+QCFG="band" command
            # Format: AT+QCFG="band",<bandval>,<ltebandval>,<tdsbandval>,<effect>
            # effect=1 means take effect immediately
            #cmd = f'AT+QCFG="band",{gsm_wcdma_hex},{lte_hex},{tds_hex},1'
            #cmd = f'AT+QCFG="band",{gsm_wcdma_hex},{lte_hex},{tds_hex}'
            #lte_hex_hexpart = "1e200000095" #this is full bands ... 
            cmd = f'AT+QCFG="band",0,{lte_hex_hexpart},0,1'

            print(f"Sending EM05 band config command: {cmd}")
            self.cmd_queue_append(cmd)
            
            # Query current band configuration to verify
            self.cmd_queue_append('AT+QCFG="band"')
            
        except Exception as e:
            print(f"Error setting EM05 LTE bands: {e}")
        
    def handle_bands_and_roam_pref(self, response: bytes):
        decoded = response.decode("utf-8", errors="ignore")
        lines = decoded.split("\r\n")

        # parse "policy_band" reply
        if "policy_band" in decoded:
            for L in lines:
                if '"lte_band"' in L:
                    self.basic["policy_lte_bands"] = L.split(",")[-1]
                    print("policy_lte_bands =>", self.basic["policy_lte_bands"])
                if '"nr5g_band"' in L:
                    self.basic["policy_nr5g_bands"] = L.split(",")[-1]
                    print("policy_nr5g_bands =>", self.basic["policy_nr5g_bands"])
           
            # For RM520, override with hardcoded available bands if response is incomplete
            if self.is_520():
                # Hardcoded available bands for RM520 based on your complete logs
                rm520_lte_bands = "1:2:3:4:5:7:8:12:13:14:17:18:19:20:25:26:28:29:30:32:34:38:39:40:41:42:43:46:48:66:71"
                rm520_nr5g_bands = "1:2:3:5:7:8:12:13:14:18:20:25:26:28:29:30:38:40:41:48:66:70:71:75:76:77:78:79"

                # Only override if the parsed response has significantly fewer bands
                current_lte = self.basic.get("policy_lte_bands", "")
                current_nr5g = self.basic.get("policy_nr5g_bands", "")

                if len(current_lte.split(":")) < 25:  # If missing bands, use hardcoded
                    self.basic["policy_lte_bands"] = rm520_lte_bands
                    print(f"RM520 override: policy_lte_bands => {self.basic['policy_lte_bands']}")

                if len(current_nr5g.split(":")) < 25:  # If missing bands, use hardcoded
                    self.basic["policy_nr5g_bands"] = rm520_nr5g_bands
                    print(f"RM520 override: policy_nr5g_bands => {self.basic['policy_nr5g_bands']}")

            return

        # parse "ue_capability_band" (current settings)
        if "ue_capability_band" in decoded:
            for L in lines:
                if '"lte_band"' in L:
                    self.basic["setting_lte_bands"] = L.split(",")[-1]
                    print("setting_lte_bands =>", self.basic["setting_lte_bands"])
                if '"nr5g_band"' in L:
                    self.basic["setting_nr5g_bands"] = L.split(",")[-1]
                    print("setting_nr5g_bands =>", self.basic["setting_nr5g_bands"])
            return
        
        if "ue_usage_setting" in decoded:
            if '+QNWPREFCFG: "ue_usage_setting"' in decoded:
                self.basic["ue_usage_setting"] = decoded.split(",")[-1].strip()
                print("ue_usage_setting => ", self.basic["ue_usage_setting"])
            return

        if "roam_pref" in decoded:
            lines = decoded.split("\r\n")
            if self.is_500u():
                for line in lines:
                    if '+QNWPREFCFG: "roam_pref",' in line:
                        self.basic["roam_pref_num"] = line.split(",")[-1].strip()
                        if self.basic["roam_pref_num"] == "0":
                            self.basic["roam_pref"] = "any"
                        elif self.basic["roam_pref_num"] == "2":
                            self.basic["roam_pref"] = "affiliated"
                        elif self.basic["roam_pref_num"] == "3":
                            self.basic["roam_pref"] = "no"
                        print("roam_pref => ", self.basic["roam_pref"])
                        return

            for line in lines:
                if '+QNWPREFCFG: "roam_pref",' in line:
                    self.basic["roam_pref_num"] = line.split(",")[-1].strip()
                    if self.basic["roam_pref_num"] == "255":
                        self.basic["roam_pref"] = "any"
                    elif self.basic["roam_pref_num"] == "3":
                        self.basic["roam_pref"] = "affiliated"
                    elif self.basic["roam_pref_num"] == "1":
                        self.basic["roam_pref"] = "no"
                    print("roam_pref => ", self.basic["roam_pref"])
                    return
        
        if "mode_pref" in decoded:
            lines = decoded.split("\r\n")
            for line in lines:
                if '+QNWPREFCFG: "mode_pref",' in line:
                    self.basic["modem_tech_pref"] = line.split(",")[-1].strip()
                    print("modem_tech_pref => ", self.basic["modem_tech_pref"]) #AUTO, LTE, NR5G
                    return
        
        if 'AT+QNWPREFCFG="nr5g_band"' in decoded:
            for L in lines:
                if L.startswith('+QNWPREFCFG: "nr5g_band"'):
                    # everything after the comma is the colon-separated list
                    self.basic["setting_nr5g_bands"] = L.split(",", 1)[1].strip()
                    self.basic["policy_nr5g_bands"] = "1:28:41:77:78:79" # default for 500u, from testing
                    print("setting_nr5g_bands =>", self.basic["setting_nr5g_bands"])
                    return

        if 'AT+QNWPREFCFG="lte_band"' in decoded:
            for L in lines:
                if L.startswith('+QNWPREFCFG: "lte_band"'):
                    self.basic["setting_lte_bands"] = L.split(",", 1)[1].strip()
                    self.basic["policy_lte_bands"] = "1:2:3:5:7:8:20:28:34:38:39:40:41" # default for 500u, from testing
                    print("setting_lte_bands =>", self.basic["setting_lte_bands"])
                    return

        print("Unexpected data in handle_bands_and_roam_pref: ", decoded)

        return

    def handle_gtact(self, response: bytes):
        """
        Handle AT+GTACT? response for FM350-GL (MediaTek) modem.
        Expected format:
        AT+GTACT?
        +GTACT: 20
        OR with bands:
        +GTACT: 20,6,3,1,2,4,5,8,101,102,103,...,5001,5002,5003,...
        OK

        Per AT command spec:
        - RAT: 1=UMTS, 2=LTE, 4=LTE/UMTS, 10=Auto, 14=NR-RAN, 20=NR-RAN/WCDMA/LTE (full auto)
        - PreferredAct: 1-8 (optional preferred access technologies)

        Band format:
        - LTE bands: 101-171 (100 + band number, e.g., 101=BAND_1, 103=BAND_3)
        - NR bands: 501-50512 ("50" + band number as string, e.g., 501=BAND_1, 5078=BAND_78)
        """
        decoded = response.decode("utf-8", errors="ignore")
        lines = decoded.split("\r\n")

        for line in lines:
            if line.startswith('+GTACT:'):
                try:
                    # Extract parameters: rat, [bands...]
                    params = line.split(":", 1)[1].strip().split(",")
                    gtact_value = params[0].strip()

                    # Map MediaTek GTACT values to standard tech_pref values
                    gtact_map = {
                        "2": "LTE",      # LTE only (4G)
                        "14": "NR5G",    # NR-RAN only (5G)
                        "10": "AUTO",    # Automatic (simple)
                        "17": "AUTO",    # NR-RAN/LTE (5G+4G auto)
                        "20": "AUTO",    # NR-RAN/WCDMA/LTE (full auto)
                    }
                    tech_pref = gtact_map.get(gtact_value, "AUTO")
                    self.basic["modem_tech_pref"] = tech_pref
                    print(f"modem_tech_pref => {tech_pref} (GTACT={gtact_value})")

                    # Parse band information if present
                    # Format: +GTACT: <rat>,<PreferredAct1>,<PreferredAct2>,<band_1>,<band_2>,...
                    # PreferredAct values: 2=WCDMA, 3=LTE, 6=NR-RAN
                    # UMTS bands: 1-10, LTE bands: 101-171, NR bands: 501-50512
                    if len(params) > 1:
                        preferred_acts = []
                        umts_bands = []
                        lte_bands = []
                        nr5g_bands = []

                        for band_str in params[1:]:
                            try:
                                band_num = int(band_str.strip())
                                # PreferredAct values (1-8) come first, max 2
                                if band_num <= 8 and len(preferred_acts) < 2 and not umts_bands and not lte_bands and not nr5g_bands:
                                    preferred_acts.append(band_num)
                                # UMTS bands: 1-10
                                elif 1 <= band_num <= 10:
                                    umts_bands.append(band_num)
                                # LTE bands are 101-171 (100 + band number)
                                elif 101 <= band_num <= 171:
                                    lte_bands.append(band_num - 100)
                                # NR bands are 501-50512 (string format: "50" + band_number)
                                elif 501 <= band_num <= 50512:
                                    band_str_num = str(band_num)
                                    if band_str_num.startswith("50"):
                                        actual_band = int(band_str_num[2:])
                                        nr5g_bands.append(actual_band)
                            except ValueError:
                                continue

                        self.basic["gtact_preferred_acts"] = preferred_acts
                        self.basic["gtact_umts_bands"] = umts_bands
                        print(f"gtact_preferred_acts => {preferred_acts}, umts_bands => {umts_bands}")

                        if lte_bands:
                            self.basic["setting_lte_bands"] = ":".join(str(b) for b in sorted(lte_bands))
                            print(f"setting_lte_bands => {self.basic['setting_lte_bands']}")

                        if nr5g_bands:
                            self.basic["setting_nr5g_bands"] = ":".join(str(b) for b in sorted(nr5g_bands))
                            print(f"setting_nr5g_bands => {self.basic['setting_nr5g_bands']}")

                except Exception as e:
                    print(f"Error parsing GTACT response: {e}")
                break

    def handle_gtccinfo(self, response: bytes):
        """
        Parse AT+GTCCINFO? response for FM350-GL serving cell information.
        Format varies by RAT:
        LTE service cell:
          <IsServiceCell>,<rat>,<mcc>,<mnc>,<tac>,<cellid>,<earfcn>,<pci>,<band>,<bw>,<rssnr>,<rxlev>,<rsrp>,<rsrq>
        NR service cell:
          <IsServiceCell>,<rat>,<mcc>,<mnc>,<tac>,<cellid>,<narfcn>,<pci>,<band>,<bw>,<ss_sinr>,<rxlev>,<ss_rsrp>,<ss_rsrq>
        rat: 4=LTE, 9=NR
        """
        if b'ERROR' in response:
            return
        decoded = response.decode("utf-8", errors="ignore")
        lines = decoded.split("\r\n")

        for line in lines:
            line = line.strip()
            if not line or line.startswith("+GTCCINFO:") or line == "OK":
                continue
            # Look for service cell lines (start with "1,")
            if not line.startswith("1,"):
                continue
            try:
                parts = line.split(",")
                if len(parts) < 14:
                    continue
                rat_num = int(parts[1])
                mcc = parts[2]
                mnc = parts[3]
                tac = parts[4]
                cellid = parts[5]
                arfcn = parts[6]
                pci = parts[7]
                band_raw = int(parts[8]) if parts[8] else 0
                bw = parts[9]
                rssnr_or_sinr = parts[10]
                rxlev = parts[11]
                rsrp_raw = parts[12]
                rsrq_raw = parts[13]

                # Decode band number from GTACT format
                rat_map = {0: "Unknown", 2: "WCDMA", 4: "LTE", 9: "NR5G"}
                rat = rat_map.get(rat_num, f"RAT{rat_num}")

                if band_raw == 0:
                    # Band not provided, derive from RAT and ARFCN
                    band = 0
                    band_str = rat
                elif 101 <= band_raw <= 171:
                    band = band_raw - 100
                    band_str = f"LTE B{band}"
                elif 501 <= band_raw <= 50512:
                    band_str_num = str(band_raw)
                    if band_str_num.startswith("50"):
                        band = int(band_str_num[2:])
                        band_str = f"NR N{band}"
                    else:
                        band = band_raw
                        band_str = f"B{band}"
                else:
                    band = band_raw
                    band_str = f"B{band}"

                duplex = "TDD" if band in [38,39,40,41,42,43,46,48,77,78,79] else "FDD"

                self.basic["serving_cellid"] = cellid
                self.basic["serving_rat"] = rat
                self.basic["serving_pci"] = pci
                self.basic["serving_tac"] = tac
                self.basic["serving_band"] = band
                self.basic["cell_band"] = band_str
                self.basic["modem_serving_info"] = f"{rat} {duplex} {band_str}, Cell ID = {cellid}, TAC = {tac}, PCI = {pci}"

                # Parse signal quality for serving_quality display
                try:
                    sinr_val = int(rssnr_or_sinr) if rssnr_or_sinr and rssnr_or_sinr != "255" else None
                    rsrp_val = int(rsrp_raw) if rsrp_raw and rsrp_raw != "255" else None
                    rsrq_val = int(rsrq_raw) if rsrq_raw and rsrq_raw != "255" else None

                    if rat_num == 9:  # NR
                        sinr_dbm = f"{-23 + sinr_val * 0.5:.1f} dB" if sinr_val is not None else "-"
                        rsrp_dbm = f"{-156 + rsrp_val} dBm" if rsrp_val is not None else "-"
                        rsrq_dbm = f"{-43 + rsrq_val * 0.5:.1f} dB" if rsrq_val is not None else "-"
                    else:  # LTE
                        sinr_dbm = f"{sinr_val * 0.5 - 50:.1f} dB" if sinr_val is not None and sinr_val != 255 else "-"
                        rsrp_dbm = f"{-140 + rsrp_val} dBm" if rsrp_val is not None else "-"
                        rsrq_dbm = f"{-19.5 + rsrq_val * 0.5:.1f} dB" if rsrq_val is not None else "-"

                    self.basic["modem_serving_quality"] = f"{sinr_dbm}, {rsrq_dbm}, {rsrp_dbm}"
                except (ValueError, TypeError):
                    pass

                print(f"GTCCINFO → {rat} {duplex} {band_str}, CellID={cellid}, TAC={tac}, PCI={pci}")
                break  # Only process first service cell
            except Exception as e:
                print(f"Error parsing GTCCINFO line '{line}': {e}")
                continue

    def handle_gtcainfo(self, response: bytes):
        """
        Parse AT+GTCAINFO? response for FM350-GL carrier aggregation info.
        Format:
        PCC: <band>,<pci>,<earfcn>,<dl_bw>,<dl_mimo>,<ul_mimo>,<dl_mod>,<ul_mod>,<rsrp>
        SCC1: <scell_state>,<ul_configured>,<band>,<pci>,<earfcn>,<dl_bw>,<ul_bw>,<dl_mimo>,<ul_mimo>,<dl_mod>,<ul_mod>,<rsrp>
        """
        if b'ERROR' in response:
            return
        decoded = response.decode("utf-8", errors="ignore")
        lines = decoded.split("\r\n")

        components = []
        for line in lines:
            line = line.strip()
            if not line or line == "OK" or line.startswith("+GTCAINFO:") or line.startswith("AT+GTCAINFO"):
                continue
            try:
                if line.startswith("PCC:"):
                    parts = line[4:].strip().split(",")
                    band_raw = int(parts[0])
                    pci = parts[1]
                    arfcn = parts[2]
                    dl_bw = parts[3]

                    if 101 <= band_raw <= 171:
                        band_str = f"LTE B{band_raw - 100}"
                    elif str(band_raw).startswith("50") and band_raw >= 501:
                        band_str = f"NR N{str(band_raw)[2:]}"
                    else:
                        band_str = f"B{band_raw}"

                    components.append(f"PCC: {band_str} (PCI={pci}, ARFCN={arfcn}, BW={dl_bw})")
                elif line.startswith("SCC"):
                    label = line.split(":")[0]
                    parts = line.split(":")[1].strip().split(",")
                    scell_state = int(parts[0])
                    band_raw = int(parts[2])

                    if 101 <= band_raw <= 171:
                        band_str = f"LTE B{band_raw - 100}"
                    elif str(band_raw).startswith("50") and band_raw >= 501:
                        band_str = f"NR N{str(band_raw)[2:]}"
                    else:
                        band_str = f"B{band_raw}"

                    state_str = "Active" if scell_state == 2 else "Inactive"
                    components.append(f"{label}: {band_str} ({state_str})")
            except Exception as e:
                print(f"Error parsing GTCAINFO line '{line}': {e}")
                continue

        if components:
            self.basic["cell_carrier_info"] = " | ".join(components)
            # Extract PCC band as cell_band for dashboard display
            for line in lines:
                line = line.strip()
                if line.startswith("PCC:"):
                    try:
                        parts = line[4:].strip().split(",")
                        band_raw = int(parts[0])
                        if 101 <= band_raw <= 171:
                            self.basic["cell_band"] = f"LTE B{band_raw - 100}"
                        elif str(band_raw).startswith("50") and band_raw >= 501:
                            self.basic["cell_band"] = f"NR N{str(band_raw)[2:]}"
                        else:
                            self.basic["cell_band"] = f"B{band_raw}"
                    except Exception:
                        pass
                    break
            print(f"GTCAINFO → {self.basic['cell_carrier_info']}")

    def handle_ccid(self, response: bytes):
        lines = response.split(b"\r\n")
        found = False
        for line in lines:
            if line.startswith(b"+CCID:"):
                try:
                    self.basic["sim_ccid"] = line.split(b":", 1)[1].strip().decode("utf-8").split(" ")[-1]
                    found = True
                except:
                    self.basic["sim_ccid"] = ""
                print(f"Parsed CCID: {self.basic['sim_ccid']}")
                break

        if found and self.basic["sim_ccid"]:
            self.cmd_mark_success("AT+CCID")
        else:
            self.cmd_mark_failure("AT+CCID")

    def handle_cimi(self, response: bytes):
        """
        Parse AT+CIMI response:
        - On success, set self.basic['sim_cimi'] = '<IMSI>'
        - On +CME ERROR: <code>, set self.basic['sim_cimi_error'] = '<code>: <desc>'
        """
        imsi = None
        error_code = None

        for line in response.split(b"\r\n"):
            if not line or line.startswith(b"AT+CIMI"):
                continue

            # 1) Direct numeric IMSI
            stripped = line.strip()
            if stripped.isdigit():
                imsi = stripped.decode("utf-8")
                break

            # 2) Echo "+CIMI: 123456789012345"
            if line.startswith(b"+CIMI:"):
                parts = line.split(b":", 1)[1].strip()
                if parts.isdigit():
                    imsi = parts.decode("utf-8")
                    break

            # 3) CME error: +CME ERROR: <n>
            m = re.match(br"\+CME ERROR:\s*(\d+)", line)
            if m:
                error_code = int(m.group(1))
                break

        if imsi:
            # Got a valid IMSI
            self.basic["sim_cimi"] = imsi
            self.cmd_mark_success("AT+CIMI")
            print(f"Parsed CIMI: {imsi}")
        elif error_code is not None:
            # Got an error instead
            desc = CME_ERRORS.get(error_code, f"Unknown error")
            self.basic["sim_cimi_error"] = f"{error_code}: {desc}"
            self.cmd_mark_failure("AT+CIMI")
            print(f"Parsed CIMI error → {error_code}: {desc}")
        else:
            # Nothing useful found
            self.basic["sim_cimi"] = ""
            self.cmd_mark_failure("AT+CIMI")
            print("Warning: Unable to parse CIMI from response.")

    def handle_qnwinfo(self, response: bytes):
        """
        Parse QNWINFO response for network information.
        Example response:
                b'+QNWINFO: "TDD NR5G","46011","NR5G BAND 78",627264\r\nOK\r\n'
        This method extracts:
            - network_mode: "TDD NR5G"
            - mccmnc:       "46011"
            - band:         "NR5G BAND 78"
            - cell_id:      "627264"
        """
        # Check for ERROR response (modem doesn't support this command)
        if b'ERROR' in response:
            self.cmd_mark_failure('AT+QNWINFO')
            return

        lines = response.split(b"\r\n")
        for line in lines:
            if line.startswith(b"+QNWINFO:"):
                # Remove '+QNWINFO:' prefix
                payload = line.split(b":", 1)[1].strip()
                # Split by comma, strip quotes, and decode to strings
                parts = [part.strip().strip(b'"').decode("utf-8", errors="ignore") for part in payload.split(b",") ]
                if len(parts) >= 4:
                    network_mode = parts[0]  # e.g. "TDD NR5G"
                    mccmnc       = parts[1]  # e.g. "46011"
                    band         = parts[2]  # e.g. "NR5G BAND 78"
                    cell_id      = parts[3]  # e.g. "627264"

                    nm_upper = network_mode.upper()
                    if "NR" in nm_upper or "5G" in nm_upper:
                        tech = "5G"
                    elif "LTE" in nm_upper or "4G" in nm_upper:
                        tech = "4G"
                    elif "WCDMA" in nm_upper or "HSPA" in nm_upper or "3G" in nm_upper:
                        tech = "3G"
                    else:
                        tech = "Other"

                    # Look up MCC/MNC details and get ISP information
                    if self.is_em05() or self.is_fm350():
                        self.basic["isp_details"] = lookup_isp_details(mccmnc)
                    
                    network_info = f"{network_mode} {band}".strip()
                    parts = network_info.split(" ")
                    #remove duplicated parts
                    parts = list(dict.fromkeys(parts))
                    network_info = " ".join(parts)

                    # Store in basic info dictionary
                    self.basic["cell_network_mode"] = network_mode
                    self.basic["cell_mccmnc"]        = mccmnc
                    self.basic["cell_band"]          = band
                    self.basic["cell_id"]       = cell_id
                    self.basic["cell_tech"]     = tech
                    self.basic["network_info"]     = network_info
                    print(
                            f"Parsed QNWINFO → mode={network_mode}, "
                            f"mccmnc={mccmnc}, band={band}, cell_id={cell_id}, cell_tech={tech}"
                    )
                else:
                    print("Warning: Unexpected +QNWINFO format:", line)
                break

    def handle_qcfg_usbnet(self, response: bytes):
        """
        Parse QCFG USBNET response for USB‐to‐modem mode.
        Example response:
            b'+QCFG: "usbnet",0\r\nOK\r\n'
        This method extracts:
            - mode_code:   integer 0/1/2
            - mode:        "qmi" / "ecm" / "mbim"
        """
        # Check for ERROR response (modem doesn't support this command)
        if b'ERROR' in response:
            self.cmd_mark_failure('AT+QCFG="usbnet"')
            return

        # find the +QCFG: "usbnet" line
        lines = response.split(b"\r\n")
        for line in lines:
            if not line.startswith(b'+QCFG:'):
                continue

            # strip prefix and split fields
            payload = line.split(b':', 1)[1].strip()
            parts = [
                p.strip().strip(b'"').decode('utf-8', errors='ignore')
                for p in payload.split(b',')
            ]

            # expect ['usbnet', '<code>']
            if len(parts) >= 2 and parts[0] == 'usbnet':
                code = int(parts[1])
                if self.is_500u():
                    mode_map = {
                        1: 'ECM',    # ECM mode
                        3: 'RNDIS',  # RNDIS mode
                        5: 'NCM'     # NCM mode
                    }
                elif self.is_520():
                    mode_map = {
                        0: 'QMI',    # QMI mode
                        1: 'ECM',    # ECM mode
                        2: 'MBIM'    # MBIM mode
                    }
                elif self.is_em05():
                    mode_map = {
                        0: 'RmNet',
                        1: 'ECM',
                        2: 'MBIM'
                    }
                else:
                    mode_map = {}

                mode = mode_map.get(code, f'unknown({code})')

                # store results
                self.basic['usbnet_mode_code'] = code
                self.basic['usbnet_mode'] = mode

                print(f'Parsed QCFG usbnet → mode_code={code}, mode={mode}')
            else:
                print('Warning: Unexpected +QCFG usbnet format:', line)
            break

    def convert_to_int(self, value: str) -> int:
        try:
            return int(value)
        except ValueError:
            return -9999

    def handle_qeng_servingcell(self, response: bytes):
        """
        Parse QENG="servingcell" response for current serving‐cell info.
        Example:
            b'+QENG: "servingcell","NOCONN","NR5G-SA","TDD",460,11,592E2E004,474,590903,627264,78,12,-72,-10,13,1,55\r\n'
            b'+QENG: "servingcell","NOCONN","LTE","FDD",460,11,592E2E004,474,590903,627264,78,12,-72,-10,13,1,55\r\n'
        Extracts:
        - state           e.g. "NOCONN"
        - rat             e.g. "NR5G-SA" or "LTE"
        - duplex          e.g. "TDD" or "FDD"
        - mcc, mnc        e.g. "460", "11"
        - cellid          e.g. "592E2E004"
        - pci             Physical Cell ID (int)
        - tac             Tracking Area Code (int)
        - arfcn           Absolute RF channel number (int)
        - band            Band number (int)
        - dl_bw_ind       DL bandwidth indicator (int)
        - rsrp, rsrq      (int) in dBm/dB
        - sinr            (int) in dB
        - scs             subcarrier spacing index (int)
        - srxlev          cell‐selection RX‐level (int)
        - frequency_mhz   computed from arfcn
        - bw_mhz          bandwidth in MHz
        """
        # Check for ERROR response (modem doesn't support this command)
        if b'ERROR' in response:
            self.cmd_mark_failure('AT+QENG="servingcell"')
            return

        # mapping DL‐bandwidth indicator → MHz
        bw_map = {0:5,1:10,2:15,3:20,4:25,5:30,6:40,7:50,8:60,9:70,10:80,11:90,12:100,13:200}

        for line in response.split(b"\r\n"):
            if not line.startswith(b'+QENG: "servingcell"'):
                continue
            try:
                # split fields
                parts = [p.strip().strip(b'"').decode("utf-8", errors="ignore")
                        for p in line.split(b",")]

                # unpack
                # parts[0] = '+QENG: "servingcell"' → state in parts[1]
                state    = parts[1]
                rat      = parts[2]
                duplex   = parts[3]
                mcc      = parts[4]
                mnc      = parts[5]
                cellid   = parts[6]
                pci      = self.convert_to_int(parts[7])
                tac      = self.convert_to_int(parts[8])
                arfcn    = self.convert_to_int(parts[9])
                band     = self.convert_to_int(parts[10])
                dl_bw_ind= self.convert_to_int(parts[11])
                rsrp     = self.convert_to_int(parts[12])
                rsrq     = self.convert_to_int(parts[13])
                sinr     = self.convert_to_int(parts[14])
                scs      = self.convert_to_int(parts[15])
                srxlev   = self.convert_to_int(parts[16])

                # compute BW
                bw_mhz = bw_map.get(dl_bw_ind, None)

                # compute frequency (only NR here; LTE would need EARFCN→MHz formula)
                if "NR5G" in rat:
                    if arfcn < 600_000:
                        freq_mhz = arfcn * 0.005
                    elif arfcn < 2_016_667:
                        freq_mhz = 3000 + (arfcn - 600_000) * 0.015
                    else:
                        freq_mhz = 24250 + (arfcn - 2_016_667) * 0.06
                else:
                    # fallback: treat ARFCN directly as MHz
                    freq_mhz = float(arfcn)

                # store in your data structure
                self.basic.update({
                    "serving_state":       state,
                    "serving_rat":         rat,
                    "serving_duplex":      duplex,
                    "serving_mcc":         mcc,
                    "serving_mnc":         mnc,
                    "serving_cellid":      cellid,
                    "serving_pci":         pci,
                    "serving_tac":         tac,
                    "serving_arfcn":       arfcn,
                    "serving_band":        band,
                    "serving_dl_bw_ind":   dl_bw_ind,
                    "serving_bw_mhz":      f"{bw_mhz} MHz" if bw_mhz is not None else None,
                    "serving_frequency":   f"{freq_mhz:.3f} MHz",
                    "serving_rsrp":        f"{rsrp} dBm",
                    "serving_rsrq":        f"{rsrq} dB",
                    "serving_sinr":        f"{sinr} dB",
                    "serving_scs":         scs,
                    "serving_srxlev":      srxlev,
                })
                self.basic["modem_serving_info"] = f"{rat}  {duplex}, Cell ID = {cellid}, TAC = {tac}, PCI = {pci}"
                self.basic["modem_serving_quality"] = (
                    f"{fmt_num(sinr, 'dB')}, "
                    f"{fmt_num(rsrq, 'dB')}, "
                    f"{fmt_num(rsrp, 'dBm')}, "
                    f"{fmt_num(srxlev, 'dB')}"
                )

                print(
                    f"ServingCell → state={state}, RAT={rat}, {duplex}, "
                    f"MCC/MNC={mcc}/{mnc}, PCI={pci}, CellID={cellid}, TAC={tac}, "
                    f"ARFCN={arfcn}, Band={band}, BW={bw_mhz} MHz, "
                    f"Freq={freq_mhz:.3f} MHz, RSRP={rsrp}dBm, "
                    f"RSRQ={rsrq}dB, SINR={sinr}dB, SCS={scs}, SRXLEV={srxlev}"
                )
            except Exception as e:
                print(f"Error parsing QENG serving cell: {e}")
            break

    def handle_qcainfo(self, response: bytes):
        """
        Parse QCAINFO response for carrier aggregation info
        and compute the actual downlink frequency in MHz.
        Example response:
            b'+QCAINFO: "PCC",627264,12,"NR5G BAND 78",593\r\nOK\r\n'
            b'+QCAINFO: "PCC",1750,100,"LTE BAND 3",1,8,-74,-11,-43,3\r\n\r\nOK\r\n'
        This method extracts:
            - component:         "PCC"
            - arfcn:             627264 (int)
            - bw_indicator:      12
            - band:              "NR5G BAND 78"
            - pci:               593
            - derives bw_mhz:    100 MHz (from indicator)
            - tech:              "5G"
            - frequency_mhz:     computed from ARFCN (≈3408.96 MHz)
            - rsrp:              -74 dBm #MAYBE LTE
            - rsrq:              -11 dB  #MAYBE LTE
            - sinr:              -43 dB  #MAYBE LTE
            - cqi:               3       #MAYBE LTE
        """
        # Check for ERROR response (modem doesn't support this command)
        if b'ERROR' in response:
            self.cmd_mark_failure('AT+QCAINFO')
            return

        for line in response.split(b"\r\n"):
            if not line.startswith(b"+QCAINFO:"):
                continue

            # parse fields
            payload = line.split(b":", 1)[1].strip()
            parts = [p.strip().strip(b'"').decode('utf-8', errors='ignore')
                    for p in payload.split(b",")]
            if len(parts) < 5:
                print("Warning: Unexpected +QCAINFO format:", line)
                break

            component, arfcn_str, bw_ind_str, band, pci = parts[:5]
            arfcn = int(arfcn_str)
            bw_ind = int(bw_ind_str)

            # -- default bandwidth string (in case no mapping is found) --
            bw_str = f"idx {bw_ind}"

            # -- detect tech and compute freq --
            band_u = band.upper()
            if band_u.startswith("NR") or "5G" in band_u:
                tech = "5G"
                nr5g_map = {
                    # FDD Bands: {dl: (f_low, n_offs), ul: (f_low, n_offs), duplex: "FDD"}
                    "n1":  {"dl": (2110, 422000), "ul": (1920, 384000), "duplex": "FDD"},  # 2100 MHz
                    "n2":  {"dl": (1930, 386000), "ul": (1850, 370000), "duplex": "FDD"},  # 1900 MHz PCS
                    "n3":  {"dl": (1805, 361000), "ul": (1710, 342000), "duplex": "FDD"},  # 1800 MHz
                    "n5":  {"dl": (869, 173800),  "ul": (824, 164800),  "duplex": "FDD"},  # 850 MHz
                    "n7":  {"dl": (2620, 524000), "ul": (2500, 500000), "duplex": "FDD"},  # 2600 MHz
                    "n8":  {"dl": (925, 185000),  "ul": (880, 176000),  "duplex": "FDD"},  # 900 MHz
                    "n12": {"dl": (729, 145800),  "ul": (699, 139800),  "duplex": "FDD"},  # 700 MHz Lower
                    "n20": {"dl": (791, 158200),  "ul": (832, 166400),  "duplex": "FDD"},  # 800 MHz
                    "n25": {"dl": (1930, 386000), "ul": (1850, 370000), "duplex": "FDD"},  # 1900 MHz Extended
                    "n28": {"dl": (758, 151600),  "ul": (703, 140600),  "duplex": "FDD"},  # 700 MHz APT
                    "n66": {"dl": (2110, 422000), "ul": (1710, 342000), "duplex": "FDD"},  # AWS-3
                    "n71": {"dl": (617, 123400),  "ul": (663, 132600),  "duplex": "FDD"},  # 600 MHz

                    # TDD Bands: {dl: (f_low, n_offs), duplex: "TDD"} (ul same as dl for TDD)
                    "n38": {"dl": (2570, 514000), "duplex": "TDD"},  # 2600 MHz
                    "n40": {"dl": (2300, 460000), "duplex": "TDD"},  # 2300 MHz
                    "n41": {"dl": (2496, 499200), "duplex": "TDD"},  # 2500 MHz
                    "n77": {"dl": (3300, 620000), "duplex": "TDD"},  # 3.7 GHz
                    "n78": {"dl": (3300, 620000), "duplex": "TDD"},  # 3.5 GHz
                    "n79": {"dl": (4400, 693334), "duplex": "TDD"},  # 4.7 GHz
                    "n257": {"dl": (26500, 2054166), "duplex": "TDD"},  # 28 GHz (mmWave)
                    "n258": {"dl": (24250, 2016666), "duplex": "TDD"},  # 26 GHz (mmWave)
                    "n260": {"dl": (37000, 2229166), "duplex": "TDD"},  # 39 GHz (mmWave)
                    "n261": {"dl": (27500, 2070833), "duplex": "TDD"},  # 28 GHz (mmWave)
                }
                bw_map = {0:5,1:10,2:15,3:20,4:25,5:30,6:40,7:50,8:60,9:70,10:80,11:90,12:100,13:200}
                
                bw_mhz = bw_map.get(bw_ind, None)
                bw_str = f"{bw_mhz} MHz" if bw_mhz else f"idx {bw_ind}"
    
                # NR-ARFCN -> MHz
                if arfcn < 600_000:
                    freq_mhz = arfcn * 0.005
                elif arfcn < 2_016_667:
                    freq_mhz = 3000 + (arfcn - 600_000) * 0.015
                else:
                    freq_mhz = 24250 + (arfcn - 2_016_667) * 0.06

            elif "LTE" in band:
                tech = "4G"
                # LTE EARFCN -> MHz mapping table
                lte_map = {
                    # FDD Bands: (downlink f_low in MHz, n_offs for EARFCN)
                    1:  (2110,     0),      # 2100 MHz
                    2:  (1930,   600),      # 1900 MHz PCS
                    3:  (1805,  1200),      # 1800 MHz
                    4:  (2110,  1950),      # AWS-1
                    5:  (869,   2400),      # 850 MHz
                    7:  (2620,  2750),      # 2600 MHz
                    8:  (925,   3450),      # 900 MHz
                    12: (729,   5010),      # 700 MHz Lower A/B/C
                    13: (746,   5180),      # 700 MHz Upper C
                    14: (758,   5280),      # 700 MHz Upper D
                    17: (734,   5730),      # 700 MHz Lower B/C
                    20: (791,   6150),      # 800 MHz
                    25: (1930,  8040),      # 1900 MHz Extended PCS
                    26: (859,   8690),      # 850 MHz Extended
                    28: (758,   9210),      # 700 MHz APT
                    30: (2350,  9770),      # 2300 MHz WCS
                    66: (2110, 131980),     # AWS-3
                    71: (617,  68930),      # 600 MHz

                    # TDD Bands: (center f_low in MHz, n_offs for EARFCN)
                    33: (1900,  36000),     # 1900 MHz
                    34: (2010,  36200),     # 2000 MHz
                    38: (2570,  37750),     # 2600 MHz
                    39: (1880,  38250),     # 1900 MHz
                    40: (2300,  41590),     # 2300 MHz
                    41: (2496,  39650),     # 2500 MHz
                    42: (3400,  41590),     # 3500 MHz
                    43: (3600,  43590),     # 3700 MHz
                    46: (5150,  46790),     # 5200 MHz (LAA)
                }
                bw_map = {6: 1.4, 15: 3, 25: 5, 50: 10, 75: 15, 100: 20}
                bw_mhz = bw_map.get(int(bw_ind), None)
                bw_str = f"{bw_mhz} MHz" if bw_mhz else f"idx {bw_ind}"
                m = re.search(r"LTE\s*BAND\s*(\d+)", band)
                if m and (band_num := int(m.group(1))) in lte_map:
                    f_low, n_offs = lte_map[band_num]
                    freq_mhz = f_low + 0.1 * (arfcn - n_offs)
                else:
                    # fallback if unknown band
                    freq_mhz = float(arfcn)

            else:
                tech = "Unknown"
                freq_mhz = float(arfcn)

            freq_str = f"{freq_mhz:.0f} MHz"

            # guess technology
            tech = "5G" if "NR" in band or "5G" in band else "Other"

            # store results
            self.basic["ca_component"]           = component
            self.basic["ca_arfcn"]               = arfcn_str
            self.basic["ca_bandwidth_indicator"] = bw_ind_str
            self.basic["ca_bandwidth_mhz"]       = bw_str
            self.basic["ca_band"]                = band
            self.basic["ca_pci"]                 = pci
            self.basic["ca_tech"]                = tech
            self.basic["ca_frequency_mhz"]       = freq_str

            if component == "PCC":
                comp_str = "Primary CC"
            else:
                comp_str = component

            self.basic["cell_carrier_info"] = comp_str + ", " + freq_str + " @ " + bw_str + " BW"

            print(
                f"Parsed QCAINFO → component={component}, "
                f"ARFCN={arfcn}, bw_ind={bw_ind} ({bw_str}), "
                f"band={band}, PCI={pci}, tech={tech}, "
                f"frequency={freq_str}"
            )
            break

    def handle_qrsrp(self, response: bytes):
        """
        Parse QRSRP response for RSRP measurements.
        Example response:
            b'+QRSRP: -71,-63,-93,-70,NR5G\r\nOK\r\n'
            b'AT+QRSRP\r\r\n+QRSRP: -99,-105,-108,-106,NR5G\r\n\r\nOK\r\n'
        The first four values are RSRP (in dBm), the last is the network type.
        """
        # Accept any response containing "+QRSRP:"
        if b"+QRSRP:" not in response:
            print("Warning: Unexpected QRSRP format:", response)
            return

        # Initialize list and split into lines
        rsrp_values = []
        lines = response.split(b"\r\n")
        for line in lines:
            if b"+QRSRP:" in line:
                # Remove '+QRSRP:' prefix
                payload = line.split(b":", 1)[1].strip()
                # Split by comma: [b"-71", b"-63", b"-93", b"-70", b"NR5G"]
                parts = payload.split(b",")
                for item in parts[:-1]:  # ignore the last 'NR5G'
                    try:
                        # Convert to integer dBm
                        rsrp_values.append(int(item))
                    except ValueError:
                        pass
                self.basic["rsrp_list"] = rsrp_values
                break

        if not rsrp_values:
            # No valid RSRP parsed; return
            return

        best_rsrp = max(rsrp_values)  # e.g. -63

        # Define min/max for mapping
        rsrp_min = -110
        rsrp_max = -60

        # Linear interpolation to percentage
        percent = (best_rsrp - rsrp_min) / (rsrp_max - rsrp_min) * 100

        # Clamp to [0, 100]
        if percent < 0:
            percent = 0
        elif percent > 100:
            percent = 100

        signal_pct = int(percent)

        # Store results
        self.basic["cell_rsrp_list"] = rsrp_values
        self.basic["cell_signal_percent_qrsrp"] = signal_pct

        print(f"Parsed QRSRP → beams={rsrp_values}, best={best_rsrp} dBm → {signal_pct}%")

    def handle_qrsrq(self, response: bytes):
        """
        Parse QRSRQ response for RSRQ measurements and compute a 0–100% quality metric.
        Example response:
                b'+QRSRQ: -11,-10,-11,-10,NR5G\r\nOK\r\n'
        The first four values are RSRQ (in dB), the last is the network type.
        We map the best (highest) RSRQ value from [–20 dB..–3 dB] into [0..100%].
        """
        lines = response.split(b"\r\n")
        rsrq_values = []
        for line in lines:
            if line.startswith(b"+QRSRQ:"):
                # Remove '+QRSRQ:' prefix
                payload = line.split(b":", 1)[1].strip()
                # Split by comma: [b"-11", b"-10", b"-11", b"-10", b"NR5G"]
                parts = payload.split(b",")
                for item in parts[:-1]:  # ignore the last 'NR5G'
                    try:
                        rsrq_values.append(int(item))
                    except ValueError:
                        pass
                break

        # Store the raw RSRQ list
        self.basic["cell_rsrq_list"] = rsrq_values


        if not rsrq_values:
            # No valid RSRQ parsed; nothing more to do
            return

        best_rsrq = max(rsrq_values)  # e.g. -10

        # Define min/max for mapping (–20 dB => 0%, –3 dB => 100%)
        rsrq_min = -20
        rsrq_max = -3

        # Linear interpolation to percentage
        percent = (best_rsrq - rsrq_min) / (rsrq_max - rsrq_min) * 100

        # Clamp to [0, 100]
        if percent < 0:
            percent = 0
        elif percent > 100:
            percent = 100

        quality_pct = int(percent)
        self.basic["cell_signal_percent_qrsrq"] = quality_pct

        print(f"Parsed QRSRQ → {rsrq_values}, best={best_rsrq} dB → {quality_pct}% quality")


    def handle_qsinr(self, response: bytes):
        """
        Parse QSINR response for SINR measurements and compute a 0–100% quality metric.
        Example response:
                b'+QSINR: 18,27,-20,-20,NR5G\r\nOK\r\n'
        The first four values are SINR (in dB), the last is the network type.
        We map the best SINR from [–10 dB..30 dB] into [0..100%].
        """
        lines = response.split(b"\r\n")
        sinr_values = []
        for line in lines:
            if line.startswith(b"+QSINR:"):
                # Remove '+QSINR:' prefix
                payload = line.split(b":", 1)[1].strip()
                # Split by comma: [b"18", b"27", b"-20", b"-20", b"NR5G"]
                parts = payload.split(b",")
                for item in parts[:-1]:  # ignore the last 'NR5G'
                    try:
                        sinr_values.append(int(item))
                    except ValueError:
                        pass
                break
        

        # Store the raw SINR list
        self.basic["cell_sinr_list"] = sinr_values
        print(f"Parsed QSINR → {sinr_values}")

        if not sinr_values:
            # No valid SINR parsed; nothing more to do
            return

        best_sinr = max(sinr_values)  # e.g. 27

        # Define min/max for mapping (–10 dB => 0%, 30 dB => 100%)
        sinr_min = -10
        sinr_max = 30

        # Linear interpolation to percentage
        percent = (best_sinr - sinr_min) / (sinr_max - sinr_min) * 100

        # Clamp to [0, 100]
        if percent < 0:
            percent = 0
        elif percent > 100:
            percent = 100

        sinr_pct = int(percent)
        self.basic["cell_signal_percent_sinr"] = sinr_pct

        print(f"Parsed QSINR → best={best_sinr} dB → {sinr_pct}% quality")


    def handle_qspn(self, response: bytes):
        lines = response.split(b"\r\n")
        for line in lines:
            if line.startswith(b"+QSPN:"):
                payload = line.split(b":", 1)[1].strip()
                parts = []
                raw_parts = payload.split(b",")
                idx = 0
                while idx < len(raw_parts):
                    part = raw_parts[idx].strip()
                    if part.startswith(b'"'):
                        text = part
                        while not text.endswith(b'"') and idx + 1 < len(raw_parts):
                            idx += 1
                            text += b"," + raw_parts[idx].strip()
                        parts.append(text.strip(b'"'))
                    else:
                        parts.append(part)
                    idx += 1

                if len(parts) >= 5:
                    def try_decode(s):
                        try:
                            if all(c in "0123456789ABCDEFabcdef" for c in s) and len(s) % 4 == 0:
                                return bytes.fromhex(s).decode("utf-16-be", errors="ignore")
                            return s
                        except Exception:
                            return s

                    long_name   = try_decode(parts[0].decode("utf-8", errors="ignore"))
                    short_name  = try_decode(parts[1].decode("utf-8", errors="ignore"))
                    native_name = try_decode(parts[2].decode("utf-8", errors="ignore"))

                    try:
                        act = int(parts[3])
                    except ValueError:
                        act = None

                    mccmnc = parts[4].decode("utf-8", errors="ignore").strip('"')

                    self.basic["cell_isp_long_name"]   = long_name
                    self.basic["cell_isp_short_name"]  = short_name
                    self.basic["cell_isp_native_name"] = native_name
                    self.basic["cell_isp_act"]         = act
                    self.basic["cell_mccmnc"]          = mccmnc
                    parts = [long_name, native_name, mccmnc]
                    # keep only truthy (non-empty) parts
                    filtered = [p for p in parts if p]
                    self.basic["isp_details"] = ", ".join(filtered)
                    print(f"Parsed QSPN → long='{long_name}', short='{short_name}', native='{native_name}', act={act}, mccmnc={mccmnc}, isp_details={self.basic['isp_details']}")
                else:
                    print("Warning: Unexpected +QSPN format:", line)
                break


    def handle_ati(self, response):
        print("ATI: check for modem model")
        try:
            result_arr = response.split(b"\r\n")
            model = result_arr[2].decode("utf-8")
            # Strip "Model: " prefix if present
            if model.startswith("Model: "):
                model = model[7:]
            firmware_version = result_arr[3].split(b" ")[1].decode("utf-8")

            self.basic["model"] = model
            self.basic["firmware_version"] = firmware_version

            # Cache model permanently - this will only happen once
            with _model_cache['lock']:
                _model_cache['model'] = model
                _model_cache['queried'] = True

            self.cmd_mark_success("ATI")
            print(f"ATI: model={model}, firmware_version={firmware_version}")
            print(f"is_em05={self.is_em05()}, is_500u={self.is_500u()}, is_520={self.is_520()}, is_500q={self.is_500q()}, is_fm350={self.is_fm350()}")
        except Exception as e:
            self.cmd_mark_failure("ATI")
            print(f"Error parsing ATI response: {e}")
                
    def handle_cnum(self, response):
        lines = response.split(b"\r\n")
        found = False
        for line in lines:
            if line.startswith(b"+CNUM:"):
                try:
                    self.basic["sim_number"] = line.split(b",")[1].decode("utf-8").strip('"')
                    found = True
                except:
                    self.basic["sim_number"] = ""
                break

        if found and self.basic["sim_number"]:
            self.cmd_mark_success("AT+CNUM")
        else:
            self.cmd_mark_failure("AT+CNUM")

    def handle_cgsn(self, response):
        """
        Parse AT+CGSN response for IMEI number.
        Example responses:
            Qualcomm: b'AT+CGSN\r\n354174400719337\r\nOK\r\n'  (IMEI at index 1)
            FM350-GL: b'AT+CGSN\r\n\r\n354174400719337\r\n\r\nOK\r\n'  (IMEI at index 2)
        """
        print(f"AT+CGSN IMEI Checking - Response length: {len(response)} bytes")
        result_arr = response.split(b"\r\n")
        print(f"AT+CGSN split into {len(result_arr)} parts")

        # Find the IMEI - it should be a 15-digit number
        imei = ""
        for i, line in enumerate(result_arr):
            line_str = line.decode("utf-8", errors="ignore").strip()
            # Debug: show what we're checking
            if line_str:  # Only print non-empty lines
                print(f"  Line {i}: '{line_str}' (len={len(line_str)}, isdigit={line_str.isdigit()})")
            # IMEI is 15 digits
            if line_str.isdigit() and len(line_str) == 15:
                imei = line_str
                print(f"  ✓ Found IMEI at line {i}")
                break

        if imei:
            self.basic["imei_num"] = imei
            self.cmd_mark_success("AT+CGSN")
            print(f"✓ Parsed IMEI: {imei}")
        else:
            self.cmd_mark_failure("AT+CGSN")
            print(f"✗ Warning: Could not parse IMEI from response")
            print(f"  Response bytes: {response!r}")
            print(f"  This might indicate the modem didn't return the IMEI - will retry next cycle")

    def handle_csq(self, response: bytes):
        """
        Parse +CSQ response for RSSI (signal strength) and BER (bit error rate).
        Example response:
                b'+CSQ: 21,99\r\nOK\r\n'
        Fields (comma-separated):
            1) rssi (0–31, 99 means “not known or not detectable”)
            2) ber  (0–7, 99 means “not known or not detectable”)
        We convert rssi → dBm using 3GPP TS 27.007: dBm = –113 + 2*rssi (if rssi != 99),
        then map to a percentage (0–100%). If rssi == 99, we treat it as unknown.
        """
        lines = response.split(b"\r\n")
        rssi_raw = None
        ber_raw = None

        # 1) Find the line that starts with "+CSQ:"
        for line in lines:
            if line.startswith(b"+CSQ:"):
                # Remove the "+CSQ:" prefix and strip whitespace
                payload = line.split(b":", 1)[1].strip()
                parts = payload.split(b",")
                # Parse RSSI
                try:
                    rssi_raw = int(parts[0])
                except (IndexError, ValueError):
                    rssi_raw = None

                # Parse BER (if present)
                if len(parts) > 1:
                    try:
                        ber_raw = int(parts[1])
                    except ValueError:
                        ber_raw = None
                break

        # 2) If we got a valid RSSI (0–31), convert to dBm; else leave as None
        rssi_dbm = None
        signal_pct = 0
        if rssi_raw is not None and 0 <= rssi_raw <= 31:
            # Per 3GPP TS 27.007: dBm = –113 + 2 * rssi
            rssi_dbm = -113 + 2 * rssi_raw

            # Map the dBm range [–113 … –51] to [0 … 100]%
            rssi_min = -113
            rssi_max = -51
            percent = (rssi_dbm - rssi_min) / (rssi_max - rssi_min) * 100

            # Clamp to [0, 100]
            if percent < 0:
                percent = 0
            elif percent > 100:
                percent = 100

            signal_pct = int(percent)

        # 3) Store parsed values in self.basic (or wherever you keep state)
        self.basic["cell_rssi_raw"] = rssi_raw
        self.basic["cell_rssi_dbm"] = rssi_dbm
        self.basic["cell_signal_percent_csq"] = signal_pct
        self.basic["cell_ber"] = ber_raw

        # 4) Print a summary for debugging
        if rssi_raw is None or rssi_raw == 99:
            print("Parsed CSQ → RSSI: unknown, BER:", ber_raw)
        else:
            print(f"Parsed CSQ → rssi_raw={rssi_raw} (≈{rssi_dbm} dBm) → {signal_pct}% signal, ber={ber_raw}")

    def handle_cesq(self, response: bytes):
        """
        Parse +CESQ response for FM350-GL (MediaTek) extended signal quality.
        Example response:
            b'+CESQ: 99,99,255,255,20,45,255,255,255\r\nOK\r\n'  (LTE)
            b'+CESQ: 99,99,255,255,255,255,46,68,58\r\nOK\r\n'  (5G NR)

        Format: +CESQ: <rxlev>,<ber>,<rscp>,<ecno>,<rsrq>,<rsrp>,<ss_rsrq>,<ss_rsrp>,<ss_sinr>

        LTE parameters (indices 4-5):
            rsrq [4]: 0-34 (-19.5 to -3 dB), 255=unknown
            rsrp [5]: 0-97 (-140 to -44 dBm), 255=unknown

        5G NR parameters (indices 6-8):
            ss_rsrq [6]: 0-126 (-43 to 20 dB), 255=unknown
            ss_rsrp [7]: 0-126 (-156 to -31 dBm), 255=unknown
            ss_sinr [8]: 0-127 (-23 to 40 dB), 255=unknown
        """
        lines = response.split(b"\r\n")

        for line in lines:
            if line.startswith(b"+CESQ:"):
                try:
                    # Extract comma-separated values
                    payload = line.split(b":", 1)[1].strip()
                    parts = [int(p.strip()) for p in payload.split(b",")]

                    if len(parts) < 9:
                        print(f"Warning: CESQ response has fewer than 9 parameters: {parts}")
                        return

                    # Extract parameters
                    rxlev = parts[0]
                    rsrq_lte = parts[4]  # LTE RSRQ
                    rsrp_lte = parts[5]  # LTE RSRP
                    ss_rsrq = parts[6]   # 5G SS-RSRQ
                    ss_rsrp = parts[7]   # 5G SS-RSRP
                    ss_sinr = parts[8]   # 5G SS-SINR

                    # Determine if we're on 5G or LTE based on which values are valid
                    is_5g = ss_rsrp != 255 and ss_rsrp >= 0
                    is_lte = rsrp_lte != 255 and rsrp_lte >= 0

                    if is_5g:
                        # Process 5G signal strength
                        # SS-RSRP: 0 = <-156dBm, 126 = -31dBm, 255 = unknown
                        if ss_rsrp != 255:
                            rsrp_dbm = -156 + ss_rsrp
                            # Map -156 to -31 dBm → 0 to 100%
                            rsrp_pct = int(((rsrp_dbm + 156) / 125) * 100)
                            rsrp_pct = max(0, min(100, rsrp_pct))
                            self.basic["cell_signal_percent_qrsrp"] = rsrp_pct
                            print(f"5G SS-RSRP: {ss_rsrp} → {rsrp_dbm} dBm → {rsrp_pct}%")
                        else:
                            self.basic["cell_signal_percent_qrsrp"] = ""

                        # SS-RSRQ: 0 = <-43dB, 126 = 20dB, 255 = unknown
                        if ss_rsrq != 255:
                            rsrq_db = -43 + (ss_rsrq * 0.5)
                            # Map -43 to 20 dB → 0 to 100%
                            rsrq_pct = int(((rsrq_db + 43) / 63) * 100)
                            rsrq_pct = max(0, min(100, rsrq_pct))
                            self.basic["cell_signal_percent_qrsrq"] = rsrq_pct
                            print(f"5G SS-RSRQ: {ss_rsrq} → {rsrq_db:.1f} dB → {rsrq_pct}%")
                        else:
                            self.basic["cell_signal_percent_qrsrq"] = ""

                        # SS-SINR: 0 = <-23dB, 127 = 40dB, 255 = unknown
                        if ss_sinr != 255:
                            sinr_db = -23 + (ss_sinr * 0.5)
                            # Map -23 to 40 dB → 0 to 100%
                            sinr_pct = int(((sinr_db + 23) / 63) * 100)
                            sinr_pct = max(0, min(100, sinr_pct))
                            self.basic["cell_signal_percent_sinr"] = sinr_pct
                            print(f"5G SS-SINR: {ss_sinr} → {sinr_db:.1f} dB → {sinr_pct}%")
                        else:
                            self.basic["cell_signal_percent_sinr"] = ""

                    elif is_lte:
                        # Process LTE signal strength
                        # RSRP: 0 = <-140dBm, 97 = -44dBm, 255 = unknown
                        if rsrp_lte != 255:
                            rsrp_dbm = -140 + rsrp_lte
                            # Map -140 to -44 dBm → 0 to 100%
                            rsrp_pct = int(((rsrp_dbm + 140) / 96) * 100)
                            rsrp_pct = max(0, min(100, rsrp_pct))
                            self.basic["cell_signal_percent_qrsrp"] = rsrp_pct
                            print(f"LTE RSRP: {rsrp_lte} → {rsrp_dbm} dBm → {rsrp_pct}%")
                        else:
                            self.basic["cell_signal_percent_qrsrp"] = ""

                        # RSRQ: 0 = <-19.5dB, 34 = -3dB, 255 = unknown
                        if rsrq_lte != 255:
                            rsrq_db = -19.5 + (rsrq_lte * 0.5)
                            # Map -19.5 to -3 dB → 0 to 100%
                            rsrq_pct = int(((rsrq_db + 19.5) / 16.5) * 100)
                            rsrq_pct = max(0, min(100, rsrq_pct))
                            self.basic["cell_signal_percent_qrsrq"] = rsrq_pct
                            print(f"LTE RSRQ: {rsrq_lte} → {rsrq_db:.1f} dB → {rsrq_pct}%")
                        else:
                            self.basic["cell_signal_percent_qrsrq"] = ""

                        # Clear SINR for LTE
                        self.basic["cell_signal_percent_sinr"] = ""

                    else:
                        # No valid signal data
                        print("CESQ: No valid LTE or 5G signal data")
                        self.basic["cell_signal_percent_qrsrp"] = ""
                        self.basic["cell_signal_percent_qrsrq"] = ""
                        self.basic["cell_signal_percent_sinr"] = ""

                except Exception as e:
                    print(f"Error parsing CESQ response: {e}")
                break

    def start_set_modem_config(self, tech_pref, roam_pref, lte_bands, nr5g_bands, usb_speed):
        print(f"Setting modem config: "
            f"tech_pref={tech_pref}, roam_pref={roam_pref}, "
            f"lte_bands={lte_bands}, nr5g_bands={nr5g_bands}, usb_speed={usb_speed}")
   
        try:
            print("About to call set_tech_pref...")
            self.set_tech_pref(tech_pref, force_update=True)
            print("set_tech_pref completed successfully")
        except Exception as e:
            print(f"Error in set_tech_pref: {e}")

        try:
            print("About to call set_roam_pref...")
            self.set_roam_pref(roam_pref)
            print("set_roam_pref completed successfully")
        except Exception as e:
            print(f"Error in set_roam_pref: {e}")

        try:
            print("About to call set_usb_speed...")
            self.set_usb_speed(usb_speed)
            print("set_usb_speed completed successfully")
        except Exception as e:
            print(f"Error in set_usb_speed: {e}")

        try:
            print("About to call set_lte_bands...")
            self.set_lte_bands(lte_bands)
            print("set_lte_bands completed successfully")
        except Exception as e:
            print(f"Error in set_lte_bands: {e}")
        if not self.is_em05():
            self.set_nr5g_bands(nr5g_bands)


    def open_modem_uart(self):
        if self.modem_uart_connected:
            return True
        try:
            self.serial_obj = serial.Serial(self.modem_tty_path, 115200, timeout=15)
        except Exception as e:
            self.modem_uart_connected = False
            with self.mutex:
                self.modem_valid = False
            pcat_logger.logger.warning("Failed to connect to modem tty device: {}".format(str(e)))
            print("Failed to connect to modem tty device: {} [{}]".format(str(e), self.modem_tty_failed_count))
            return False
        self.modem_uart_connected = True
        with self.mutex:
            self.modem_valid = True
        pcat_logger.logger.info("Connected to modem tty device.\n")
        return True

    def close_modem_uart(self):
        if self.serial_obj:
            self.serial_obj.close()
        self.modem_uart_connected = False

    def _get_retry_delay(self):
        """
        Calculate progressive retry delay based on system uptime and retry count.
        Base delays:
        - Initial (< 2 minutes uptime): 2 seconds
        - After 2 minutes uptime: 5 seconds
        - After 10 minutes uptime: 60 seconds

        With exponential backoff based on consecutive failures:
        - Multiplied by min(retry_count, 4) for exponential backoff
        """
        try:
            with open("/proc/uptime", "r") as f:
                uptime_seconds = float(f.readline().split()[0])

            # Base delay based on uptime
            if uptime_seconds > 600:  # > 10 minutes
                base_delay = 60
            elif uptime_seconds > 120:  # > 2 minutes
                base_delay = 5
            else:
                base_delay = 2

            # Apply exponential backoff based on consecutive failures
            # Cap the multiplier to prevent excessive delays
            backoff_multiplier = min(self.modem_tty_failed_count, 4)
            delay = base_delay * max(1, backoff_multiplier)

            # Cap maximum delay to reasonable limits
            max_delay = 300 if uptime_seconds > 600 else 60  # 5 min max for long uptime, 1 min for early
            return min(delay, max_delay)

        except Exception as e:
            print(f"Failed to read uptime, using default delay: {e}")
            return 2

    def _get_model_with_retry(self, retries: int = 3, delay: float = 1.0) -> str | None:
        """
        Try up to `retries` times to read self.basic["model"], waiting `delay` seconds
        between attempts. Returns the model string or None if still unavailable.
        First check permanent cache, then fall back to basic dict.
        """
        # First check permanent cache
        with _model_cache['lock']:
            if _model_cache['model'] is not None:
                return _model_cache['model']
        
        # Fall back to retries on basic dict for legacy compatibility
        for _ in range(retries):
            model = self.basic.get("model")
            if model is not None:
                return model
            time.sleep(delay)
        return None

    def is_500u(self) -> bool:
        model = self._get_model_with_retry()
        return bool(model) and "500U" in model

    def is_520(self) -> bool:
        model = self._get_model_with_retry()
        return bool(model) and "520" in model

    def is_500q(self) -> bool:
        model = self._get_model_with_retry()
        return bool(model) and "500Q" in model

    def is_em05(self) -> bool:
        with _em05_cache['lock']:
            if _em05_cache['result'] is not None:
                return _em05_cache['result']
        
        model = self._get_model_with_retry()
        result = bool(model) and "EM05" in model
        
        with _em05_cache['lock']:
            _em05_cache['result'] = result
            _em05_cache['queried'] = True
        
        return result

    def is_fm350(self) -> bool:
        model = self._get_model_with_retry()
        return bool(model) and "FM350" in model

    def is_supported_modem(self) -> bool:
        return (
            self.is_500u() or
            self.is_520() or
            self.is_500q() or
            self.is_em05() or
            self.is_fm350()
        )

    def get_inferred_sim_state(self, socket_sim_state: str) -> str:
        """
        Infer SIM state from available modem data if socket state is unknown/absent.
        For FM350 and other modems where pcat-manager may not detect SIM state correctly.

        Args:
            socket_sim_state: The SIM state from pcat-manager socket

        Returns:
            Inferred SIM state: "ready", "absent", "unknown", etc.
        """
        # If socket already has a valid state (not unknown/absent), use it
        if socket_sim_state and socket_sim_state not in ["unknown", "absent", ""]:
            return socket_sim_state

        # Check if we have valid SIM data from AT commands
        has_ccid = bool(self.basic.get("sim_ccid", ""))
        has_cimi = bool(self.basic.get("sim_cimi", ""))
        has_number = bool(self.basic.get("sim_number", ""))
        has_cpin_ready = self.basic.get("cpin_state") == "READY"

        # If we have any SIM identifiers and CPIN is ready, SIM is ready
        if (has_ccid or has_cimi or has_number) and has_cpin_ready:
            return "ready"
        elif (has_ccid or has_cimi or has_number):
            # Have SIM data but CPIN not confirmed ready - likely ready but be conservative
            return "ready"
        elif socket_sim_state == "unknown":
            # No SIM data and socket says unknown - keep as unknown
            return "unknown"
        else:
            # No SIM data detected - likely absent
            return "absent"


def fmt_num(x, unit=None):
    """
    If x>500 returns "-" (optionally omitting the unit);
    otherwise returns e.g. "123 dB" or "456" if unit is None.
    """
    if x > 1000 or x < -1000:
        return "- " + unit
    return f"{x}{(' ' + unit) if unit else ''}"

def lookup_isp_details(mccmnc: str) -> str:
    """
    Look up ISP/carrier details based on MCC/MNC code
    Returns formatted string with carrier name and country, or just the MCC/MNC if lookup fails
    """
    if not mccmnc or len(mccmnc) < 5:
        return mccmnc
    
        # Try exact match first
    if mccmnc in MCCMNC_MAP:
        return MCCMNC_MAP[mccmnc]
    
    # Try partial matches for cases where we have the MCC but not exact MNC
    mcc = mccmnc[:3] if len(mccmnc) >= 3 else ""
    
    if mcc in COUNTRY_MAP:
        return f"Unknown Carrier ({COUNTRY_MAP[mcc]}) - {mccmnc}"
    
    # Fallback: return the original MCC/MNC
    return mccmnc

# For self test
if __name__ == "__main__":
    modem_client = PcModemClient("/dev/ttyUSB3", "/etc/pc_modem.db")
    modem_client.open_modem_uart()

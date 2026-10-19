#@Author    : c2h2

"""
PcSmsClient - SMS Management Client for Phonicat Routers

This module handles SMS message operations for Phonicat devices, including receiving,
sending, and managing SMS messages through external sms_tool commands and a local SQLite database.

PUBLIC FUNCTIONS (API endpoints use these):
========================================
- __init__(usb_dev, modem_client, modem_db_path) : Initialize SMS client with USB device and database
- send_sms(send_to_number, sms_text) : Send SMS message to specified number (uses pysms_tool)
- get_sms() : Retrieve all SMS messages from database (combines sent and received)
- delete_sms_from_db(sms_id) : Delete specific SMS message by database ID
- clear_all_sms() : Clear all SMS messages from both database and modem storage (uses pysms_tool)
- run() : Main thread loop that processes SMS hooks every 60 seconds
- process_hook_shell(dry_run=False) : Execute custom hook scripts for unprocessed SMS messages

PYSMS_TOOL FUNCTIONS (new implementation):
=========================================
- execute_sms_command(cmd) : Execute SMS commands using pysms_tool instead of subprocess
- recv_from_sms_tool() : Fetch new SMS messages from modem via pysms_tool
- _delete_messages_from_modem(messages_to_delete) : Remove processed messages using pysms_tool

INTERNAL PRIVATE FUNCTIONS:
==========================
- exec_sql(sql_command) : Execute SQL commands on the SMS database
- get_sms_from_db() : Raw database query to retrieve SMS records
- save_sms_to_db(sms_list) : Insert SMS messages into database and delete from modem storage
- fix_date(sms_list) : Convert 2-digit year timestamps to 4-digit format (YY/MM/DD -> YYYY-MM-DD)
- combine_sms_parts(sms_list) : Merge multi-part SMS messages into single messages

DATABASE SCHEMA:
===============
- Table: sms (id, from_num, to_num, topic, hash_digest, storage, status, content, 
          hook_processed, direction, sent_at, raw_json, created_at)
- Direction: 0=received, 1=sent
- Storage: SM (SIM card), ME (modem memory), MT (combined)
- Hook processing: Custom scripts executed for new received messages

THREAD SAFETY:
==============
- Uses modem_client.serial_lock to coordinate serial port access
- Thread-safe database operations with connection per operation
- Automatic cleanup and port reopening on exceptions
"""

import threading
# import pyprctl # patch:set real thread name
import subprocess
import json
import datetime
import time
import signal
import hashlib
import os
import sqlite3
import sys

import pcat_config
import app as app_module
from pcat_util import isp_dict, pcat_logger
from database import init_db, connect_db, get_rows
from pysms_tool import SMSTool

INTERVAL_SMS_HOOK = 30
SMS_DEBUG_BOOL = True

# AT+CLCC <mode> values whose current bearer is voice (27.007): 0 voice, plus
# the combined modes that start on voice. 1 (data) and 2 (fax) are not calls
# this UI has any business showing -- see get_call_status().
VOICE_CALL_MODES = (0, 3, 4, 5)


SMS_INIT_SQL = '''
CREATE TABLE IF NOT EXISTS sms(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_num VARCHAR(64),
    to_num VARCHAR(64),
    topic VARCHAR(64),
    hash_digest VARCHAR(256) UNIQUE,
    storage VARCHAR(10),
    status VARCHAR(20),
    content TEXT,
    hook_processed INTEGER DEFAULT 0,
    direction INTEGER,
    sent_at DATETIME,
    raw_json TEXT,
    created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_hook_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sms_id INTEGER,
    sender VARCHAR(64),
    message_content TEXT,
    message_timestamp DATETIME,
    hook_script_content TEXT,
    hook_command TEXT,
    execution_start DATETIME NOT NULL,
    execution_end DATETIME,
    execution_duration_ms INTEGER,
    return_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    success INTEGER DEFAULT 0,
    error_message TEXT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY (sms_id) REFERENCES sms (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_sms_hook_history_created_at ON sms_hook_history(created_at);
CREATE INDEX IF NOT EXISTS idx_sms_hook_history_sms_id ON sms_hook_history(sms_id);
CREATE INDEX IF NOT EXISTS idx_sms_hook_history_success ON sms_hook_history(success);
'''

class PcSmsClient (threading.Thread):
    def __init__(self, usb_dev, modem_client, modem_db_path):
        threading.Thread.__init__(self)
        self.usb_dev = usb_dev
        self.modem_client = modem_client
        self.running_flag = False
        self.modem_db_path = modem_db_path
        self.last_check_time = None
        self.last_recv_time = None
        self.recv_in_progress = False
        self.recv_lock = threading.Lock()

        init_db(modem_db_path, SMS_INIT_SQL)
        
        # Ensure hook history table exists (migration for existing databases)
        self.ensure_hook_history_table(modem_db_path)
        
        connect_db(modem_db_path)
        print("SMS database initialization completed")

    def exec_sql(self, sql_command):
        if isinstance(sql_command, str):
            sql_command = [sql_command]
        connect_db(self.modem_db_path, sql_command)
    
    def exec_sql_param(self, sql_command, params=[]):
        """Execute parameterized SQL command to prevent SQL injection"""
        from database import connect_db_param
        connect_db_param(self.modem_db_path, [(sql_command, params)])
    
    def escape_sql_string(self, value):
        """Escape SQL string values to prevent injection and handle quotes"""
        if value is None:
            return ''
        return str(value).replace("'", "''")
    
    def ensure_hook_history_table(self, db_path):
        """Ensure the hook history table exists (migration for existing databases)"""
        try:
            from database import get_rows, connect_db
            
            # Check if table exists
            try:
                get_rows(db_path, "SELECT COUNT(*) FROM sms_hook_history LIMIT 1")
                print("Hook history table already exists")
                return
            except:
                print("Hook history table does not exist, creating it...")
                
            # Create the hook history table
            hook_history_sql = """
CREATE TABLE IF NOT EXISTS sms_hook_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sms_id INTEGER,
    sender VARCHAR(64),
    message_content TEXT,
    message_timestamp DATETIME,
    hook_script_content TEXT,
    hook_command TEXT,
    execution_start DATETIME NOT NULL,
    execution_end DATETIME,
    execution_duration_ms INTEGER,
    return_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    success INTEGER DEFAULT 0,
    error_message TEXT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY (sms_id) REFERENCES sms (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_sms_hook_history_created_at ON sms_hook_history(created_at);
CREATE INDEX IF NOT EXISTS idx_sms_hook_history_sms_id ON sms_hook_history(sms_id);
CREATE INDEX IF NOT EXISTS idx_sms_hook_history_success ON sms_hook_history(success);
"""
            
            # Execute the creation commands
            commands = [cmd.strip() for cmd in hook_history_sql.split(';') if cmd.strip()]
            connect_db(db_path, commands)
            print("Hook history table created successfully")
            
        except Exception as e:
            print(f"Error ensuring hook history table: {e}")
            pcat_logger.logger.error(f"Error ensuring hook history table: {e}")


    def run(self):
        while True:
            time.sleep(INTERVAL_SMS_HOOK)
            self.process_hook_shell()

    def process_hook_shell(self, dry_run=False):
        """
        Check db for hook_processed = 0, execute cmd, update hook_processed = 1 unless dry_run=True.
        Returns True if any rows were processed, False otherwise.
        """
        cmd_base = "/etc/pcat_hook_cmd.sh"

        # Check preconditions quietly first: this runs every 30s from the hook
        # thread and used to print a banner each pass even when disabled.
        if not pcat_config.get_sms_hook_enabled():
            return False

        if not os.path.exists(cmd_base):
            pcat_logger.logger.debug(f"sms hook script not found: {cmd_base}")
            return False

        pcat_logger.logger.debug(f"SMS hook processing started: dry_run={dry_run}, database={self.modem_db_path}")

        # Read current hook script content for logging
        hook_script_content = ""
        try:
            with open(cmd_base, 'r', encoding='utf-8', errors='ignore') as f:
                hook_script_content = f.read()
        except Exception as e:
            print(f"Warning: Could not read hook script content: {e}")

        try:
            # Import get_rows locally to handle any import issues
            from database import get_rows
            rows = get_rows(self.modem_db_path, "SELECT * FROM sms WHERE hook_processed = 0;")
            if not rows:
                return False
            pcat_logger.logger.info(f"Found {len(rows)} unprocessed SMS messages for hook")

            processed = False
            for i, row in enumerate(rows):
                print(f"Processing SMS {i+1}/{len(rows)}: ID={dict(row).get('id', 'unknown')}")
                # Convert row to dict if not already
                row_dict = dict(row) if isinstance(row, sqlite3.Row) else row
                
                # Prepare command with arguments as a list to handle special characters
                cmd = [cmd_base, row_dict.get("from_num", ""), row_dict.get("content", ""), row_dict.get("sent_at", "")]
                cmd_str = " ".join([f"'{arg}'" if " " in str(arg) else str(arg) for arg in cmd])
                print("hook_shell cmd: ", cmd)
                
                # Record execution start time
                execution_start = datetime.datetime.now()
                execution_end = None
                execution_duration_ms = 0
                return_code = -1
                stdout_text = ""
                stderr_text = ""
                success = 0
                error_message = ""
                
                try:
                    # Execute command with timeout
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        stdout, stderr = p.communicate(timeout=30)  # 30 second timeout
                        execution_end = datetime.datetime.now()
                        execution_duration_ms = int((execution_end - execution_start).total_seconds() * 1000)
                        return_code = p.returncode
                        
                        # Decode output safely
                        stdout_text = stdout.decode('utf-8', errors='ignore')
                        stderr_text = stderr.decode('utf-8', errors='ignore')
                        
                        print("hook_shell stdout: ", stdout_text)
                        print("hook_shell stderr: ", stderr_text)
                        
                        if return_code == 0:
                            success = 1
                            if not dry_run:
                                # Update hook_processed to 1
                                self.exec_sql_param("UPDATE sms SET hook_processed = 1 WHERE id = ?", [row_dict["id"]])
                                pcat_logger.logger.info(f"Processed hook for SMS id {row_dict['id']} (dry_run={dry_run})")
                            else:
                                pcat_logger.logger.info(f"Dry-run: Skipped updating hook_processed for SMS id {row_dict['id']}")
                            processed = True
                        else:
                            error_message = f"Hook command failed with return code {return_code}"
                            pcat_logger.logger.error(f"Hook command failed for SMS id {row_dict['id']}: {stderr_text}")
                            
                    except subprocess.TimeoutExpired:
                        p.kill()
                        execution_end = datetime.datetime.now()
                        execution_duration_ms = int((execution_end - execution_start).total_seconds() * 1000)
                        error_message = "Hook execution timed out after 30 seconds"
                        stderr_text = "Process timed out"
                        return_code = -1
                        pcat_logger.logger.error(f"Hook command timed out for SMS id {row_dict['id']}")
                        
                except Exception as e:
                    execution_end = datetime.datetime.now()
                    execution_duration_ms = int((execution_end - execution_start).total_seconds() * 1000)
                    error_message = f"Exception during hook execution: {str(e)}"
                    stderr_text = str(e)
                    return_code = -1
                    pcat_logger.logger.error(f"Failed to process hook for SMS id {row_dict.get('id', 'unknown')}: {e}")
                
                # Log execution history to database (always log, even for dry runs)
                print("=== ATTEMPTING TO LOG HOOK HISTORY ===")
                try:
                    current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    execution_start_str = execution_start.strftime('%Y-%m-%d %H:%M:%S')
                    execution_end_str = execution_end.strftime('%Y-%m-%d %H:%M:%S') if execution_end else execution_start_str
                    print(f"Times: start={execution_start_str}, end={execution_end_str}, current={current_time}")
                    
                    # Truncate large outputs to prevent database bloat (keep last 2000 chars)
                    stdout_truncated = stdout_text[-2000:] if len(stdout_text) > 2000 else stdout_text
                    stderr_truncated = stderr_text[-2000:] if len(stderr_text) > 2000 else stderr_text
                    hook_script_truncated = hook_script_content[-5000:] if len(hook_script_content) > 5000 else hook_script_content
                    
                    history_sql = """INSERT INTO sms_hook_history(
                        sms_id, sender, message_content, message_timestamp, 
                        hook_script_content, hook_command, execution_start, execution_end,
                        execution_duration_ms, return_code, stdout, stderr, 
                        success, error_message, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"""
                    
                    history_params = [
                        row_dict['id'],
                        row_dict.get("from_num", ""),
                        row_dict.get("content", ""),
                        row_dict.get("sent_at", ""),
                        hook_script_truncated,
                        cmd_str,
                        execution_start_str,
                        execution_end_str,
                        execution_duration_ms,
                        return_code,
                        stdout_truncated,
                        stderr_truncated,
                        success,
                        error_message,
                        current_time
                    ]
                    
                    print(f"About to execute SQL: {history_sql[:300]}...")
                    self.exec_sql_param(history_sql, history_params)
                    print(f"SUCCESS: Logged hook execution history for SMS id {row_dict['id']}")
                    print(f"Database path: {self.modem_db_path}")
                    
                    # Verify the record was actually inserted
                    try:
                        from database import get_rows_param
                        verify_result = get_rows_param(self.modem_db_path, "SELECT COUNT(*) as count FROM sms_hook_history WHERE sms_id = ?", [row_dict['id']])
                        record_count = verify_result[0]['count'] if verify_result else 0
                        print(f"VERIFICATION: Found {record_count} records for SMS id {row_dict['id']} in hook_history table")
                    except Exception as verify_error:
                        print(f"VERIFICATION ERROR: {verify_error}")
                    
                except Exception as e:
                    print(f"ERROR: Failed to log hook execution history: {e}")
                    print(f"SQL command was: {history_sql[:500]}...")
                    pcat_logger.logger.error(f"Failed to log hook execution history: {e}")
            
            return processed
        except Exception as e:
            print("process_hook_shell error: ", e)
            pcat_logger.logger.error(f"Failed to query or process hook: {e}")
            return False

    def get_sms(self):
        #default we get from db:
        current_time = time.time()

        # Check if we should call recv_from_sms_tool (throttled to once per 8 seconds)
        should_fetch = False
        is_startup_fetch = False
        with self.recv_lock:
            if (self.last_check_time is None or current_time - self.last_check_time > 9):
                # Check throttling - only call recv_from_sms_tool if not in progress and 8+ seconds passed
                if (not self.recv_in_progress and
                    (self.last_recv_time is None or current_time - self.last_recv_time >= 8)):
                    should_fetch = True
                    is_startup_fetch = (self.last_recv_time is None)  # Track if this is the first fetch
                    self.recv_in_progress = True
                    self.last_recv_time = current_time
                self.last_check_time = current_time

        if should_fetch:
            try:
                fetch_result = self.recv_from_sms_tool()
                # At startup, if recv_from_sms_tool fails but we have database content, that's OK
                if is_startup_fetch and fetch_result is None:
                    if app_module.is_wwan_powered():
                        print("SMS startup fetch failed but continuing with database content")
            except Exception as e:
                print(f"SMS fetch error during {'startup' if is_startup_fetch else 'refresh'}: {e}")
                # Don't fail the entire get_sms call if refresh fails
            finally:
                with self.recv_lock:
                    self.recv_in_progress = False
        
        # Optimized: get pre-sorted messages and process more efficiently
        rows = get_rows(self.modem_db_path, 
            "SELECT id, from_num, to_num, content, status, direction, sent_at, created_at, raw_json "
            "FROM sms ORDER BY created_at DESC;")
        
        ret_list = {"msg": []}
        for row in rows:
            # Convert row to dict once
            sms = {k: row[k] for k in row.keys()}
            
            if sms["raw_json"]:
                # Received messages: parse JSON once and merge with id
                try:
                    obj = json.loads(sms["raw_json"])
                    obj["id"] = sms["id"]
                    ret_list["msg"].append(obj)
                except (json.JSONDecodeError, TypeError):
                    # Fallback to basic format if JSON parsing fails
                    ret_list["msg"].append(self._create_basic_message_obj(sms))
            else:
                # Sent messages: create object directly
                ret_list["msg"].append(self._create_basic_message_obj(sms))
        
        return ret_list

    def _create_basic_message_obj(self, sms):
        """Create a standardized message object from database row data."""
        return {
            "id": sms["id"],
            "sender": "me" if sms["direction"] == 1 else sms["from_num"],
            "from": sms["from_num"] or "",
            "to": sms["to_num"] or "",
            "content": sms["content"] or "",
            "send_at": sms["sent_at"] or sms["created_at"],
            "status": sms["status"] or "",
            "direction": sms["direction"],
            "timestamp": sms["sent_at"] or sms["created_at"]
        }

    def get_sms_from_db(self):
        rows = get_rows(self.modem_db_path, "SELECT * FROM sms ORDER BY created_at DESC;")
        messages = [{k: row[k] for k in row.keys()} for row in rows]
        return {"msg": messages}
  
    def fix_date(self, sms_list): # fix 06/04/25 16:51:30 to 2006-04-25 16:51:30
        """
        Convert timestamps in sms_list from "YY/MM/DD HH:MM:SS"
        to "YYYY-MM-DD HH:MM:SS", treating the two-digit year as 20YY.
        """
        for index, sms in enumerate(sms_list["msg"]):
            raw_ts = sms.get('timestamp')
            if not raw_ts:
                continue

            try: # reformat to four-digit year
                dt = datetime.datetime.strptime(raw_ts, "%m/%d/%y %H:%M:%S")
                sms_list["msg"][index]['timestamp'] = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                # if parsing fails, leave it untouched or log a warning
                pcat_logger.logger.warning(f"Could not parse timestamp: {raw_ts}")
        return sms_list

    def combine_sms_parts(self, sms_list):
        # 1. Separate out multipart messages vs. single messages
        multipart_groups = {}
        single_messages = []
        smss = sms_list["msg"]
        for sms in smss:
            index = sms.get('index')
            total = sms.get('total', 1)
            part  = sms.get('part',  1)
            ref   = sms.get('reference')
            sender= sms.get('sender')

            # If this SMS has multiple parts, group it by (sender, reference)
            if total > 1 and ref is not None:
                key = (sender, ref)
                multipart_groups.setdefault(key, []).append(sms)
            else:
                # single-part: just pass through
                temp_sms = sms.copy()
                temp_sms['indexes'] = [index]
                single_messages.append(temp_sms)

        # 2. Merge each multipart group
        merged = []
        for (sender, ref), parts in multipart_groups.items():
            # Sort by part number
            parts.sort(key=lambda x: x['part'])
            # Start from the first part’s metadata...
            first = parts[0].copy()
            first['indexes'] = [p['index'] for p in parts]
            # ...but rebuild content by concatenating all parts in order
            first['content'] = ''.join(p['content'] for p in parts)
            # Clean up fields you no longer need (optional)
            first.pop('part', None)
            first.pop('total', None)
            merged.append(first)
        
        # 3. Combine and re-sort by original index (or timestamp) if you need deterministic order
        combined = single_messages + merged
        combined.sort(key=lambda x: x.get('index', 0))
        fmt_combined = {}
        fmt_combined["msg"] = combined
        return fmt_combined

    def save_sms_to_db(self, sms_list):
        """
        Insert new SMS messages into the local SQLite DB, ignoring duplicates.
        After saving, delete the message from modem/SIM storage to prevent overflow.
        """
        # Define helper function to escape SQL string values
        def escape_sql_string(value):
            return str(value).replace("'", "''") if value is not None else ''

        print("save_sms_to_db: ", sms_list)
        messages_to_delete = []  # Track messages to delete from modem after saving
        
        for sms in sms_list.get('msg', []):
            # Compute unique hash
            payload = json.dumps(sms, sort_keys=True)
            hash_digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
            # Skip if exists
            sql = "SELECT 1 FROM sms WHERE hash_digest = ?;"
            print("sql: ", sql, "params:", [hash_digest])
            from database import get_rows_param
            rows = get_rows_param(self.modem_db_path, sql, [hash_digest])
            if len(rows) > 0:
                print(f"SMS already exists in database, skipping duplicate")
                # Still add to delete list if message exists in database but not yet deleted from modem
                message_index = sms.get('index')
                message_storage = sms.get('storage', 'SM')  # Default to SM if not specified
                if message_index:
                    messages_to_delete.append((message_index, message_storage))
                continue
                
            # Extract fields
            from_num   = sms.get('sender', '')
            to_num     = sms.get('recipient', '')
            topic      = sms.get('topic', '')
            storage    = sms.get('storage', '')
            status     = sms.get('status', '')
            content    = sms.get('content', '')
            direction  = sms.get('direction', 1)
            hook_processed = sms.get('hook_processed', 0)
            sent_at = sms.get('timestamp', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            created_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            raw_json = json.dumps(sms)
            
            # Insert into database
            complete_sql = f"""INSERT INTO sms(
                from_num, to_num, topic, hash_digest,
                storage, status, content, direction, sent_at, raw_json, created_at
            ) VALUES(
                '{escape_sql_string(from_num)}',
                '{escape_sql_string(to_num)}',
                '{escape_sql_string(topic)}',
                '{escape_sql_string(hash_digest)}',
                '{escape_sql_string(storage)}',
                '{escape_sql_string(status)}',
                '{escape_sql_string(content)}',
                {direction},
                '{escape_sql_string(sent_at)}',
                '{escape_sql_string(raw_json)}',
                '{escape_sql_string(created_at)}'
            );"""
            print("complete_sql: ", complete_sql)
            
            try:
                self.exec_sql(complete_sql)
                print(f"SMS saved to database successfully")
                
                # Add to delete list only after successful database save
                message_index = sms.get('index')
                message_storage = sms.get('storage', 'SM')  # Default to SM if not specified
                if message_index:
                    messages_to_delete.append((message_index, message_storage))
                    print(f"Queued SMS index {message_index} from storage {message_storage} for deletion")
                    
            except Exception as e:
                print(f"Failed to save SMS to database: {e}")
                pcat_logger.logger.error(f"Failed to save SMS to database: {e}")
                continue  # Don't delete from modem if database save failed
        
        # Delete saved messages from modem/SIM storage
        self._delete_messages_from_modem(messages_to_delete)


    def delete_sms_from_db(self, sms_id):
        """
        Delete a single SMS record by database ID.
        """
        try:
            sql_command = "DELETE FROM sms WHERE id = ?;"
            print("delete_sms_from_db: ", sql_command, "params:", [int(sms_id)])
            self.exec_sql_param(sql_command, [int(sms_id)])
        except Exception as e:
            pcat_logger.logger.error(f"Failed to delete sms from db: {e}")
            return False
        return True

    # ============================================================================
    # PYSMS_TOOL IMPLEMENTATION FUNCTIONS
    # ============================================================================
    # These functions use the new pysms_tool instead of subprocess calls
    
    def _wait_for_serial_connection(self, timeout_seconds=10):
        """
        Wait for the pc_modem_client to establish serial connection.
        If no connection exists, trigger one by adding a simple AT command.
        Returns the serial object if available, None if timeout.
        
        Note: This method assumes the serial_lock is already acquired by the caller.
        """
        # First check if connection already exists
        if self.modem_client.serial_obj is not None:
            if hasattr(self.modem_client.serial_obj, 'is_open') and self.modem_client.serial_obj.is_open:
                return self.modem_client.serial_obj
        
        # Now wait for connection to be established
        start_time = time.time()
        tries = 0
        while time.time() - start_time < timeout_seconds:
            # Check if modem client has established connection
            if self.modem_client.serial_obj is not None:
                if hasattr(self.modem_client.serial_obj, 'is_open') and self.modem_client.serial_obj.is_open:
                    return self.modem_client.serial_obj
            tries += 1
            time.sleep(0.5)  # Wait 500ms before checking again
        
        if app_module.is_wwan_powered():
            print("Timeout waiting for pc_modem_client serial connection, returning existing serial object anyway.")
        return self.modem_client.serial_obj
    
    
    def send_sms(self, send_to_number, sms_text):
        """
        Send SMS using pysms_tool instead of subprocess.
        """
        if not app_module.is_wwan_powered():
            print("Cannot send SMS: WWAN powered off")
            return False

        print(f"send_sms (pysms_tool): {send_to_number} - {sms_text}")
        pcat_logger.logger.info(f"Sending SMS via pysms_tool to {send_to_number}")
        send_success = False
        
        try:
            # Get serial connection with minimal lock time
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if serial_obj is None:
                    print("Cannot send SMS: No serial connection available")
                    return False
            
            # Release lock during SMS operation to avoid blocking other operations
            # Create SMSTool instance with shared serial port
            sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL)
            
            # Send SMS using pysms_tool (this can take time, so done outside lock)
            print(f"About to send SMS via SMSTool: to={send_to_number}, text_length={len(sms_text)}")
            
            # Use a separate lock or timeout to prevent conflicts during actual SMS operations
            send_success = False
            try:
                # Temporarily acquire lock just for the actual send operation
                with self.modem_client.serial_lock:
                    send_success = sms_tool.send_sms(send_to_number, sms_text)
                    print(f"SMSTool.send_sms returned: {send_success}")
            except Exception as sms_send_error:
                print(f"SMS send operation failed: {sms_send_error}")
                pcat_logger.logger.error(f"SMS send operation failed: {sms_send_error}")
                # Don't cleanup serial connection - let modem_client handle reconnection
                # Cleanup is too aggressive and breaks subsequent operations
                # The serial port is shared and managed by modem_client, not SMS client
                return False
            
            # Log more details for debugging emoji issues
            if not send_success:
                print(f"SMS send reported as failed but may have been delivered. Text contains emojis: {any(ord(c) > 127 for c in sms_text)}")
                pcat_logger.logger.warning(f"SMSTool reported SMS send failure for message to {send_to_number}, but message may have been delivered")
                
        except Exception as e:
            print(f"send_sms error: {e}")
            pcat_logger.logger.error(f"Failed to send SMS via pysms_tool: {e}")
            return False
        
        # Store sent message in database
        if send_success:
            try:
                # Set timestamp to 10 seconds ago to account for sending delay
                send_time = datetime.datetime.now() - datetime.timedelta(seconds=10)
                current_time = send_time.strftime('%Y-%m-%d %H:%M:%S')
                hash_input = f"{send_to_number}:{sms_text}:{current_time}"
                hash_digest = hashlib.md5(hash_input.encode()).hexdigest()
                
                # Insert sent message into database with direction=1 (sent)
                sql_command = """
                INSERT INTO sms (from_num, to_num, status, content, direction, sent_at, created_at, hash_digest, raw_json)
                VALUES (?, ?, 'SENT', ?, 1, ?, ?, ?, NULL)
                """
                conn = sqlite3.connect(self.modem_db_path)
                cursor = conn.cursor()
                cursor.execute(sql_command, ('', send_to_number, sms_text, current_time, current_time, hash_digest))
                conn.commit()
                conn.close()
                print(f"Stored sent SMS to {send_to_number} in database")
            except Exception as e:
                print(f"Failed to store sent SMS in database: {e}")
                pcat_logger.logger.error(f"Failed to store sent SMS in database: {e}")
        
        return send_success
    
    def recv_from_sms_tool(self):
        """
        Receive SMS using pysms_tool instead of subprocess.
        """
        if not app_module.is_wwan_powered():
            print("***** Skipping SMS fetch: WWAN powered off *****")
            return None

        print("***** Fetching Modem SMS via pysms_tool *****")

        try:
            # Get serial connection with minimal lock time
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if serial_obj is None:
                    if app_module.is_wwan_powered():
                        print("Cannot receive SMS: No serial connection available")
                    return None
            
            # Release lock during SMS operation to avoid blocking other operations
            # Create SMSTool instance with shared serial port
            sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL)
            
            # Get all messages (this can take time, so done with minimal lock)
            all_messages = []
            try:
                with self.modem_client.serial_lock:
                    all_messages = sms_tool.get_all_messages(storage_types=["SM", "ME"], delete_after=True)
            except Exception as sms_recv_error:
                pcat_logger.logger.error(f"SMS receive operation failed: {sms_recv_error}")
                # Don't cleanup serial connection - let modem_client handle reconnection
                # Cleanup is too aggressive and breaks subsequent operations
                # The serial port is shared and managed by modem_client, not SMS client
                return None

        except Exception as e:
            pcat_logger.logger.error("Failed to recv from sms tool via pysms_tool", exc_info=e)
            return None

        try:
            # Convert pysms_tool format to the format expected by the rest of the system
            sms_list = {"msg": []}
            
            for msg in all_messages:
                decoded = msg['decoded']
                
                # Convert timestamp format from "2025/01/15 12:34:56 GMT+08:00" to "01/15/25 12:34:56"
                timestamp = decoded.get('timestamp', '')
                if timestamp:
                    try:
                        # Parse the timestamp and convert to expected format
                        # Handle format like "2025/01/15 12:34:56 GMT+08:00"
                        timestamp_part = timestamp.split(' GMT')[0]  # Remove timezone
                        dt = datetime.datetime.strptime(timestamp_part, "%Y/%m/%d %H:%M:%S")
                        # Convert to MM/DD/YY HH:MM:SS format
                        timestamp = dt.strftime("%m/%d/%y %H:%M:%S")
                    except:
                        # If parsing fails, keep original
                        pass
                
                # Create message object in expected format
                message_obj = {
                    'index': msg['index'],
                    'sender': decoded.get('sender', ''),
                    'timestamp': timestamp,
                    'content': decoded.get('message', ''),
                    'status': 'REC UNREAD',  # Default status
                    'storage': msg.get('storage', 'SM'),
                    'direction': 0,  # Received message
                }
                
                # Add multipart SMS fields if present
                if 'reference' in decoded:
                    message_obj['reference'] = decoded['reference']
                if 'part' in decoded:
                    message_obj['part'] = decoded['part']
                if 'total' in decoded:
                    message_obj['total'] = decoded['total']
                
                sms_list["msg"].append(message_obj)
          
            if len(sms_list["msg"]) == 0:
                return sms_list  # empty for now
                
            # Process the messages with existing methods
            sms_list = self.combine_sms_parts(sms_list)
            sms_list = self.fix_date(sms_list)
            
            reverse_sorted_sms_list = {}
            reverse_sorted_sms_list["msg"] = sorted(sms_list["msg"], key=lambda x: x.get('index', 0), reverse=True)
            pcat_logger.logger.info(f"Retrieved {len(reverse_sorted_sms_list['msg'])} messages via pysms_tool")
            self.save_sms_to_db(reverse_sorted_sms_list)
            return reverse_sorted_sms_list
            
        except Exception as e:
            print("recv_from_sms_tool error 2: ", e)
            pcat_logger.logger.error("Failed to parse sms list from pysms_tool")
            return None
    
    def _delete_messages_from_modem(self, messages_to_delete):
        """
        Delete messages from modem using pysms_tool instead of subprocess.
        messages_to_delete: list of tuples (index, storage)
        """
        if not messages_to_delete:
            return
            
        print(f"Deleting {len(messages_to_delete)} messages from modem storage via pysms_tool")
        
        # Group by storage type for efficiency
        storage_groups = {}
        for index, storage in messages_to_delete:
            if storage not in storage_groups:
                storage_groups[storage] = []
            storage_groups[storage].append(index)
        
        for storage, indexes in storage_groups.items():
            deleted_count = 0
            # Get serial connection with minimal lock time
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if serial_obj is None:
                    print(f"Cannot delete messages from {storage}: No serial connection available")
                    return
            
            # Delete messages with minimal lock time per operation  
            for index in indexes:
                try:
                    with self.modem_client.serial_lock:
                        # Use pysms_tool to delete specific message by index with shared serial port
                        sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL, storage=storage)
                        success = sms_tool.delete_sms(str(index))
                        
                        if success:
                            deleted_count += 1
                            print(f"Deleted SMS index {index} from {storage} storage")
                        else:
                            print(f"Warning: Failed to delete SMS index {index} from {storage}")
                            
                except Exception as e:
                    print(f"Warning: Error deleting SMS index {index} from {storage}: {e}")
                    continue
            
            print(f"Successfully deleted {deleted_count}/{len(indexes)} messages from {storage} storage via pysms_tool")
    
    def clear_all_sms(self):
        """
        Clear all SMS using pysms_tool instead of subprocess.
        This clears SMS from database and sends commands to clear modem storage.
        """
        print("Clearing all SMS messages via pysms_tool")
        
        # Clear all SMS from database
        try:
            self.exec_sql("DELETE FROM sms")
            print("Database: All SMS messages cleared successfully")
        except Exception as e:
            pcat_logger.logger.error(f"Failed to clear SMS from database: {e}")
            raise Exception(f"Database clear failed: {e}")
        
        # Clear all SMS from modem storage areas using pysms_tool
        try:
            # Get serial connection with minimal lock time
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if serial_obj is None:
                    raise Exception("Failed to get serial connection for clearing SMS")
                    
            storage_types = ["SM", "ME"]
            cleared_count = 0
            
            for storage in storage_types:
                try:
                    with self.modem_client.serial_lock:
                        # Use pysms_tool to delete all messages from this storage with shared serial port
                        sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL, storage=storage)
                        success = sms_tool.delete_sms("all")
                        
                        if success:
                            cleared_count += 1
                            print(f"Cleared all messages from {storage} storage via pysms_tool")
                        else:
                            print(f"Warning: Failed to clear {storage} storage via pysms_tool")
                            
                except Exception as e:
                    print(f"Warning: Failed to clear {storage} storage via pysms_tool: {e}")
                    continue
            
        except Exception as e:
            pcat_logger.logger.error(f"Failed to clear SMS from modem via pysms_tool: {e}")
            # Don't raise exception here as database was already cleared successfully
            print(f"Warning: Modem clear operations failed: {e}")


        print("Cleared all SMS messages from modem storage via pysms_tool")
        
        return True

    # Voice Call Management Functions
    def make_voice_call(self, phone_number):
        """Make a voice call using pysms_tool."""
        try:
            # Get serial connection
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if serial_obj is None:
                    print("Cannot make voice call: No serial connection available")
                    return False
            
            # Make the call using pysms_tool
            with self.modem_client.serial_lock:
                sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL)
                success = sms_tool.make_voice_call(phone_number)
                
                if success:
                    print(f"Voice call initiated to {phone_number}")
                    # The history row is written by pc_call_log when the call
                    # ends and its duration is known; just name the callee so
                    # it does not have to wait for AT+CLCC to report it.
                    import pc_call_log
                    pc_call_log.call_session_tracker.note_number(phone_number)
                    return True
                else:
                    print(f"Failed to initiate voice call to {phone_number}")
                    return False
                    
        except Exception as e:
            pcat_logger.logger.error(f"Error making voice call: {e}")
            print(f"Error making voice call: {e}")
            return False

    def answer_call(self):
        """Answer incoming voice call using pysms_tool."""
        try:
            # Get serial connection
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if serial_obj is None:
                    print("Cannot answer call: No serial connection available")
                    return False
            
            # Answer the call using pysms_tool
            with self.modem_client.serial_lock:
                sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL)
                success = sms_tool.answer_call()
                
                if success:
                    print("Voice call answered successfully")
                    return True
                else:
                    print("Failed to answer voice call")
                    return False
                    
        except Exception as e:
            pcat_logger.logger.error(f"Error answering voice call: {e}")
            print(f"Error answering voice call: {e}")
            return False

    def hangup_call(self):
        """Hang up current voice call using pysms_tool."""
        try:
            # Get serial connection
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if serial_obj is None:
                    print("Cannot hang up call: No serial connection available")
                    return False
            
            # Hang up the call using pysms_tool
            with self.modem_client.serial_lock:
                sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL)
                success = sms_tool.hangup_call()
                
                if success:
                    print("Voice call ended successfully")
                    import pc_call_log
                    pc_call_log.call_session_tracker.finish()
                    return True
                else:
                    print("Failed to hang up voice call")
                    return False
                    
        except Exception as e:
            pcat_logger.logger.error(f"Error hanging up voice call: {e}")
            print(f"Error hanging up voice call: {e}")
            return False

    def get_call_status(self):
        """Get current call status using pysms_tool."""
        try:
            # Get serial connection
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if serial_obj is None:
                    print("Cannot get call status: No serial connection available")
                    return {"calls": [], "has_active_calls": False}
            
            # Get call status using pysms_tool
            with self.modem_client.serial_lock:
                sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL)
                # Use AT+CLCC to check for active calls
                response = sms_tool._send_command("AT+CLCC")
                
                calls = []
                if response and "OK" in response:
                    lines = response.split('\n')
                    for line in lines:
                        # Parse AT+CLCC response format: +CLCC: <idx>,<dir>,<stat>,<mode>,<mpty>,<number>,<type>
                        if line.startswith('+CLCC:'):
                            parts = line.replace('+CLCC: ', '').split(',')
                            if len(parts) >= 6:
                                idx = int(parts[0])
                                direction = int(parts[1])  # 0=MO, 1=MT
                                status = int(parts[2])     # 0=active, 1=held, 2=dialing, 3=alerting, 4=incoming, 5=waiting
                                mode = int(parts[3])       # 0=voice, 1=data, 2=fax
                                number = parts[5].strip('"')
                                
                                # Voice calls only. AT+CLCC lists every bearer
                                # the module has up, not just phone calls: on
                                # EM05-CN the LTE data context and the IMS
                                # context both show as
                                #   +CLCC: n,1,0,1,0,"",128
                                # (mode 1 = data, status 0 = active), which the
                                # phone page read as two calls in progress and
                                # sat there showing an in-call timer with
                                # nothing dialled. Modes per 27.007: 0 voice,
                                # 1 data, 2 fax, 3-5 the voice-first combined
                                # modes, 6+ data/fax-first.
                                if mode not in VOICE_CALL_MODES:
                                    continue

                                # Map status codes to readable text
                                status_map = {
                                    0: "active",
                                    1: "held",
                                    2: "dialing",
                                    3: "alerting",
                                    4: "incoming",
                                    5: "waiting"
                                }

                                calls.append({
                                    "id": idx,
                                    "number": number,
                                    "status": status_map.get(status, "unknown"),
                                    "direction": "outgoing" if direction == 0 else "incoming",
                                    "mode": "voice" if mode == 0 else "data"
                                })
                
                has_active_calls = len(calls) > 0
                return {
                    "calls": calls,
                    "has_active_calls": has_active_calls
                }
                    
        except Exception as e:
            pcat_logger.logger.error(f"Error getting call status: {e}")
            print(f"Error getting call status: {e}")
            return {"calls": [], "has_active_calls": False}

    def set_audio_path(self, path):
        """Set audio path for voice calls using pysms_tool."""
        try:
            # Get serial connection
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if serial_obj is None:
                    print("Cannot set audio path: No serial connection available")
                    return False
            
            # Set audio path using pysms_tool
            with self.modem_client.serial_lock:
                sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL)
                success = sms_tool.set_audio_path(path)
                
                if success:
                    print(f"Audio path set to {path}")
                    return True
                else:
                    print(f"Failed to set audio path to {path}")
                    return False
                    
        except Exception as e:
            pcat_logger.logger.error(f"Error setting audio path: {e}")
            print(f"Error setting audio path: {e}")
            return False

    def _ensure_call_history_table(self):
        """Create call_history table if it doesn't exist."""
        try:
            sql_create_table = '''
            CREATE TABLE IF NOT EXISTS call_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('outgoing', 'incoming', 'missed')),
                duration TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            '''
            self.exec_sql(sql_create_table)
        except Exception as e:
            pcat_logger.logger.error(f"Error creating call_history table: {e}")

    def get_call_history(self):
        """Get call history from SQLite database."""
        try:
            self._ensure_call_history_table()
            
            sql = '''
            SELECT id, number, type, duration, 
                   datetime(timestamp, 'localtime') as timestamp
            FROM call_history 
            ORDER BY timestamp DESC 
            LIMIT 100
            '''
            
            # exec_sql() takes no params and returns None; reads must go through
            # get_rows_param (which returns sqlite3.Row objects).
            from database import get_rows_param
            rows = get_rows_param(self.modem_db_path, sql)
            call_history = []
            for row in rows:
                call_history.append({
                    "id": str(row[0]),
                    "number": row[1],
                    "type": row[2],
                    "duration": row[3],
                    "timestamp": row[4]
                })
            return call_history

        except Exception as e:
            pcat_logger.logger.error(f"Error getting call history: {e}")
            return []

    def log_call_history(self, call_type, number, status, duration=None):
        """Log call history to SQLite database."""
        try:
            self._ensure_call_history_table()
            
            # Map status to call type for better classification
            if status in ['busy', 'no_carrier', 'no_answer']:
                call_type = 'missed' if call_type == 'incoming' else call_type
            
            sql = '''
            INSERT INTO call_history (number, type, duration, timestamp)
            VALUES (?, ?, ?, datetime('now', 'localtime'))
            '''
            
            # Was self.exec_sql(sql, params) — exec_sql takes no params arg and
            # returns None, so this always raised TypeError and calls were never
            # logged. Use the parameterized writer.
            self.exec_sql_param(sql, (number, call_type, duration))
            print(f"Logged call: {call_type} to/from {number} with status {status}")
            return True

        except Exception as e:
            pcat_logger.logger.error(f"Error logging call history: {e}")
            print(f"Error logging call history: {e}")
            return False

    def clear_call_history(self):
        """Clear all call history records from SQLite database."""
        try:
            self._ensure_call_history_table()
            
            sql = 'DELETE FROM call_history'
            self.exec_sql(sql)
            print("Call history cleared from database")
            return True

        except Exception as e:
            pcat_logger.logger.error(f"Error clearing call history: {e}")
            print(f"Error clearing call history: {e}")
            return False

    def delete_call_record(self, call_id):
        """Delete specific call record from SQLite database."""
        try:
            self._ensure_call_history_table()

            sql = 'DELETE FROM call_history WHERE id = ?'
            self.exec_sql_param(sql, (call_id,))
            print(f"Call record {call_id} deleted from database")
            return True

        except Exception as e:
            pcat_logger.logger.error(f"Error deleting call record: {e}")
            print(f"Error deleting call record: {e}")
            return False

    def _ensure_contacts_table(self):
        """Create phone_contacts table if it doesn't exist."""
        try:
            sql_create_table = '''
            CREATE TABLE IF NOT EXISTS phone_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                number TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            '''
            self.exec_sql(sql_create_table)
        except Exception as e:
            pcat_logger.logger.error(f"Error creating phone_contacts table: {e}")

    def get_contacts(self):
        """Get the phone address book from SQLite database, sorted by name."""
        try:
            self._ensure_contacts_table()

            sql = '''
            SELECT id, name, number
            FROM phone_contacts
            ORDER BY name COLLATE NOCASE ASC
            LIMIT 500
            '''
            from database import get_rows_param
            rows = get_rows_param(self.modem_db_path, sql)
            contacts = []
            for row in rows:
                contacts.append({
                    "id": str(row[0]),
                    "name": row[1],
                    "number": row[2]
                })
            return contacts

        except Exception as e:
            pcat_logger.logger.error(f"Error getting contacts: {e}")
            return []

    def add_contact(self, name, number):
        """Add a contact to the phone address book."""
        try:
            self._ensure_contacts_table()

            sql = 'INSERT INTO phone_contacts (name, number) VALUES (?, ?)'
            self.exec_sql_param(sql, (name, number))
            print(f"Contact added: {name} {number}")
            return True

        except Exception as e:
            pcat_logger.logger.error(f"Error adding contact: {e}")
            return False

    def update_contact(self, contact_id, name, number):
        """Update an existing contact in the phone address book."""
        try:
            self._ensure_contacts_table()

            sql = 'UPDATE phone_contacts SET name = ?, number = ? WHERE id = ?'
            self.exec_sql_param(sql, (name, number, contact_id))
            print(f"Contact {contact_id} updated: {name} {number}")
            return True

        except Exception as e:
            pcat_logger.logger.error(f"Error updating contact: {e}")
            return False

    def delete_contact(self, contact_id):
        """Delete a contact from the phone address book."""
        try:
            self._ensure_contacts_table()

            sql = 'DELETE FROM phone_contacts WHERE id = ?'
            self.exec_sql_param(sql, (contact_id,))
            print(f"Contact {contact_id} deleted")
            return True

        except Exception as e:
            pcat_logger.logger.error(f"Error deleting contact: {e}")
            return False

    def get_incoming_call_status(self):
        """Check for incoming call status and RING notifications."""
        try:
            # Get serial connection. PcModemClient has no get_serial(); the
            # port is reached through the same wait-for-connection helper every
            # other method here uses, so a modem that is still coming up is
            # waited out instead of raising.
            serial_obj = None
            with self.modem_client.serial_lock:
                serial_obj = self._wait_for_serial_connection(timeout_seconds=3.0)
                if not serial_obj:
                    return {
                        "active": False,
                        "number": None,
                        "ring_count": 0,
                        "duration": 0
                    }
            
            # Check call status using pysms_tool to see if there's an incoming call
            with self.modem_client.serial_lock:
                sms_tool = SMSTool(serial_port=serial_obj, debug=SMS_DEBUG_BOOL)
                # Use AT+CLCC to check for active calls
                response = sms_tool._send_command("AT+CLCC")
                
                if response and "OK" in response:
                    lines = response.split('\n')
                    for line in lines:
                        # Parse AT+CLCC response format: +CLCC: <idx>,<dir>,<stat>,<mode>,<mpty>,<number>,<type>
                        if line.startswith('+CLCC:'):
                            parts = line.replace('+CLCC: ', '').split(',')
                            if len(parts) >= 6:
                                direction = int(parts[1])  # 0=MO, 1=MT
                                status = int(parts[2])     # 4=incoming, 0=active, etc.
                                mode = int(parts[3])       # 0=voice, 1=data, 2=fax
                                number = parts[5].strip('"')
                                
                                # Incoming voice call: MT direction, ringing
                                # status, and a voice bearer -- see the note on
                                # VOICE_CALL_MODES, a data context can sit in
                                # this list too.
                                if direction == 1 and status == 4 and \
                                        mode in VOICE_CALL_MODES:
                                    return {
                                        "active": True,
                                        "number": number,
                                        "ring_count": 1,
                                        "duration": 0
                                    }
                
                # No incoming calls found
                return {
                    "active": False,
                    "number": None,
                    "ring_count": 0,
                    "duration": 0
                }
                
        except Exception as e:
            pcat_logger.logger.error(f"Error checking incoming call status: {e}")
            return {
                "active": False,
                "number": None,
                "ring_count": 0,
                "duration": 0
            }
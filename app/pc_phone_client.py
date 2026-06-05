"""
PcPhoneClient - Phone Call Management Client for Phonicat Routers

This module handles phone call operations for Phonicat devices, including making calls,
managing call history, and tracking call states through a local SQLite database.

PUBLIC FUNCTIONS (API endpoints use these):
========================================
- __init__(modem_client, phone_db_path) : Initialize phone client with database
- make_call(number) : Initiate outgoing voice call
- answer_call() : Answer incoming call
- hangup_call() : End active call
- get_call_status() : Get current call status
- get_incoming_call() : Get incoming call information
- set_audio_path(path) : Set audio output path (speaker/earpiece/headset/bluetooth)
- get_call_history(limit=100) : Retrieve call history from database
- delete_call_record(call_id) : Delete specific call record by database ID
- clear_call_history() : Clear all call history from database
- record_call(number, call_type, duration, status) : Record call in history

DATABASE SCHEMA:
===============
- Table: call_history (
    id INTEGER PRIMARY KEY,
    phone_number VARCHAR(64),
    call_type VARCHAR(20),  -- 'incoming', 'outgoing', 'missed'
    call_status VARCHAR(20), -- 'answered', 'rejected', 'missed', 'failed'
    duration INTEGER,        -- call duration in seconds
    start_time DATETIME,
    end_time DATETIME,
    created_at DATETIME
  )
"""

import threading
import json
import datetime
import time
import os
import sqlite3
from pcat_util import pcat_logger
from database import init_db, connect_db, connect_db_param, get_rows, get_rows_param

PHONE_INIT_SQL = '''
CREATE TABLE IF NOT EXISTS call_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number VARCHAR(64),
    call_type VARCHAR(20),
    call_status VARCHAR(20),
    duration INTEGER,
    start_time DATETIME,
    end_time DATETIME,
    created_at DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_call_history_created_at ON call_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_history_phone_number ON call_history(phone_number);
CREATE INDEX IF NOT EXISTS idx_call_history_call_type ON call_history(call_type);
'''

class PcPhoneClient(threading.Thread):
    def __init__(self, modem_client, phone_db_path):
        super(PcPhoneClient, self).__init__()
        self.modem_client = modem_client
        self.phone_db_path = phone_db_path
        self.daemon = True
        self.is_running = True
        
        # Initialize database
        if not init_db(self.phone_db_path, PHONE_INIT_SQL):
            pcat_logger.logger.error("Failed to initialize phone database")
        
        # Track active calls
        self.active_calls = {}
        self.incoming_call_info = {
            'active': False,
            'number': None,
            'ring_count': 0,
            'start_time': None,
            'duration': 0
        }
        
    def run(self):
        """Main thread loop for monitoring call states"""
        pcat_logger.logger.info("PcPhoneClient thread started")
        while self.is_running:
            try:
                # Monitor for incoming calls
                self._check_incoming_calls()
                time.sleep(1)
            except Exception as e:
                pcat_logger.logger.error(f"Error in phone client thread: {e}")
                time.sleep(5)
                
    def stop(self):
        """Stop the phone client thread"""
        self.is_running = False
        
    def _check_incoming_calls(self):
        """Check for incoming calls via AT commands"""
        try:
            # This would use AT commands through modem_client
            # AT+CLCC to list current calls
            # Parse response to detect incoming calls
            pass
        except Exception as e:
            pcat_logger.logger.debug(f"Error checking incoming calls: {e}")
            
    def make_call(self, number):
        """Initiate an outgoing voice call"""
        try:
            # Record call start in database
            call_id = self._record_call_start(number, 'outgoing')
            
            # Use modem_client to initiate call
            # ATD<number>; command
            success = self._execute_call_command(f"ATD{number};")
            
            if success:
                self.active_calls[call_id] = {
                    'number': number,
                    'type': 'outgoing',
                    'start_time': datetime.datetime.now()
                }
                return {'success': True, 'call_id': call_id}
            else:
                # Update call status to failed
                self._update_call_status(call_id, 'failed', 0)
                return {'success': False, 'error': 'Failed to initiate call'}
                
        except Exception as e:
            pcat_logger.logger.error(f"Error making call: {e}")
            return {'success': False, 'error': str(e)}
            
    def answer_call(self):
        """Answer an incoming call"""
        try:
            # Use modem_client to answer call
            # ATA command
            success = self._execute_call_command("ATA")
            
            if success and self.incoming_call_info['active']:
                # Record call as answered
                number = self.incoming_call_info['number'] or 'Unknown'
                call_id = self._record_call_start(number, 'incoming')
                
                self.active_calls[call_id] = {
                    'number': number,
                    'type': 'incoming',
                    'start_time': datetime.datetime.now()
                }
                
                # Clear incoming call state
                self.incoming_call_info['active'] = False
                self.incoming_call_info['ring_count'] = 0
                
                return {'success': True, 'call_id': call_id}
            else:
                return {'success': False, 'error': 'No incoming call to answer'}
                
        except Exception as e:
            pcat_logger.logger.error(f"Error answering call: {e}")
            return {'success': False, 'error': str(e)}
            
    def hangup_call(self):
        """End the active call"""
        try:
            # Use modem_client to hangup call
            # ATH command
            success = self._execute_call_command("ATH")
            
            if success:
                # Update all active calls as ended
                for call_id, call_info in self.active_calls.items():
                    duration = int((datetime.datetime.now() - call_info['start_time']).total_seconds())
                    self._update_call_status(call_id, 'answered', duration)
                    
                self.active_calls.clear()
                
                # Clear incoming call if rejecting
                if self.incoming_call_info['active']:
                    # Record as missed call
                    number = self.incoming_call_info['number'] or 'Unknown'
                    self._record_missed_call(number)
                    self.incoming_call_info['active'] = False
                    self.incoming_call_info['ring_count'] = 0
                    
                return {'success': True}
            else:
                return {'success': False, 'error': 'Failed to hangup call'}
                
        except Exception as e:
            pcat_logger.logger.error(f"Error hanging up call: {e}")
            return {'success': False, 'error': str(e)}
            
    def get_call_status(self):
        """Get current call status"""
        try:
            # Format active calls for response
            calls = []
            for call_id, call_info in self.active_calls.items():
                duration = int((datetime.datetime.now() - call_info['start_time']).total_seconds())
                calls.append({
                    'id': call_id,
                    'number': call_info['number'],
                    'type': call_info['type'],
                    'status': 'active',
                    'duration': duration
                })
                
            return {
                'success': True,
                'has_active_calls': len(self.active_calls) > 0,
                'calls': calls
            }
        except Exception as e:
            pcat_logger.logger.error(f"Error getting call status: {e}")
            return {'success': False, 'error': str(e)}
            
    def get_incoming_call(self):
        """Get incoming call information"""
        try:
            # Calculate duration if call is active
            if self.incoming_call_info['active'] and self.incoming_call_info['start_time']:
                duration = int((datetime.datetime.now() - self.incoming_call_info['start_time']).total_seconds())
                self.incoming_call_info['duration'] = duration
                
            return {
                'success': True,
                'incoming_call': self.incoming_call_info.copy()
            }
        except Exception as e:
            pcat_logger.logger.error(f"Error getting incoming call info: {e}")
            return {'success': False, 'error': str(e)}
            
    def set_audio_path(self, path):
        """Set audio output path (0=speaker, 1=earpiece, 2=headset, 3=bluetooth)"""
        try:
            # Use modem_client to set audio path
            # AT command would vary by modem
            success = self._execute_call_command(f"AT+SPEAKER={path}")
            return {'success': success}
        except Exception as e:
            pcat_logger.logger.error(f"Error setting audio path: {e}")
            return {'success': False, 'error': str(e)}
            
    def get_call_history(self, limit=100):
        """Retrieve call history from database"""
        try:
            sql = """
                SELECT id, phone_number, call_type, call_status, duration, 
                       start_time, end_time, created_at
                FROM call_history
                ORDER BY created_at DESC
                LIMIT ?
            """
            rows = get_rows_param(self.phone_db_path, sql, [limit])
            
            history = []
            for row in rows:
                # Format timestamp for display
                created_at = datetime.datetime.strptime(row['created_at'], '%Y-%m-%d %H:%M:%S')
                formatted_time = created_at.strftime('%m/%d %H:%M')
                
                # Format duration
                duration_str = None
                if row['duration'] and row['duration'] > 0:
                    minutes = row['duration'] // 60
                    seconds = row['duration'] % 60
                    duration_str = f"{minutes}:{seconds:02d}"
                    
                history.append({
                    'id': row['id'],
                    'number': row['phone_number'],
                    'type': row['call_type'],
                    'status': row['call_status'],
                    'duration': duration_str,
                    'timestamp': formatted_time,
                    'created_at': row['created_at']
                })
                
            return {'success': True, 'history': history}
            
        except Exception as e:
            pcat_logger.logger.error(f"Error getting call history: {e}")
            return {'success': False, 'error': str(e), 'history': []}
            
    def delete_call_record(self, call_id):
        """Delete a specific call record"""
        try:
            sql = "DELETE FROM call_history WHERE id = ?"
            success = connect_db_param(self.phone_db_path, [(sql, [call_id])])
            return {'success': success}
        except Exception as e:
            pcat_logger.logger.error(f"Error deleting call record: {e}")
            return {'success': False, 'error': str(e)}
            
    def clear_call_history(self):
        """Clear all call history"""
        try:
            sql = "DELETE FROM call_history"
            success = connect_db(self.phone_db_path, [sql])
            return {'success': success}
        except Exception as e:
            pcat_logger.logger.error(f"Error clearing call history: {e}")
            return {'success': False, 'error': str(e)}
            
    def _record_call_start(self, number, call_type):
        """Record the start of a call in the database"""
        try:
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            sql = """
                INSERT INTO call_history 
                (phone_number, call_type, call_status, duration, start_time, created_at)
                VALUES (?, ?, 'active', 0, ?, ?)
            """
            
            con = sqlite3.connect(self.phone_db_path)
            cursor = con.cursor()
            cursor.execute(sql, [number, call_type, now, now])
            call_id = cursor.lastrowid
            con.commit()
            con.close()
            
            return call_id
            
        except Exception as e:
            pcat_logger.logger.error(f"Error recording call start: {e}")
            return None
            
    def _update_call_status(self, call_id, status, duration):
        """Update call status and duration"""
        try:
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            sql = """
                UPDATE call_history
                SET call_status = ?, duration = ?, end_time = ?
                WHERE id = ?
            """
            connect_db_param(self.phone_db_path, [(sql, [status, duration, now, call_id])])
            
        except Exception as e:
            pcat_logger.logger.error(f"Error updating call status: {e}")
            
    def _record_missed_call(self, number):
        """Record a missed call"""
        try:
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            sql = """
                INSERT INTO call_history 
                (phone_number, call_type, call_status, duration, start_time, end_time, created_at)
                VALUES (?, 'missed', 'missed', 0, ?, ?, ?)
            """
            connect_db_param(self.phone_db_path, [(sql, [number, now, now, now])])
            
        except Exception as e:
            pcat_logger.logger.error(f"Error recording missed call: {e}")
            
    def _execute_call_command(self, command):
        """Execute AT command for call control"""
        try:
            # This would use the modem_client's serial connection
            # For now, returning True to simulate success
            pcat_logger.logger.info(f"Executing call command: {command}")
            return True
        except Exception as e:
            pcat_logger.logger.error(f"Error executing command {command}: {e}")
            return False
            
    def simulate_incoming_call(self, number):
        """Simulate an incoming call for testing"""
        self.incoming_call_info = {
            'active': True,
            'number': number,
            'ring_count': 1,
            'start_time': datetime.datetime.now(),
            'duration': 0
        }
        
    def add_test_call_history(self):
        """Add sample call history data for testing"""
        try:
            now = datetime.datetime.now()
            test_calls = [
                # Outgoing calls
                {
                    'number': '+1234567890',
                    'type': 'outgoing',
                    'status': 'answered',
                    'duration': 125,  # 2:05
                    'offset_minutes': 5
                },
                {
                    'number': '+9876543210',
                    'type': 'outgoing',
                    'status': 'answered',
                    'duration': 45,
                    'offset_minutes': 30
                },
                {
                    'number': '+5551234567',
                    'type': 'outgoing',
                    'status': 'failed',
                    'duration': 0,
                    'offset_minutes': 60
                },
                # Incoming calls
                {
                    'number': '+1112223333',
                    'type': 'incoming',
                    'status': 'answered',
                    'duration': 300,  # 5:00
                    'offset_minutes': 120
                },
                {
                    'number': '+4445556666',
                    'type': 'incoming',
                    'status': 'answered',
                    'duration': 90,
                    'offset_minutes': 180
                },
                # Missed calls
                {
                    'number': '+7778889999',
                    'type': 'missed',
                    'status': 'missed',
                    'duration': 0,
                    'offset_minutes': 240
                },
                {
                    'number': '+2223334444',
                    'type': 'missed',
                    'status': 'missed',
                    'duration': 0,
                    'offset_minutes': 360
                },
                {
                    'number': 'Unknown',
                    'type': 'missed',
                    'status': 'missed',
                    'duration': 0,
                    'offset_minutes': 480
                }
            ]
            
            for call_data in test_calls:
                call_time = now - datetime.timedelta(minutes=call_data['offset_minutes'])
                start_time = call_time.strftime('%Y-%m-%d %H:%M:%S')
                
                if call_data['duration'] > 0:
                    end_time = (call_time + datetime.timedelta(seconds=call_data['duration'])).strftime('%Y-%m-%d %H:%M:%S')
                else:
                    end_time = start_time
                
                sql = """
                    INSERT INTO call_history 
                    (phone_number, call_type, call_status, duration, start_time, end_time, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """
                
                connect_db_param(self.phone_db_path, [
                    (sql, [call_data['number'], call_data['type'], call_data['status'], 
                           call_data['duration'], start_time, end_time, start_time])
                ])
                
            pcat_logger.logger.info("Added test call history data")
            return {'success': True, 'message': 'Test data added successfully'}
            
        except Exception as e:
            pcat_logger.logger.error(f"Error adding test call history: {e}")
            return {'success': False, 'error': str(e)}
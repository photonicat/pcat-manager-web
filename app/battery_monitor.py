#@Author    : battery monitoring system

import sqlite3
import os
import time
import threading
from datetime import datetime, timedelta
from pcat_util import pcat_logger
import database

# Battery monitoring configuration
BATTERY_DB_PATH = "/tmp/battery_data.db"
BATTERY_COLLECTION_INTERVAL = 5 * 60  # 5 minutes default, adjustable
BATTERY_DATA_RETENTION_DAYS = 30  # Keep 30 days of data

# Battery data collection thread
battery_collection_thread = None
battery_collection_running = False
battery_collection_lock = threading.Lock()

# Battery database schema - Enhanced to store all parameters
BATTERY_DB_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS battery_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    voltage REAL NOT NULL,
    current REAL NOT NULL,
    soc INTEGER NOT NULL,
    temperature REAL,
    power REAL,
    energy REAL,
    capacity_remaining REAL,
    capacity_full REAL,
    health_percentage REAL,
    status TEXT,
    technology TEXT,
    manufacturer TEXT,
    model TEXT,
    serial_number TEXT,
    charge_cycles INTEGER,
    design_capacity REAL,
    present INTEGER DEFAULT 1,
    online INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_battery_timestamp ON battery_readings(timestamp);
CREATE INDEX IF NOT EXISTS idx_battery_created_at ON battery_readings(created_at);
CREATE INDEX IF NOT EXISTS idx_battery_soc ON battery_readings(soc);
CREATE INDEX IF NOT EXISTS idx_battery_status ON battery_readings(status);

CREATE TABLE IF NOT EXISTS battery_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_start_timestamp INTEGER NOT NULL,
    cycle_end_timestamp INTEGER,
    start_soc INTEGER NOT NULL,
    end_soc INTEGER,
    start_voltage REAL,
    end_voltage REAL,
    discharge_depth REAL,
    energy_discharged REAL,
    energy_charged REAL,
    efficiency REAL,
    cycle_completed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_battery_cycles_start ON battery_cycles(cycle_start_timestamp);
CREATE INDEX IF NOT EXISTS idx_battery_cycles_completed ON battery_cycles(cycle_completed);

CREATE TABLE IF NOT EXISTS battery_parameters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parameter_name TEXT NOT NULL,
    parameter_value TEXT NOT NULL,
    parameter_type TEXT DEFAULT 'string',
    timestamp INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_battery_params_name ON battery_parameters(parameter_name);
CREATE INDEX IF NOT EXISTS idx_battery_params_timestamp ON battery_parameters(timestamp);

CREATE TABLE IF NOT EXISTS battery_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved INTEGER DEFAULT 0,
    timestamp INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_battery_alerts_type ON battery_alerts(alert_type);
CREATE INDEX IF NOT EXISTS idx_battery_alerts_resolved ON battery_alerts(resolved);
CREATE INDEX IF NOT EXISTS idx_battery_alerts_timestamp ON battery_alerts(timestamp);
"""

def init_battery_db():
    """Initialize battery monitoring database"""
    try:
        success = database.init_db(BATTERY_DB_PATH, BATTERY_DB_CREATE_SQL)
        if success:
            pcat_logger.logger.info("Battery database initialized successfully")
            # Clean old data on startup
            cleanup_old_battery_data()
            return True
        else:
            pcat_logger.logger.error("Failed to initialize battery database")
            return False
    except Exception as e:
        pcat_logger.logger.error(f"Error initializing battery database: {e}")
        return False

def cleanup_old_battery_data():
    """Remove battery data older than retention period"""
    try:
        cutoff_timestamp = int(time.time()) - (BATTERY_DATA_RETENTION_DAYS * 24 * 60 * 60)
        
        con = sqlite3.connect(BATTERY_DB_PATH)
        cursor = con.cursor()
        
        # Delete old readings
        cursor.execute("DELETE FROM battery_readings WHERE timestamp < ?", (cutoff_timestamp,))
        deleted_readings = cursor.rowcount
        
        # Delete old incomplete cycles
        cursor.execute("DELETE FROM battery_cycles WHERE cycle_start_timestamp < ? AND cycle_completed = 0", (cutoff_timestamp,))
        deleted_incomplete_cycles = cursor.rowcount
        
        # Delete old completed cycles (keep last 100 cycles regardless of age)
        cursor.execute("""
            DELETE FROM battery_cycles 
            WHERE cycle_start_timestamp < ? 
            AND cycle_completed = 1 
            AND id NOT IN (
                SELECT id FROM battery_cycles 
                WHERE cycle_completed = 1 
                ORDER BY cycle_start_timestamp DESC 
                LIMIT 100
            )
        """, (cutoff_timestamp,))
        deleted_old_cycles = cursor.rowcount
        
        # Delete old parameters
        cursor.execute("DELETE FROM battery_parameters WHERE timestamp < ?", (cutoff_timestamp,))
        deleted_parameters = cursor.rowcount
        
        # Delete old resolved alerts
        cursor.execute("DELETE FROM battery_alerts WHERE timestamp < ? AND resolved = 1", (cutoff_timestamp,))
        deleted_alerts = cursor.rowcount
        
        # Vacuum database to reclaim space
        cursor.execute("VACUUM")
        
        con.commit()
        con.close()
        
        total_deleted = deleted_readings + deleted_incomplete_cycles + deleted_old_cycles + deleted_parameters + deleted_alerts
        if total_deleted > 0:
            pcat_logger.logger.info(f"Database cleanup completed: {deleted_readings} readings, {deleted_incomplete_cycles} incomplete cycles, {deleted_old_cycles} old cycles, {deleted_parameters} parameters, {deleted_alerts} alerts deleted")
            
    except Exception as e:
        pcat_logger.logger.error(f"Error cleaning up old battery data: {e}")

def get_comprehensive_battery_data():
    """Get comprehensive battery data from all available sources
    This function reads from /sys/class/power_supply/ and other hardware sources
    """
    try:
        # Base battery paths (common locations)
        battery_paths = [
            "/sys/class/power_supply/BAT0",
            "/sys/class/power_supply/BAT1", 
            "/sys/class/power_supply/battery",
            "/sys/class/power_supply/axp20x-battery"
        ]
        
        battery_data = {}
        battery_path = None
        
        # Find the actual battery path
        for path in battery_paths:
            if os.path.exists(path):
                battery_path = path
                break
        
        if battery_path:
            # Read all available parameters
            param_files = {
                'voltage_now': 'voltage',
                'current_now': 'current', 
                'capacity': 'soc',
                'temp': 'temperature',
                'power_now': 'power',
                'energy_now': 'energy',
                'energy_full': 'capacity_remaining',
                'energy_full_design': 'capacity_full',
                'health': 'health_percentage',
                'status': 'status',
                'technology': 'technology',
                'manufacturer': 'manufacturer',
                'model_name': 'model',
                'serial_number': 'serial_number',
                'cycle_count': 'charge_cycles',
                'charge_full_design': 'design_capacity',
                'present': 'present',
                'online': 'online'
            }
            
            for file_name, param_name in param_files.items():
                file_path = os.path.join(battery_path, file_name)
                try:
                    if os.path.exists(file_path):
                        with open(file_path, 'r') as f:
                            value = f.read().strip()
                            
                            # Convert values based on parameter type
                            if param_name in ['voltage', 'current', 'power', 'energy', 'capacity_remaining', 'capacity_full', 'design_capacity']:
                                # Convert microunits to standard units
                                battery_data[param_name] = float(value) / 1000000 if value.isdigit() or value.replace('-', '').isdigit() else 0.0
                            elif param_name in ['soc', 'health_percentage', 'charge_cycles', 'present', 'online']:
                                battery_data[param_name] = int(value) if value.isdigit() else 0
                            elif param_name == 'temperature':
                                # Temperature is usually in tenths of degrees Celsius
                                battery_data[param_name] = float(value) / 10.0 if value.isdigit() else 0.0
                            else:
                                battery_data[param_name] = value
                                
                except (IOError, ValueError) as e:
                    pcat_logger.logger.debug(f"Could not read {file_path}: {e}")
                    
        # If no real battery found, use simulated data for development
        if not battery_data:
            import random
            battery_data = {
                'voltage': round(3.7 + random.uniform(-0.3, 0.3), 3),
                'current': round(random.uniform(-2.5, 2.5), 3),
                'soc': random.randint(10, 100),
                'temperature': round(random.uniform(25.0, 45.0), 1),
                'power': round(random.uniform(0.5, 15.0), 2),
                'energy': round(random.uniform(10.0, 50.0), 2),
                'capacity_remaining': round(random.uniform(40.0, 50.0), 2),
                'capacity_full': 50.0,
                'health_percentage': random.randint(80, 100),
                'status': random.choice(['Charging', 'Discharging', 'Not charging', 'Full']),
                'technology': 'Li-ion',
                'manufacturer': 'Simulated',
                'model': 'DEV-BATTERY',
                'serial_number': 'SIM001',
                'charge_cycles': random.randint(50, 500),
                'design_capacity': 50.0,
                'present': 1,
                'online': 1
            }
            
        battery_data['timestamp'] = int(time.time())
        return battery_data
        
    except Exception as e:
        pcat_logger.logger.error(f"Error getting comprehensive battery data: {e}")
        # Return minimal data structure
        return {
            'voltage': 0.0,
            'current': 0.0, 
            'soc': 0,
            'temperature': 0.0,
            'timestamp': int(time.time())
        }

def get_battery_hardware_data():
    """Get current battery data from hardware - now uses comprehensive data"""
    return get_comprehensive_battery_data()

def record_battery_data():
    """Record comprehensive battery data to database"""
    try:
        data = get_battery_hardware_data()
        
        con = sqlite3.connect(BATTERY_DB_PATH)
        cursor = con.cursor()
        
        # Insert comprehensive battery reading
        cursor.execute("""
            INSERT INTO battery_readings (
                timestamp, voltage, current, soc, temperature, power, energy,
                capacity_remaining, capacity_full, health_percentage, status,
                technology, manufacturer, model, serial_number, charge_cycles,
                design_capacity, present, online
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get('timestamp', int(time.time())),
            data.get('voltage', 0.0),
            data.get('current', 0.0), 
            data.get('soc', 0),
            data.get('temperature', 0.0),
            data.get('power', 0.0),
            data.get('energy', 0.0),
            data.get('capacity_remaining', 0.0),
            data.get('capacity_full', 0.0),
            data.get('health_percentage', 0),
            data.get('status', 'Unknown'),
            data.get('technology', 'Unknown'),
            data.get('manufacturer', 'Unknown'),
            data.get('model', 'Unknown'),
            data.get('serial_number', 'Unknown'),
            data.get('charge_cycles', 0),
            data.get('design_capacity', 0.0),
            data.get('present', 1),
            data.get('online', 1)
        ))
        
        # Store individual parameters for flexibility
        for param_name, param_value in data.items():
            if param_name != 'timestamp':
                param_type = 'float' if isinstance(param_value, float) else 'int' if isinstance(param_value, int) else 'string'
                cursor.execute("""
                    INSERT INTO battery_parameters (parameter_name, parameter_value, parameter_type, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (param_name, str(param_value), param_type, data['timestamp']))
        
        con.commit()
        con.close()
        
        # Update battery cycles and check for alerts
        update_battery_cycles(data)
        check_battery_alerts(data)
        
        pcat_logger.logger.debug(f"Comprehensive battery data recorded: V={data.get('voltage', 0)}V, I={data.get('current', 0)}A, SOC={data.get('soc', 0)}%, Status={data.get('status', 'Unknown')}")
        return True
        
    except Exception as e:
        pcat_logger.logger.error(f"Error recording battery data: {e}")
        return False

def check_battery_alerts(current_data):
    """Check for battery alert conditions and log them"""
    try:
        con = sqlite3.connect(BATTERY_DB_PATH)
        cursor = con.cursor()
        timestamp = current_data['timestamp']
        
        alerts = []
        
        # Check critical conditions
        soc = current_data.get('soc', 100)
        temp = current_data.get('temperature', 25.0)
        voltage = current_data.get('voltage', 3.7)
        health = current_data.get('health_percentage', 100)
        
        if soc <= 10:
            alerts.append(('low_battery', 'critical', f'Battery critically low: {soc}%'))
        elif soc <= 20:
            alerts.append(('low_battery', 'warning', f'Battery low: {soc}%'))
            
        if temp >= 45.0:
            alerts.append(('high_temperature', 'critical', f'Battery overheating: {temp}°C'))
        elif temp >= 40.0:
            alerts.append(('high_temperature', 'warning', f'Battery temperature high: {temp}°C'))
            
        if temp <= 0.0:
            alerts.append(('low_temperature', 'warning', f'Battery temperature low: {temp}°C'))
            
        if voltage <= 3.0:
            alerts.append(('low_voltage', 'critical', f'Battery voltage critically low: {voltage}V'))
        elif voltage <= 3.3:
            alerts.append(('low_voltage', 'warning', f'Battery voltage low: {voltage}V'))
            
        if health <= 50:
            alerts.append(('poor_health', 'critical', f'Battery health poor: {health}%'))
        elif health <= 70:
            alerts.append(('poor_health', 'warning', f'Battery health degraded: {health}%'))
        
        # Insert new alerts
        for alert_type, severity, message in alerts:
            cursor.execute("""
                INSERT INTO battery_alerts (alert_type, severity, message, timestamp)
                VALUES (?, ?, ?, ?)
            """, (alert_type, severity, message, timestamp))
            
        if alerts:
            con.commit()
            pcat_logger.logger.warning(f"Battery alerts generated: {len(alerts)} alerts")
        
        con.close()
        
    except Exception as e:
        pcat_logger.logger.error(f"Error checking battery alerts: {e}")

def update_battery_cycles(current_data):
    """Update battery cycle tracking based on current reading"""
    try:
        con = sqlite3.connect(BATTERY_DB_PATH)
        cursor = con.cursor()
        
        # Get the most recent incomplete cycle
        cursor.execute("""
            SELECT id, start_soc, cycle_start_timestamp 
            FROM battery_cycles 
            WHERE cycle_completed = 0 
            ORDER BY cycle_start_timestamp DESC 
            LIMIT 1
        """)
        
        current_cycle = cursor.fetchone()
        current_soc = current_data['soc']
        current_timestamp = current_data['timestamp']
        
        if current_cycle is None:
            # Start new cycle if SOC is high (above 80%)
            if current_soc >= 80:
                cursor.execute("""
                    INSERT INTO battery_cycles (cycle_start_timestamp, start_soc)
                    VALUES (?, ?)
                """, (current_timestamp, current_soc))
                pcat_logger.logger.debug(f"Started new battery cycle at SOC {current_soc}%")
        else:
            cycle_id, start_soc, start_timestamp = current_cycle
            
            # Complete cycle if we've discharged significantly and then recharged back up
            if start_soc >= 80 and current_soc >= 80:
                # Check if we had a significant discharge in between
                cursor.execute("""
                    SELECT MIN(soc) as min_soc 
                    FROM battery_readings 
                    WHERE timestamp BETWEEN ? AND ?
                """, (start_timestamp, current_timestamp))
                
                result = cursor.fetchone()
                min_soc = result[0] if result and result[0] is not None else start_soc
                
                # If we discharged at least 20% and recharged back
                if min_soc <= start_soc - 20:
                    discharge_depth = (start_soc - min_soc) / 100.0
                    
                    cursor.execute("""
                        UPDATE battery_cycles 
                        SET cycle_end_timestamp = ?, end_soc = ?, discharge_depth = ?, cycle_completed = 1
                        WHERE id = ?
                    """, (current_timestamp, current_soc, discharge_depth, cycle_id))
                    
                    pcat_logger.logger.info(f"Completed battery cycle: {discharge_depth*100:.1f}% discharge depth")
                    
                    # Start new cycle
                    cursor.execute("""
                        INSERT INTO battery_cycles (cycle_start_timestamp, start_soc)
                        VALUES (?, ?)
                    """, (current_timestamp, current_soc))
        
        con.commit()
        con.close()
        
    except Exception as e:
        pcat_logger.logger.error(f"Error updating battery cycles: {e}")

def get_battery_data_for_period(hours=24):
    """Get battery data for the specified period"""
    try:
        end_time = int(time.time())
        start_time = end_time - (hours * 60 * 60)
        
        con = sqlite3.connect(BATTERY_DB_PATH)
        cursor = con.cursor()
        
        cursor.execute("""
            SELECT timestamp, voltage, current, soc, temperature
            FROM battery_readings 
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
        """, (start_time, end_time))
        
        rows = cursor.fetchall()
        con.close()
        
        data = []
        for row in rows:
            data.append({
                'timestamp': row[0],
                'voltage': row[1],
                'current': row[2],
                'soc': row[3],
                'temperature': row[4]
            })
        
        return data
        
    except Exception as e:
        pcat_logger.logger.error(f"Error getting battery data for period: {e}")
        return []

def get_battery_cycles():
    """Get battery cycle history"""
    try:
        con = sqlite3.connect(BATTERY_DB_PATH)
        cursor = con.cursor()
        
        cursor.execute("""
            SELECT cycle_start_timestamp, cycle_end_timestamp, start_soc, end_soc, 
                   discharge_depth, cycle_completed
            FROM battery_cycles 
            ORDER BY cycle_start_timestamp DESC
            LIMIT 50
        """)
        
        rows = cursor.fetchall()
        con.close()
        
        cycles = []
        for row in rows:
            cycles.append({
                'start_timestamp': row[0],
                'end_timestamp': row[1],
                'start_soc': row[2],
                'end_soc': row[3],
                'discharge_depth': row[4],
                'completed': row[5] == 1
            })
        
        return cycles
        
    except Exception as e:
        pcat_logger.logger.error(f"Error getting battery cycles: {e}")
        return []

def get_battery_stats():
    """Get battery statistics"""
    try:
        con = sqlite3.connect(BATTERY_DB_PATH)
        cursor = con.cursor()
        
        # Get latest reading
        cursor.execute("""
            SELECT voltage, current, soc, temperature, timestamp
            FROM battery_readings 
            ORDER BY timestamp DESC 
            LIMIT 1
        """)
        latest = cursor.fetchone()
        
        # Get cycle count
        cursor.execute("SELECT COUNT(*) FROM battery_cycles WHERE cycle_completed = 1")
        cycle_count = cursor.fetchone()[0]
        
        # Get average discharge depth
        cursor.execute("SELECT AVG(discharge_depth) FROM battery_cycles WHERE cycle_completed = 1")
        avg_discharge = cursor.fetchone()[0]
        
        con.close()
        
        stats = {
            'cycle_count': cycle_count,
            'avg_discharge_depth': round(avg_discharge * 100, 1) if avg_discharge else 0,
            'latest': {
                'voltage': latest[0] if latest else 0,
                'current': latest[1] if latest else 0,
                'soc': latest[2] if latest else 0,
                'temperature': latest[3] if latest else 0,
                'timestamp': latest[4] if latest else 0
            } if latest else None
        }
        
        return stats
        
    except Exception as e:
        pcat_logger.logger.error(f"Error getting battery stats: {e}")
        return {'cycle_count': 0, 'avg_discharge_depth': 0, 'latest': None}

def battery_collection_worker():
    """Background worker for battery data collection with maintenance scheduling"""
    global battery_collection_running
    
    pcat_logger.logger.info(f"Battery data collection started with {BATTERY_COLLECTION_INTERVAL}s interval")
    
    last_cleanup_time = time.time()
    cleanup_interval = 24 * 60 * 60  # Run cleanup once per day
    
    while battery_collection_running:
        try:
            # Record battery data
            record_battery_data()
            
            # Check if it's time for database maintenance
            current_time = time.time()
            if current_time - last_cleanup_time >= cleanup_interval:
                pcat_logger.logger.info("Running scheduled database maintenance...")
                cleanup_old_battery_data()
                last_cleanup_time = current_time
            
            time.sleep(BATTERY_COLLECTION_INTERVAL)
            
        except Exception as e:
            pcat_logger.logger.error(f"Error in battery collection worker: {e}")
            time.sleep(60)  # Wait 1 minute before retrying

def start_battery_monitoring():
    """Start battery data collection"""
    global battery_collection_thread, battery_collection_running
    
    with battery_collection_lock:
        if battery_collection_running:
            pcat_logger.logger.warning("Battery monitoring already running")
            return False
        
        if not init_battery_db():
            pcat_logger.logger.error("Failed to initialize battery database")
            return False
        
        battery_collection_running = True
        battery_collection_thread = threading.Thread(target=battery_collection_worker, daemon=True)
        battery_collection_thread.start()
        
        pcat_logger.logger.info("Battery monitoring started")
        return True

def stop_battery_monitoring():
    """Stop battery data collection"""
    global battery_collection_running
    
    with battery_collection_lock:
        if not battery_collection_running:
            pcat_logger.logger.warning("Battery monitoring not running")
            return
        
        battery_collection_running = False
        pcat_logger.logger.info("Battery monitoring stopped")

def set_collection_interval(seconds):
    """Set battery data collection interval"""
    global BATTERY_COLLECTION_INTERVAL
    
    if seconds < 60:  # Minimum 1 minute
        seconds = 60
    elif seconds > 1800:  # Maximum 30 minutes
        seconds = 1800
    
    BATTERY_COLLECTION_INTERVAL = seconds
    pcat_logger.logger.info(f"Battery collection interval set to {seconds} seconds")
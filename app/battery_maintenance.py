#!/usr/bin/env python3

"""
Battery Database Maintenance Utility
Provides manual database maintenance and analysis tools
"""

import sys
import os
import sqlite3
import time
from datetime import datetime, timedelta

# Add the app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import battery_monitor
from pcat_util import pcat_logger

def show_database_stats():
    """Show comprehensive database statistics"""
    try:
        con = sqlite3.connect(battery_monitor.BATTERY_DB_PATH)
        cursor = con.cursor()
        
        print("=" * 60)
        print("BATTERY DATABASE STATISTICS")
        print("=" * 60)
        
        # Battery readings stats
        cursor.execute("SELECT COUNT(*) FROM battery_readings")
        readings_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM battery_readings")
        time_range = cursor.fetchone()
        if time_range[0] and time_range[1]:
            start_date = datetime.fromtimestamp(time_range[0])
            end_date = datetime.fromtimestamp(time_range[1])
            days_span = (end_date - start_date).days
        else:
            start_date = end_date = None
            days_span = 0
        
        print(f"Battery Readings: {readings_count:,} records")
        if start_date:
            print(f"Date Range: {start_date.strftime('%Y-%m-%d %H:%M')} to {end_date.strftime('%Y-%m-%d %H:%M')}")
            print(f"Span: {days_span} days")
        
        # Cycles stats
        cursor.execute("SELECT COUNT(*) FROM battery_cycles WHERE cycle_completed = 1")
        completed_cycles = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM battery_cycles WHERE cycle_completed = 0")
        incomplete_cycles = cursor.fetchone()[0]
        
        cursor.execute("SELECT AVG(discharge_depth) FROM battery_cycles WHERE cycle_completed = 1")
        avg_depth = cursor.fetchone()[0]
        
        print(f"\nBattery Cycles: {completed_cycles} completed, {incomplete_cycles} incomplete")
        if avg_depth:
            print(f"Average Discharge Depth: {avg_depth*100:.1f}%")
        
        # Parameters stats
        cursor.execute("SELECT COUNT(*) FROM battery_parameters")
        params_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(DISTINCT parameter_name) FROM battery_parameters")
        unique_params = cursor.fetchone()[0]
        
        print(f"\nBattery Parameters: {params_count:,} records, {unique_params} unique parameters")
        
        # Alerts stats
        cursor.execute("SELECT COUNT(*) FROM battery_alerts WHERE resolved = 0")
        active_alerts = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM battery_alerts WHERE resolved = 1")
        resolved_alerts = cursor.fetchone()[0]
        
        print(f"\nBattery Alerts: {active_alerts} active, {resolved_alerts} resolved")
        
        # Recent data sample
        cursor.execute("""
            SELECT timestamp, voltage, current, soc, status 
            FROM battery_readings 
            ORDER BY timestamp DESC 
            LIMIT 5
        """)
        recent_data = cursor.fetchall()
        
        print(f"\nRecent Readings:")
        print("Timestamp           Voltage  Current    SOC  Status")
        print("-" * 55)
        for row in recent_data:
            ts = datetime.fromtimestamp(row[0])
            print(f"{ts.strftime('%Y-%m-%d %H:%M:%S')}  {row[1]:6.2f}V  {row[2]:7.3f}A  {row[3]:3d}%  {row[4]}")
        
        con.close()
        
    except Exception as e:
        print(f"Error showing database stats: {e}")

def cleanup_database(days_to_keep=30, dry_run=False):
    """Clean up old database records"""
    try:
        cutoff_timestamp = int(time.time()) - (days_to_keep * 24 * 60 * 60)
        cutoff_date = datetime.fromtimestamp(cutoff_timestamp)
        
        print(f"Cleaning up data older than {days_to_keep} days (before {cutoff_date.strftime('%Y-%m-%d %H:%M:%S')})")
        
        if dry_run:
            print("DRY RUN MODE - No changes will be made")
        
        con = sqlite3.connect(battery_monitor.BATTERY_DB_PATH)
        cursor = con.cursor()
        
        # Count records to be deleted
        tables_to_clean = [
            ("battery_readings", "timestamp"),
            ("battery_parameters", "timestamp"),
            ("battery_alerts", "timestamp")
        ]
        
        total_to_delete = 0
        for table_name, time_column in tables_to_clean:
            cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {time_column} < ?", (cutoff_timestamp,))
            count = cursor.fetchone()[0]
            print(f"{table_name}: {count:,} records to delete")
            total_to_delete += count
        
        # Count cycles to delete (incomplete ones)
        cursor.execute("SELECT COUNT(*) FROM battery_cycles WHERE cycle_start_timestamp < ? AND cycle_completed = 0", (cutoff_timestamp,))
        incomplete_cycles = cursor.fetchone()[0]
        print(f"battery_cycles (incomplete): {incomplete_cycles} records to delete")
        
        # Count old completed cycles (keep last 100)
        cursor.execute("""
            SELECT COUNT(*) FROM battery_cycles 
            WHERE cycle_start_timestamp < ? 
            AND cycle_completed = 1 
            AND id NOT IN (
                SELECT id FROM battery_cycles 
                WHERE cycle_completed = 1 
                ORDER BY cycle_start_timestamp DESC 
                LIMIT 100
            )
        """, (cutoff_timestamp,))
        old_cycles = cursor.fetchone()[0]
        print(f"battery_cycles (old completed): {old_cycles} records to delete (keeping latest 100)")
        
        total_to_delete += incomplete_cycles + old_cycles
        
        if total_to_delete == 0:
            print("No records to delete.")
            con.close()
            return
        
        print(f"\nTotal records to delete: {total_to_delete:,}")
        
        if not dry_run:
            if input("Proceed with deletion? (y/N): ").lower() != 'y':
                print("Operation cancelled.")
                con.close()
                return
            
            # Perform actual cleanup
            battery_monitor.cleanup_old_battery_data()
            print("Cleanup completed successfully!")
        
        con.close()
        
    except Exception as e:
        print(f"Error during cleanup: {e}")

def export_data_csv(output_file, days=7):
    """Export battery data to CSV file"""
    try:
        import csv
        
        start_timestamp = int(time.time()) - (days * 24 * 60 * 60)
        
        con = sqlite3.connect(battery_monitor.BATTERY_DB_PATH)
        cursor = con.cursor()
        
        cursor.execute("""
            SELECT timestamp, voltage, current, soc, temperature, power, energy,
                   capacity_remaining, capacity_full, health_percentage, status,
                   technology, manufacturer, model, charge_cycles
            FROM battery_readings 
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, (start_timestamp,))
        
        rows = cursor.fetchall()
        
        if not rows:
            print("No data found for the specified period.")
            con.close()
            return
        
        with open(output_file, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            
            # Write header
            writer.writerow([
                'DateTime', 'Timestamp', 'Voltage(V)', 'Current(A)', 'SOC(%)', 'Temperature(°C)',
                'Power(W)', 'Energy(Wh)', 'Capacity Remaining(Wh)', 'Capacity Full(Wh)',
                'Health(%)', 'Status', 'Technology', 'Manufacturer', 'Model', 'Charge Cycles'
            ])
            
            # Write data
            for row in rows:
                timestamp = row[0]
                datetime_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                writer.writerow([datetime_str] + list(row))
        
        con.close()
        print(f"Exported {len(rows):,} records to {output_file}")
        
    except Exception as e:
        print(f"Error exporting data: {e}")

def main():
    if len(sys.argv) < 2:
        print("Battery Database Maintenance Utility")
        print("\nUsage:")
        print("  python3 battery_maintenance.py stats                    - Show database statistics")
        print("  python3 battery_maintenance.py cleanup [days]          - Clean up old data (default: 30 days)")
        print("  python3 battery_maintenance.py cleanup-dry [days]      - Show what would be cleaned up")
        print("  python3 battery_maintenance.py export [file] [days]    - Export data to CSV (default: 7 days)")
        print("\nExamples:")
        print("  python3 battery_maintenance.py stats")
        print("  python3 battery_maintenance.py cleanup 45")
        print("  python3 battery_maintenance.py cleanup-dry 30")
        print("  python3 battery_maintenance.py export battery_data.csv 14")
        return
    
    command = sys.argv[1].lower()
    
    if command == 'stats':
        show_database_stats()
    
    elif command == 'cleanup':
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        cleanup_database(days, dry_run=False)
    
    elif command == 'cleanup-dry':
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        cleanup_database(days, dry_run=True)
    
    elif command == 'export':
        output_file = sys.argv[2] if len(sys.argv) > 2 else 'battery_export.csv'
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 7
        export_data_csv(output_file, days)
    
    else:
        print(f"Unknown command: {command}")
        print("Use 'python3 battery_maintenance.py' for help")

if __name__ == "__main__":
    main()
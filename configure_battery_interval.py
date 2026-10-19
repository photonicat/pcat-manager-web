#!/usr/bin/env python3

import sys
import os

# Add the app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

import battery_monitor

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 configure_battery_interval.py <minutes>")
        print("Configure battery data collection interval (1-30 minutes)")
        print("Current interval:", battery_monitor.BATTERY_COLLECTION_INTERVAL // 60, "minutes")
        sys.exit(1)
    
    try:
        minutes = int(sys.argv[1])
        if minutes < 1 or minutes > 30:
            print("Error: Interval must be between 1 and 30 minutes")
            sys.exit(1)
        
        seconds = minutes * 60
        battery_monitor.set_collection_interval(seconds)
        print(f"Battery collection interval set to {minutes} minutes ({seconds} seconds)")
        
    except ValueError:
        print("Error: Please provide a valid number of minutes")
        sys.exit(1)

if __name__ == "__main__":
    main()
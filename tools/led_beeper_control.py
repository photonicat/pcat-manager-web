#!/usr/bin/env python3

import socket
import sys
import json

def send_command(command_dict):
    """Send command to pcat-manager socket and return response"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect('/tmp/pcat-manager.sock')
        command_json = json.dumps(command_dict)
        s.send(command_json.encode() + b'\0')
        response = s.recv(1024)
        return json.loads(response.decode().strip('\0'))
    except Exception as e:
        return {"error": str(e)}
    finally:
        s.close()

def get_io_status():
    """Get current PMU I/O status (LED and beeper)"""
    command = {"command": "pmu-io-get"}
    response = send_command(command)
    return response

def set_io_status(led_state=None, beeper_state=None):
    """Set PMU I/O status (LED and/or beeper)"""
    command = {"command": "pmu-io-set"}
    
    if led_state is not None:
        command["status-led-v2-enabled"] = 1 if led_state else 0
    if beeper_state is not None:
        command["beeper-enabled"] = 1 if beeper_state else 0
        
    response = send_command(command)
    return response

def print_status(response):
    """Print formatted status from pmu-io-get response"""
    if "error" in response:
        print(f"Error: {response['error']}")
        return
    
    if response.get("code") == 0:
        led_status = "ON" if response.get("status-led-v2-enabled", 0) else "OFF"
        beeper_status = "ON" if response.get("beeper-enabled", 0) else "OFF"
        print(f"Power LED: {led_status}")
        print(f"Beeper: {beeper_status}")
    else:
        print(f"Command failed with code: {response.get('code')}")

def print_usage():
    print("Usage: led_beeper_control.py [command] [args]")
    print("Commands:")
    print("  status                    - Print current LED and beeper status")
    print("  led-set <on|off>          - Set power LED status")
    print("  beeper-set <on|off>       - Set beeper status")
    print("  both-set <on|off> <on|off> - Set both LED and beeper (led beeper)")

def main():
    if len(sys.argv) < 2:
        print_usage()
        return

    command = sys.argv[1]

    if command == "status":
        response = get_io_status()
        print_status(response)
    
    elif command == "led-set":
        if len(sys.argv) < 3:
            print("Error: led-set requires state argument (on|off)")
            return
        state = sys.argv[2].lower()
        if state not in ["on", "off"]:
            print("Error: state must be 'on' or 'off'")
            return
        
        response = set_io_status(led_state=(state == "on"))
        if "error" in response:
            print(f"Error: {response['error']}")
        elif response.get("code") == 0:
            print(f"Power LED set to {state.upper()}")
        else:
            print(f"Command failed with code: {response.get('code')}")
    
    elif command == "beeper-set":
        if len(sys.argv) < 3:
            print("Error: beeper-set requires state argument (on|off)")
            return
        state = sys.argv[2].lower()
        if state not in ["on", "off"]:
            print("Error: state must be 'on' or 'off'")
            return
        
        response = set_io_status(beeper_state=(state == "on"))
        if "error" in response:
            print(f"Error: {response['error']}")
        elif response.get("code") == 0:
            print(f"Beeper set to {state.upper()}")
        else:
            print(f"Command failed with code: {response.get('code')}")
    
    elif command == "both-set":
        if len(sys.argv) < 4:
            print("Error: both-set requires two state arguments (on|off) (on|off)")
            return
        led_state = sys.argv[2].lower()
        beeper_state = sys.argv[3].lower()
        if led_state not in ["on", "off"] or beeper_state not in ["on", "off"]:
            print("Error: both states must be 'on' or 'off'")
            return
        
        response = set_io_status(led_state=(led_state == "on"), beeper_state=(beeper_state == "on"))
        if "error" in response:
            print(f"Error: {response['error']}")
        elif response.get("code") == 0:
            print(f"Power LED set to {led_state.upper()}, Beeper set to {beeper_state.upper()}")
        else:
            print(f"Command failed with code: {response.get('code')}")
    
    else:
        print(f"Unknown command: {command}")
        print_usage()

if __name__ == "__main__":
    main()